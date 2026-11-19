"""The worker loop, health endpoint, size limit and migration re-run safety."""
import pytest
from fastapi.testclient import TestClient

from app import db, ingest, jobs, worker
from app.main import app
from app.migrate import migrate

from helpers import deliver, make_source, one, reopen_pool, signed_headers


@pytest.fixture
def quiet_worker(monkeypatch):
    """Run worker.run_forever() for real, without touching the test runner's signal handlers or sleeping."""
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(worker, "_stopping", False)
    return monkeypatch


def test_worker_processes_due_jobs_then_stops_when_asked(hub, quiet_worker):
    make_source()
    for n in range(3):
        deliver(key=f"evt-{n}", payload={"n": n})
    real_run_one = jobs.run_one

    def run_one_then_stop_when_idle(worker_id):
        worked = real_run_one(worker_id)
        if not worked:
            worker._request_stop(None, None)
        return worked

    quiet_worker.setattr(jobs, "run_one", run_one_then_stop_when_idle)
    worker.run_forever()
    reopen_pool()

    assert one("SELECT count(*) AS n FROM jobs WHERE status = 'succeeded'")["n"] == 3


def test_worker_survives_a_database_error_in_the_loop(hub, quiet_worker):
    calls = {"n": 0}

    def flaky_run_one(worker_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database went away")
        worker._request_stop(None, None)
        return False

    quiet_worker.setattr(jobs, "run_one", flaky_run_one)
    worker.run_forever()  # must not raise

    assert calls["n"] == 2


def test_worker_recovers_stuck_jobs_on_its_first_pass(hub, quiet_worker):
    make_source()
    receipt = deliver()
    with db.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = 1, locked_by = 'gone', "
            "locked_at = now() - interval '1 day' WHERE id = %s",
            (receipt.job_id,),
        )
    quiet_worker.setattr(jobs, "run_one", lambda worker_id: worker._request_stop(None, None) or False)
    worker.run_forever()
    reopen_pool()

    assert one("SELECT status FROM jobs WHERE id = %s", (receipt.job_id,))["status"] == "failed"


def test_healthz_ok_and_503_when_database_is_down(hub, monkeypatch):
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}

        def broken():
            raise RuntimeError("down")

        monkeypatch.setattr(db, "connection", broken)
        resp = client.get("/healthz")
        assert resp.status_code == 503
        assert "down" not in resp.text


def test_declared_oversize_body_is_413_before_it_is_read(hub):
    make_source()
    with TestClient(app) as client:
        body = b'{"blob": "' + b"x" * (ingest.MAX_BODY_BYTES + 10) + b'"}'
        resp = client.post(
            "/ingest/shop",
            content=body,
            headers=signed_headers(body, "big"),
        )
    assert resp.status_code == 413
    reopen_pool()
    assert one("SELECT count(*) AS n FROM webhook_events")["n"] == 0


def test_stored_headers_never_include_the_signature(hub):
    make_source()
    with TestClient(app) as client:
        body = b'{"n": 1}'
        client.post(
            "/ingest/shop",
            content=body,
            headers={
                **signed_headers(body, "k1"),
                "Authorization": "Bearer should-not-be-stored",
                "User-Agent": "shop-webhooks/1.0",
            },
        )
    reopen_pool()
    stored = one("SELECT headers FROM webhook_events")["headers"]
    assert stored.get("user-agent") == "shop-webhooks/1.0"
    assert not {"x-hub-signature-256", "authorization", "x-idempotency-key"} & set(stored)


def test_migrate_is_safe_to_run_twice(test_database_url):
    assert migrate(test_database_url) == []


def test_worker_backs_off_while_the_database_keeps_failing(hub, monkeypatch):
    sleeps = []
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)
    monkeypatch.setattr(worker, "_stopping", False)
    calls = {"n": 0}

    def always_failing(worker_id):
        calls["n"] += 1
        if calls["n"] == 5:
            worker._request_stop(None, None)
        raise RuntimeError("database is down")

    monkeypatch.setattr(jobs, "run_one", always_failing)
    worker.run_forever()

    assert sleeps == sorted(sleeps) and sleeps[0] < sleeps[-1]  # pauses grow
    assert max(sleeps) <= worker.MAX_ERROR_PAUSE
