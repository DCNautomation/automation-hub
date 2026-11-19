"""Every API response carries the hardening headers, including errors and rejected requests."""
import pytest
from fastapi.testclient import TestClient

from app.main import app



@pytest.fixture
def client(hub):
    with TestClient(app) as c:
        yield c


def _hardened(response):
    h = response.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    assert h["cache-control"] == "no-store"
    assert h["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"


def test_health_and_json_responses_are_hardened(client):
    _hardened(client.get("/healthz"))


def test_error_responses_are_hardened_too(client):
    _hardened(client.get("/runs"))  # 401, no token
    _hardened(client.get("/no-such-route"))  # 404
    _hardened(client.post("/ingest/ghost", content=b"{}"))  # rejected webhook


def test_login_responses_are_never_cacheable(client):
    response = client.post("/auth/login", json={"username": "nobody", "password": "x" * 12})
    assert response.status_code == 401
    _hardened(response)


def test_the_docs_page_keeps_working_so_it_gets_no_script_blocking_policy(client):
    page = client.get("/docs")
    assert page.status_code == 200
    assert "content-security-policy" not in page.headers
    assert page.headers["x-content-type-options"] == "nosniff"
