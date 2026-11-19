"""Operations: worker health for an uptime monitor, cleanup, and request rate limits."""
import threading
import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import config, db, health, jobs, maintenance, ratelimit, worker
from app.main import app

from helpers import deliver, login_headers, make_job, make_source, one, reopen_pool, rows, signed_headers


@pytest.fixture
def client(hub):
    with TestClient(app) as c:
        yield c


def _age_heartbeat(worker_id, seconds):
    with db.connection() as conn:
        conn.execute(
            "UPDATE worker_heartbeats SET beat_at = now() - make_interval(secs => %s) WHERE worker_id = %s",
            (seconds, worker_id),
        )


# ----- worker health ----------------------------------------------------------------------------

def test_no_heartbeat_means_unhealthy(client):
    r = client.get("/healthz/worker")
    assert r.status_code == 503
    assert r.json()["problem"] == "no worker has reported in recently"
    assert r.json()["workers_alive"] == 0


def test_a_fresh_heartbeat_means_healthy(client):
    health.beat("w1")
    r = client.get("/healthz/worker")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["workers_alive"] == 1 and body["problem"] is None


def test_a_stale_heartbeat_means_unhealthy_again(client):
    health.beat("w1")
    _age_heartbeat("w1", config.WORKER_STALE_SECONDS + 5)
    assert client.get("/healthz/worker").status_code == 503


def test_one_live_worker_is_enough_when_another_is_gone(client):
    health.beat("dead")
    _age_heartbeat("dead", 3600)
    health.beat("alive")
    body = client.get("/healthz/worker").json()
    assert body["ok"] and body["workers_alive"] == 1


def test_beating_workers_that_ignore_due_jobs_are_reported_as_stuck(client):
    health.beat("w1")
    job = make_job()
    with db.connection() as conn:
        conn.execute("UPDATE jobs SET run_at = now() - make_interval(secs => %s) WHERE id = %s",
                     (config.QUEUE_LAG_SECONDS + 30, job))
    r = client.get("/healthz/worker")
    assert r.status_code == 503
    assert r.json()["problem"] == "jobs are waiting and no worker is picking them up"
    assert r.json()["oldest_waiting_seconds"] > config.QUEUE_LAG_SECONDS


def test_recent_waiting_jobs_and_finished_or_future_jobs_do_not_count_as_lag(client):
    health.beat("w1")
    make_job()  # due just now: normal
    old_done = make_job(status="succeeded")
    future = make_job(status="failed", attempts=1)
    with db.connection() as conn:
        conn.execute("UPDATE jobs SET run_at = now() - interval '1 day' WHERE id = %s", (old_done,))
        conn.execute("UPDATE jobs SET run_at = now() + interval '1 hour' WHERE id = %s", (future,))
    assert client.get("/healthz/worker").status_code == 200


def test_a_deliberate_stop_clears_the_heartbeat(client):
    health.beat("w1")
    health.clear("w1")
    assert client.get("/healthz/worker").status_code == 503


def test_the_heartbeat_thread_keeps_beating_and_then_stops(hub):
    beat = health.Heartbeat("w-thread", interval=0.05)
    beat.start()
    deadline = time.monotonic() + 3
    while not rows("SELECT 1 FROM worker_heartbeats") and time.monotonic() < deadline:
        time.sleep(0.02)
    first = one("SELECT beat_at FROM worker_heartbeats WHERE worker_id = 'w-thread'")["beat_at"]
    time.sleep(0.3)
    assert one("SELECT beat_at FROM worker_heartbeats WHERE worker_id = 'w-thread'")["beat_at"] > first
    beat.stop()
    beat.join(timeout=3)
    assert not beat.is_alive()


def test_the_heartbeat_thread_survives_a_failed_beat(hub, monkeypatch):
    calls = []

    def flaky(worker_id):
        calls.append(worker_id)
        if len(calls) == 1:
            raise psycopg.OperationalError("database blip")

    monkeypatch.setattr(health, "beat", flaky)
    beat = health.Heartbeat("w-flaky", interval=0.05)
    beat.start()
    deadline = time.monotonic() + 3
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    beat.stop()
    beat.join(timeout=3)
    assert len(calls) >= 3


def test_a_running_worker_reports_in_and_clears_itself_on_stop(hub, monkeypatch):
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(worker, "_stopping", False)
    beats = []
    real_beat = health.beat
    monkeypatch.setattr(health, "beat", lambda wid: (beats.append(wid), real_beat(wid)))
    real_run_one = jobs.run_one

    def stop_once_a_beat_has_landed(worker_id):
        deadline = time.monotonic() + 3
        while not beats and time.monotonic() < deadline:
            time.sleep(0.02)
        worker._request_stop(None, None)
        return real_run_one(worker_id)

    monkeypatch.setattr(jobs, "run_one", stop_once_a_beat_has_landed)
    worker.run_forever()
    reopen_pool()
    assert worker.WORKER_ID in beats
    assert rows("SELECT 1 FROM worker_heartbeats") == []


def test_worker_health_works_with_only_the_app_roles_rights(hub, app_url):
    db.init_pool(app_url)
    health.beat("w1")
    assert health.worker_status()["ok"]
    health.clear("w1")


# ----- cleanup ----------------------------------------------------------------------------------

def _user_id():
    login_headers()
    return one("SELECT id FROM users WHERE username = 'diego'")["id"]


def _session(user_id, token_hash, expires_sql):
    with db.connection() as conn:
        conn.execute(f"INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (%s, %s, {expires_sql})",
                     (user_id, token_hash))


def _attempt(age_sql):
    with db.connection() as conn:
        conn.execute(f"INSERT INTO login_attempts (username, ip, succeeded, attempted_at) "
                     f"VALUES ('x', '1.1.1.1', false, {age_sql})")


def test_purge_removes_only_what_is_safely_old(hub):
    uid = _user_id()
    with db.connection() as conn:
        conn.execute("DELETE FROM sessions")  # drop what the login above created
        conn.execute("DELETE FROM login_attempts")
    _session(uid, "long-expired", "now() - interval '3 days'")
    _session(uid, "just-expired", "now() - interval '1 hour'")
    _session(uid, "still-valid", "now() + interval '1 hour'")
    _attempt("now() - interval '40 days'")
    _attempt("now() - interval '10 days'")
    health.beat("gone")
    _age_heartbeat("gone", 3 * 86400)
    health.beat("here")

    counts = maintenance.purge()

    assert counts == {"sessions": 1, "login_attempts": 1, "worker_heartbeats": 1}
    assert {r["token_hash"] for r in rows("SELECT token_hash FROM sessions")} == {"just-expired", "still-valid"}
    assert one("SELECT count(*) AS n FROM login_attempts")["n"] == 1
    assert [r["worker_id"] for r in rows("SELECT worker_id FROM worker_heartbeats")] == ["here"]


def test_purge_never_deletes_the_records_of_what_happened(hub):
    make_job(status="succeeded")
    before = {t: one(f"SELECT count(*) AS n FROM {t}")["n"] for t in ("jobs", "webhook_events", "audit_log")}
    maintenance.purge()
    after = {t: one(f"SELECT count(*) AS n FROM {t}")["n"] for t in ("jobs", "webhook_events", "audit_log")}
    assert before == after and before["jobs"] == 1


def test_a_zero_day_retention_setting_is_raised_to_one_day(hub, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_ATTEMPT_RETENTION_DAYS", 0)
    _attempt("now() - interval '2 hours'")
    maintenance.purge()
    assert one("SELECT count(*) AS n FROM login_attempts")["n"] == 1  # the lockout window stays intact


def test_purge_works_with_only_the_app_roles_rights(hub, app_url):
    db.init_pool(app_url)
    _attempt("now() - interval '90 days'")
    assert maintenance.purge()["login_attempts"] == 1


def test_the_worker_loop_runs_cleanup(hub, monkeypatch):
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(worker, "_stopping", False)
    ran = []
    monkeypatch.setattr(maintenance, "purge", lambda: ran.append(1) or {})
    monkeypatch.setattr(jobs, "run_one", lambda wid: worker._request_stop(None, None) or False)
    worker.run_forever()
    assert ran == [1]


# ----- rate limits ------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_limiter_allows_up_to_the_limit_then_says_when_to_retry():
    clock = Clock()
    limiter = ratelimit.RateLimiter(3, clock=clock)
    assert [limiter.check("a")[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = limiter.check("a")
    assert not allowed and 1 <= retry_after <= 61
    assert limiter.check("b")[0]  # other keys are unaffected


def test_limiter_window_slides():
    clock = Clock()
    limiter = ratelimit.RateLimiter(2, clock=clock)
    limiter.check("a")
    clock.now += 30
    limiter.check("a")
    assert not limiter.check("a")[0]
    clock.now += 31  # the first hit is now more than a minute old
    assert limiter.check("a")[0]
    assert not limiter.check("a")[0]


def test_a_limit_of_zero_turns_it_off():
    limiter = ratelimit.RateLimiter(0)
    assert all(limiter.check("a")[0] for _ in range(1000))


def test_idle_keys_are_dropped_so_memory_stays_bounded(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 10)
    limiter = ratelimit.RateLimiter(5, clock=clock)
    for n in range(10):
        limiter.check(f"ip-{n}")
    clock.now += 120
    limiter.check("fresh-1")
    limiter.check("fresh-2")  # now over MAX_KEYS: everything idle for a minute is dropped
    assert len(limiter._hits) <= 3


def _send(client, key, ip=None, source="shop", **kw):
    body = b'{"n": 1}'
    headers = signed_headers(body, key)
    if ip:
        headers["X-Forwarded-For"] = ip
    return client.post(f"/ingest/{source}", content=body, headers=headers)


def test_ingest_is_limited_per_client_address(client, monkeypatch):
    monkeypatch.setattr(config, "INGEST_RATE_PER_MINUTE", 3)
    monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 1)
    make_source()
    assert [_send(client, f"k{n}", "9.9.9.9").status_code for n in range(3)] == [202, 202, 202]
    blocked = _send(client, "k4", "9.9.9.9")
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert _send(client, "k5", "8.8.8.8").status_code == 202  # a different sender is fine


def test_junk_requests_use_up_the_ingest_limit_too(client, monkeypatch):
    monkeypatch.setattr(config, "INGEST_RATE_PER_MINUTE", 2)
    assert client.post("/ingest/ghost", content=b"{}").status_code == 401
    assert client.post("/ingest/ghost", content=b"{}").status_code == 401
    assert client.post("/ingest/ghost", content=b"{}").status_code == 429


def test_the_dashboard_api_is_limited_per_client_address(client, monkeypatch):
    headers = login_headers()
    monkeypatch.setattr(config, "READ_RATE_PER_MINUTE", 4)
    assert [client.get("/runs", headers=headers).status_code for _ in range(4)] == [200] * 4
    blocked = client.get("/runs", headers=headers)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1


def test_guessing_tokens_is_throttled_before_the_database_is_asked(client, monkeypatch):
    monkeypatch.setattr(config, "READ_RATE_PER_MINUTE", 3)
    codes = [client.get("/runs", headers={"Authorization": f"Bearer guess{n}"}).status_code for n in range(5)]
    assert codes == [401, 401, 401, 429, 429]


def test_health_checks_and_login_are_not_throttled_by_the_read_limit(client, monkeypatch):
    monkeypatch.setattr(config, "READ_RATE_PER_MINUTE", 1)
    monkeypatch.setattr(config, "INGEST_RATE_PER_MINUTE", 1)
    assert [client.get("/healthz").status_code for _ in range(5)] == [200] * 5
    assert client.post("/auth/login", json={"username": "nobody", "password": "x" * 12}).status_code == 401
    assert client.post("/auth/login", json={"username": "nobody", "password": "x" * 12}).status_code == 401


def test_throttled_responses_carry_the_security_headers(client, monkeypatch):
    monkeypatch.setattr(config, "READ_RATE_PER_MINUTE", 1)
    client.get("/runs")
    r = client.get("/runs")
    assert r.status_code == 429
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
