"""The migration runner on throwaway databases: partial failure and concurrent runners."""
import threading
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from app import config
from app.migrate import MIGRATIONS_DIR, migrate


def _url(name):
    base = config.MIGRATE_DATABASE_URL
    return urlunsplit(urlsplit(base)._replace(path="/" + name))


@pytest.fixture
def scratch_db():
    name = "hub_migrate_scratch"
    with psycopg.connect(_url("postgres"), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name}")
        admin.execute(f"CREATE DATABASE {name}")
    yield _url(name)
    with psycopg.connect(_url("postgres"), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_failed_migration_rolls_back_alone_and_earlier_ones_stay(scratch_db, tmp_path):
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE good (id int);")
    (tmp_path / "002_bad.sql").write_text("CREATE TABLE half (id int); SELECT 1/0;")

    with pytest.raises(psycopg.errors.DivisionByZero):
        migrate(scratch_db, migrations_dir=tmp_path)

    with psycopg.connect(scratch_db) as conn:
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        applied = [r[0] for r in conn.execute("SELECT name FROM schema_migrations")]
    assert "good" in tables and "half" not in tables
    assert applied == ["001_ok.sql"]


def test_fixing_the_bad_migration_and_rerunning_applies_only_what_is_left(scratch_db, tmp_path):
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE good (id int);")
    (tmp_path / "002_bad.sql").write_text("SELECT 1/0;")
    with pytest.raises(psycopg.errors.DivisionByZero):
        migrate(scratch_db, migrations_dir=tmp_path)

    (tmp_path / "002_bad.sql").write_text("CREATE TABLE fixed (id int);")
    assert migrate(scratch_db, migrations_dir=tmp_path) == ["002_bad.sql"]


def test_two_runners_starting_together_apply_each_file_once(scratch_db):
    results, errors = [], []

    def run():
        try:
            results.append(migrate(scratch_db, migrations_dir=MIGRATIONS_DIR))
        except Exception as exc:  # noqa: BLE001 - the test reports whatever went wrong
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    applied = sorted(name for r in results for name in r)
    # exactly one runner did the work: every file once, none twice
    assert applied == sorted(f.name for f in MIGRATIONS_DIR.glob("*.sql"))
