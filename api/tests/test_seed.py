"""The demo seed produces real runs through the real engine, and resets cleanly."""
import psycopg
import pytest

from app import auth, config, db, seed, users

from helpers import make_job, one, rows


def _by_status():
    return {r["status"]: r["n"] for r in rows("SELECT status, count(*) AS n FROM jobs GROUP BY status")}


def test_demo_creates_nineteen_real_runs_in_the_expected_states(hub):
    result = seed.demo()
    assert result["counts"] == {"succeeded": 12, "dead": 4, "failed": 3}
    assert _by_status() == result["counts"]
    assert one("SELECT count(*) AS n FROM webhook_events")["n"] == 19
    assert {r["name"] for r in rows("SELECT name FROM sources")} == {
        "demo-shop", "demo-billing", "demo-support", "demo-crm"}


def test_failures_have_real_attempt_histories_and_error_text(hub):
    seed.demo()
    dead = rows("SELECT j.attempts, j.max_attempts, j.last_error, s.name FROM jobs j JOIN sources s ON s.id = j.source_id "
                "WHERE j.status = 'dead' ORDER BY j.id")
    assert [(d["name"], d["attempts"], d["max_attempts"]) for d in dead] == [
        ("demo-billing", 3, 3), ("demo-billing", 3, 3), ("demo-billing", 3, 3), ("demo-support", 2, 2)]
    assert all(d["last_error"].startswith("RuntimeError: ") for d in dead)
    retrying = rows("SELECT attempts, run_at > now() AS waiting FROM jobs WHERE status = 'failed' ORDER BY attempts, id")
    assert [r["attempts"] for r in retrying] == [1, 1, 2]
    assert all(r["waiting"] for r in retrying)  # backoff was really scheduled, in the future
    # 12 done runs x 1 try, billing 3 dead x 3 tries + support 1 dead x 2 tries, crm retrying 1 + 1 + 2 tries
    assert one("SELECT count(*) AS n FROM job_attempts WHERE ok IS NOT NULL")["n"] == 12 + 11 + 4


def test_the_viewer_login_works_and_is_read_only(hub):
    result = seed.demo()
    session = auth.login(seed.VIEWER, result["viewer_password"], "10.0.0.9")
    assert session.principal.role == "viewer"


def test_a_chosen_viewer_password_is_used(hub):
    seed.demo("a-demo-password-1234")
    assert auth.login(seed.VIEWER, "a-demo-password-1234", "10.0.0.9").principal.username == seed.VIEWER


def test_a_short_viewer_password_is_refused_before_anything_is_written(hub):
    with pytest.raises(ValueError, match="password must be"):
        seed.demo("short")
    assert rows("SELECT 1 FROM sources") == [] and rows("SELECT 1 FROM jobs") == []


def test_a_second_seed_is_refused_not_duplicated(hub):
    seed.demo()
    with pytest.raises(seed.AlreadySeeded):
        seed.demo()
    assert one("SELECT count(*) AS n FROM jobs")["n"] == 19


def test_an_existing_viewer_login_is_left_alone(hub):
    users.create_user(seed.VIEWER, "my-own-viewer-password", "viewer")
    result = seed.demo()
    assert result["viewer_password"] is None  # nothing new to show: the old password still rules
    assert auth.login(seed.VIEWER, "my-own-viewer-password", "10.0.0.9").principal.role == "viewer"


def test_reset_removes_only_demo_data_and_allows_reseeding(hub, test_database_url):
    keep = make_job(source="real-source")
    seed.demo()
    removed = seed.reset(test_database_url)
    assert removed == {"runs": 19, "events": 19, "sources": 4, "users": 1}
    assert [r["id"] for r in rows("SELECT id FROM jobs")] == [keep]
    assert [r["name"] for r in rows("SELECT name FROM sources")] == ["real-source"]
    assert rows("SELECT 1 FROM users") == []
    assert seed.demo()["counts"] == {"succeeded": 12, "dead": 4, "failed": 3}


def test_reset_needs_the_owner_login_the_app_role_cannot_do_it(hub, app_url):
    seed.demo()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        seed.reset(app_url)
    assert one("SELECT count(*) AS n FROM jobs")["n"] == 19


def test_the_whole_seed_works_with_only_the_app_roles_rights(hub, app_url):
    db.init_pool(app_url)
    assert seed.demo()["counts"] == {"succeeded": 12, "dead": 4, "failed": 3}


def test_cli_seeds_then_refuses_then_resets(hub, monkeypatch, test_database_url, capsys):
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    monkeypatch.setattr(config, "MIGRATE_DATABASE_URL", test_database_url)
    monkeypatch.setenv("DEMO_VIEWER_PASSWORD", "cli-demo-password-1")
    seed.main(["demo"])
    out = capsys.readouterr().out
    assert "'succeeded': 12" in out and "demo-viewer" in out and "cli-demo-password-1" in out
    with pytest.raises(SystemExit, match="already exists"):
        seed.main(["demo"])
    seed.main(["reset"])
    assert "'runs': 19" in capsys.readouterr().out


def test_cli_reports_a_too_short_password_plainly(hub, monkeypatch, test_database_url):
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    monkeypatch.setenv("DEMO_VIEWER_PASSWORD", "short")
    with pytest.raises(SystemExit, match="password must be"):
        seed.main(["demo"])
