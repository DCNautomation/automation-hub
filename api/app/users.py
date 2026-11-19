"""Create dashboard users. There is no sign-up endpoint on purpose: accounts are made here.

    python -m app.users create <username> --role admin
The password is read from HUB_NEW_PASSWORD if set, otherwise prompted for (never a command-line argument).
"""
import argparse
import getpass
import os
import re
import sys

import psycopg

from app import audit, config, db, passwords

USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,39}")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 200
ROLES = ("admin", "viewer")


class UserExists(Exception):
    pass


def normalize_username(username):
    return (username or "").strip().lower()


def create_user(username, password, role="viewer"):
    name = normalize_username(username)
    if not USERNAME_RE.fullmatch(name):
        raise ValueError("username must be 3 to 40 characters: a-z, 0-9, dot, dash, underscore")
    if not MIN_PASSWORD_LENGTH <= len(password or "") <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters")
    if role not in ROLES:
        raise ValueError(f"role must be one of: {', '.join(ROLES)}")
    password_hash = passwords.hash_password(password)
    try:
        with db.connection() as conn:
            user_id = conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
                (name, password_hash, role),
            ).fetchone()["id"]
            audit.record(conn, "cli", "user.created", "user", user_id, after={"username": name, "role": role})
    except psycopg.errors.UniqueViolation:
        raise UserExists(name)
    return user_id


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m app.users")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create a user")
    create.add_argument("username")
    create.add_argument("--role", choices=ROLES, default="viewer")
    args = parser.parse_args(argv)

    password = os.environ.get("HUB_NEW_PASSWORD")
    if not password:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            sys.exit("Passwords do not match.")
    db.init_pool(config.DATABASE_URL)
    try:
        create_user(args.username, password, args.role)
    except (ValueError, UserExists) as exc:
        sys.exit(f"Not created: {exc}")
    finally:
        db.close_pool()
    print(f"Created {args.role} {normalize_username(args.username)}")


if __name__ == "__main__":
    main()
