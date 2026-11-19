"""Alert rules, the outbox, and what gets sent when."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import alerts, config, db, worker

from conftest import TOKEN
from helpers import make_job, make_due, make_source, one, rows


def _deliveries(status=None):
    sql = "SELECT * FROM alert_deliveries" + (" WHERE status = %s" if status else "") + " ORDER BY id"
    return rows(sql, (status,) if status else ())


def _make_due():
    with db.connection() as conn:
        conn.execute("UPDATE alert_deliveries SET next_try_at = now() WHERE status = 'pending'")


def _dead(source="shop", attempts=3, error="RuntimeError: card declined"):
    return make_job("dead", attempts=attempts, max_attempts=attempts, last_error=error, source=source)


def _failed_try(job_id, attempt_no, ok=False, minutes_ago=0):
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO job_attempts (job_id, attempt_no, worker_id, ok, finished_at) "
            "VALUES (%s, %s, 'w', %s, now() - make_interval(mins => %s))",
            (job_id, attempt_no, ok, minutes_ago),
        )


# ----- rules --------------------------------------------------------------------------------

def test_create_and_list_rules(hub):
    dead = alerts.create_rule("job_dead", chat="12345")
    spike = alerts.create_rule("failure_spike", chat="-100987", threshold=5, window_minutes=10)
    listed = alerts.list_rules()
    assert [(r["id"], r["kind"], r["target"], r["active"]) for r in listed] == [
        (dead, "job_dead", "12345", True), (spike, "failure_spike", "-100987", True)]
    assert (listed[1]["threshold"], listed[1]["window_minutes"]) == (5, 10)
    assert [a["action"] for a in rows("SELECT action FROM audit_log WHERE entity_type = 'alert_rule'")] == ["alert_rule.created"] * 2


def test_chat_defaults_to_the_environment_setting(hub, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "777")
    alerts.create_rule("job_dead")
    assert alerts.list_rules()[0]["target"] == "777"


@pytest.mark.parametrize("chat", [None, "", "abc", "12 34", "@x", "1;DROP", "https://t.me/x"])
def test_a_rule_needs_a_real_chat_id(hub, chat):
    with pytest.raises(ValueError):
        alerts.create_rule("job_dead", chat=chat)


@pytest.mark.parametrize("kwargs", [{}, {"threshold": 5}, {"window_minutes": 5}, {"threshold": 0, "window_minutes": 5}])
def test_a_spike_rule_needs_both_numbers(hub, kwargs):
    with pytest.raises(ValueError):
        alerts.create_rule("failure_spike", chat="1", **kwargs)


def test_a_dead_rule_takes_no_numbers_and_unknown_kinds_are_refused(hub):
    with pytest.raises(ValueError):
        alerts.create_rule("job_dead", chat="1", threshold=3)
    with pytest.raises(ValueError):
        alerts.create_rule("nope", chat="1")


def test_the_database_itself_refuses_a_spike_rule_without_numbers(hub):
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as conn:
            conn.execute("INSERT INTO alert_rules (kind, channel, target) VALUES ('failure_spike', 'telegram', '1')")


def test_rules_can_be_switched_off_and_on(hub):
    rule = alerts.create_rule("job_dead", chat="1")
    alerts.set_active(rule, False)
    assert alerts.list_rules()[0]["active"] is False
    alerts.set_active(rule, True)
    assert alerts.list_rules()[0]["active"] is True
    with pytest.raises(LookupError):
        alerts.set_active(999, True)


def test_command_line_adds_lists_and_disables(hub, test_database_url, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    for argv in (["rule", "add", "dead", "--chat", "42"], ["rule", "add", "spike", "--chat", "42", "--threshold", "4", "--window", "15"],
                 ["rule", "list"], ["rule", "disable", "1"], ["rule", "list"]):
        alerts.main(argv)
        db.init_pool(test_database_url)  # main() closes the shared pool when it finishes
    out = capsys.readouterr().out
    assert "Created alert rule 1" in out and ">= 4 failed tries in 15 min" in out and "Rule 1 disabled" in out
    assert out.rstrip().endswith("on") and "1  job_dead  chat 42  off" in out


def test_command_line_reports_bad_input_as_a_message(hub, test_database_url, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    with pytest.raises(SystemExit) as exit_info:
        alerts.main(["rule", "add", "spike", "--chat", "42"])
    assert "Failed" in str(exit_info.value)
    db.init_pool(test_database_url)


def test_test_command_sends_a_message_and_complains_without_a_token(hub, test_database_url, monkeypatch, telegram_up, capsys):
    monkeypatch.setattr(config, "DATABASE_URL", test_database_url)
    alerts.main(["test", "--chat", "42"])
    db.init_pool(test_database_url)
    assert "Test message sent" in capsys.readouterr().out and telegram_up.requests[0]["body"]["chat_id"] == "42"
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(SystemExit) as exit_info:
        alerts.main(["test", "--chat", "42"])
    assert "TELEGRAM_BOT_TOKEN" in str(exit_info.value)
    db.init_pool(test_database_url)


# ----- dead runs ----------------------------------------------------------------------------

def test_a_run_that_gives_up_sends_one_message_with_what_you_need(hub, telegram_up):
    alerts.create_rule("job_dead", chat="12345")
    job = _dead("billing", attempts=3, error="RuntimeError: card declined")

    assert alerts.tick() == {"queued": 1, "sent": 1, "failed": 0}
    assert len(telegram_up.requests) == 1
    assert telegram_up.requests[0]["body"]["chat_id"] == "12345"
    text = telegram_up.texts[0]
    assert text.splitlines() == [
        f"Run {job} gave up", "Source: billing", "Tries: 3 of 3", "Last error: RuntimeError: card declined", f"http://dash.test/runs/{job}"]
    assert _deliveries()[0]["status"] == "sent" and _deliveries()[0]["sent_at"] is not None
    assert one("SELECT count(*) AS n FROM audit_log WHERE action = 'alert.sent'")["n"] == 1


def test_the_same_death_is_never_alerted_twice(hub, telegram_up):
    alerts.create_rule("job_dead", chat="1")
    _dead()
    alerts.tick()
    assert alerts.tick() == {"queued": 0, "sent": 0, "failed": 0}
    assert len(telegram_up.requests) == 1


def test_runs_that_died_before_the_rule_existed_stay_quiet(hub, telegram_up):
    _dead()
    alerts.create_rule("job_dead", chat="1")
    alerts.tick()
    assert telegram_up.requests == [] and _deliveries() == []


def test_a_run_retried_by_hand_that_dies_again_alerts_again(hub, telegram_up):
    alerts.create_rule("job_dead", chat="1")
    job = _dead(attempts=3)
    alerts.tick()
    with db.connection() as conn:  # what a manual retry plus a second death looks like
        conn.execute("UPDATE jobs SET attempts = 6, max_attempts = 6, finished_at = now() WHERE id = %s", (job,))
    alerts.tick()
    alerts.tick()
    assert len(telegram_up.requests) == 2 and "Tries: 6 of 6" in telegram_up.texts[1]


def test_a_burst_becomes_a_few_messages_plus_one_summary(hub, telegram_up, monkeypatch):
    monkeypatch.setattr(config, "ALERT_MAX_PER_TICK", 3)
    alerts.create_rule("job_dead", chat="1")
    ids = [_dead() for _ in range(8)]
    assert alerts.tick()["sent"] == 4
    assert len(telegram_up.requests) == 4
    assert "5 more runs gave up" in telegram_up.texts[-1] and f"runs {ids[3]} to {ids[-1]}" in telegram_up.texts[-1]
    assert len(_deliveries("suppressed")) == 5
    assert alerts.tick() == {"queued": 0, "sent": 0, "failed": 0}  # nothing re-alerted afterwards


def test_a_switched_off_rule_sends_nothing(hub, telegram_up):
    rule = alerts.create_rule("job_dead", chat="1")
    alerts.set_active(rule, False)
    _dead()
    alerts.tick()
    assert telegram_up.requests == [] and _deliveries() == []


def test_nothing_happens_until_a_bot_token_is_set(hub, telegram_up, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    alerts.create_rule("job_dead", chat="1")
    _dead()
    assert alerts.tick() is None
    assert telegram_up.requests == [] and _deliveries() == []


def test_each_rule_goes_to_its_own_chat(hub, telegram_up):
    alerts.create_rule("job_dead", chat="111")
    alerts.create_rule("job_dead", chat="222")
    _dead()
    alerts.tick()
    assert sorted(r["body"]["chat_id"] for r in telegram_up.requests) == ["111", "222"]


def test_error_text_is_collapsed_to_one_short_line(hub, telegram_up):
    alerts.create_rule("job_dead", chat="1")
    _dead(error="Traceback:\n  line 1\n\n  line 2 " + "x" * 1000)
    alerts.tick()
    line = [l for l in telegram_up.texts[0].splitlines() if l.startswith("Last error:")][0]
    assert line.startswith("Last error: Traceback: line 1 line 2 x") and len(line) <= len("Last error: ") + alerts.ERROR_CHARS
    assert len(telegram_up.texts[0].splitlines()) == 5


def test_messages_contain_no_secrets_and_no_em_dashes(hub, telegram_up):
    make_source("shop")
    alerts.create_rule("job_dead", chat="1")
    _dead()
    alerts.tick()
    assert "test-secret" not in telegram_up.texts[0] and TOKEN not in telegram_up.texts[0] and "—" not in telegram_up.texts[0]


# ----- delivery, retries, concurrency ---------------------------------------------------------

def test_an_outage_delays_the_alert_instead_of_losing_it(hub, telegram_up):
    alerts.create_rule("job_dead", chat="1")
    _dead()
    telegram_up.script = [(500, {"ok": False, "description": "oops"})]
    assert alerts.tick() == {"queued": 1, "sent": 0, "failed": 1}

    pending = _deliveries("pending")[0]
    assert pending["attempts"] == 1 and "500" in pending["last_error"] and TOKEN not in pending["last_error"]
    assert one("SELECT next_try_at > now() AS later FROM alert_deliveries")["later"]
    assert alerts.tick()["sent"] == 0 and len(telegram_up.requests) == 1  # backing off, not hammering

    _make_due()
    assert alerts.tick()["sent"] == 1
    assert _deliveries()[0]["status"] == "sent" and len(telegram_up.requests) == 2


def test_it_gives_up_after_the_configured_number_of_tries(hub, telegram_up, monkeypatch):
    monkeypatch.setattr(config, "ALERT_MAX_ATTEMPTS", 2)
    alerts.create_rule("job_dead", chat="1")
    _dead()
    telegram_up.script = [(500, {"ok": False}), (500, {"ok": False})]
    alerts.tick()
    _make_due()
    alerts.tick()
    _make_due()
    alerts.tick()
    assert _deliveries()[0]["status"] == "failed" and _deliveries()[0]["attempts"] == 2
    assert len(telegram_up.requests) == 2
    assert one("SELECT count(*) AS n FROM audit_log WHERE action = 'alert.failed'")["n"] == 1


def test_a_rate_limit_answer_is_respected(hub, telegram_up):
    alerts.create_rule("job_dead", chat="1")
    _dead()
    telegram_up.script = [(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 600}})]
    alerts.tick()
    assert one("SELECT extract(epoch FROM next_try_at - now()) AS s FROM alert_deliveries")["s"] > 590


def test_a_rule_switched_off_while_a_message_waits_suppresses_it(hub, telegram_up):
    rule = alerts.create_rule("job_dead", chat="1")
    _dead()
    alerts.evaluate()
    alerts.set_active(rule, False)
    assert alerts.deliver() == (0, 0)
    assert _deliveries()[0]["status"] == "suppressed" and telegram_up.requests == []


def test_two_workers_delivering_at_once_send_a_message_exactly_once(hub, telegram_up):
    alerts.create_rule("job_dead", chat="1")
    _dead()
    alerts.evaluate()
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: alerts.deliver(), range(6)))
    assert len(telegram_up.requests) == 1


def test_two_workers_evaluating_at_once_queue_it_exactly_once(hub, telegram_up):
    alerts.create_rule("job_dead", chat="1")
    _dead()
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: alerts.evaluate(), range(6)))
    assert len(_deliveries()) == 1


def test_two_workers_evaluating_a_spike_at_once_alert_once(hub, telegram_up):
    alerts.create_rule("failure_spike", chat="1", threshold=2, window_minutes=10)
    job = make_job("failed")
    _failed_try(job, 1)
    _failed_try(job, 2)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: alerts.evaluate(), range(8)))
    assert len(_deliveries()) == 1


# ----- failure spikes -------------------------------------------------------------------------

def test_a_spike_alerts_once_when_the_threshold_is_reached(hub, telegram_up):
    alerts.create_rule("failure_spike", chat="1", threshold=3, window_minutes=10)
    a, b = make_job("failed", source="billing"), make_job("failed", source="crm")
    _failed_try(a, 1)
    _failed_try(a, 2)
    alerts.tick()
    assert telegram_up.requests == []  # 2 < 3

    _failed_try(b, 1)
    alerts.tick()
    assert telegram_up.texts == ["3 failed tries in the last 10 minutes\nMost from: billing (2), crm (1)\nhttp://dash.test/?status=failed&status=dead"]
    alerts.tick()
    assert len(telegram_up.requests) == 1  # cooling down


def test_a_spike_can_alert_again_after_the_window_passes(hub, telegram_up):
    alerts.create_rule("failure_spike", chat="1", threshold=2, window_minutes=10)
    job = make_job("failed")
    _failed_try(job, 1)
    _failed_try(job, 2)
    alerts.tick()
    with db.connection() as conn:
        conn.execute("UPDATE alert_deliveries SET created_at = now() - interval '11 minutes'")
    alerts.tick()
    assert len(telegram_up.requests) == 2


def test_old_failures_and_successful_tries_do_not_count(hub, telegram_up):
    alerts.create_rule("failure_spike", chat="1", threshold=2, window_minutes=10)
    job = make_job("failed")
    _failed_try(job, 1, minutes_ago=30)
    _failed_try(job, 2, ok=True)
    _failed_try(job, 3)
    alerts.tick()
    assert telegram_up.requests == []


# ----- the worker runs it ---------------------------------------------------------------------

@pytest.fixture
def quiet_worker(monkeypatch):
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(worker, "_stopping", False)
    return monkeypatch


def test_the_worker_loop_runs_the_alert_pass_and_survives_it_failing(hub, quiet_worker):
    calls = []

    def exploding_tick():
        calls.append(1)
        worker._request_stop(None, None)
        raise RuntimeError("telegram exploded")

    quiet_worker.setattr(alerts, "tick", exploding_tick)
    worker.run_forever()  # must not raise
    assert calls == [1]


def test_the_worker_sends_a_real_alert_for_a_run_that_dies(hub, quiet_worker, telegram_up):
    from app import jobs
    from helpers import deliver, reopen_pool

    make_source("shop", handler="nope", max_attempts=1)
    alerts.create_rule("job_dead", chat="1")
    job = deliver("shop", key="doomed").job_id
    real_run_one = jobs.run_one
    state = {"idle": 0}

    def run_then_stop(worker_id):
        worked = real_run_one(worker_id)
        if not worked:
            state["idle"] += 1
            if state["idle"] >= 2:  # one idle pass after the death lets the alert pass run again
                worker._request_stop(None, None)
        return worked

    quiet_worker.setattr(jobs, "run_one", run_then_stop)
    quiet_worker.setattr(config, "ALERT_INTERVAL_SECONDS", 0)
    worker.run_forever()
    reopen_pool()
    assert any(f"Run {job} gave up" in t for t in telegram_up.texts)
    assert "unknown handler: nope" in telegram_up.texts[0]
