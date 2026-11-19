-- Automation Hub: initial Postgres schema.
-- Design rules:
--   * Every webhook is stored raw before anything runs.
--   * UNIQUE (source_id, idempotency_key) makes duplicate deliveries impossible to run twice.
--   * Jobs are claimed with FOR UPDATE SKIP LOCKED, so many workers never grab the same job.
--   * audit_log is append-only: a trigger rejects UPDATE and DELETE.
--   * All timestamps are TIMESTAMPTZ (UTC). The UI converts to Asia/Manila for display.

CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE CHECK (length(username) BETWEEN 3 AND 40),
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('admin', 'viewer')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Lockout is computed from these rows (username + ip), so it survives restarts
-- and one IP cannot lock another person out.
CREATE TABLE login_attempts (
    id           BIGSERIAL PRIMARY KEY,
    username     TEXT NOT NULL,
    ip           TEXT NOT NULL,
    succeeded    BOOLEAN NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX login_attempts_lookup_idx ON login_attempts (username, ip, attempted_at DESC);

CREATE TABLE sources (
    id             BIGSERIAL PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9_-]{1,39}$'),
    signing_secret TEXT NOT NULL CHECK (length(signing_secret) >= 16),
    handler        TEXT NOT NULL DEFAULT 'log',
    max_attempts   INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 20),
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE webhook_events (
    id              BIGSERIAL PRIMARY KEY,
    source_id       BIGINT NOT NULL REFERENCES sources (id),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    headers         JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload         JSONB NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_id, idempotency_key)
);
CREATE INDEX webhook_events_received_idx ON webhook_events (received_at DESC);

CREATE TABLE jobs (
    id           BIGSERIAL PRIMARY KEY,
    event_id     BIGINT NOT NULL UNIQUE REFERENCES webhook_events (id),
    source_id    BIGINT NOT NULL REFERENCES sources (id),
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'failed', 'succeeded', 'dead')),
    attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
    run_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by    TEXT,
    locked_at    TIMESTAMPTZ,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);
-- Partial index: the claim query only ever looks at jobs that are waiting to run.
CREATE INDEX jobs_claim_idx ON jobs (run_at) WHERE status IN ('queued', 'failed');
CREATE INDEX jobs_status_created_idx ON jobs (status, created_at DESC);
CREATE INDEX jobs_source_created_idx ON jobs (source_id, created_at DESC);

CREATE TABLE job_attempts (
    id          BIGSERIAL PRIMARY KEY,
    job_id      BIGINT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    attempt_no  INTEGER NOT NULL CHECK (attempt_no >= 1),
    worker_id   TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    ok          BOOLEAN,
    error       TEXT,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    UNIQUE (job_id, attempt_no)
);

CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   BIGINT,
    before      JSONB,
    after       JSONB
);
CREATE INDEX audit_log_at_idx ON audit_log (at DESC);
CREATE INDEX audit_log_entity_idx ON audit_log (entity_type, entity_id);

CREATE FUNCTION audit_log_block_changes() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_block_changes();

CREATE TABLE alert_rules (
    id             BIGSERIAL PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('job_dead', 'failure_spike')),
    channel        TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
    target         TEXT NOT NULL,
    threshold      INTEGER CHECK (threshold IS NULL OR threshold >= 1),
    window_minutes INTEGER CHECK (window_minutes IS NULL OR window_minutes >= 1),
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
