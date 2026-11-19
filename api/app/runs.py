"""Read and act on runs (a run is one job and the webhook event behind it) for the dashboard API.

Lists use keyset pagination (`before` = the id of the last row on the previous page) instead of
OFFSET, so page 500 costs the same as page 1 and rows added mid-browse can't shift or repeat items.
"""
from app import audit, db

STATUSES = ("queued", "running", "failed", "succeeded", "dead")
LIST_ERROR_CHARS = 200
RETRYABLE = ("failed", "dead")

_RUN_COLUMNS = (
    "j.id, j.status, s.name AS source, e.idempotency_key, j.attempts, j.max_attempts, "
    "j.created_at, j.run_at, j.finished_at"
)
_RUN_FROM = "FROM jobs j JOIN sources s ON s.id = j.source_id JOIN webhook_events e ON e.id = j.event_id"


class RunNotFound(Exception):
    pass


class NotRetryable(Exception):
    def __init__(self, status):
        super().__init__(f"a {status} run cannot be retried")
        self.status = status


def list_runs(statuses=None, source=None, since=None, until=None, before=None, limit=25):
    where, params = [], []
    if statuses:
        where.append("j.status = ANY(%s)")
        params.append(list(statuses))
    if source:
        where.append("s.name = %s")
        params.append(source)
    if since is not None:
        where.append("j.created_at >= %s")
        params.append(since)
    if until is not None:
        where.append("j.created_at < %s")
        params.append(until)
    if before is not None:
        where.append("j.id < %s")
        params.append(before)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db.connection() as conn:
        rows = conn.execute(
            f"SELECT {_RUN_COLUMNS}, left(j.last_error, %s) AS last_error {_RUN_FROM} "
            f"{clause} ORDER BY j.id DESC LIMIT %s",
            (LIST_ERROR_CHARS, *params, limit + 1),
        ).fetchall()
    more = len(rows) > limit
    items = rows[:limit]
    return {"items": items, "next_before": items[-1]["id"] if more else None}


def summary(since=None):
    where, params = ("WHERE created_at >= %s", (since,)) if since is not None else ("", ())
    with db.connection() as conn:
        rows = conn.execute(f"SELECT status, count(*) AS n FROM jobs {where} GROUP BY status", params).fetchall()
    counts = {status: 0 for status in STATUSES}
    counts.update({row["status"]: row["n"] for row in rows})
    return {"counts": counts, "total": sum(counts.values())}


def run_detail(job_id):
    with db.connection() as conn:
        run = conn.execute(
            f"SELECT {_RUN_COLUMNS}, j.last_error, j.locked_by, j.locked_at, "
            "e.id AS event_id, e.payload, e.headers, e.received_at "
            f"{_RUN_FROM} WHERE j.id = %s",
            (job_id,),
        ).fetchone()
        if run is None:
            raise RunNotFound(job_id)
        attempts = conn.execute(
            "SELECT attempt_no, worker_id, started_at, finished_at, ok, error, duration_ms "
            "FROM job_attempts WHERE job_id = %s ORDER BY attempt_no",
            (job_id,),
        ).fetchall()
        trail = conn.execute(
            "SELECT id, at, actor, action, before, after FROM audit_log "
            "WHERE entity_type = 'job' AND entity_id = %s ORDER BY id",
            (job_id,),
        ).fetchall()
    event = {k: run.pop(k) for k in ("event_id", "payload", "headers", "received_at")}
    return {**run, "event": event, "attempts_log": attempts, "audit": trail}


def retry(job_id, actor):
    """Put a failed or dead run back in the queue right now.

    A dead run is out of attempts, so it is given a fresh budget of the source's max_attempts on top
    of what it already used (attempt numbers keep counting up so the history stays intact). A failed
    run is just waiting out its backoff, so it only has that wait skipped.
    """
    with db.connection() as conn:
        # FOR UPDATE makes two simultaneous clicks queue up: the second sees 'queued' and is refused.
        job = conn.execute(
            "SELECT j.id, j.status, j.attempts, j.max_attempts, j.run_at, s.max_attempts AS budget "
            "FROM jobs j JOIN sources s ON s.id = j.source_id WHERE j.id = %s FOR UPDATE OF j",
            (job_id,),
        ).fetchone()
        if job is None:
            raise RunNotFound(job_id)
        if job["status"] not in RETRYABLE:
            raise NotRetryable(job["status"])
        max_attempts = job["attempts"] + job["budget"] if job["status"] == "dead" else job["max_attempts"]
        conn.execute(
            "UPDATE jobs SET status = 'queued', run_at = now(), finished_at = NULL, max_attempts = %s "
            "WHERE id = %s",
            (max_attempts, job_id),
        )
        audit.record(
            conn,
            actor,
            "job.manual_retry",
            "job",
            job_id,
            before={"status": job["status"], "attempts": job["attempts"], "max_attempts": job["max_attempts"]},
            after={"status": "queued", "attempts": job["attempts"], "max_attempts": max_attempts},
        )
    return run_detail(job_id)


def list_sources():
    """Names and settings only. The signing secret never leaves the database."""
    with db.connection() as conn:
        return conn.execute(
            "SELECT id, name, handler, max_attempts, active, created_at FROM sources ORDER BY name"
        ).fetchall()
