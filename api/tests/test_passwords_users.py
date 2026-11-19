"""Password hashing (pure) and creating users."""
import pytest

from app import passwords, users

from helpers import PASSWORD, one, rows


def test_hash_then_verify_roundtrip():
    stored = passwords.hash_password(PASSWORD)
    assert stored.startswith("scrypt$")
    assert passwords.verify_password(PASSWORD, stored)


def test_wrong_password_and_near_misses_fail():
    stored = passwords.hash_password(PASSWORD)
    for attempt in ("", PASSWORD + " ", PASSWORD.upper(), "correct horse batter"):
        assert not passwords.verify_password(attempt, stored)


def test_same_password_hashes_differently_each_time():
    assert passwords.hash_password(PASSWORD) != passwords.hash_password(PASSWORD)


def test_unicode_passwords_work():
    stored = passwords.hash_password("pässwörd-日本語-0123")
    assert passwords.verify_password("pässwörd-日本語-0123", stored)
    assert not passwords.verify_password("passwörd-日本語-0123", stored)


@pytest.mark.parametrize(
    "stored",
    ["", "garbage", "scrypt$", "scrypt$16$8$1$bad$bad", "md5$16$8$1$AAAA$AAAA", "scrypt$x$8$1$AAAA$AAAA", None],
)
def test_malformed_stored_hash_is_just_false(stored):
    assert not passwords.verify_password(PASSWORD, stored)


def test_params_travel_with_the_hash_so_cost_can_be_raised_later(monkeypatch):
    old = passwords.hash_password(PASSWORD)
    monkeypatch.setattr(passwords, "SCRYPT_N", 2**5)
    assert passwords.verify_password(PASSWORD, old)
    assert passwords.hash_password(PASSWORD).split("$")[1] == str(2**5)


def test_production_cost_is_not_weakened():
    # conftest lowers N for speed. This reads the real default from source so it can't drift unnoticed.
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module("app.passwords"))
    assert "SCRYPT_N = 2**15" in source


def test_create_user_stores_a_hash_never_the_password(hub):
    user_id = users.create_user("Diego", PASSWORD, "admin")
    row = one("SELECT * FROM users WHERE id = %s", (user_id,))
    assert row["username"] == "diego" and row["role"] == "admin"
    assert PASSWORD not in row["password_hash"]
    assert passwords.verify_password(PASSWORD, row["password_hash"])
    audit = rows("SELECT actor, action, after FROM audit_log WHERE entity_type = 'user'")
    assert [(a["actor"], a["action"]) for a in audit] == [("cli", "user.created")]
    assert PASSWORD not in str(audit[0]["after"])


def test_usernames_are_case_insensitive_so_duplicates_are_refused(hub):
    users.create_user("diego", PASSWORD)
    with pytest.raises(users.UserExists):
        users.create_user("DIEGO", PASSWORD)


@pytest.mark.parametrize("name", ["", "ab", "has space", "x" * 41, "-leading", "emoji\U0001F600", "semi;colon"])
def test_bad_usernames_are_refused(hub, name):
    with pytest.raises(ValueError):
        users.create_user(name, PASSWORD)


@pytest.mark.parametrize("password", ["", "short", "x" * 11, "x" * 201, None])
def test_bad_passwords_are_refused(hub, password):
    with pytest.raises(ValueError):
        users.create_user("diego", password)


def test_unknown_role_is_refused(hub):
    with pytest.raises(ValueError):
        users.create_user("diego", PASSWORD, "superuser")
    assert one("SELECT count(*) AS n FROM users")["n"] == 0


def test_cli_creates_a_user_from_the_environment(hub, monkeypatch, test_database_url):
    from app import config, db

    monkeypatch.setenv("HUB_NEW_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    users.main(["create", "cli-user", "--role", "viewer"])
    db.init_pool(test_database_url)  # main() closes the shared pool when it finishes
    assert one("SELECT role FROM users WHERE username = 'cli-user'")["role"] == "viewer"


def test_cli_exits_with_a_message_for_a_duplicate(hub, monkeypatch, test_database_url):
    from app import config, db

    users.create_user("cli-user", PASSWORD)
    monkeypatch.setenv("HUB_NEW_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    with pytest.raises(SystemExit) as exc:
        users.main(["create", "cli-user"])
    assert "Not created" in str(exc.value)
    db.init_pool(test_database_url)
