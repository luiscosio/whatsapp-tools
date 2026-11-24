from contextlib import nullcontext
from datetime import date
from unittest.mock import Mock

from wa import files, history


def test_phone_sync_resumes_before_date_boundary(monkeypatch):
    page = Mock()
    messages = [{"id": "old", "pre": "[9:00 AM, 11/20/2026] A: ", "text": "test"}]
    reads = iter([[], messages])

    def evaluate(script):
        if script == history.SYNC_STATE_JS:
            return "phone_sync_paused"
        if script == files.SCROLL_UP_JS:
            return -1
        if '[role="row"]' in script:
            return next(reads)
        return None

    page.evaluate.side_effect = evaluate
    monkeypatch.setattr(history, "chat_page", lambda *_: nullcontext(page))
    result = history.read_messages(group="example", since=date(2026, 11, 21), max_scrolls=1)
    assert result["complete"] and result["stop_reason"] == "date_boundary"
    page.wait_for_timeout.assert_any_call(2000)


def test_reports_partial_when_phone_is_still_paused(monkeypatch):
    page = Mock()
    page.evaluate.side_effect = [None, [{"id": "1", "pre": "", "text": "test"}], -1, "phone_sync_paused"]
    monkeypatch.setattr(history, "chat_page", lambda *_: nullcontext(page))
    monkeypatch.setattr(history, "SYNC_WAIT_SECONDS", 0)
    result = history.read_messages(group="example")
    assert result["messages"][0]["id"] == "1"
    assert not result["complete"]
    assert result["stop_reason"] == "phone_sync_paused"
