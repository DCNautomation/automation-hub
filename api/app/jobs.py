"""The job engine: claim one due job, run its handler, then schedule a retry or mark it dead.

Claiming is a single UPDATE ... FOR UPDATE SKIP LOCKED, so any number of workers can poll the
same table and each job is handed to exactly one of them. The handler runs OUTSIDE any open
transaction (a slow handler must not hold a database connection or row lock), so a worker that
dies mid-job leaves a 'running' row behind; requeue_stale() recovers those.

Delivery is at-least-once: a handler can run twice (worker killed mid-job, or a job slower than
STALE_JOB_SECONDS), so real handlers should be safe to repeat.
"""
import logging
import random
import time

import psycopg
from psycopg_pool import PoolTimeout

from app import audit, config, db, handlers

log = logging.getLogger("jobs")

MAX_ERROR_CHARS = 2000
FINISH_RETRY_PAUSES = (0.5, 2.0)

CLAIM_SQL = """
UPDATE jobs
   SET status = 'running', attempts = attempts + 1, locked_by = %s, locked_at = now()
 WHERE id = (
       SELECT id FROM jobs
        WHERE status IN ('queued', 'failed') AND run_at <= now()
        ORDER BY run_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1)
RETURNING id, event_id, source_id, attempts, max_attempts
"""


def backoff_seconds(attempt, base, cap, rand=random.random):
    """Exponential delay with 'equal jitter': somewhere between half and all of the ceiling.

    Half keeps retries from firing instantly; the random part keeps a burst of failed jobs
    from all retrying at the same second.
    """
    ceiling = min(cap, base * 2 ** (attempt - 1))
    return ceiling * (0.5 + 0.5 * rand())


def run_one(worker_id):
    """Claim and run a single due job. Returns False when nothing was due."""
    with db.connection() as conn:
        job = conn.execute(CLAIM_SQL, (worker_id,)).fetchone()
        if job is None:
            return False
        ctx = conn.execute(
            "SELECT s.handler, e.payload FROM jobs j "
            "JOIN sources s ON s.id = j.source_id JOIN webhook_events e ON e.id = j.event_id "
            "WHERE j.id = %s",
            (job["id"],),
        ).fetchone()
        conn.execute(
            "INSERT INTO job_attempts (job_id, attempt_no, worker_id) VALUES (%s, %s, %s)",
            (job["id"], job["attempts"], worker_id),
        )
        audit.record(conn, worker_id, "job.started", "job", job["id"], after={"attempt": job["attempts"]})

    started = time.monotonic()
    error = None
    try:
        handler = handlers.HANDLERS.get(ctx["handler"])
        if handler is None:
            raise LookupError(f"unknown handler: {ctx['handler']}")
        handler(ctx["payload"])
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
        log.warning("job %s attempt %s failed: %s", job["id"], job["attempts"], error)
    duration_ms = int((time.monotonic() - started) * 1000)

    _finish_with_retry(worker_id, job, error, duration_ms)
    return True


def _finish_with_retry(worker_id, job, error, duration_ms):
    """The handler already ran, so a database blip while recording the result must not lose it:
    a job left 'running' would be requeued and its handler run a second time."""
    for pause in FINISH_RETRY_PAUSES:
        try:
            return _finish(worker_id, job, error, duration_ms)
        except (psycopg.OperationalError, PoolTimeout) as exc:
            log.warning("job %s: recording the result failed (%s), retrying in %ss", job["id"], exc, pause)
            time.sleep(pause)
    _finish(worker_id, job, error, duration_ms)


def _finish(worker_id, job, error, duration_ms):
    # Only the worker that still owns this exact attempt may record its outcome. If the job ran
    # longer than STALE_JOB_SECONDS and was requeued (maybe already claimed again), this result
    # is stale and must not overwrite the newer state.
    owned = " WHERE id = %s AND status = 'running' AND locked_by = %s AND attempts = %s"
    owner = (job["id"], worker_id, job["attempts"])
    with db.connection() as conn:
        if error is None:
            action, after = "job.succeeded", {"attempt": job["attempts"]}
            cur = conn.execute(
                "UPDATE jobs SET status = 'succeeded', locked_by = NULL, locked_at = NULL, "
                "last_error = NULL, finished_at = now()" + owned,
                owner,
            )
        elif job["attempts"] >= job["max_attempts"]:
            action, after = "job.dead", {"attempts": job["attempts"], "error": error}
            cur = conn.execute(
                "UPDATE jobs SET status = 'dead', locked_by = NULL, locked_at = NULL, "
                "last_error = %s, finished_at = now()" + owned,
                (error, *owner),
            )
        else:
            delay = backoff_seconds(job["attempts"], config.RETRY_BASE_SECONDS, config.RETRY_MAX_SECONDS)
            action = "job.failed"
            after = {"attempt": job["attempts"], "retry_in_seconds": round(delay, 1), "error": error}
            cur = conn.execute(
                "UPDATE jobs SET status = 'failed', locked_by = NULL, locked_at = NULL, last_error = %s, "
                "run_at = now() + make_interval(secs => %s)" + owned,
                (error, delay, *owner),
            )
        if cur.rowcount == 0:
            log.warning("job %s attempt %s lost its claim while running; result discarded",
                        job["id"], job["attempts"])
            return
        conn.execute(
            "UPDATE job_attempts SET finished_at = now(), ok = %s, error = %s, duration_ms = %s "
            "WHERE job_id = %s AND attempt_no = %s",
            (error is None, error, duration_ms, job["id"], job["attempts"]),
        )
        audit.record(conn, worker_id, action, "job", job["id"], after=after)


def requeue_stale(timeout_seconds):
    """Recover jobs stuck in 'running' because their worker died. Returns how many were touched.

    A recovered job counts the lost attempt, so a job that kills its worker every time still
    ends up dead instead of looping forever.
    """
    with db.connection() as conn:
        stuck = conn.execute(
            "UPDATE jobs SET "
            "  status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'failed' END, "
            "  finished_at = CASE WHEN attempts >= max_attempts THEN now() END, "
            "  last_error = 'worker lost: job was running for over ' || %s || ' seconds', "
            "  run_at = now(), locked_by = NULL, locked_at = NULL "
            "WHERE status = 'running' AND locked_at < now() - make_interval(secs => %s) "
            "RETURNING id, status, attempts",
            (timeout_seconds, timeout_seconds),
        ).fetchall()
        for job in stuck:
            conn.execute(
                "UPDATE job_attempts SET finished_at = now(), ok = FALSE, error = 'worker lost' "
                "WHERE job_id = %s AND attempt_no = %s AND finished_at IS NULL",
                (job["id"], job["attempts"]),
            )
            action = "job.dead" if job["status"] == "dead" else "job.requeued_stale"
            audit.record(conn, "reaper", action, "job", job["id"], after={"attempts": job["attempts"]})
    return len(stuck)
