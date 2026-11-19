"""Small builders so each test reads as the scenario it checks."""
import json
import time

from app import db

SECRET = "test-secret-0123456789"


def make_source(name="shop", handler="log", max_attempts=5, active=True, secret=SECRET):
    with db.connection() as conn:
        return conn.execute(
            "INSERT INTO sources (name, signing_secret, handler, max_attempts, active) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (name, secret, handler, max_attempts, active),
        ).fetchone()["id"]


def signed_headers(body, key="evt-1", secret=SECRET, timestamp=None, signature=None):
    """The three headers a sender attaches. Like a real sender, signs the key with whitespace trimmed."""
    from app import signing  # imported here so DB-only tests don't depend on the engine

    stamp = str(int(time.time())) if timestamp is None else str(timestamp)
    headers = {
        "X-Hub-Timestamp": stamp,
        "X-Hub-Signature-256": signature or signing.sign(secret, body, stamp, (key or "").strip()),
    }
    if key is not None:
        headers["X-Idempotency-Key"] = key
    return headers


def deliver(source="shop", key="evt-1", payload=None, secret=SECRET, body=None, signature=None, timestamp=None, now=None):
    """Send one webhook through the ingest path, signed correctly unless told otherwise."""
    from app import ingest  # imported here so DB-only tests don't depend on the engine

    if body is None:
        body = json.dumps(payload if payload is not None else {"n": 1}).encode()
    sent = signed_headers(body, key, secret, timestamp, signature)
    return ingest.receive(
        source, body, sent["X-Hub-Signature-256"], sent["X-Hub-Timestamp"], key, now=now
    )


def rows(sql, params=()):
    with db.connection() as conn:
        return conn.execute(sql, params).fetchall()


def one(sql, params=()):
    with db.connection() as conn:
        return conn.execute(sql, params).fetchone()


def reopen_pool():
    """run_forever() and the app lifespan close the shared pool on exit; reopen it to assert afterwards."""
    from app import config

    db.init_pool(config.DATABASE_URL)


def make_due(job_id):
    """Pretend the retry delay has passed."""
    with db.connection() as conn:
        conn.execute("UPDATE jobs SET run_at = now() - interval '1 second' WHERE id = %s", (job_id,))


PASSWORD = "correct horse battery"
_job_counter = iter(range(1, 10**9))


def make_user(username="diego", role="admin", password=PASSWORD):
    from app import users

    return users.create_user(username, password, role)


def login_headers(username="diego", role="admin", password=PASSWORD, ip="10.0.0.1"):
    """Create the user (if new) and return an Authorization header for a fresh session."""
    from app import auth, users

    try:
        make_user(username, role, password)
    except users.UserExists:
        pass
    return {"Authorization": "Bearer " + auth.login(username, password, ip).token}


def make_job(status="queued", attempts=0, max_attempts=None, last_error=None, source="shop", created_at=None):
    """A real event and job (through ingest) forced into the given state. Creates the source if needed."""
    if not one("SELECT 1 AS x FROM sources WHERE name = %s", (source,)):
        make_source(source)
    receipt = deliver(source, key=f"job-{next(_job_counter)}", payload={"n": 1})
    finished = "now()" if status in ("succeeded", "dead") else "NULL"
    with db.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status = %s, attempts = %s, last_error = %s, finished_at = " + finished +
            ", max_attempts = COALESCE(%s, max_attempts), created_at = COALESCE(%s, created_at) WHERE id = %s",
            (status, attempts, last_error, max_attempts, created_at, receipt.job_id),
        )
    return receipt.job_id
