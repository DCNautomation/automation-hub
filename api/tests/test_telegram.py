"""The Telegram client, against a local fake server."""
import pytest

from app import config, telegram

from conftest import TOKEN


def test_posts_the_message_to_the_bot_api(telegram_up):
    telegram.send_message(TOKEN, "12345", "hello there")
    request = telegram_up.requests[0]
    assert request["path"] == f"/bot{TOKEN}/sendMessage"
    assert request["body"] == {"chat_id": "12345", "text": "hello there", "disable_web_page_preview": True}


def test_overlong_text_is_cut_to_stay_under_the_limit(telegram_up):
    telegram.send_message(TOKEN, "1", "x" * 10_000)
    assert len(telegram_up.texts[0]) == telegram.MAX_TEXT


def test_an_error_answer_says_what_telegram_said_and_never_includes_the_token(telegram_up):
    telegram_up.script = [(400, {"ok": False, "description": f"Bad Request: chat not found {TOKEN}"})]
    with pytest.raises(telegram.TelegramError) as caught:
        telegram.send_message(TOKEN, "1", "hi")
    assert "400" in str(caught.value) and "chat not found" in str(caught.value)
    assert TOKEN not in str(caught.value)


def test_rate_limit_answer_carries_retry_after(telegram_up):
    telegram_up.script = [(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}})]
    with pytest.raises(telegram.TelegramError) as caught:
        telegram.send_message(TOKEN, "1", "hi")
    assert caught.value.retry_after == 7


def test_a_non_json_error_body_is_still_a_clean_error(telegram_up):
    telegram_up.script = [(502, "<html>bad gateway</html>")]
    with pytest.raises(telegram.TelegramError) as caught:
        telegram.send_message(TOKEN, "1", "hi")
    assert "502" in str(caught.value)


def test_a_200_that_is_not_ok_is_an_error(telegram_up):
    telegram_up.script = [(200, {"ok": False})]
    with pytest.raises(telegram.TelegramError):
        telegram.send_message(TOKEN, "1", "hi")


def test_unreachable_server_is_a_clean_error_without_the_url_or_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_API_BASE", "http://127.0.0.1:9")  # nothing listens on the discard port
    with pytest.raises(telegram.TelegramError) as caught:
        telegram.send_message(TOKEN, "1", "hi", timeout=2)
    assert "could not reach Telegram" in str(caught.value)
    assert TOKEN not in str(caught.value) and "127.0.0.1" not in str(caught.value)
