-- M3: login sessions and the indexes the dashboard API needs.
--   * sessions stores only a SHA-256 of the bearer token, so a leaked database
--     can't be used to log in. Deleting a user deletes their sessions.
--   * The per-IP login throttle counts recent failures by ip.
--   * Filtered, keyset-paginated run lists walk jobs by (status, id DESC).

CREATE TABLE sessions (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX sessions_user_idx ON sessions (user_id, expires_at);

CREATE INDEX login_attempts_ip_idx ON login_attempts (ip, attempted_at DESC) WHERE NOT succeeded;

CREATE INDEX jobs_status_id_idx ON jobs (status, id DESC);
