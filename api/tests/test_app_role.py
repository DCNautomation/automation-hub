"""The running app connects as hub_app, a role that owns nothing and cannot touch audit_log's history."""
import psycopg
import pytest

from app import alerts, auth, db, dbroles, jobs, runs
from app.migrate import migrate

from helpers import deliver, login_headers, make_source, one, rows

def _denied(app_url, sql):
    with psycopg.connect(app_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(sql)


def test_the_app_role_can_add_to_the_audit_log_but_not_rewrite_it(hub, app_url):
    with psycopg.connect(app_url, autocommit=True) as conn:
        conn.execute("INSERT INTO audit_log (actor, action, entity_type) VALUES ('t', 'a', 'x')")
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1
    # Denied by privilege itself, before the append-only trigger is even reached.
    _denied(app_url, "UPDATE audit_log SET actor = 'x'")
    _denied(app_url, "DELETE FROM audit_log")
    _denied(app_url, "TRUNCATE audit_log")
    assert one("SELECT count(*) AS n FROM audit_log")["n"] == 1


@pytest.mark.parametrize("statement", [
    "DROP TABLE audit_log",
    "ALTER TABLE audit_log DISABLE TRIGGER ALL",
    "DROP TRIGGER audit_log_no_update_delete ON audit_log",
    "CREATE TABLE sneaky (id int)",
    "TRUNCATE jobs CASCADE",
    "DELETE FROM jobs",
    "DELETE FROM users",
    "SELECT * FROM schema_migrations",
    "ALTER ROLE hub_app SUPERUSER",
    "ALTER ROLE hub_app_test SUPERUSER",
])
def test_the_app_role_cannot_change_the_schema_or_wipe_data(hub, app_url, statement):
    _denied(app_url, statement)


def test_the_owner_cannot_truncate_the_audit_log_either(hub):
    with db.connection() as conn:
        conn.execute("INSERT INTO audit_log (actor, action, entity_type) VALUES ('t', 'a', 'x')")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with db.connection() as conn:
            conn.execute("TRUNCATE audit_log")
    assert one("SELECT count(*) AS n FROM audit_log")["n"] == 1


def test_the_app_role_is_not_privileged(hub):
    role = one("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = 'hub_app'")
    assert not any(role.values())
    owned = one("SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'public' AND tableowner = 'hub_app'")
    assert owned["n"] == 0


def test_every_table_is_readable_by_the_app_role_so_a_forgotten_grant_fails_here(hub):
    """A new migration that adds a table without a GRANT would break production; this catches it."""
    ungranted = rows(
        "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
        "AND relname <> 'schema_migrations' AND NOT has_table_privilege('hub_app', oid, 'SELECT')"
    )
    assert ungranted == []


def test_the_whole_engine_works_as_the_app_role(hub, app_url, telegram_up):
    """Ingest, run, fail to dead, alert, log in, retry and log out, with no owner rights."""
    db.init_pool(app_url)
    alerts.create_rule("job_dead", chat="12345")
    make_source("good")
    make_source("bad", handler="nope", max_attempts=1)

    deliver("good", key="g1")
    deliver("bad", key="b1")
    assert jobs.run_one("w1") and jobs.run_one("w1")
    assert one("SELECT count(*) AS n FROM jobs WHERE status = 'succeeded'")["n"] == 1
    dead = one("SELECT id FROM jobs WHERE status = 'dead'")["id"]

    alerts.tick()
    assert len(telegram_up.texts) == 1

    headers = login_headers()
    token = headers["Authorization"].removeprefix("Bearer ")
    assert auth.authenticate(token).username == "diego"
    runs.retry(dead, "user:diego")
    assert one("SELECT status FROM jobs WHERE id = %s", (dead,))["status"] == "queued"
    auth.logout(token)
    assert auth.authenticate(token) is None
    assert one("SELECT count(*) AS n FROM audit_log WHERE action = 'job.manual_retry'")["n"] == 1


def test_migrating_twice_does_not_break_the_role(test_database_url):
    assert migrate(test_database_url) == []


def test_setting_the_password_rejects_short_ones_and_the_cli_needs_the_variable(monkeypatch):
    with pytest.raises(ValueError, match="at least 12"):
        dbroles.set_app_password("short")
    monkeypatch.setattr(dbroles.config, "APP_DB_PASSWORD", "")
    with pytest.raises(SystemExit, match="APP_DB_PASSWORD is not set"):
        dbroles.main()
    monkeypatch.setattr(dbroles.config, "APP_DB_PASSWORD", "short")
    with pytest.raises(SystemExit, match="at least 12"):
        dbroles.main()
