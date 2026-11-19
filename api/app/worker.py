"""Worker process. Run: python -m app.worker

Polls for due jobs and runs them one at a time. Run as many copies as you like: the claim
query hands each job to exactly one. Also recovers jobs left 'running' by a crashed worker and
sends failure alerts (a no-op until TELEGRAM_BOT_TOKEN is set), reports a heartbeat for /healthz/worker
and does hourly cleanup (maintenance.py).
"""
import logging
import os
import signal
import socket
import time

from app import alerts, config, db, health, jobs, maintenance

log = logging.getLogger("worker")
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
REAP_EVERY_SECONDS = 60
MAX_ERROR_PAUSE = 30

_stopping = False


def _request_stop(signum, frame):
    global _stopping
    _stopping = True


def run_forever():
    db.init_pool(config.DATABASE_URL)
    # Docker stops containers with SIGTERM: finish the current job, then exit.
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    log.info("worker %s started (poll every %ss)", WORKER_ID, config.WORKER_POLL_SECONDS)
    last_reap = None
    last_alert_pass = None
    last_purge = None
    errors_in_a_row = 0
    if not config.TELEGRAM_BOT_TOKEN:
        log.info("alerts are off: TELEGRAM_BOT_TOKEN is not set")
    heartbeat = health.Heartbeat(WORKER_ID)
    heartbeat.start()
    try:
        while not _stopping:
            if last_reap is None or time.monotonic() - last_reap >= REAP_EVERY_SECONDS:
                try:
                    recovered = jobs.requeue_stale(config.STALE_JOB_SECONDS)
                    last_reap = time.monotonic()
                    if recovered:
                        log.warning("recovered %s stuck job(s)", recovered)
                except Exception:
                    log.exception("stale-job check failed")  # last_reap unchanged: retried next pass
            if last_purge is None or time.monotonic() - last_purge >= config.PURGE_EVERY_SECONDS:
                last_purge = time.monotonic()
                try:
                    maintenance.purge()
                except Exception:
                    log.exception("cleanup pass failed")
            if last_alert_pass is None or time.monotonic() - last_alert_pass >= config.ALERT_INTERVAL_SECONDS:
                last_alert_pass = time.monotonic()  # set first: a failing pass waits its turn, it doesn't spin
                try:
                    alerts.tick()
                except Exception:
                    log.exception("alert pass failed")
            try:
                worked = jobs.run_one(WORKER_ID)
                errors_in_a_row = 0
            except Exception:
                # Database hiccup: log it and keep the worker alive instead of crash-looping.
                errors_in_a_row += 1
                log.exception("job loop error (%s in a row)", errors_in_a_row)
                worked = False
            if not worked:
                # Back off while the database is failing so an outage doesn't flood the log.
                time.sleep(min(MAX_ERROR_PAUSE, config.WORKER_POLL_SECONDS * 2 ** errors_in_a_row))
    finally:
        log.info("worker %s stopping", WORKER_ID)
        heartbeat.stop()
        heartbeat.join(timeout=5)  # wait out a beat already in flight, or it could re-add the row we delete next
        try:
            health.clear(WORKER_ID)  # a deliberate stop shows as "down" right away instead of after a timeout
        except Exception:
            log.exception("could not clear heartbeat")
        db.close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_forever()
