"""Apply numbered SQL files from db/migrations once each. Run: python -m app.migrate"""
from pathlib import Path

import psycopg

from app import config

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"
LOCK_ID = 727274  # arbitrary constant shared by every runner so only one migrates at a time


def migrate(database_url=None, migrations_dir=None):
    directory = Path(migrations_dir) if migrations_dir else MIGRATIONS_DIR
    applied_now = []
    # autocommit means each `conn.transaction()` below is a real BEGIN/COMMIT, so a migration
    # applies fully or not at all, and earlier migrations stay applied if a later one fails.
    with psycopg.connect(database_url or config.MIGRATE_DATABASE_URL, autocommit=True) as conn:
        # Two containers starting together must not both apply the same file.
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                applied_now.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    return applied_now


if __name__ == "__main__":
    applied = migrate()
    print("applied:", ", ".join(applied) if applied else "nothing new")
