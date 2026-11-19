"""Cheap checks that run without Postgres. DB-backed tests arrive with the core engine."""
import re
from pathlib import Path

from app.migrate import MIGRATIONS_DIR


def test_migrations_exist_and_are_numbered():
    files = sorted(Path(MIGRATIONS_DIR).glob("*.sql"))
    assert files, "no migrations found"
    for f in files:
        assert re.match(r"^\d{3}_[a-z0-9_]+\.sql$", f.name), f"bad migration name: {f.name}"


def test_initial_schema_has_core_guards():
    sql = (Path(MIGRATIONS_DIR) / "001_initial.sql").read_text(encoding="utf-8")
    assert "UNIQUE (source_id, idempotency_key)" in sql
    assert "audit_log_no_update_delete" in sql
    assert "jobs_claim_idx" in sql
