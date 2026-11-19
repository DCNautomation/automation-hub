"""Running jobs: claim, success, retry with backoff, dead state, concurrent workers."""
import threading
import time

from app import handlers, jobs

from helpers import deliver, make_due, make_source, one, rows


def _register(monkeypatch, name, fn):
    monkeypatch.setitem(handlers.HANDLERS, name, fn)


def _boom(payload):
    raise RuntimeError("boom")


def test_run_one_returns_false_when_nothing_is_due(hub):
    assert jobs.run_one("w1") is False


def test_successful_job_is_recorded_end_to_end(hub, monkeypatch):
    seen = []
    _register(monkeypatch, "collect", seen.append)
    make_source(handler="collect")
    receipt = deliver(payload={"order": 7})

    assert jobs.run_one("w1") is True

    assert seen == [{"order": 7}]
    job = one("SELECT * FROM jobs WHERE id = %s", (receipt.job_id,))
    assert (job["status"], job["attempts"]) == ("succeeded", 1)
    assert job["finished_at"] is not None and job["locked_by"] is None
    attempt = one("SELECT * FROM job_attempts WHERE job_id = %s", (receipt.job_id,))
    assert (attempt["attempt_no"], attempt["worker_id"], attempt["ok"]) == (1, "w1", True)
    assert attempt["finished_at"] is not None and attempt["duration_ms"] >= 0
    actions = [a["action"] for a in rows("SELECT action FROM audit_log WHERE entity_type = 'job' ORDER BY id")]
    assert actions == ["job.started", "job.succeeded"]
    assert jobs.run_one("w1") is False


def test_failing_job_is_rescheduled_with_a_delay_not_retried_immediately(hub, monkeypatch):
    _register(monkeypatch, "boom", _boom)
    make_source(handler="boom", max_attempts=3)
    receipt = deliver()

    assert jobs.run_one("w1") is True

    job = one("SELECT *, run_at > now() AS in_future FROM jobs WHERE id = %s", (receipt.job_id,))
    assert (job["status"], job["attempts"]) == ("failed", 1)
    assert "boom" in job["last_error"]
    assert job["in_future"] is True and job["locked_by"] is None and job["finished_at"] is None
    attempt = one("SELECT * FROM job_attempts WHERE job_id = %s", (receipt.job_id,))
    assert attempt["ok"] is False and "boom" in attempt["error"]
    # Not due yet, so a worker must leave it alone.
    assert jobs.run_one("w1") is False


def test_job_retries_when_due_then_goes_dead_after_max_attempts(hub, monkeypatch):
    calls = []

    def always_fails(payload):
        calls.append(1)
        raise RuntimeError("still broken")

    _register(monkeypatch, "boom", always_fails)
    make_source(handler="boom", max_attempts=3)
    receipt = deliver()

    for expected_attempts in (1, 2, 3):
        make_due(receipt.job_id)
        assert jobs.run_one("w1") is True
        assert one("SELECT attempts FROM jobs WHERE id = %s", (receipt.job_id,))["attempts"] == expected_attempts

    job = one("SELECT * FROM jobs WHERE id = %s", (receipt.job_id,))
    assert job["status"] == "dead" and job["finished_at"] is not None
    assert len(calls) == 3
    assert [a["attempt_no"] for a in rows("SELECT attempt_no FROM job_attempts ORDER BY attempt_no")] == [1, 2, 3]
    assert "job.dead" in [a["action"] for a in rows("SELECT action FROM audit_log")]

    # Dead is final: even if its run_at is in the past, nobody picks it up.
    make_due(receipt.job_id)
    assert jobs.run_one("w1") is False
    assert len(calls) == 3


def test_job_can_succeed_on_a_later_attempt(hub, monkeypatch):
    state = {"n": 0}

    def flaky(payload):
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("temporary")

    _register(monkeypatch, "flaky", flaky)
    make_source(handler="flaky", max_attempts=5)
    receipt = deliver()

    for _ in range(3):
        make_due(receipt.job_id)
        assert jobs.run_one("w1") is True

    job = one("SELECT * FROM jobs WHERE id = %s", (receipt.job_id,))
    assert (job["status"], job["attempts"]) == ("succeeded", 3)


def test_duplicate_webhook_runs_the_handler_exactly_once(hub, monkeypatch):
    calls = []
    _register(monkeypatch, "collect", calls.append)
    make_source(handler="collect")
    for _ in range(3):
        deliver(key="evt-1", payload={"order": 1})

    while jobs.run_one("w1"):
        pass

    assert calls == [{"order": 1}]


def test_unknown_handler_fails_the_job_instead_of_crashing_the_worker(hub):
    make_source(handler="does-not-exist")
    receipt = deliver()

    assert jobs.run_one("w1") is True

    job = one("SELECT * FROM jobs WHERE id = %s", (receipt.job_id,))
    assert job["status"] == "failed" and "does-not-exist" in job["last_error"]


def test_two_workers_never_claim_the_same_job(hub, monkeypatch):
    runs = {}
    guard = threading.Lock()

    def slow(payload):
        time.sleep(0.02)  # widen the window where a double-claim would show up
        with guard:
            runs[payload["n"]] = runs.get(payload["n"], 0) + 1

    _register(monkeypatch, "slow", slow)
    make_source(handler="slow")
    total = 24
    for n in range(total):
        deliver(key=f"evt-{n}", payload={"n": n})

    def worker(worker_id):
        while jobs.run_one(worker_id):
            pass

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(runs) == list(range(total))
    assert all(count == 1 for count in runs.values())
    assert one("SELECT count(*) AS n FROM jobs WHERE status = 'succeeded' AND attempts = 1")["n"] == total
    assert one("SELECT count(*) AS n FROM job_attempts")["n"] == total


def test_crashed_workers_stuck_job_is_requeued(hub, monkeypatch):
    calls = []
    _register(monkeypatch, "collect", calls.append)
    make_source(handler="collect", max_attempts=5)
    receipt = deliver()
    # Simulate a worker that claimed the job and then died.
    from app import db

    with db.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = 1, locked_by = 'dead-worker', "
            "locked_at = now() - interval '1 hour' WHERE id = %s",
            (receipt.job_id,),
        )

    assert jobs.requeue_stale(timeout_seconds=600) == 1

    job = one("SELECT * FROM jobs WHERE id = %s", (receipt.job_id,))
    assert job["status"] == "failed" and job["locked_by"] is None
    assert "job.requeued_stale" in [a["action"] for a in rows("SELECT action FROM audit_log")]
    make_due(receipt.job_id)
    assert jobs.run_one("w2") is True
    assert one("SELECT status FROM jobs WHERE id = %s", (receipt.job_id,))["status"] == "succeeded"


def test_recently_locked_running_job_is_left_alone(hub):
    make_source()
    receipt = deliver()
    from app import db

    with db.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = 1, locked_by = 'busy', locked_at = now() WHERE id = %s",
            (receipt.job_id,),
        )

    assert jobs.requeue_stale(timeout_seconds=600) == 0
    assert one("SELECT status FROM jobs WHERE id = %s", (receipt.job_id,))["status"] == "running"


def test_slow_handler_cannot_overwrite_a_newer_attempt(hub, monkeypatch):
    """A handler outlives the stale timeout, the job is requeued and claimed again; the old
    worker's late result must be discarded instead of clobbering the new attempt."""
    from app import db

    receipt_holder = {}

    def slow_then_reaped(payload):
        # While "worker A" is still inside its handler: the reaper requeues the job and
        # worker B claims it (attempt 2).
        with db.connection() as conn:
            conn.execute("UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = %s", (receipt_holder["job"],))
        assert jobs.requeue_stale(timeout_seconds=600) == 1
        # Claim attempt 2 by hand, but leave it running (B is mid-job).
        with db.connection() as conn:
            conn.execute(jobs.CLAIM_SQL, ("worker-B",))

    _register(monkeypatch, "slow", slow_then_reaped)
    make_source(handler="slow", max_attempts=5)
    receipt_holder["job"] = deliver().job_id

    assert jobs.run_one("worker-A") is True  # A finishes after B has already taken over

    job = one("SELECT * FROM jobs WHERE id = %s", (receipt_holder["job"],))
    assert (job["status"], job["locked_by"], job["attempts"]) == ("running", "worker-B", 2)
    # A's attempt row was closed by the reaper as lost; A's success was not recorded over it.
    attempt_1 = one("SELECT * FROM job_attempts WHERE job_id = %s AND attempt_no = 1", (receipt_holder["job"],))
    assert attempt_1["ok"] is False and attempt_1["error"] == "worker lost"
    assert "job.succeeded" not in [a["action"] for a in rows("SELECT action FROM audit_log")]


def test_database_blip_while_recording_the_result_is_retried(hub, monkeypatch):
    import psycopg

    calls = collect = []
    _register(monkeypatch, "collect", collect.append)
    make_source(handler="collect")
    receipt = deliver()
    real_finish, state = jobs._finish, {"calls": 0}

    def flaky_finish(*args):
        state["calls"] += 1
        if state["calls"] < 3:
            raise psycopg.OperationalError("connection dropped")
        return real_finish(*args)

    monkeypatch.setattr(jobs, "_finish", flaky_finish)
    monkeypatch.setattr(jobs.time, "sleep", lambda seconds: None)

    assert jobs.run_one("w1") is True

    assert state["calls"] == 3 and len(calls) == 1  # recorded on the 3rd try, handler ran once
    assert one("SELECT status FROM jobs WHERE id = %s", (receipt.job_id,))["status"] == "succeeded"
