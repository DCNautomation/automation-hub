"""Settings read from environment variables (see .env.example)."""
import os


def _int(name, default):
    return int(os.environ.get(name, default))


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://hub:hub@localhost:5432/hub")
# Migrations and role setup need the database owner. The running app (DATABASE_URL) should connect as the
# limited hub_app role instead; when only DATABASE_URL is set, both are the same connection.
MIGRATE_DATABASE_URL = os.environ.get("MIGRATE_DATABASE_URL") or DATABASE_URL
APP_DB_PASSWORD = os.environ.get("APP_DB_PASSWORD", "")
WORKER_POLL_SECONDS = _int("WORKER_POLL_SECONDS", 2)
RETRY_BASE_SECONDS = _int("RETRY_BASE_SECONDS", 10)
RETRY_MAX_SECONDS = _int("RETRY_MAX_SECONDS", 3600)
# A job still 'running' after this long is assumed to belong to a dead worker.
STALE_JOB_SECONDS = _int("STALE_JOB_SECONDS", 600)

# Worker health. /healthz/worker is unhealthy when no heartbeat is newer than WORKER_STALE_SECONDS, or when a due
# job has been waiting longer than QUEUE_LAG_SECONDS (workers alive but not picking jobs up).
HEARTBEAT_SECONDS = _int("HEARTBEAT_SECONDS", 10)
WORKER_STALE_SECONDS = _int("WORKER_STALE_SECONDS", 60)
QUEUE_LAG_SECONDS = _int("QUEUE_LAG_SECONDS", 300)
PURGE_EVERY_SECONDS = _int("PURGE_EVERY_SECONDS", 3600)
LOGIN_ATTEMPT_RETENTION_DAYS = _int("LOGIN_ATTEMPT_RETENTION_DAYS", 30)

# Requests allowed per client address per minute (0 turns a limit off). Counted per API process.
INGEST_RATE_PER_MINUTE = _int("INGEST_RATE_PER_MINUTE", 300)
READ_RATE_PER_MINUTE = _int("READ_RATE_PER_MINUTE", 300)

# Login. Lockout is per (username, ip); the per-IP cap stops one address spraying many usernames.
SESSION_TTL_SECONDS = _int("SESSION_TTL_SECONDS", 12 * 3600)
LOGIN_MAX_FAILURES = _int("LOGIN_MAX_FAILURES", 5)
LOGIN_IP_MAX_FAILURES = _int("LOGIN_IP_MAX_FAILURES", 30)
LOGIN_WINDOW_SECONDS = _int("LOGIN_WINDOW_SECONDS", 900)
# 0 = use the socket address. Behind a proxy, set how many proxies sit in front of the API and the
# client address is read from that many entries from the right of X-Forwarded-For.
TRUSTED_PROXY_HOPS = _int("TRUSTED_PROXY_HOPS", 0)

# Browsers allowed to call the API from another origin (the dashboard dev server by default).
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o.strip()
]

# Alerts. The bot token is a secret: environment only, never stored in the database.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  # default chat for new rules
TELEGRAM_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
SHEET_WEBHOOK_URL = os.environ.get("SHEET_WEBHOOK_URL", "")  # Google Apps Script web app that appends a row; blank = skip the Sheet step
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:5173").rstrip("/")  # used for links in messages
ALERT_INTERVAL_SECONDS = _int("ALERT_INTERVAL_SECONDS", 15)
ALERT_MAX_ATTEMPTS = _int("ALERT_MAX_ATTEMPTS", 8)  # sends tried before a message is given up on
ALERT_MAX_PER_TICK = _int("ALERT_MAX_PER_TICK", 5)  # more dead runs than this at once become one summary
