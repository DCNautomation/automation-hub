"""POST /auth/login, /auth/me, /auth/logout over HTTP."""
import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app

from helpers import PASSWORD, login_headers, make_user, one


@pytest.fixture
def client(hub):
    with TestClient(app) as c:
        yield c


def _login(client, username="diego", password=PASSWORD):
    return client.post("/auth/login", json={"username": username, "password": password})


def test_login_returns_token_user_and_expiry_but_nothing_secret(client):
    make_user("diego", "admin")
    resp = _login(client)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"token", "expires_at", "user"}
    assert body["user"] == {"username": "diego", "role": "admin"}
    assert "scrypt" not in resp.text and PASSWORD not in resp.text


def test_token_opens_me_and_logout_closes_it(client):
    make_user("diego", "viewer")
    headers = {"Authorization": "Bearer " + _login(client).json()["token"]}
    assert client.get("/auth/me", headers=headers).json() == {"username": "diego", "role": "viewer"}
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_wrong_password_and_unknown_user_get_the_identical_401(client):
    make_user()
    wrong = _login(client, password="not the password")
    unknown = _login(client, username="nobody")
    assert (wrong.status_code, wrong.json()) == (unknown.status_code, unknown.json())
    assert wrong.status_code == 401 and wrong.json() == {"detail": "invalid username or password"}


def test_lockout_returns_429_with_retry_after_and_hides_which_limit_hit(client):
    make_user()
    for _ in range(config.LOGIN_MAX_FAILURES):
        assert _login(client, password="nope nope nope").status_code == 401
    locked = _login(client)  # correct password, still refused
    assert locked.status_code == 429
    assert 1 <= int(locked.headers["Retry-After"]) <= config.LOGIN_WINDOW_SECONDS
    assert locked.json() == {"detail": "too many failed attempts, try again later"}


def test_lockout_is_per_ip_using_the_forwarded_header_only_when_trusted(client, monkeypatch):
    make_user()
    for _ in range(config.LOGIN_MAX_FAILURES):
        _login(client, password="nope nope nope")
    # Not trusting proxies: a spoofed X-Forwarded-For must NOT dodge the lockout.
    spoof = client.post(
        "/auth/login",
        json={"username": "diego", "password": PASSWORD},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert spoof.status_code == 429
    # Trusting one proxy: the rightmost entry is the address that proxy saw, and it is a new address.
    monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 1)
    real = client.post(
        "/auth/login",
        json={"username": "diego", "password": PASSWORD},
        headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.9"},
    )
    assert real.status_code == 200
    assert one("SELECT count(*) AS n FROM login_attempts WHERE ip = '203.0.113.9' AND succeeded")["n"] == 1


def test_a_forged_left_hand_forwarded_entry_changes_nothing(client, monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 1)
    make_user()
    for forged in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        client.post(
            "/auth/login",
            json={"username": "diego", "password": "nope nope nope"},
            headers={"X-Forwarded-For": f"{forged}, 203.0.113.9"},
        )
    assert one("SELECT count(*) AS n FROM login_attempts WHERE ip = '203.0.113.9'")["n"] == 3


@pytest.mark.parametrize(
    "body",
    [{}, {"username": "diego"}, {"password": PASSWORD}, {"username": "", "password": "x"},
     {"username": "x" * 101, "password": "x"}, {"username": "diego", "password": "x" * 201}, {"username": 5, "password": 5}],
)
def test_malformed_login_bodies_are_422_and_record_nothing(client, body):
    assert client.post("/auth/login", json=body).status_code == 422
    assert one("SELECT count(*) AS n FROM login_attempts")["n"] == 0


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": ""}, {"Authorization": "Bearer"}, {"Authorization": "Bearer nonsense"},
     {"Authorization": "Basic Zm9vOmJhcg=="}, {"Authorization": "bearer"}],
)
def test_protected_endpoints_refuse_missing_or_bad_credentials(client, headers):
    resp = client.get("/auth/me", headers=headers)
    assert resp.status_code == 401 and resp.headers["WWW-Authenticate"] == "Bearer"


def test_scheme_name_is_case_insensitive(client):
    headers = login_headers()
    token = headers["Authorization"].split(" ")[1]
    assert client.get("/auth/me", headers={"Authorization": f"bearer {token}"}).status_code == 200


def test_cors_allows_the_dashboard_origin_and_no_one_else(client):
    ask = lambda origin: client.options(
        "/runs",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "Authorization"},
    )
    ok, bad = ask("http://localhost:5173"), ask("https://evil.example")
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in bad.headers
    assert "access-control-allow-credentials" not in ok.headers
