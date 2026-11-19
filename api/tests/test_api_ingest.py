"""The HTTP face of ingest: POST /ingest/{source}."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import ingest
from app.main import app

from helpers import SECRET, make_source, one, signed_headers


@pytest.fixture
def client(hub):
    with TestClient(app) as c:
        yield c


def _post(client, source="shop", payload=None, key="evt-1", secret=SECRET, signature=None, headers=None, timestamp=None):
    body = json.dumps(payload if payload is not None else {"n": 1}).encode()
    send = {**signed_headers(body, key, secret, timestamp, signature), "Content-Type": "application/json"}
    send.update(headers or {})
    return client.post(f"/ingest/{source}", content=body, headers=send)


def _raw_post(client, body, key="k", **kw):
    return client.post("/ingest/shop", content=body, headers=signed_headers(body, key, **kw))


def test_new_event_is_accepted_with_202(client):
    make_source()
    resp = _post(client)
    assert resp.status_code == 202
    body = resp.json()
    assert body["duplicate"] is False and body["event_id"] and body["job_id"]


def test_duplicate_returns_200_and_the_original_ids(client):
    make_source()
    first = _post(client).json()
    resp = _post(client)
    assert resp.status_code == 200
    assert resp.json() == {**first, "duplicate": True}
    assert one("SELECT count(*) AS n FROM jobs")["n"] == 1


def test_bad_signature_is_401(client):
    make_source()
    assert _post(client, secret="the-wrong-secret-0123").status_code == 401
    assert _post(client, signature="sha256=" + "0" * 64).status_code == 401
    assert one("SELECT count(*) AS n FROM webhook_events")["n"] == 0


def test_unknown_source_is_indistinguishable_from_a_bad_signature(client):
    make_source()
    make_source("paused", active=False)
    bad_sig = _post(client, secret="the-wrong-secret-0123")
    for source in ("nope", "paused"):
        resp = _post(client, source=source)
        assert (resp.status_code, resp.json()) == (bad_sig.status_code, bad_sig.json()) == (401, {"detail": "invalid signature"})


@pytest.mark.parametrize(
    "body",
    [b'{"a": NaN}', b'{"a": Infinity}', b'{"a": "\\u0000"}'],
    ids=["nan", "infinity", "nul-escape"],
)
def test_unstorable_json_is_422_not_a_500(client, body):
    make_source()
    resp = _raw_post(client, body)
    assert resp.status_code == 422
    assert one("SELECT count(*) AS n FROM webhook_events")["n"] == 0


def test_deepest_nesting_that_fits_the_size_limit_never_causes_a_server_error(client):
    make_source()
    depth = 35000  # about 245 KB, just under the limit
    body = b'{"a":' * depth + b"1" + b"}" * depth
    assert len(body) <= ingest.MAX_BODY_BYTES
    resp = _raw_post(client, body, key="deep")
    assert resp.status_code in (202, 422)  # stored or cleanly rejected, never a 500


def test_chunked_oversize_body_is_cut_off_with_413(client):
    make_source()

    def endless():
        for _ in range(200):  # 200 x 4 KB, no Content-Length header
            yield b"x" * 4096

    resp = client.post("/ingest/shop", content=endless(), headers={"X-Idempotency-Key": "k"})
    assert resp.status_code == 413


def test_missing_signature_header_is_401(client):
    make_source()
    resp = client.post("/ingest/shop", content=b'{"n": 1}', headers={"X-Idempotency-Key": "k"})
    assert resp.status_code == 401


def test_missing_idempotency_key_is_400(client):
    make_source()
    assert _post(client, key=None).status_code == 400


def test_non_object_payload_is_422(client):
    make_source()
    assert _raw_post(client, b"[1, 2, 3]").status_code == 422


def test_error_responses_never_leak_internals(client):
    make_source()
    stale = _post(client, timestamp=int(time.time()) - 3600)
    responses = (_post(client, secret="the-wrong-secret-0123"), _post(client, source="nope"), _post(client, key=None), stale)
    for resp in responses:
        assert SECRET not in resp.text and "Traceback" not in resp.text


def test_missing_timestamp_header_is_401_like_any_bad_signature(client):
    make_source()
    body = b'{"n": 1}'
    headers = signed_headers(body)
    del headers["X-Hub-Timestamp"]
    resp = client.post("/ingest/shop", content=body, headers=headers)
    assert (resp.status_code, resp.json()) == (401, {"detail": "invalid signature"})


def test_stale_but_correctly_signed_request_is_401_with_a_clear_reason(client):
    make_source()
    for offset in (-3600, 3600):
        resp = _post(client, timestamp=int(time.time()) + offset)
        assert resp.status_code == 401
        assert resp.json() == {"detail": "timestamp outside the allowed window, check the sender's clock"}
    assert one("SELECT count(*) AS n FROM webhook_events")["n"] == 0


def test_stale_with_a_bad_signature_gets_the_generic_401(client):
    make_source()
    resp = _post(client, secret="the-wrong-secret-0123", timestamp=int(time.time()) - 3600)
    assert resp.json() == {"detail": "invalid signature"}


def test_resending_a_captured_request_under_a_new_key_is_401(client):
    make_source()
    body = json.dumps({"n": 1}).encode()
    sent = signed_headers(body, "evt-1")
    assert client.post("/ingest/shop", content=body, headers=sent).status_code == 202
    replay = {**sent, "X-Idempotency-Key": "evt-2"}
    assert client.post("/ingest/shop", content=body, headers=replay).status_code == 401
    assert one("SELECT count(*) AS n FROM jobs")["n"] == 1
