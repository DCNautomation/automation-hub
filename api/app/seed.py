"""Repeatable demo data, so the dashboard always has something real to show.

    python -m app.seed demo      add the demo sources, 19 runs and a read-only demo-viewer login
    python -m app.seed reset     remove everything demo (needs the database owner login)

Nothing here fakes a state. Every event goes through the real signed ingest path, and the failures
come from the real worker code failing, retrying with backoff and giving up, so what the dashboard
shows is what the engine actually did. Only the `demo-` sources and the `demo-viewer` user are
ever created or removed.

Known limit: the audit log is append-only, so a reset leaves its rows behind. They are harmless
(they only point at runs that no longer exist) and the next seed writes fresh ones.
"""
import argparse
import json
import os
import secrets
import sys
import time

import psycopg

from app import config, db, ingest, jobs, signing, users

PREFIX = "demo-"
VIEWER = "demo-viewer"

# source -> (max_attempts, [(payload, tries the job should have used by the end)])
# A job that keeps failing for as many tries as its source allows ends "dead"; fewer leaves it "retrying".
_ORDERS = [
    {"event": "order.paid", "order_id": 1001 + n, "total_php": total, "customer": name}
    for n, (total, name) in enumerate([
        (1250, "A. Reyes"), (480, "M. Santos"), (2310, "J. Cruz"), (990, "L. Bautista"),
        (150, "R. Garcia"), (3200, "K. Mendoza"), (760, "T. Flores"), (1840, "D. Ramos"),
    ])
]
PLAN = {
    "demo-shop": (5, [(p, 1) for p in _ORDERS]),
    "demo-billing": (3, [
        ({"event": "invoice.created", "invoice": "INV-2041", "error": "payment gateway declined the card (insufficient funds)"}, 3),
        ({"event": "invoice.created", "invoice": "INV-2042", "error": "payment gateway did not answer within 30s"}, 3),
        ({"event": "invoice.created", "invoice": "INV-2043", "error": "invoice total does not match its line items"}, 3),
        ({"event": "invoice.created", "invoice": "INV-2044"}, 1),
        ({"event": "invoice.created", "invoice": "INV-2045"}, 1),
    ]),
    "demo-support": (2, [
        ({"event": "ticket.opened", "ticket": "T-318", "error": "helpdesk API returned 503 service unavailable"}, 2),
    ]),
    "demo-crm": (5, [
        ({"event": "contact.updated", "contact": "C-77", "error": "CRM rate limit hit (429 too many requests)"}, 1),
        ({"event": "contact.updated", "contact": "C-78", "error": "CRM rate limit hit (429 too many requests)"}, 1),
        ({"event": "contact.updated", "contact": "C-79", "error": "CRM did not answer within 30s"}, 2),
        ({"event": "contact.updated", "contact": "C-80"}, 1),
        ({"event": "contact.updated", "contact": "C-81"}, 1),
    ]),
}


class AlreadySeeded(Exception):
    pass


def demo(viewer_password=None):
    """Create the demo data. Returns {"counts": {...}, "viewer_password": str or None}.
    The viewer password is returned only when the login is created here; an existing one is left alone."""
    from app import sources  # imported late: sources imports handlers, which seed does not need at import time

    password = viewer_password or secrets.token_urlsafe(12)
    # Checked before anything is written, so a bad password can't leave half a dataset behind.
    if not users.MIN_PASSWORD_LENGTH <= len(password) <= users.MAX_PASSWORD_LENGTH:
        raise ValueError(f"password must be {users.MIN_PASSWORD_LENGTH} to {users.MAX_PASSWORD_LENGTH} characters")
    with db.connection() as conn:
        if conn.execute("SELECT 1 FROM sources WHERE name LIKE %s LIMIT 1", (PREFIX + "%",)).fetchone():
            raise AlreadySeeded("demo data already exists; run `python -m app.seed reset` first")

    wanted = {}  # job id -> tries it should have used
    for name, (max_attempts, events) in PLAN.items():
        _, secret = sources.create_source(name, handler="demo", max_attempts=max_attempts)
        for n, (payload, tries) in enumerate(events, start=1):
            body = json.dumps(payload).encode()
            key = f"{name}-{n:03d}"
            stamp = str(int(time.time()))
            receipt = ingest.receive(name, body, signing.sign(secret, body, stamp, key), stamp, key)
            wanted[receipt.job_id] = tries

    _drain()
    for round_number in range(2, 1 + max(wanted.values())):
        again = [j for j, tries in wanted.items() if tries >= round_number]
        with db.connection() as conn:
            # Skip the retry wait that a real clock would impose; the attempt itself is fully real.
            conn.execute(
                "UPDATE jobs SET run_at = now() WHERE id = ANY(%s) AND status = 'failed'", (again,)
            )
        _drain()

    try:
        users.create_user(VIEWER, password, "viewer")
    except users.UserExists:
        password = None
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT j.status, count(*) AS n FROM jobs j JOIN sources s ON s.id = j.source_id "
            "WHERE s.name LIKE %s GROUP BY j.status", (PREFIX + "%",)
        ).fetchall()
    return {"counts": {r["status"]: r["n"] for r in rows}, "viewer_password": password}


def _drain():
    while jobs.run_one("seed"):
        pass


def reset(database_url=None):
    """Delete the demo sources, their events and runs, and the demo viewer. Needs the owner login:
    the app's own role cannot delete runs (on purpose). Returns how many of each were removed."""
    with psycopg.connect(database_url or config.MIGRATE_DATABASE_URL) as conn:
        scope = "(SELECT id FROM sources WHERE name LIKE %s)"
        like = (PREFIX + "%",)
        removed = {
            "runs": conn.execute(f"DELETE FROM jobs WHERE source_id IN {scope}", like).rowcount,
            "events": conn.execute(f"DELETE FROM webhook_events WHERE source_id IN {scope}", like).rowcount,
            "sources": conn.execute("DELETE FROM sources WHERE name LIKE %s", like).rowcount,
            "users": conn.execute("DELETE FROM users WHERE username = %s", (VIEWER,)).rowcount,
        }
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m app.seed")
    parser.add_argument("command", choices=["demo", "reset"])
    args = parser.parse_args(argv)
    try:
        if args.command == "reset":
            print("removed:", reset())
            return
        db.init_pool(config.DATABASE_URL)
        try:
            result = demo(os.environ.get("DEMO_VIEWER_PASSWORD") or None)
        finally:
            db.close_pool()
    except (AlreadySeeded, ValueError, psycopg.errors.InsufficientPrivilege) as exc:
        sys.exit(f"Failed: {exc}")
    print("runs by state:", result["counts"])
    if result["viewer_password"]:
        print(f"Read-only login created. Username: {VIEWER}   Password (shown once): {result['viewer_password']}")
    else:
        print(f"{VIEWER} already existed, so its password was left alone.")


if __name__ == "__main__":
    main()
