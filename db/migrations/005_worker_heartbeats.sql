-- M6: tell an uptime monitor when the worker is down or stuck.
-- Each worker process writes a row every few seconds. GET /healthz/worker reports unhealthy when no
-- worker has beaten recently, or when jobs are due but nothing is picking them up.
-- Old rows (crashed or replaced workers) are deleted by the worker's cleanup pass.

CREATE TABLE worker_heartbeats (
    worker_id  TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    beat_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON worker_heartbeats TO hub_app;
