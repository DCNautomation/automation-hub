"""Manage webhook sources (who may send events, and what runs for them).

    python -m app.sources create shop-orders --handler log --max-attempts 5
    python -m app.sources list
    python -m app.sources rotate shop-orders
    python -m app.sources disable shop-orders      (enable puts it back)

A new or rotated signing secret is printed ONCE, to your terminal, and never again. It is stored
in the database because the hub needs it to check signatures, but no endpoint, log or audit row
ever shows it. Give it to whatever sends the webhooks (n8n, a store, a script).
"""
import argparse
import re
import secrets
import sys

import psycopg

from app import audit, config, db, handlers

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,39}")  # same rule as the sources.name CHECK in the database


class SourceExists(Exception):
    pass


class SourceNotFound(Exception):
    pass


def new_secret():
    return secrets.token_urlsafe(32)  # 43 characters, 256 bits


def create_source(name, handler="log", max_attempts=5):
    """Returns (id, signing secret). The secret is only ever returned here."""
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError("name must be 2 to 40 characters: a-z, 0-9, dash, underscore, starting with a letter or digit")
    if handler not in handlers.HANDLERS:
        raise ValueError(f"unknown handler {handler!r}; available: {', '.join(sorted(handlers.HANDLERS))}")
    if not 1 <= max_attempts <= 20:
        raise ValueError("max attempts must be between 1 and 20")
    secret = new_secret()
    try:
        with db.connection() as conn:
            source_id = conn.execute(
                "INSERT INTO sources (name, signing_secret, handler, max_attempts) VALUES (%s, %s, %s, %s) RETURNING id",
                (name, secret, handler, max_attempts),
            ).fetchone()["id"]
            audit.record(conn, "cli", "source.created", "source", source_id,
                         after={"name": name, "handler": handler, "max_attempts": max_attempts})
    except psycopg.errors.UniqueViolation:
        raise SourceExists(name)
    return source_id, secret


def rotate_secret(name):
    """Replace the signing secret. Senders using the old one are rejected from this moment."""
    secret = new_secret()
    with db.connection() as conn:
        row = conn.execute("UPDATE sources SET signing_secret = %s WHERE name = %s RETURNING id", (secret, name)).fetchone()
        if row is None:
            raise SourceNotFound(name)
        audit.record(conn, "cli", "source.secret_rotated", "source", row["id"], after={"name": name})
    return secret


def set_active(name, active):
    with db.connection() as conn:
        row = conn.execute("UPDATE sources SET active = %s WHERE name = %s RETURNING id", (active, name)).fetchone()
        if row is None:
            raise SourceNotFound(name)
        audit.record(conn, "cli", "source.enabled" if active else "source.disabled", "source", row["id"],
                     after={"name": name, "active": active})


def list_sources():
    with db.connection() as conn:
        return conn.execute(
            "SELECT name, handler, max_attempts, active, created_at FROM sources ORDER BY name"
        ).fetchall()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m app.sources")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="add a source and print its signing secret once")
    create.add_argument("name")
    create.add_argument("--handler", default="log")
    create.add_argument("--max-attempts", type=int, default=5)
    sub.add_parser("list")
    for name in ("rotate", "enable", "disable"):
        sub.add_parser(name).add_argument("name")
    args = parser.parse_args(argv)

    db.init_pool(config.DATABASE_URL)
    try:
        if args.command == "create":
            _, secret = create_source(args.name, args.handler, args.max_attempts)
            print(f"Created source {args.name}.")
            _print_secret(secret)
        elif args.command == "rotate":
            _print_secret(rotate_secret(args.name))
        elif args.command == "list":
            for s in list_sources():
                print(f"{s['name']:<24} handler={s['handler']:<8} max_attempts={s['max_attempts']:<3} "
                      f"{'active' if s['active'] else 'DISABLED'}")
        else:
            set_active(args.name, args.command == "enable")
            print(f"{args.name} {'enabled' if args.command == 'enable' else 'disabled'}")
    except (ValueError, SourceExists, SourceNotFound) as exc:
        reason = f"already exists: {exc}" if isinstance(exc, SourceExists) else (
            f"no such source: {exc}" if isinstance(exc, SourceNotFound) else str(exc))
        sys.exit(f"Failed: {reason}")
    finally:
        db.close_pool()


def _print_secret(secret):
    print("Signing secret (shown once, copy it now):")
    print(f"  {secret}")


if __name__ == "__main__":
    main()
