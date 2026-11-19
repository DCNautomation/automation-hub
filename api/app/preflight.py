"""Refuse to start in production with settings that are unsafe or still on their local defaults.

    python -m app.preflight            checks only when HUB_ENV=production, otherwise does nothing
    python -m app.preflight --force    run the checks now (to see what a production start would say)

The container start-up runs this after migrating and before serving. Any FAIL stops the start and
prints why; WARN lines are things worth knowing that do not block a start.
"""
import os
import sys
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

from app import config

APP_ROLE = "hub_app"
MIN_SECRET_LENGTH = 20  # for database passwords
MIN_OWNER_SECRET_LENGTH = 16  # managed hosts (Neon) issue random 16-character owner passwords
MIN_SIGNING_SECRET_LENGTH = 32
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
FAIL, WARN = "FAIL", "WARN"


def settings_from_env():
    return {
        "owner_url": config.MIGRATE_DATABASE_URL,
        "app_url": config.DATABASE_URL,
        "app_password": config.APP_DB_PASSWORD,
        "cors_origins": config.CORS_ORIGINS,
        "dashboard_url": config.DASHBOARD_URL,
        "proxy_hops": config.TRUSTED_PROXY_HOPS,
        "telegram_token": config.TELEGRAM_BOT_TOKEN,
        "ingest_limit": config.INGEST_RATE_PER_MINUTE,
        "read_limit": config.READ_RATE_PER_MINUTE,
    }


def _weak(password, min_length=MIN_SECRET_LENGTH):
    p = (password or "").lower()
    return len(p) < min_length or any(bad in p for bad in ("change-me", "changeme", "password")) or p in ("hub",)


def _is_public_https(url):
    parts = urlsplit(url or "")
    return parts.scheme == "https" and (parts.hostname or "") not in LOCAL_HOSTS


def check(s):
    """Findings for a settings dict (see settings_from_env), as (level, message) pairs."""
    out = []
    owner, app = urlsplit(s["owner_url"] or ""), urlsplit(s["app_url"] or "")
    if _weak(owner.password, MIN_OWNER_SECRET_LENGTH):
        out.append((FAIL, f"the database owner password (POSTGRES_PASSWORD) is weak or still a default; use {MIN_OWNER_SECRET_LENGTH}+ random characters"))
    if _weak(s["app_password"]):
        out.append((FAIL, f"APP_DB_PASSWORD is weak or still a default; use {MIN_SECRET_LENGTH}+ random characters"))
    if app.username != APP_ROLE:
        out.append((FAIL, f"DATABASE_URL logs in as {app.username!r}; the running app must use the limited {APP_ROLE!r} role"))
    if s["proxy_hops"] < 1:
        out.append((FAIL, "TRUSTED_PROXY_HOPS is 0; behind a host's proxy the login lockout would see the proxy's address, not the client's (usually set 1)"))
    origins = s["cors_origins"]
    if not origins or not all(_is_public_https(o) for o in origins):
        out.append((FAIL, "CORS_ORIGINS must list only the real https address of the dashboard (no localhost, no http)"))
    if not _is_public_https(s["dashboard_url"]):
        out.append((FAIL, "DASHBOARD_URL must be the real https address of the dashboard (alert links point there)"))
    if not s["telegram_token"]:
        out.append((WARN, "alerts are off: TELEGRAM_BOT_TOKEN is not set, so nobody hears about dead runs"))
    if s["ingest_limit"] <= 0 or s["read_limit"] <= 0:
        out.append((WARN, "a request rate limit is switched off (INGEST_RATE_PER_MINUTE or READ_RATE_PER_MINUTE is 0)"))
    return out


def check_database():
    """Findings that need a look at the data: weak signing secrets and leftover demo data."""
    out = []
    try:
        # A direct connection with a short timeout: through the pool an unreachable database would hang for 30 s.
        with psycopg.connect(config.DATABASE_URL, connect_timeout=5, row_factory=dict_row) as conn:
            sources = conn.execute("SELECT name, handler, length(signing_secret) AS n FROM sources").fetchall()
    except psycopg.Error as exc:
        return [(WARN, f"could not check the data ({type(exc).__name__}); signing secrets were not inspected")]
    for src in sources:
        if src["n"] < MIN_SIGNING_SECRET_LENGTH:
            out.append((FAIL, f"source {src['name']!r} has a short signing secret; run `python -m app.sources rotate {src['name']}`"))
    demo = [s["name"] for s in sources if s["name"].startswith("demo-") or s["handler"] == "demo"]
    if demo:
        out.append((WARN, "demo data is present (" + ", ".join(demo) + "); fine for a public demo, remove it for real use"))
    return out


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if os.environ.get("HUB_ENV", "development") != "production" and "--force" not in args:
        print("preflight: skipped (HUB_ENV is not production)")
        return
    findings = check(settings_from_env()) + check_database()
    for level, message in findings:
        print(f"preflight {level}: {message}")
    failed = [m for level, m in findings if level == FAIL]
    if failed:
        sys.exit(f"preflight: refusing to start, {len(failed)} problem(s) above")
    print("preflight: ok" + (f" ({len(findings)} warning(s))" if findings else ""))


if __name__ == "__main__":
    main()
