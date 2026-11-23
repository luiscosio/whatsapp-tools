"""send_batch never sends an item twice once WhatsApp has taken it (2026-11-15 double send)."""

from __future__ import annotations

import contextlib

from wa import batch as whatsapp
from wa.session import WhatsAppError


class _Keys:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)


class _Page:
    def __init__(self) -> None:
        self.keyboard = _Keys()

    def wait_for_timeout(self, _ms: int) -> None:
        pass


def _run(monkeypatch, outcomes):
    page = _Page()
    calls: list[str] = []

    @contextlib.contextmanager
    def fake_chat(*_a, **_k):
        yield page

    def fake_send(_page, _group, text, _media):
        calls.append(text)
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome

    monkeypatch.setattr(whatsapp, "chat_page", fake_chat)
    monkeypatch.setattr(whatsapp, "_send_one", fake_send)
    reports: list[tuple[int, str | None]] = []
    ok = whatsapp.send_batch(
        [("hola", None), ("adios", None)], group="Test Group", report=lambda n, e: reports.append((n, e))
    )
    return ok, calls, reports, page


def test_unconfirmed_send_is_not_retried(monkeypatch):
    ok, calls, reports, page = _run(monkeypatch, [whatsapp.SentUnconfirmed("sent but not confirmed")])
    assert not ok
    assert calls == ["hola"]  # sent once, never again, and the batch stops
    assert reports == [(0, "sent but not confirmed")]
    assert page.keyboard.pressed == []  # no Escape: it would close the chat


def test_failure_before_sending_is_retried(monkeypatch):
    ok, calls, reports, _ = _run(
        monkeypatch, [WhatsAppError("no selector matched for 'composer'"), None, None]
    )
    assert ok
    assert calls == ["hola", "hola", "adios"]
    assert reports == [(0, None), (1, None)]
