"""Receiving a webhook: signature, validation, idempotency."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import ingest, signing

from helpers import SECRET, deliver, make_source, one, rows


NOW = 1_800_000_000


def _counts():
    return (
        one("SELECT count(*) AS n FROM webhook_events")["n"],
        one("SELECT count(*) AS n FROM jobs")["n"],
    )


def test_valid_delivery_stores_event_and_queues_a_job(hub):
    make_source(max_attempts=4)
    receipt = deliver(payload={"order": 42})

    assert receipt.duplicate is False
    event = one("SELECT * FROM webhook_events WHERE id = %s", (receipt.event_id,))
    assert event["payload"] == {"order": 42}
    assert event["idempotency_key"] == "evt-1"
    job = one("SELECT * FROM jobs WHERE id = %s", (receipt.job_id,))
    assert (job["status"], job["attempts"], job["max_attempts"]) == ("queued", 0, 4)
    audit = rows("SELECT action FROM audit_log WHERE entity_type = 'webhook_event'")
    assert [a["action"] for a in audit] == ["event.received"]


def test_duplicate_delivery_is_acknowledged_but_creates_nothing_new(hub):
    make_source()
    first = deliver(key="evt-1")
    second = deliver(key="evt-1")

    assert second.duplicate is True
    assert (second.event_id, second.job_id) == (first.event_id, first.job_id)
    assert _counts() == (1, 1)


def test_same_key_on_a_different_source_is_not_a_duplicate(hub):
    make_source("shop-a", secret="secret-a-0123456789")
    make_source("shop-b", secret="secret-b-0123456789")
    deliver("shop-a", key="evt-1", secret="secret-a-0123456789")
    receipt = deliver("shop-b", key="evt-1", secret="secret-b-0123456789")

    assert receipt.duplicate is False
    assert _counts() == (2, 2)


def test_bad_signature_is_rejected_and_nothing_is_stored(hub):
    make_source()
    with pytest.raises(ingest.BadSignature):
        deliver(secret="the-wrong-secret-0123")
    with pytest.raises(ingest.BadSignature):
        deliver(signature="sha256=" + "0" * 64)
    assert _counts() == (0, 0)


def test_signature_must_cover_the_exact_body(hub):
    make_source()
    signed_for = json.dumps({"n": 1}).encode()
    with pytest.raises(ingest.BadSignature):
        deliver(
            body=json.dumps({"n": 2}).encode(),
            timestamp=NOW,
            now=NOW,
            signature=signing.sign(SECRET, signed_for, str(NOW), "evt-1"),
        )
    assert _counts() == (0, 0)


# Replay protection: the signature covers timestamp, key and body, and the timestamp must be fresh.


def test_a_captured_request_cannot_be_resent_under_a_new_key(hub):
    make_source()
    body = json.dumps({"n": 1}).encode()
    captured = signing.sign(SECRET, body, str(NOW), "evt-1")
    deliver(key="evt-1", timestamp=NOW, now=NOW, signature=captured)
    with pytest.raises(ingest.BadSignature):
        deliver(key="evt-2", timestamp=NOW, now=NOW, signature=captured)
    assert _counts() == (1, 1)


def test_the_timestamp_is_part_of_what_is_signed(hub):
    make_source()
    body = json.dumps({"n": 1}).encode()
    old = signing.sign(SECRET, body, str(NOW - 10), "evt-1")
    with pytest.raises(ingest.BadSignature):
        deliver(timestamp=NOW, now=NOW, signature=old)
    assert _counts() == (0, 0)


def test_an_exact_resend_inside_the_window_is_just_a_duplicate(hub):
    make_source()
    first = deliver(timestamp=NOW, now=NOW)
    again = deliver(timestamp=NOW, now=NOW + 120)
    assert again.duplicate is True and again.job_id == first.job_id
    assert _counts() == (1, 1)


@pytest.mark.parametrize("offset", [-ingest.TIMESTAMP_TOLERANCE_SECONDS, 0, ingest.TIMESTAMP_TOLERANCE_SECONDS])
def test_timestamps_up_to_the_tolerance_either_way_are_accepted(hub, offset):
    make_source()
    assert deliver(timestamp=NOW + offset, now=NOW).duplicate is False


@pytest.mark.parametrize("offset", [-ingest.TIMESTAMP_TOLERANCE_SECONDS - 1, ingest.TIMESTAMP_TOLERANCE_SECONDS + 1, -86400])
def test_validly_signed_but_stale_or_future_requests_are_rejected(hub, offset):
    make_source()
    with pytest.raises(ingest.StaleTimestamp):
        deliver(timestamp=NOW + offset, now=NOW)
    assert _counts() == (0, 0)


def test_staleness_is_only_revealed_to_someone_who_can_sign(hub):
    make_source()
    with pytest.raises(ingest.BadSignature):
        deliver(secret="the-wrong-secret-0123", timestamp=NOW - 86400, now=NOW)
    assert _counts() == (0, 0)


@pytest.mark.parametrize("stamp", [None, "", "abc", "-5", "1.5e9", "12 34", "١٢٣", "9" * 40])
def test_missing_or_malformed_timestamp_fails_like_a_bad_signature(hub, stamp):
    make_source()
    body = json.dumps({"n": 1}).encode()
    signature = signing.sign(SECRET, body, stamp or "", "evt-1")
    with pytest.raises(ingest.BadSignature):
        ingest.receive("shop", body, signature, stamp, "evt-1", now=NOW)
    assert _counts() == (0, 0)


def test_unknown_or_inactive_source_looks_the_same(hub):
    make_source("paused", active=False)
    with pytest.raises(ingest.UnknownSource):
        deliver("nope")
    with pytest.raises(ingest.UnknownSource):
        deliver("paused")
    assert _counts() == (0, 0)


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[1, 2, 3]", b'"just a string"', b"", b"null", b'{"a": NaN}', b'{"a": "\\u0000"}'],
)
def test_payload_must_be_a_json_object(hub, body):
    make_source()
    with pytest.raises(ingest.InvalidPayload):
        deliver(body=body)
    assert _counts() == (0, 0)


def test_oversized_payload_is_rejected(hub):
    make_source()
    with pytest.raises(ingest.InvalidPayload):
        deliver(body=json.dumps({"blob": "x" * (ingest.MAX_BODY_BYTES + 1)}).encode())
    assert _counts() == (0, 0)


@pytest.mark.parametrize("key", ["", "   ", None, "k" * 201])
def test_idempotency_key_is_required_and_bounded(hub, key):
    make_source()
    with pytest.raises(ingest.MissingIdempotencyKey):
        deliver(key=key)
    assert _counts() == (0, 0)


def test_simultaneous_duplicate_deliveries_store_exactly_one(hub):
    make_source()
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda _: deliver(key="same"), range(8)))

    assert sum(1 for r in receipts if not r.duplicate) == 1
    assert len({(r.event_id, r.job_id) for r in receipts}) == 1
    assert _counts() == (1, 1)
