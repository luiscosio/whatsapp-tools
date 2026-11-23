"""Send ordered text and media to an exact group title."""

from collections.abc import Callable
from pathlib import Path

from .media import prepared_media
from .sender import SentUnconfirmed, send_chunk, send_media
from .session import chat_page

BATCH_RETRIES = 2


def _probe(text: str) -> str:
    """A short slice of the message that the chat-list preview will show:
    first line, markup and emoji removed."""
    line = next((ln for ln in text.splitlines() if ln.strip()), "")
    clean = "".join(ch for ch in line if ch.isalnum() or ch in " .,:;()-")
    return " ".join(clean.split())[:24]


def _send_one(page, group, text, media):
    if media:
        with prepared_media(media) as attachment:
            send_media(page, attachment, text)
    else:
        send_chunk(page, text)


def send(text, media=None, *, group, profile=None, headless=True):
    with chat_page(group, profile, headless) as page:
        _send_one(page, group, text, media)


def send_batch(
    items: list[tuple[str, Path | None]],
    *,
    group: str,
    profile: str | None = None,
    headless: bool = True,
    retries: int = BATCH_RETRIES,
    report: Callable[[int, str | None], None] = lambda n, error: None,
) -> bool:
    """Send (text, media) items in order in one browser session.

    An item that fails is retried up to `retries` times; if it still fails the
    batch stops there. `report(n, error)` is called once per item tried
    (error None = sent). Returns True when every item was sent. A failure to
    open or verify the chat raises, since nothing was sent."""
    with chat_page(group, profile, headless) as page:
        for n, (text, media) in enumerate(items):
            error = None
            for _ in range(1 + retries):
                try:
                    _send_one(page, group, text, media)
                    error = None
                    break
                except SentUnconfirmed as e:
                    # Never retry once the message was handed over: a retry sent the
                    # same text twice on 2026-11-15. Stop and let a person check.
                    error = str(e).splitlines()[0]
                    break
                except Exception as e:  # noqa: BLE001
                    error = str(e).splitlines()[0] if str(e) else type(e).__name__
                    if media:
                        page.keyboard.press("Escape")  # close a half-open media preview
                    # (Escape with no preview open closes the chat; nothing else to undo.)
                    page.wait_for_timeout(1000)
            report(n, error)
            if error:
                return False
    return True
