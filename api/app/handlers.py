"""What a job does with its payload. A source picks one by name (sources.handler).

A handler takes the parsed payload dict and raises on failure; the worker turns that into a
retry. Add real integrations here (n8n call, email, CRM write) and register them in HANDLERS.
"""
import json
import logging
import urllib.error
import urllib.request

from app import config, telegram

log = logging.getLogger("handlers")


def log_handler(payload):
    log.info("payload received: %s", payload)


def demo_handler(payload):
    """For the seeded demo data (python -m app.seed). Succeeds, unless the payload carries an
    "error" message, in which case it raises that, so demo failures come from the real retry path."""
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    log.info("demo payload handled: %s", payload)


def facebook_comment_comments(payload):
    """Pull the new comments out of a Facebook Page "feed" webhook. Anything else is ignored."""
    found = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if change.get("field") == "feed" and value.get("item") == "comment" and value.get("verb") == "add":
                found.append({
                    "comment_id": str(value.get("comment_id", "")),
                    "post_id": str(value.get("post_id", "")),
                    "name": str((value.get("from") or {}).get("name", "Someone")),
                    "message": str(value.get("message", "")),
                })
    return found


def _append_to_sheet(row):
    request = urllib.request.Request(
        config.SHEET_WEBHOOK_URL, data=json.dumps(row).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            pass
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"could not save to the Sheet ({type(getattr(exc, 'reason', exc)).__name__})") from None


def facebook_comment_handler(payload):
    """New Page comment: save a row to the Google Sheet (when SHEET_WEBHOOK_URL is set), then tell
    Diego on Telegram. Read-only toward Facebook: it never replies or posts. Any failure raises, so
    the hub retries and, if every try fails, the dead-run alert fires. The Sheet row carries the
    comment id so the Sheet script can skip a duplicate if a retry repeats this step."""
    comments = facebook_comment_comments(payload)
    if not comments:
        log.info("facebook event ignored: no new comment in it")
        return
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        raise RuntimeError("Telegram is not configured")
    for comment in comments:
        if config.SHEET_WEBHOOK_URL:
            _append_to_sheet(comment)
        text = f"New comment on your Page\nFrom: {comment['name']}\n{comment['message']}"
        telegram.send_message(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, text)


HANDLERS = {"log": log_handler, "demo": demo_handler, "facebook_comment": facebook_comment_handler}
