"""Creating and managing sources without ever exposing the signing secret."""
import json

import pytest
from fastapi.testclient import TestClient

from app import config, db, sources
from app.main import app

from helpers import login_headers, one, reopen_pool, rows, signed_headers


def test_create_returns_a_strong_secret_once_and_the_source_can_receive_webhooks(hub):
    source_id, secret = sources.create_source("shop-orders")
    assert len(secret) >= 40
    body = json.dumps({"order": 1}).encode()
    headers = signed_headers(body, "evt-1", secret=secret)
    with TestClient(app) as client:
        r = client.post("/ingest/shop-orders", content=body, headers=headers)
    reopen_pool()  # the app's lifespan closes the shared pool when the client exits
    assert r.status_code == 202
    assert one("SELECT handler, max_attempts, active FROM sources WHERE id = %s", (source_id,)) == {
        "handler": "log", "max_attempts": 5, "active": True}


def test_every_secret_is_different(hub):
    secrets_seen = {sources.create_source(f"s-{n}")[1] for n in range(5)}
    assert len(secrets_seen) == 5


@pytest.mark.parametrize("name", ["", "A", "a", "Shop", "has space", "-lead", "x" * 41, "emoji-☃", "a/b"])
def test_bad_names_are_refused(hub, name):
    with pytest.raises(ValueError, match="name must be"):
        sources.create_source(name)


def test_unknown_handler_and_bad_attempt_counts_are_refused(hub):
    with pytest.raises(ValueError, match="unknown handler"):
        sources.create_source("ok-name", handler="nope")
    for bad in (0, 21, -1):
        with pytest.raises(ValueError, match="between 1 and 20"):
            sources.create_source("ok-name", max_attempts=bad)
    assert rows("SELECT 1 FROM sources") == []


def test_duplicate_names_are_refused(hub):
    sources.create_source("dup")
    with pytest.raises(sources.SourceExists):
        sources.create_source("dup")


def test_rotating_invalidates_the_old_secret_immediately(hub):
    _, old = sources.create_source("rot")
    new = sources.rotate_secret("rot")
    assert new != old
    body = b'{"n": 1}'
    with TestClient(app) as client:
        assert client.post("/ingest/rot", content=body, headers=signed_headers(body, "a", secret=old)).status_code == 401
        assert client.post("/ingest/rot", content=body, headers=signed_headers(body, "b", secret=new)).status_code == 202


def test_disable_stops_deliveries_and_enable_restores_them(hub):
    _, secret = sources.create_source("toggle")
    body = b'{"n": 1}'
    with TestClient(app) as client:
        sources.set_active("toggle", False)
        assert client.post("/ingest/toggle", content=body, headers=signed_headers(body, "a", secret=secret)).status_code == 401
        sources.set_active("toggle", True)
        assert client.post("/ingest/toggle", content=body, headers=signed_headers(body, "a", secret=secret)).status_code == 202


def test_unknown_source_names_are_reported(hub):
    with pytest.raises(sources.SourceNotFound):
        sources.rotate_secret("ghost")
    with pytest.raises(sources.SourceNotFound):
        sources.set_active("ghost", False)


def test_the_secret_never_reaches_the_audit_log_the_api_or_the_list(hub):
    _, secret = sources.create_source("leak-check")
    rotated = sources.rotate_secret("leak-check")
    sources.set_active("leak-check", False)
    everything = json.dumps(rows("SELECT * FROM audit_log"), default=str)
    assert secret not in everything and rotated not in everything
    assert "signing_secret" not in json.dumps(sources.list_sources(), default=str)
    with TestClient(app) as client:
        api = client.get("/sources", headers=login_headers()).text
    reopen_pool()
    assert secret not in api and rotated not in api and "signing_secret" not in api
    actions = [r["action"] for r in rows("SELECT action FROM audit_log WHERE entity_type = 'source' ORDER BY id")]
    assert actions == ["source.created", "source.secret_rotated", "source.disabled"]


def test_cli_creates_lists_rotates_and_prints_the_secret_once(hub, monkeypatch, test_database_url, capsys):
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    sources.main(["create", "cli-src", "--max-attempts", "3"])
    created = capsys.readouterr().out
    secret = created.split("copy it now):")[1].strip()
    assert "Created source cli-src." in created and len(secret) >= 40

    sources.main(["list"])
    listing = capsys.readouterr().out
    assert "cli-src" in listing and "max_attempts=3" in listing and secret not in listing

    sources.main(["rotate", "cli-src"])
    assert capsys.readouterr().out.split("copy it now):")[1].strip() != secret
    sources.main(["disable", "cli-src"])
    sources.main(["list"])
    assert "DISABLED" in capsys.readouterr().out
    sources.main(["enable", "cli-src"])
    assert "enabled" in capsys.readouterr().out


@pytest.mark.parametrize("argv, message", [
    (["create", "Bad Name"], "name must be"),
    (["create", "dup"], "already exists: dup"),
    (["rotate", "ghost"], "no such source: ghost"),
])
def test_cli_failures_exit_with_a_plain_reason(hub, monkeypatch, test_database_url, argv, message):
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    sources.create_source("dup")
    with pytest.raises(SystemExit, match=message):
        sources.main(argv)


def test_source_management_works_with_only_the_app_roles_rights(hub, app_url):
    db.init_pool(app_url)
    sources.create_source("as-app")
    sources.rotate_secret("as-app")
    sources.set_active("as-app", False)
    assert sources.list_sources()[0]["active"] is False
