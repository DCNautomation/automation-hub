"""The production preflight blocks unsafe or default settings and says why."""
import pytest

from app import config, preflight

from helpers import make_source

GOOD = {
    "owner_url": "postgresql://hub:Zx9kQ2mV7nR4tW8yB3cF6hJ1@db.example.com:5432/hub",
    "app_url": "postgresql://hub_app:Lp5dS8gH2jK9mN4qR7tV1xZ3@db.example.com:5432/hub",
    "app_password": "Lp5dS8gH2jK9mN4qR7tV1xZ3",
    "cors_origins": ["https://hub.example.com"],
    "dashboard_url": "https://hub.example.com",
    "proxy_hops": 1,
    "telegram_token": "123:abc",
    "ingest_limit": 300,
    "read_limit": 300,
}


def test_good_production_settings_have_no_findings():
    assert preflight.check(GOOD) == []


@pytest.mark.parametrize("change, phrase", [
    ({"owner_url": "postgresql://hub:change-me@db.example.com/hub"}, "owner password"),
    ({"owner_url": "postgresql://hub:short@db.example.com/hub"}, "owner password"),
    ({"owner_url": "postgresql://hub@db.example.com/hub"}, "owner password"),
    ({"app_password": "change-me-app-password"}, "APP_DB_PASSWORD"),
    ({"app_password": "tooshort"}, "APP_DB_PASSWORD"),
    ({"app_url": "postgresql://hub:Zx9kQ2mV7nR4tW8yB3cF6hJ1@db.example.com/hub"}, "limited 'hub_app' role"),
    ({"proxy_hops": 0}, "TRUSTED_PROXY_HOPS"),
    ({"cors_origins": []}, "CORS_ORIGINS"),
    ({"cors_origins": ["http://localhost:5173"]}, "CORS_ORIGINS"),
    ({"cors_origins": ["https://hub.example.com", "http://127.0.0.1:8080"]}, "CORS_ORIGINS"),
    ({"cors_origins": ["http://hub.example.com"]}, "CORS_ORIGINS"),
    ({"cors_origins": ["https://localhost"]}, "CORS_ORIGINS"),
    ({"dashboard_url": "http://127.0.0.1:5173"}, "DASHBOARD_URL"),
    ({"dashboard_url": "http://hub.example.com"}, "DASHBOARD_URL"),
])
def test_each_unsafe_setting_is_a_failure_that_names_it(change, phrase):
    findings = preflight.check({**GOOD, **change})
    assert [m for level, m in findings if level == "FAIL" and phrase in m], findings


@pytest.mark.parametrize("change, phrase", [
    ({"telegram_token": ""}, "alerts are off"),
    ({"ingest_limit": 0}, "rate limit"),
    ({"read_limit": 0}, "rate limit"),
])
def test_things_worth_knowing_warn_without_blocking(change, phrase):
    findings = preflight.check({**GOOD, **change})
    assert [level for level, _ in findings] == ["WARN"] and phrase in findings[0][1]


def test_the_local_dev_defaults_all_fail():
    dev = preflight.check({
        "owner_url": "postgresql://hub:change-me@localhost:5432/hub",
        "app_url": "postgresql://hub:change-me@localhost:5432/hub",
        "app_password": "change-me-app-password",
        "cors_origins": ["http://localhost:5173"], "dashboard_url": "http://127.0.0.1:5173",
        "proxy_hops": 0, "telegram_token": "", "ingest_limit": 300, "read_limit": 300,
    })
    assert sum(1 for level, _ in dev if level == "FAIL") == 6


def test_short_signing_secrets_fail_and_good_ones_pass(hub, monkeypatch, test_database_url):
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    make_source("weak", secret="sixteen-char-key!")
    make_source("strong", secret="x" * 43)
    findings = preflight.check_database()
    assert [(level, "weak" in m and "strong" not in m) for level, m in findings] == [("FAIL", True)]


def test_demo_data_is_a_warning_not_a_failure(hub, monkeypatch, test_database_url):
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    make_source("demo-shop", handler="demo", secret="x" * 43)
    findings = preflight.check_database()
    assert [level for level, _ in findings] == ["WARN"] and "demo-shop" in findings[0][1]


def test_an_unreachable_database_warns_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=1")
    findings = preflight.check_database()
    assert findings and findings[0][0] == "WARN" and "could not check" in findings[0][1]


def test_outside_production_the_check_is_skipped(monkeypatch, capsys):
    monkeypatch.delenv("HUB_ENV", raising=False)
    preflight.main([])
    assert "skipped" in capsys.readouterr().out


def test_in_production_bad_settings_stop_the_start(monkeypatch, capsys, hub, test_database_url):
    monkeypatch.setenv("HUB_ENV", "production")
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    with pytest.raises(SystemExit, match="refusing to start"):
        preflight.main([])
    assert "preflight FAIL" in capsys.readouterr().out


def test_force_runs_the_checks_outside_production(monkeypatch, hub, test_database_url):
    monkeypatch.delenv("HUB_ENV", raising=False)
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    with pytest.raises(SystemExit):
        preflight.main(["--force"])


def test_good_settings_in_production_start_cleanly(monkeypatch, capsys, hub, test_database_url):
    monkeypatch.setenv("HUB_ENV", "production")
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)  # data checks run against the test database
    monkeypatch.setattr(preflight, "settings_from_env", lambda: GOOD)
    preflight.main([])
    assert "preflight: ok" in capsys.readouterr().out
