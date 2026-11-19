"""Shared fixtures. DB-backed tests run on a separate `hub_test` database on the same
Postgres server, so they never touch dev data. Postgres must be up (docker compose up -d postgres).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from app import config, db, dbroles, passwords, ratelimit
from app.migrate import migrate

TEST_DB = "hub_test"


def _with_dbname(url, name):
    return urlunsplit(urlsplit(url)._replace(path="/" + name))


@pytest.fixture(scope="session")
def test_database_url():
    base = config.MIGRATE_DATABASE_URL  # the owner: creating a database and migrating need more than hub_app has
    with psycopg.connect(_with_dbname(base, "postgres"), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone()
        if not exists:
            conn.execute(f"CREATE DATABASE {TEST_DB}")
    url = _with_dbname(base, TEST_DB)
    migrate(url)
    return url


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """Request counters are process-wide; start every test from zero."""
    ratelimit.reset_all()
    yield


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch):
    """scrypt at production cost is ~100 ms a hash; tests make dozens, so use the minimum."""
    monkeypatch.setattr(passwords, "SCRYPT_N", 2**4)


@pytest.fixture
def hub(test_database_url, monkeypatch):
    """Empty tables and an open pool on the test database for the duration of one test."""
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    db.init_pool(test_database_url)
    with db.connection() as conn:
        # audit_log refuses TRUNCATE (that is tested); the owner skips the trigger just for this reset.
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(
            "TRUNCATE audit_log, job_attempts, jobs, webhook_events, alert_deliveries, alert_rules, "
            "sources, login_attempts, sessions, users, worker_heartbeats RESTART IDENTITY CASCADE"
        )
    yield
    db.close_pool()


class FakeTelegram:
    """A tiny local stand-in for api.telegram.org, so alerts go over real HTTP in tests."""

    def __init__(self):
        self.requests = []
        self.script = []  # one (status, body) per request, consumed in order; then 200 ok
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"path": self.path, "body": json.loads(raw or b"{}")})
                status, body = outer.script.pop(0) if outer.script else (200, {"ok": True, "result": {}})
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def texts(self):
        return [r["body"]["text"] for r in self.requests]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


TOKEN = "111:fake-bot-token-AAAA"


@pytest.fixture
def telegram_up(monkeypatch):
    """Alerts configured and pointed at the fake Telegram."""
    fake = FakeTelegram()
    monkeypatch.setattr(config, "TELEGRAM_API_BASE", fake.url)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(config, "DASHBOARD_URL", "http://dash.test")
    yield fake
    fake.stop()


APP_PASSWORD = "app-role-password-for-tests"


TEST_LOGIN = "hub_app_test"


@pytest.fixture(scope="session")
def app_url(test_database_url):
    """A URL that logs in as a throwaway member of hub_app, so it has exactly hub_app's rights.

    Roles are shared by the whole Postgres server, so the tests must never change the password of the
    real hub_app (the running dev stack uses it). The password is set through the same function the
    real stack uses.
    """
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(f"DROP ROLE IF EXISTS {TEST_LOGIN}")
        conn.execute(f"CREATE ROLE {TEST_LOGIN} LOGIN IN ROLE hub_app")
    dbroles.set_app_password(APP_PASSWORD, test_database_url, role=TEST_LOGIN)
    parts = urlsplit(test_database_url)
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    yield urlunsplit(parts._replace(netloc=f"{TEST_LOGIN}:{APP_PASSWORD}@{host}"))
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(f"DROP ROLE IF EXISTS {TEST_LOGIN}")
