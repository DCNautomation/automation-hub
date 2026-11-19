import pytest

from app import config, handlers, telegram

PAYLOAD = {"object": "page", "entry": [{"id": "1", "changes": [{"field": "feed", "value": {
    "item": "comment", "verb": "add", "comment_id": "c1", "post_id": "p1",
    "from": {"id": "9", "name": "Ana"}, "message": "magkano po?"}}]}]}


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(config, "SHEET_WEBHOOK_URL", "")
    monkeypatch.setattr(telegram, "send_message", lambda t, c, text: out.append(text))
    return out


def test_new_comment_notifies(sent):
    handlers.facebook_comment_handler(PAYLOAD)
    assert len(sent) == 1 and "Ana" in sent[0] and "magkano po?" in sent[0]


def test_other_events_are_ignored(sent):
    handlers.facebook_comment_handler({"entry": [{"changes": [{"field": "feed", "value": {"item": "like", "verb": "add"}}]}]})
    assert sent == []


def test_sheet_row_saved_before_notify(sent, monkeypatch):
    rows = []
    monkeypatch.setattr(config, "SHEET_WEBHOOK_URL", "https://example.test/sheet")
    monkeypatch.setattr(handlers, "_append_to_sheet", rows.append)
    handlers.facebook_comment_handler(PAYLOAD)
    assert rows == [{"comment_id": "c1", "post_id": "p1", "name": "Ana", "message": "magkano po?"}]
    assert len(sent) == 1


def test_sheet_failure_raises_so_hub_retries(sent, monkeypatch):
    monkeypatch.setattr(config, "SHEET_WEBHOOK_URL", "https://example.test/sheet")
    def boom(row): raise RuntimeError("could not save to the Sheet")
    monkeypatch.setattr(handlers, "_append_to_sheet", boom)
    with pytest.raises(RuntimeError):
        handlers.facebook_comment_handler(PAYLOAD)
    assert sent == []


def test_missing_telegram_config_raises(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(RuntimeError):
        handlers.facebook_comment_handler(PAYLOAD)
