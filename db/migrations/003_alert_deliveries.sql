-- M5: alerts. alert_rules (from 001) says WHEN to alert; alert_deliveries is the outbox of messages
-- waiting to go out. Rows are written in the same pass that decides to alert and sent separately, so a
-- Telegram outage or a restart never loses an alert, and the UNIQUE key means the same event is never
-- queued twice for a rule.

ALTER TABLE alert_rules
    ADD CONSTRAINT alert_rules_spike_needs_numbers
    CHECK (kind <> 'failure_spike' OR (threshold IS NOT NULL AND window_minutes IS NOT NULL));

CREATE TABLE alert_deliveries (
    id          BIGSERIAL PRIMARY KEY,
    rule_id     BIGINT NOT NULL REFERENCES alert_rules (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('job_dead', 'failure_spike')),
    dedupe_key  TEXT NOT NULL,
    message     TEXT NOT NULL,
    -- suppressed = covered by a summary message instead of getting its own
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed', 'suppressed')),
    attempts    INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_try_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ,
    UNIQUE (rule_id, dedupe_key)
);
CREATE INDEX alert_deliveries_pending_idx ON alert_deliveries (next_try_at) WHERE status = 'pending';
CREATE INDEX alert_deliveries_rule_kind_idx ON alert_deliveries (rule_id, kind, created_at DESC);
