"""The dashboard API: runs list, summary, detail, retry, sources."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import jobs, ingest
from app.main import app

from helpers import SECRET, login_headers, make_due, make_job, make_source, one, rows


@pytest.fixture
def client(hub):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(hub):
    return login_headers("boss", "admin")


@pytest.fixture
def viewer(hub):
    return login_headers("watcher", "viewer")


ENDPOINTS = [
    ("GET", "/runs"),
    ("GET", "/runs/summary"),
    ("GET", "/runs/1"),
    ("POST", "/runs/1/retry"),
    ("GET", "/sources"),
]


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_every_endpoint_requires_a_login(client, method, path):
    assert client.request(method, path).status_code == 401


# ----- list ---------------------------------------------------------------------------------

def test_list_is_newest_first_with_the_fields_the_table_needs(client, viewer):
    first = make_job("succeeded", attempts=1)
    second = make_job("dead", attempts=5, max_attempts=5, last_error="boom", source="billing")
    body = client.get("/runs", headers=viewer).json()

    assert [r["id"] for r in body["items"]] == [second, first]
    assert body["next_before"] is None
    top = body["items"][0]
    assert top["status"] == "dead" and top["source"] == "billing" and top["last_error"] == "boom"
    assert set(top) == {"id", "status", "source", "idempotency_key", "attempts", "max_attempts",
                        "created_at", "run_at", "finished_at", "last_error"}


def test_empty_list_is_a_valid_page(client, viewer):
    assert client.get("/runs", headers=viewer).json() == {"items": [], "next_before": None}


def test_pagination_walks_every_run_exactly_once(client, viewer):
    made = [make_job() for _ in range(7)]
    seen, before, pages = [], None, 0
    while True:
        params = {"limit": 3, **({"before": before} if before else {})}
        page = client.get("/runs", params=params, headers=viewer).json()
        seen += [r["id"] for r in page["items"]]
        pages += 1
        before = page["next_before"]
        if before is None:
            break
    assert seen == sorted(made, reverse=True) and pages == 3


def test_a_full_last_page_does_not_promise_another(client, viewer):
    for _ in range(4):
        make_job()
    assert client.get("/runs", params={"limit": 4}, headers=viewer).json()["next_before"] is None


def test_new_runs_arriving_mid_browse_do_not_shift_the_next_page(client, viewer):
    for _ in range(5):
        make_job()
    page1 = client.get("/runs", params={"limit": 2}, headers=viewer).json()
    make_job()  # a webhook lands while someone is paging
    page2 = client.get("/runs", params={"limit": 2, "before": page1["next_before"]}, headers=viewer).json()
    ids = [r["id"] for r in page1["items"] + page2["items"]]
    assert ids == sorted(set(ids), reverse=True) and len(ids) == 4


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"limit": "x"}, {"before": 0}, {"before": "x"}, {"status": "bogus"}])
def test_bad_query_params_are_422(client, viewer, params):
    assert client.get("/runs", params=params, headers=viewer).status_code == 422


def test_filter_by_one_or_several_statuses(client, viewer):
    ok = make_job("succeeded")
    failed = make_job("failed")
    dead = make_job("dead")
    ids = lambda params: {r["id"] for r in client.get("/runs", params=params, headers=viewer).json()["items"]}
    assert ids({"status": "dead"}) == {dead}
    assert ids({"status": ["failed", "dead"]}) == {failed, dead}
    assert ids({}) == {ok, failed, dead}


def test_filter_by_source(client, viewer):
    a, b = make_job(source="shop"), make_job(source="billing")
    got = lambda src: [r["id"] for r in client.get("/runs", params={"source": src}, headers=viewer).json()["items"]]
    assert got("billing") == [b] and got("shop") == [a] and got("nope") == []


def test_filter_by_time_range_treats_bare_timestamps_as_utc(client, viewer):
    now = datetime.now(timezone.utc)
    old = make_job(created_at=now - timedelta(days=3))
    new = make_job()
    ids = lambda params: [r["id"] for r in client.get("/runs", params=params, headers=viewer).json()["items"]]

    assert ids({"since": (now - timedelta(days=1)).isoformat()}) == [new]
    assert ids({"until": (now - timedelta(days=1)).isoformat()}) == [old]
    naive = (now - timedelta(days=1)).replace(tzinfo=None).isoformat()
    assert ids({"since": naive}) == [new]
    assert client.get("/runs", params={"since": "yesterday"}, headers=viewer).status_code == 422


def test_filters_combine_and_still_paginate(client, viewer):
    for _ in range(4):
        make_job("dead", source="billing")
    make_job("dead", source="shop")
    make_job("succeeded", source="billing")
    page = client.get("/runs", params={"status": "dead", "source": "billing", "limit": 3}, headers=viewer).json()
    assert len(page["items"]) == 3 and page["next_before"] is not None
    rest = client.get(
        "/runs", params={"status": "dead", "source": "billing", "limit": 3, "before": page["next_before"]}, headers=viewer
    ).json()
    assert len(rest["items"]) == 1 and rest["next_before"] is None


def test_filter_values_are_parameters_not_sql(client, viewer):
    make_job()
    resp = client.get("/runs", params={"source": "x' OR '1'='1"}, headers=viewer)
    assert resp.status_code == 200 and resp.json()["items"] == []


def test_list_shortens_long_errors_but_detail_keeps_them(client, viewer):
    job = make_job("dead", last_error="E" * 1500)
    listed = client.get("/runs", headers=viewer).json()["items"][0]
    assert len(listed["last_error"]) == 200
    assert len(client.get(f"/runs/{job}", headers=viewer).json()["last_error"]) == 1500


# ----- summary ------------------------------------------------------------------------------

def test_summary_counts_every_status_including_zeros(client, viewer):
    make_job("succeeded")
    make_job("succeeded")
    make_job("dead")
    body = client.get("/runs/summary", headers=viewer).json()
    assert body == {"counts": {"queued": 0, "running": 0, "failed": 0, "succeeded": 2, "dead": 1}, "total": 3}


def test_summary_can_be_limited_to_recent_runs(client, viewer):
    make_job("dead", created_at=datetime.now(timezone.utc) - timedelta(days=5))
    make_job("dead")
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert client.get("/runs/summary", params={"since": since}, headers=viewer).json()["counts"]["dead"] == 1


def test_summary_on_an_empty_system(client, viewer):
    assert client.get("/runs/summary", headers=viewer).json()["total"] == 0


# ----- detail -------------------------------------------------------------------------------

def test_detail_has_event_attempts_and_audit_trail(client, viewer):
    make_source("shop", max_attempts=2)
    receipt = ingest_failing_job()
    body = client.get(f"/runs/{receipt}", headers=viewer).json()

    assert body["event"]["payload"] == {"n": 1}
    assert body["event"]["headers"] == {}
    assert [a["attempt_no"] for a in body["attempts_log"]] == [1, 2]
    assert all(a["ok"] is False and a["error"] for a in body["attempts_log"])
    assert [e["action"] for e in body["audit"]] == ["job.started", "job.failed", "job.started", "job.dead"]
    assert body["status"] == "dead" and body["attempts"] == 2


def ingest_failing_job():
    """Run a job through the real engine until it dies (handler 'nope' does not exist)."""
    from app import db

    with db.connection() as conn:
        conn.execute("UPDATE sources SET handler = 'nope' WHERE name = 'shop'")
    from helpers import deliver

    job_id = deliver("shop", key="fail-1").job_id
    for _ in range(2):
        make_due(job_id)
        jobs.run_one("w1")
    return job_id


def test_detail_for_a_missing_run_is_404(client, viewer):
    assert client.get("/runs/999", headers=viewer).status_code == 404


def test_detail_with_a_non_numeric_id_is_422(client, viewer):
    assert client.get("/runs/abc", headers=viewer).status_code == 422


def test_detail_never_exposes_the_signing_secret(client, viewer):
    job = make_job()
    assert SECRET not in client.get(f"/runs/{job}", headers=viewer).text


# ----- retry --------------------------------------------------------------------------------

def test_viewers_cannot_retry(client, viewer):
    job = make_job("dead", attempts=5, max_attempts=5)
    assert client.post(f"/runs/{job}/retry", headers=viewer).status_code == 403
    assert one("SELECT status FROM jobs WHERE id = %s", (job,))["status"] == "dead"


def test_admin_retry_of_a_dead_run_requeues_it_with_a_fresh_budget(client, admin):
    job = make_job("dead", attempts=5, max_attempts=5, last_error="boom")
    resp = client.post(f"/runs/{job}/retry", headers=admin)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued" and body["finished_at"] is None
    assert body["attempts"] == 5 and body["max_attempts"] == 10  # used 5 + the source's budget of 5
    assert body["last_error"] == "boom"  # kept until the next attempt says otherwise
    entry = body["audit"][-1]
    assert entry["action"] == "job.manual_retry" and entry["actor"] == "user:boss"
    assert entry["before"] == {"status": "dead", "attempts": 5, "max_attempts": 5}
    assert entry["after"] == {"status": "queued", "attempts": 5, "max_attempts": 10}


def test_retry_of_a_failed_run_skips_the_backoff_and_keeps_its_budget(client, admin):
    job = make_job("failed", attempts=2, max_attempts=5)
    from app import db

    with db.connection() as conn:
        conn.execute("UPDATE jobs SET run_at = now() + interval '1 hour' WHERE id = %s", (job,))
    body = client.post(f"/runs/{job}/retry", headers=admin).json()
    assert body["status"] == "queued" and body["max_attempts"] == 5
    assert one("SELECT run_at <= now() AS due FROM jobs WHERE id = %s", (job,))["due"]


def test_a_retried_dead_run_really_runs_again_and_keeps_its_attempt_history(client, admin):
    make_source("shop", max_attempts=2)
    job = make_job("dead", attempts=2, max_attempts=2, source="shop")
    from app import db

    with db.connection() as conn:  # history from the two earlier attempts
        conn.execute(
            "INSERT INTO job_attempts (job_id, attempt_no, worker_id, ok, error) VALUES (%s, 1, 'w', FALSE, 'x'), (%s, 2, 'w', FALSE, 'x')",
            (job, job),
        )
    client.post(f"/runs/{job}/retry", headers=admin)

    assert jobs.run_one("w2") is True
    body = client.get(f"/runs/{job}", headers=admin).json()
    assert body["status"] == "succeeded" and body["attempts"] == 3
    assert [a["attempt_no"] for a in body["attempts_log"]] == [1, 2, 3]


@pytest.mark.parametrize("status", ["queued", "running", "succeeded"])
def test_only_failed_or_dead_runs_can_be_retried(client, admin, status):
    job = make_job(status)
    resp = client.post(f"/runs/{job}/retry", headers=admin)
    assert resp.status_code == 409 and status in resp.json()["detail"]
    assert one("SELECT count(*) AS n FROM audit_log WHERE action = 'job.manual_retry'")["n"] == 0


def test_retrying_a_missing_run_is_404(client, admin):
    assert client.post("/runs/999/retry", headers=admin).status_code == 404


def test_double_click_second_retry_is_refused(client, admin):
    job = make_job("dead", attempts=5, max_attempts=5)
    assert client.post(f"/runs/{job}/retry", headers=admin).status_code == 200
    assert client.post(f"/runs/{job}/retry", headers=admin).status_code == 409
    assert one("SELECT max_attempts FROM jobs WHERE id = %s", (job,))["max_attempts"] == 10


def test_simultaneous_retries_apply_exactly_once(client, admin):
    job = make_job("dead", attempts=5, max_attempts=5)
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: client.post(f"/runs/{job}/retry", headers=admin).status_code, range(8)))
    assert sorted(codes) == [200] + [409] * 7
    assert one("SELECT max_attempts FROM jobs WHERE id = %s", (job,))["max_attempts"] == 10
    assert one("SELECT count(*) AS n FROM audit_log WHERE action = 'job.manual_retry'")["n"] == 1


# ----- sources ------------------------------------------------------------------------------

def test_sources_list_has_no_secrets(client, viewer):
    make_source("shop")
    make_source("billing", active=False)
    resp = client.get("/sources", headers=viewer)
    assert [s["name"] for s in resp.json()] == ["billing", "shop"]
    assert set(resp.json()[0]) == {"id", "name", "handler", "max_attempts", "active", "created_at"}
    assert SECRET not in resp.text and "signing_secret" not in resp.text
