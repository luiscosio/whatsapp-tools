"""Send text (and one media file) to a verified chat. Never types into an unverified chat."""

from __future__ import annotations

import re
import time
from pathlib import Path

from wa.session import ACTION_TIMEOUT, WhatsAppError, first_selector, open_chat
from wa.text import match_probe

CONFIRM_TIMEOUT = 60  # seconds for WhatsApp to mark a message sent
MEDIA_TIMEOUT = 240  # a large upload takes longer
CHUNK_DELAY = 4.0  # bursts look like automation and WhatsApp silently refuses later chunks
MAX_MEDIA_BYTES = 16 * 1024 * 1024
CAPTION_LIMIT = 1000

# WhatsApp's own per-message status, matched as whole tokens (substring tests
# confirmed an upload that never finished).
SENT = {"sent", "delivered", "read", "msg-check", "msg-dblcheck", "msg-dblcheck-ack"}
PENDING = {"msg-time", "pending", "wds-ic-pending"}
SENT.update({"wds-ic-read", "wds-ic-delivered", "wds-ic-sent", "ic-read", "ic-delivered", "ic-sent"})


class SentUnconfirmed(WhatsAppError):
    """WhatsApp may already have received the message."""


def ticked(icons):
    tokens = {str(icon).strip().lower() for icon in icons}
    return bool(SENT & tokens) and not PENDING & tokens


FAILED = ("went wrong", "not sent", "failed to send", "msg-error")

# Outgoing message rows with their status icons. A row is the [role="row"] that holds
# the bubble and its tick; media bubbles nest several [data-id], so the first one is used.
OUTGOING_JS = """
(needle) => [...document.querySelectorAll('#main [role="row"]')]
  .filter(r => r.querySelector('[data-id]'))
  .filter(r => r.querySelector('.message-out, [data-icon="tail-out"], [data-icon^="msg-"]'))
  .filter(r => !needle || (r.innerText || '').includes(needle))
  .map(r => ({
    id: r.querySelector('[data-id]').getAttribute('data-id') || '',
    icons: [...r.querySelectorAll('[aria-label],[data-icon]')]
      .map(e => (e.getAttribute('aria-label') || e.getAttribute('data-icon') || '').trim().toLowerCase()),
  }))
"""

MEDIA_ITEM = re.compile(r"^(Photos|Fotos) (&|y) (videos|vídeos)$", re.IGNORECASE)
ATTACH_SELECTORS = [
    'button[title="Attach"]',
    'div[title="Attach"]',
    '[aria-label="Attach"]',
    'span[data-icon="plus-rounded"]',
    'span[data-icon="plus"]',
    'span[data-icon="clip"]',
]
CAPTION_SELECTORS = [
    '[data-testid="media-caption-input-container"]',
    'div[contenteditable="true"][aria-label*="caption" i]',
]
MEDIA_SEND = 'div[role="button"][aria-label^="Send"], span[data-icon="send"]'


def _outgoing(page, needle: str) -> list[dict]:
    try:
        return page.evaluate(OUTGOING_JS, needle)
    except Exception as exc:
        raise WhatsAppError("Could not read outgoing message state.") from exc


def _type_lines(page, text: str) -> None:
    """Line by line with Shift+Enter (Enter sends). insert_text keeps emoji intact."""
    lines = text.split("\n")
    for n, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
        if n < len(lines) - 1:
            page.keyboard.press("Shift+Enter")


def confirm_sent(page, needle: str, previous: set[str], timeout: float) -> None:
    """Wait for a new outgoing row (not in `previous`) to show a sent/delivered tick.

    An error label fails at once; a pending clock keeps waiting until the timeout.
    """
    deadline = time.monotonic() + timeout
    last = "no new row"
    while time.monotonic() < deadline:
        for row in reversed(_outgoing(page, needle)):
            if not row["id"] or row["id"] in previous:
                continue
            icons = [i for i in row["icons"] if i]
            joined = " ".join(icons)
            if any(marker in joined for marker in FAILED):
                raise WhatsAppError(f"WhatsApp rejected the message ({joined})")
            if ticked(icons):
                return
            last = joined or "pending"
            break
        page.wait_for_timeout(500)
    raise WhatsAppError(f"send not confirmed within {timeout:.0f}s (last state: {last})")


def send_chunk(page, chunk: str) -> None:
    needle = match_probe(chunk)
    previous = {r["id"] for r in _outgoing(page, needle)}
    page.click(first_selector(page, "composer"))
    _type_lines(page, chunk)
    try:
        page.keyboard.press("Enter")
        confirm_sent(page, needle, previous, CONFIRM_TIMEOUT)
    except Exception as exc:
        raise SentUnconfirmed("Check the chat before resending.") from exc


def send_media(page, media: Path, caption: str) -> None:
    previous = {r["id"] for r in _outgoing(page, "")}
    for selector in ATTACH_SELECTORS:
        if page.locator(selector).count():
            page.click(selector)
            break
    else:
        raise WhatsAppError("no attach button found; WhatsApp's DOM may have changed")
    page.wait_for_timeout(800)
    with page.expect_file_chooser(timeout=ACTION_TIMEOUT) as chooser:
        page.get_by_text(MEDIA_ITEM).first.click()
    chooser.value.set_files(str(media))
    # Type nothing unless the media preview is really open: a caption typed without
    # it would land in the chat as a plain message.
    send_button = page.locator(MEDIA_SEND).first
    send_button.wait_for(state="visible", timeout=60_000)
    if caption:
        box = page.locator(", ".join(CAPTION_SELECTORS)).first
        box.wait_for(state="visible", timeout=ACTION_TIMEOUT)
        box.focus()
        page.wait_for_timeout(300)
        _type_lines(page, caption)
    try:
        send_button.click(force=True)
        confirm_sent(page, match_probe(caption) if caption else "", previous, MEDIA_TIMEOUT)
    except Exception as exc:
        raise SentUnconfirmed("Check the chat before resending the attachment.") from exc


def send(page, chat: str, chunks: list[str], media: Path | None) -> int:
    """Open and verify `chat`, then send. Returns the number of messages sent.

    Stops at the first unconfirmed message: a message that breaks off midway beats
    one that keeps sending into a chat that is rejecting it.
    """
    open_chat(page, chat)  # raises on header mismatch: nothing has been typed yet
    if media and media.stat().st_size > MAX_MEDIA_BYTES:
        raise WhatsAppError(f"{media.name} is over WhatsApp's ~16 MB limit")
    sent = 0
    if media:
        caption = chunks[0] if chunks and len(chunks[0]) <= CAPTION_LIMIT else ""
        send_media(page, media, caption)
        sent += 1
        chunks = chunks[1:] if caption else chunks
    for chunk in chunks:
        if sent:
            time.sleep(CHUNK_DELAY)
        send_chunk(page, chunk)
        sent += 1
    return sent
