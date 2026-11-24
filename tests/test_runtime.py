import pytest

from wa import browser, request
from wa.session import WhatsAppError, prepare_profile


def test_preview_does_not_start_a_browser(monkeypatch, tmp_path, capsys):
    from wa import cli

    monkeypatch.setattr(browser, "sync_playwright", lambda: pytest.fail("browser started"))
    msg = tmp_path / "message.md"
    msg.write_text("Fixture")
    assert cli.main(["send", "--chat", "Example", "--file", str(msg)]) == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_live_request_requires_explicit_authorization():
    with pytest.raises(ValueError, match="requires --yes"):
        request.dispatch({"operation": "batch", "items": [], "group": "Test"})


def test_lock_from_another_host_is_preserved(tmp_path):
    lock = tmp_path / "SingletonLock"
    lock.symlink_to("other-host-42")
    with pytest.raises(WhatsAppError):
        prepare_profile(tmp_path)
    assert lock.is_symlink()


def test_default_native_launch_has_no_window(monkeypatch, tmp_path):
    from types import SimpleNamespace

    calls = []
    connections = iter([None, object()])
    monkeypatch.setattr(browser, "_connect", lambda *_: next(connections))
    monkeypatch.setattr(
        browser.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append(args) or SimpleNamespace(pid=42, poll=lambda: None),
    )
    browser._native_chrome(None, tmp_path, True)
    assert "--headless=new" in calls[0]
    assert "--remote-debugging-address=127.0.0.1" in calls[0]


def test_historical_rows_survive_virtualization(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from wa import history

    row = {"id": "1", "pre": "[4:08 PM, 11/19/2026] Test: ", "text": "Example"}
    snapshots = iter([None, [row], 10, [{**row, "text": "", "pre": ""}], 0])
    page = SimpleNamespace(
        wait_for_function=lambda *a, **kw: None,
        wait_for_timeout=lambda _: None,
        evaluate=lambda _: next(snapshots),
    )
    monkeypatch.setattr(history, "chat_page", lambda *args: nullcontext(page))
    result = history.read_messages(group="Example", max_scrolls=2)
    assert result["messages"][0]["date"] == "2026-11-19"
    assert result["messages"][0]["text"] == "Example"


def test_manual_session_is_preserved_on_a_headless_request(monkeypatch, tmp_path):
    from unittest.mock import Mock

    active = Mock()
    active.new_browser_cdp_session.return_value.send.return_value = {"userAgent": "Chrome/150"}
    monkeypatch.setattr(browser, "_connect", lambda *_: active)
    with pytest.raises(WhatsAppError, match="Close the dedicated"):
        browser._native_chrome(None, tmp_path, True)
    assert active.new_browser_cdp_session.return_value.send.call_count == 1


def test_owned_headless_session_closes_only_its_tab(monkeypatch, tmp_path):
    from contextlib import nullcontext
    from pathlib import Path
    from unittest.mock import Mock

    active = Mock()
    page = Mock()
    active.contexts = [Mock()]
    active.contexts[0].new_page.return_value = page
    monkeypatch.setattr(browser.sys, "platform", "darwin")
    monkeypatch.setattr(browser, "CHROME", Path(__file__))
    monkeypatch.setattr(browser, "sync_playwright", lambda: nullcontext(None))
    monkeypatch.setattr(browser, "_native_chrome", lambda *_: (active, True))
    with browser.session_page(tmp_path):
        pass
    page.close.assert_called_once()
    page.bring_to_front.assert_not_called()
    active.new_browser_cdp_session.assert_not_called()
