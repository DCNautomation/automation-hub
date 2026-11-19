"""Is the worker alive and keeping up? Feeds GET /healthz/worker, which an uptime monitor can watch.

Two failure modes are covered, because /healthz alone only proves the API and database are up:
  * no worker has sent a heartbeat recently (crashed, never started, or cannot reach the database)
  * workers are beating but due jobs are not being picked up (a handler hung the loop)
"""
import logging
import threading

from app import config, db

log = logging.getLogger("health")


def beat(worker_id):
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO worker_heartbeats (worker_id) VALUES (%s) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at = now()",
            (worker_id,),
        )


def clear(worker_id):
    """A worker that stops on purpose removes itself, so a real shutdown shows up straight away."""
    with db.connection() as conn:
        conn.execute("DELETE FROM worker_heartbeats WHERE worker_id = %s", (worker_id,))


def worker_status():
    with db.connection() as conn:
        beats = conn.execute(
            "SELECT count(*) FILTER (WHERE beat_at > now() - make_interval(secs => %s)) AS alive, "
            "extract(epoch FROM now() - max(beat_at)) AS last_beat_seconds_ago FROM worker_heartbeats",
            (config.WORKER_STALE_SECONDS,),
        ).fetchone()
        waiting = conn.execute(
            "SELECT extract(epoch FROM now() - min(run_at)) AS oldest FROM jobs "
            "WHERE status IN ('queued', 'failed') AND run_at <= now()"
        ).fetchone()
    last = beats["last_beat_seconds_ago"]
    oldest = waiting["oldest"]
    problem = None
    if beats["alive"] == 0:
        problem = "no worker has reported in recently"
    elif oldest is not None and oldest > config.QUEUE_LAG_SECONDS:
        problem = "jobs are waiting and no worker is picking them up"
    return {
        "ok": problem is None,
        "problem": problem,
        "workers_alive": beats["alive"],
        "last_beat_seconds_ago": None if last is None else round(float(last), 1),
        "oldest_waiting_seconds": None if oldest is None else round(float(oldest), 1),
    }


class Heartbeat(threading.Thread):
    """Beats from its own thread, so a handler that runs for minutes does not look like a dead worker.
    A handler that never returns is caught by the queue-lag check instead."""

    def __init__(self, worker_id, interval=None):
        super().__init__(daemon=True, name="heartbeat")
        self.worker_id = worker_id
        self.interval = interval or config.HEARTBEAT_SECONDS
        self._stop_requested = threading.Event()

    def run(self):
        while not self._stop_requested.is_set():
            try:
                beat(self.worker_id)
            except Exception:
                log.exception("heartbeat failed")
            self._stop_requested.wait(self.interval)

    def stop(self):
        self._stop_requested.set()
