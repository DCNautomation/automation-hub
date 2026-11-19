"""Housekeeping the worker runs about once an hour, so tables that only ever grow stay small.

Run by hand: python -m app.maintenance

Deliberately NOT purged: jobs, events, attempts, audit_log and alert_deliveries. Those are the
record of what happened. How long to keep them is a business decision, not a cleanup default.
"""
import logging

from app import config, db

log = logging.getLogger("maintenance")


def purge():
    """Delete expired sessions, old login attempts and heartbeats of workers that are long gone."""
    # The lockout only looks 15 minutes back, so even a short retention never weakens it. The floor of
    # one day is for the same reason: keep at least everything the throttle could still need.
    retention_days = max(config.LOGIN_ATTEMPT_RETENTION_DAYS, 1)
    with db.connection() as conn:
        sessions = conn.execute("DELETE FROM sessions WHERE expires_at < now() - interval '1 day'").rowcount
        attempts = conn.execute(
            "DELETE FROM login_attempts WHERE attempted_at < now() - make_interval(days => %s)", (retention_days,)
        ).rowcount
        beats = conn.execute("DELETE FROM worker_heartbeats WHERE beat_at < now() - interval '1 day'").rowcount
    counts = {"sessions": sessions, "login_attempts": attempts, "worker_heartbeats": beats}
    if any(counts.values()):
        log.info("cleanup removed %s", counts)
    return counts


if __name__ == "__main__":
    db.init_pool(config.DATABASE_URL)
    try:
        print(purge())
    finally:
        db.close_pool()
