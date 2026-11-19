"""Receive one webhook: authenticate it, validate it, store it raw, queue exactly one job.

Order matters: source lookup, size, signature, timestamp freshness, then idempotency key and
payload shape, so an unauthenticated sender learns nothing beyond "no" (an unknown source, a bad
signature and a missing timestamp all look the same) and nothing is written for a rejected
request. Freshness is checked only after the signature passes, so "too old" is only ever told to
someone holding the secret.
"""
import json
import time
from typing import NamedTuple

import psycopg
from psycopg.types.json import Jsonb

from app import audit, db, signing

MAX_BODY_BYTES = 256 * 1024
MAX_KEY_LENGTH = 200
TIMESTAMP_TOLERANCE_SECONDS = 300  # accepted either side of the server clock
DUMMY_SECRET = "unknown-source-timing-pad"


class IngestError(Exception):
    """Base for every reason a webhook is turned away."""


class UnknownSource(IngestError):
    pass


class BadSignature(IngestError):
    pass


class StaleTimestamp(IngestError):
    """Correctly signed, but the timestamp is outside the tolerance window."""


class MissingIdempotencyKey(IngestError):
    pass


class InvalidPayload(IngestError):
    pass


class PayloadTooLarge(InvalidPayload):
    pass


class Receipt(NamedTuple):
    event_id: int
    job_id: int
    duplicate: bool


def receive(source_name, body, signature, timestamp, idempotency_key, headers=None, now=None):
    key = (idempotency_key or "").strip()
    with db.connection() as conn:
        source = conn.execute(
            "SELECT id, signing_secret, max_attempts FROM sources WHERE name = %s AND active",
            (source_name,),
        ).fetchone()
    if source is None:
        # Do the same hashing work a real source would, so response time doesn't reveal which
        # source names exist. The HTTP layer answers this exactly like a bad signature.
        signing.verify(DUMMY_SECRET, body[:MAX_BODY_BYTES], signature, timestamp, key)
        raise UnknownSource(source_name)

    if len(body) > MAX_BODY_BYTES:
        raise PayloadTooLarge(f"body over {MAX_BODY_BYTES} bytes")
    sent_at = signing.parse_timestamp(timestamp)
    if sent_at is None or not signing.verify(source["signing_secret"], body, signature, timestamp, key):
        raise BadSignature(source_name)
    if abs((time.time() if now is None else now) - sent_at) > TIMESTAMP_TOLERANCE_SECONDS:
        raise StaleTimestamp(source_name)

    if not key or len(key) > MAX_KEY_LENGTH:
        raise MissingIdempotencyKey(f"key must be 1 to {MAX_KEY_LENGTH} characters")
    payload = _parse_object(body)

    try:
        return _store(source, source_name, key, payload, headers)
    except psycopg.errors.DataError:
        # Valid JSON that Postgres jsonb refuses (for example a \u0000 escape in a string).
        raise InvalidPayload("payload contains values that cannot be stored")


def _parse_object(body):
    def reject_constant(name):
        raise ValueError(f"{name} is not valid JSON")

    try:
        # parse_constant rejects NaN/Infinity, which Python accepts but strict JSON and Postgres don't.
        payload = json.loads(body, parse_constant=reject_constant)
    except (ValueError, RecursionError):  # RecursionError: absurdly deep nesting
        raise InvalidPayload("body is not valid JSON")
    if not isinstance(payload, dict):
        raise InvalidPayload("payload must be a JSON object")
    return payload


def _store(source, source_name, key, payload, headers):
    with db.connection() as conn:
        # ON CONFLICT DO NOTHING waits for a concurrent insert of the same key to commit, then
        # returns no row: that is what makes simultaneous duplicate deliveries safe.
        row = conn.execute(
            "INSERT INTO webhook_events (source_id, idempotency_key, headers, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (source_id, idempotency_key) DO NOTHING RETURNING id",
            (source["id"], key, Jsonb(headers or {}), Jsonb(payload)),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                "SELECT e.id AS event_id, j.id AS job_id FROM webhook_events e "
                "JOIN jobs j ON j.event_id = e.id WHERE e.source_id = %s AND e.idempotency_key = %s",
                (source["id"], key),
            ).fetchone()
            return Receipt(existing["event_id"], existing["job_id"], True)

        event_id = row["id"]
        job_id = conn.execute(
            "INSERT INTO jobs (event_id, source_id, max_attempts) VALUES (%s, %s, %s) RETURNING id",
            (event_id, source["id"], source["max_attempts"]),
        ).fetchone()["id"]
        audit.record(
            conn,
            f"ingest:{source_name}",
            "event.received",
            "webhook_event",
            event_id,
            after={"source": source_name, "idempotency_key": key, "job_id": job_id},
        )
    return Receipt(event_id, job_id, False)
