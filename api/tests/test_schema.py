"""The database itself enforces the guarantees, not just the app code."""
import psycopg
import pytest

from app import db

from helpers import make_source, one


def _event(conn, source_id, key="k1"):
    return conn.execute(
        "INSERT INTO webhook_events (source_id, idempotency_key, payload) VALUES (%s, %s, '{}') RETURNING id",
        (source_id, key),
    ).fetchone()["id"]


def test_audit_log_rejects_update_and_delete(hub):
    with db.connection() as conn:
        conn.execute("INSERT INTO audit_log (actor, action, entity_type) VALUES ('t', 'a', 'x')")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with db.connection() as conn:
            conn.execute("UPDATE audit_log SET actor = 'changed'")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with db.connection() as conn:
            conn.execute("DELETE FROM audit_log")
    assert one("SELECT count(*) AS n FROM audit_log")["n"] == 1


def test_same_idempotency_key_twice_on_one_source_is_rejected(hub):
    source_id = make_source()
    with db.connection() as conn:
        _event(conn, source_id)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.connection() as conn:
            _event(conn, source_id)


def test_same_idempotency_key_on_two_sources_is_allowed(hub):
    a, b = make_source("shop-a"), make_source("shop-b")
    with db.connection() as conn:
        _event(conn, a)
        _event(conn, b)
    assert one("SELECT count(*) AS n FROM webhook_events")["n"] == 2


def test_source_name_and_secret_are_validated(hub):
    with pytest.raises(psycopg.errors.CheckViolation):
        make_source(name="Bad Name!")
    with pytest.raises(psycopg.errors.CheckViolation):
        make_source(name="ok-name", secret="short")


def test_job_status_is_limited_to_known_states(hub):
    source_id = make_source()
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as conn:
            event_id = _event(conn, source_id)
            conn.execute(
                "INSERT INTO jobs (event_id, source_id, status, max_attempts) VALUES (%s, %s, 'weird', 3)",
                (event_id, source_id),
            )
