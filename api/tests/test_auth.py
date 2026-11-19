"""Login, lockout, throttling and sessions at the engine level (no HTTP)."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import auth, config

from helpers import PASSWORD, make_user, one, rows

IP = "10.0.0.1"


def _fail(name="diego", ip=IP, times=1):
    for _ in range(times):
        with pytest.raises(auth.InvalidCredentials):
            auth.login(name, "wrong password!!", ip)


def _failures(name="diego", ip=IP):
    return one(
        "SELECT count(*) AS n FROM login_attempts WHERE username = %s AND ip = %s AND NOT succeeded", (name, ip)
    )["n"]


def _age_attempts(seconds):
    from app import db

    with db.connection() as conn:
        conn.execute("UPDATE login_attempts SET attempted_at = attempted_at - make_interval(secs => %s)", (seconds,))


def test_login_returns_a_working_token_and_stores_only_its_hash(hub):
    make_user("diego", "admin")
    session = auth.login("diego", PASSWORD, IP)

    assert session.principal.username == "diego" and session.principal.role == "admin"
    assert auth.authenticate(session.token) == session.principal
    stored = rows("SELECT token_hash FROM sessions")
    assert len(stored) == 1 and session.token not in stored[0]["token_hash"]


def test_two_logins_get_different_tokens(hub):
    make_user()
    assert auth.login("diego", PASSWORD, IP).token != auth.login("diego", PASSWORD, IP).token


def test_username_is_case_insensitive_at_login(hub):
    make_user("diego")
    assert auth.login("  DiEgO ", PASSWORD, IP).principal.username == "diego"


def test_wrong_password_and_unknown_user_fail_the_same_way_and_are_recorded(hub):
    make_user()
    _fail("diego")
    _fail("nobody")
    assert _failures("diego") == 1 and _failures("nobody") == 1
    assert one("SELECT count(*) AS n FROM sessions")["n"] == 0


def test_successful_login_is_audited_without_secrets(hub):
    make_user()
    auth.login("diego", PASSWORD, IP)
    entry = rows("SELECT actor, action, after FROM audit_log WHERE action = 'auth.login'")[0]
    assert entry["actor"] == "user:diego" and entry["after"] == {"ip": IP}


def test_lockout_after_max_failures_even_with_the_right_password(hub):
    make_user()
    _fail(times=config.LOGIN_MAX_FAILURES)
    with pytest.raises(auth.LoginLocked) as locked:
        auth.login("diego", PASSWORD, IP)
    assert 1 <= locked.value.retry_after <= config.LOGIN_WINDOW_SECONDS


def test_attempts_made_while_locked_are_not_recorded(hub):
    make_user()
    _fail(times=config.LOGIN_MAX_FAILURES)
    for _ in range(3):
        with pytest.raises(auth.LoginLocked):
            auth.login("diego", "anything at all", IP)
    assert _failures() == config.LOGIN_MAX_FAILURES


def test_retry_after_counts_down_from_the_oldest_counted_failure(hub):
    make_user()
    _fail(times=config.LOGIN_MAX_FAILURES)
    _age_attempts(config.LOGIN_WINDOW_SECONDS - 100)
    with pytest.raises(auth.LoginLocked) as locked:
        auth.login("diego", PASSWORD, IP)
    assert 95 <= locked.value.retry_after <= 101


def test_lockout_only_applies_to_that_ip(hub):
    make_user()
    _fail(times=config.LOGIN_MAX_FAILURES, ip=IP)
    assert auth.login("diego", PASSWORD, "10.0.0.99").principal.username == "diego"


def test_lockout_only_applies_to_that_username(hub):
    make_user("diego")
    make_user("maria")
    _fail("diego", times=config.LOGIN_MAX_FAILURES)
    assert auth.login("maria", PASSWORD, IP).principal.username == "maria"


def test_lockout_ends_when_the_window_passes(hub):
    make_user()
    _fail(times=config.LOGIN_MAX_FAILURES)
    _age_attempts(config.LOGIN_WINDOW_SECONDS + 1)
    assert auth.login("diego", PASSWORD, IP).principal.username == "diego"


def test_a_success_resets_the_failure_count(hub):
    make_user()
    _fail(times=config.LOGIN_MAX_FAILURES - 1)
    auth.login("diego", PASSWORD, IP)
    _fail(times=config.LOGIN_MAX_FAILURES - 1)
    assert auth.login("diego", PASSWORD, IP).principal.username == "diego"


def test_unknown_usernames_lock_like_real_ones(hub):
    # If only real accounts locked, the 429 would reveal which usernames exist.
    _fail("ghost", times=config.LOGIN_MAX_FAILURES)
    with pytest.raises(auth.LoginLocked):
        auth.login("ghost", "whatever password", IP)


def test_one_ip_spraying_many_usernames_gets_throttled(hub, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_IP_MAX_FAILURES", 8)
    make_user("diego")
    for n in range(8):
        _fail(f"guess{n}")
    with pytest.raises(auth.LoginLocked):
        auth.login("diego", PASSWORD, IP)
    assert auth.login("diego", PASSWORD, "10.0.0.99").principal.username == "diego"


def test_parallel_guessing_cannot_get_past_the_limit(hub):
    make_user()

    def guess(_):
        try:
            auth.login("diego", "wrong password!!", IP)
        except (auth.InvalidCredentials, auth.LoginLocked) as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(guess, range(40)))

    assert outcomes.count("InvalidCredentials") <= config.LOGIN_MAX_FAILURES
    assert _failures() <= config.LOGIN_MAX_FAILURES
    assert set(outcomes) <= {"InvalidCredentials", "LoginLocked"}


@pytest.mark.parametrize("token", [None, "", "nonsense", "x" * 500])
def test_unknown_tokens_are_not_authenticated(hub, token):
    assert auth.authenticate(token) is None


def test_expired_session_is_not_authenticated(hub):
    from app import db

    make_user()
    session = auth.login("diego", PASSWORD, IP)
    with db.connection() as conn:
        conn.execute("UPDATE sessions SET expires_at = now() - interval '1 second'")
    assert auth.authenticate(session.token) is None


def test_logout_ends_only_that_session(hub):
    make_user()
    first, second = auth.login("diego", PASSWORD, IP), auth.login("diego", PASSWORD, IP)
    auth.logout(first.token)
    assert auth.authenticate(first.token) is None
    assert auth.authenticate(second.token) is not None


def test_login_cleans_up_that_users_expired_sessions(hub):
    from app import db

    make_user()
    old = auth.login("diego", PASSWORD, IP)
    with db.connection() as conn:
        conn.execute("UPDATE sessions SET expires_at = now() - interval '1 day'")
    auth.login("diego", PASSWORD, IP)
    assert one("SELECT count(*) AS n FROM sessions")["n"] == 1
    assert auth.authenticate(old.token) is None


def test_deleting_a_user_ends_their_sessions(hub):
    from app import db

    make_user()
    session = auth.login("diego", PASSWORD, IP)
    with db.connection() as conn:
        conn.execute("DELETE FROM users WHERE username = 'diego'")
    assert auth.authenticate(session.token) is None
