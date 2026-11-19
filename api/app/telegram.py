"""Send a plain-text message through the Telegram Bot API, using only the standard library.

The bot token is part of the request URL, so no error text built here ever includes the URL, and any
stray copy of the token is masked before an error leaves this module (it ends up in the database
and the logs).
"""
import json
import urllib.error
import urllib.request

from app import config

MAX_TEXT = 3500  # Telegram's limit is 4096; stay well under


class TelegramError(Exception):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def send_message(token, chat_id, text, timeout=10):
    url = f"{config.TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:MAX_TEXT], "disable_web_page_preview": True}
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as exc:
        description, retry_after = _read_error(exc)
        raise TelegramError(_mask(f"Telegram answered {exc.code}: {description}", token), retry_after) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TelegramError(_mask(f"could not reach Telegram ({type(reason).__name__})", token)) from None
    if not (isinstance(answer, dict) and answer.get("ok")):
        raise TelegramError("Telegram did not accept the message")


def _read_error(exc):
    try:
        body = json.load(exc)
        retry_after = (body.get("parameters") or {}).get("retry_after")
        return str(body.get("description", "no description"))[:200], retry_after
    except (ValueError, AttributeError):
        return "no description", None


def _mask(text, token):
    return text.replace(token, "***") if token else text
