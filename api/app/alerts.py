"""Failure alerts over Telegram.

Two kinds of rule (rows in alert_rules):
  * job_dead: a run used its last try and gave up. One message per run, per death (a run that is
    retried by hand and dies again alerts again). Only runs that die AFTER the rule was created
    count, so switching alerts on never floods you with old failures.
  * failure_spike: at least `threshold` failed tries inside the last `window_minutes`. At most one
    message per window, so a bad hour is one alert and not hundreds.

Two steps, both run by the worker every few seconds (tick):
  1. evaluate(): decide what to alert about and write it to alert_deliveries (the outbox). Done under
     an advisory lock so two workers never queue the same thing, and the table's UNIQUE key makes it
     idempotent anyway.
  2. deliver(): claim pending messages (FOR UPDATE SKIP LOCKED plus a lease, like the job queue), send
     them, and either mark them sent or retry with backoff. A Telegram outage delays alerts; it
     doesn't lose them. Delivery is at-least-once: a worker that dies between sending and recording
     can cause one repeat.

Messages are plain text and never contain secrets. Error text is cut short because handler errors
are stored as-is (see the known limits in next-actions.md).

CLI: python -m app.alerts rule add dead | rule add spike --threshold 5 --window 10 | rule list |
     rule disable ID | rule enable ID | test
"""
import argparse
import logging
import re
import sys

from app import audit, config, db, jobs, telegram

log = logging.getLogger("alerts")

KINDS = {"dead": "job_dead", "spike": "failure_spike"}
LOCK_ID = 727275  # distinct from the migration lock
LEASE_SECONDS = 120  # longer than a worst-case batch of sends, so a live sender is never raced
SEND_BATCH = 10
ERROR_CHARS = 300
CHAT_RE = re.compile(r"-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31}")


# ----- rules ------------------------------------------------------------------------------------

def create_rule(kind, chat=None, threshold=None, window_minutes=None):
    if kind not in KINDS.values():
        raise ValueError(f"kind must be one of: {', '.join(KINDS.values())}")
    target = (chat or config.TELEGRAM_CHAT_ID or "").strip()
    if not CHAT_RE.fullmatch(target):
        raise ValueError("chat must be a Telegram chat id (a number) or @channelname; pass --chat or set TELEGRAM_CHAT_ID")
    if kind == "failure_spike":
        if not threshold or threshold < 1 or not window_minutes or window_minutes < 1:
            raise ValueError("a spike rule needs --threshold and --window, both at least 1")
    elif threshold or window_minutes:
        raise ValueError("a dead-run rule takes no threshold or window")
    with db.connection() as conn:
        rule_id = conn.execute(
            "INSERT INTO alert_rules (kind, channel, target, threshold, window_minutes) "
            "VALUES (%s, 'telegram', %s, %s, %s) RETURNING id",
            (kind, target, threshold, window_minutes),
        ).fetchone()["id"]
        audit.record(conn, "cli", "alert_rule.created", "alert_rule", rule_id,
                     after={"kind": kind, "target": target, "threshold": threshold, "window_minutes": window_minutes})
    return rule_id


def list_rules():
    with db.connection() as conn:
        return conn.execute("SELECT * FROM alert_rules ORDER BY id").fetchall()


def set_active(rule_id, active):
    with db.connection() as conn:
        done = conn.execute("UPDATE alert_rules SET active = %s WHERE id = %s", (active, rule_id)).rowcount
        if not done:
            raise LookupError(f"no alert rule {rule_id}")
        audit.record(conn, "cli", "alert_rule.enabled" if active else "alert_rule.disabled", "alert_rule", rule_id)


# ----- messages ---------------------------------------------------------------------------------

def _one_line(text, limit=ERROR_CHARS):
    return " ".join((text or "").split())[:limit]


def dead_message(job):
    lines = [
        f"Run {job['id']} gave up",
        f"Source: {job['source']}",
        f"Tries: {job['attempts']} of {job['max_attempts']}",
        f"Last error: {_one_line(job['last_error']) or 'none recorded'}",
        f"{config.DASHBOARD_URL}/runs/{job['id']}",
    ]
    return "\n".join(lines)


def spike_message(count, window_minutes, by_source):
    top = ", ".join(f"{row['source']} ({row['n']})" for row in by_source)
    return "\n".join([
        f"{count} failed tries in the last {window_minutes} minutes",
        f"Most from: {top}" if top else "Sources: none",
        f"{config.DASHBOARD_URL}/?status=failed&status=dead",
    ])


# ----- step 1: decide ---------------------------------------------------------------------------

def evaluate():
    """Queue messages for anything newly worth alerting about. Returns how many were queued."""
    queued = 0
    with db.connection() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK_ID,)).fetchone()["ok"]:
            return 0  # another worker is evaluating right now
        for rule in conn.execute("SELECT * FROM alert_rules WHERE active AND channel = 'telegram' ORDER BY id").fetchall():
            if rule["kind"] == "job_dead":
                queued += _evaluate_dead(conn, rule)
            else:
                queued += _evaluate_spike(conn, rule)
    return queued


def _evaluate_dead(conn, rule):
    fresh = conn.execute(
        "SELECT j.id, j.attempts, j.max_attempts, j.last_error, s.name AS source "
        "FROM jobs j JOIN sources s ON s.id = j.source_id "
        "WHERE j.status = 'dead' AND j.finished_at >= %s "
        "  AND NOT EXISTS (SELECT 1 FROM alert_deliveries d WHERE d.rule_id = %s "
        "                  AND d.dedupe_key = 'dead:' || j.id || ':' || j.attempts) "
        "ORDER BY j.id LIMIT 500",
        (rule["created_at"], rule["id"]),
    ).fetchall()
    cap = config.ALERT_MAX_PER_TICK
    for job in fresh[:cap]:
        _enqueue(conn, rule, "job_dead", f"dead:{job['id']}:{job['attempts']}", dead_message(job))
    overflow = fresh[cap:]
    if overflow:
        # A burst becomes one summary. Each run still gets a row, so none is re-alerted next pass.
        for job in overflow:
            _enqueue(conn, rule, "job_dead", f"dead:{job['id']}:{job['attempts']}", "covered by a summary", "suppressed")
        ids = [j["id"] for j in overflow]
        text = f"{len(overflow)} more runs gave up (runs {ids[0]} to {ids[-1]})\n{config.DASHBOARD_URL}/?status=dead"
        _enqueue(conn, rule, "job_dead", f"dead-summary:{ids[0]}-{ids[-1]}", text)
    return min(len(fresh), cap) + (1 if overflow else 0)


def _evaluate_spike(conn, rule):
    window = rule["window_minutes"]
    count = conn.execute(
        "SELECT count(*) AS n FROM job_attempts WHERE ok = FALSE AND finished_at > now() - make_interval(mins => %s)",
        (window,),
    ).fetchone()["n"]
    if count < rule["threshold"]:
        return 0
    cooling = conn.execute(
        "SELECT 1 FROM alert_deliveries WHERE rule_id = %s AND kind = 'failure_spike' "
        "AND created_at > now() - make_interval(mins => %s) LIMIT 1",
        (rule["id"], window),
    ).fetchone()
    if cooling:
        return 0
    by_source = conn.execute(
        "SELECT s.name AS source, count(*) AS n FROM job_attempts a JOIN jobs j ON j.id = a.job_id "
        "JOIN sources s ON s.id = j.source_id WHERE a.ok = FALSE AND a.finished_at > now() - make_interval(mins => %s) "
        "GROUP BY s.name ORDER BY n DESC, s.name LIMIT 3",
        (window,),
    ).fetchall()
    # Microseconds, so two spikes can never share a key. The cooldown above and the advisory lock in
    # evaluate() are what actually keep this to one message per window.
    stamp = conn.execute("SELECT (extract(epoch FROM clock_timestamp()) * 1000000)::bigint AS t").fetchone()["t"]
    return _enqueue(conn, rule, "failure_spike", f"spike:{stamp}", spike_message(count, window, by_source))


def _enqueue(conn, rule, kind, dedupe_key, message, status="pending"):
    row = conn.execute(
        "INSERT INTO alert_deliveries (rule_id, kind, dedupe_key, message, status) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (rule_id, dedupe_key) DO NOTHING RETURNING id",
        (rule["id"], kind, dedupe_key, message, status),
    ).fetchone()
    return 1 if row else 0


# ----- step 2: send -----------------------------------------------------------------------------

def deliver():
    """Send what is due. Returns (sent, failed_this_pass)."""
    with db.connection() as conn:
        claimed = conn.execute(
            "UPDATE alert_deliveries SET attempts = attempts + 1, next_try_at = now() + make_interval(secs => %s) "
            "WHERE id IN (SELECT id FROM alert_deliveries WHERE status = 'pending' AND next_try_at <= now() "
            "             ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s) "
            "RETURNING id, rule_id, message, attempts",
            (LEASE_SECONDS, SEND_BATCH),
        ).fetchall()
    sent = failed = 0
    for item in sorted(claimed, key=lambda r: r["id"]):
        with db.connection() as conn:
            rule = conn.execute("SELECT target, active FROM alert_rules WHERE id = %s", (item["rule_id"],)).fetchone()
        if rule is None or not rule["active"]:
            _record(item, "suppressed", "rule is off or gone")
            continue
        try:
            telegram.send_message(config.TELEGRAM_BOT_TOKEN, rule["target"], item["message"])
        except telegram.TelegramError as exc:
            failed += 1
            _record_failure(item, str(exc), exc.retry_after)
        else:
            sent += 1
            _record(item, "sent", None)
    return sent, failed


def _record(item, status, note):
    with db.connection() as conn:
        done = conn.execute(
            "UPDATE alert_deliveries SET status = %s, last_error = %s, "
            "sent_at = CASE WHEN %s = 'sent' THEN now() END WHERE id = %s AND status = 'pending'",
            (status, note, status, item["id"]),
        ).rowcount
        if done and status == "sent":
            audit.record(conn, "alerts", "alert.sent", "alert_delivery", item["id"], after={"attempt": item["attempts"]})


def _record_failure(item, error, retry_after):
    final = item["attempts"] >= config.ALERT_MAX_ATTEMPTS
    delay = max(jobs.backoff_seconds(item["attempts"], base=30, cap=900), retry_after or 0)
    with db.connection() as conn:
        done = conn.execute(
            "UPDATE alert_deliveries SET last_error = %s, status = %s, "
            "next_try_at = now() + make_interval(secs => %s) WHERE id = %s AND status = 'pending'",
            (error, "failed" if final else "pending", delay, item["id"]),
        ).rowcount
        if done and final:
            audit.record(conn, "alerts", "alert.failed", "alert_delivery", item["id"],
                         after={"attempts": item["attempts"], "error": error})
    log.warning("alert %s not sent (attempt %s): %s", item["id"], item["attempts"], error)


def tick():
    """One pass for the worker loop. Does nothing until a bot token is configured."""
    if not config.TELEGRAM_BOT_TOKEN:
        return None
    queued = evaluate()
    sent, failed = deliver()
    return {"queued": queued, "sent": sent, "failed": failed}


# ----- command line -----------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m app.alerts")
    sub = parser.add_subparsers(dest="command", required=True)
    rule = sub.add_parser("rule").add_subparsers(dest="action", required=True)
    add = rule.add_parser("add")
    add.add_argument("kind", choices=sorted(KINDS))
    add.add_argument("--chat", help="Telegram chat id or @channel (default: TELEGRAM_CHAT_ID)")
    add.add_argument("--threshold", type=int, help="spike: failed tries that trigger it")
    add.add_argument("--window", type=int, help="spike: window in minutes")
    rule.add_parser("list")
    for name in ("enable", "disable"):
        rule.add_parser(name).add_argument("id", type=int)
    test = sub.add_parser("test", help="send a test message to check the token and chat id")
    test.add_argument("--chat")
    args = parser.parse_args(argv)

    db.init_pool(config.DATABASE_URL)
    try:
        if args.command == "test":
            _send_test(args.chat)
        elif args.action == "add":
            rule_id = create_rule(KINDS[args.kind], args.chat, args.threshold, args.window)
            print(f"Created alert rule {rule_id}")
        elif args.action == "list":
            for r in list_rules():
                extra = f" >= {r['threshold']} failed tries in {r['window_minutes']} min" if r["threshold"] else ""
                print(f"{r['id']}  {r['kind']}{extra}  chat {r['target']}  {'on' if r['active'] else 'off'}")
        else:
            set_active(args.id, args.action == "enable")
            print(f"Rule {args.id} {'enabled' if args.action == 'enable' else 'disabled'}")
    except (ValueError, LookupError, telegram.TelegramError) as exc:
        sys.exit(f"Failed: {exc}")
    finally:
        db.close_pool()


def _send_test(chat):
    if not config.TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    target = (chat or config.TELEGRAM_CHAT_ID or "").strip()
    if not CHAT_RE.fullmatch(target):
        raise ValueError("pass --chat or set TELEGRAM_CHAT_ID")
    telegram.send_message(config.TELEGRAM_BOT_TOKEN, target, "Automation Hub test alert. If you can read this, alerts will reach you.")
    print("Test message sent")


if __name__ == "__main__":
    main()
