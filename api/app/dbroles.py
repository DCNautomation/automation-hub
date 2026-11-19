"""Set the password of the limited hub_app database role. Run as the database owner, after migrate.

    python -m app.dbroles

Reads APP_DB_PASSWORD from the environment (never a command-line argument). Safe to repeat.
"""
import sys

import psycopg
from psycopg import sql

from app import config

APP_ROLE = "hub_app"
MIN_PASSWORD_LENGTH = 12


def set_app_password(password, database_url=None, role=APP_ROLE):
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"APP_DB_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters")
    with psycopg.connect(database_url or config.MIGRATE_DATABASE_URL, autocommit=True) as conn:
        # ALTER ROLE cannot take bind parameters, so the value is quoted as a literal by psycopg.
        conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))


def main():
    if not config.APP_DB_PASSWORD:
        sys.exit("APP_DB_PASSWORD is not set.")
    try:
        set_app_password(config.APP_DB_PASSWORD)
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"password set for role {APP_ROLE}")


if __name__ == "__main__":
    main()
