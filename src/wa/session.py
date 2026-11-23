"""Browser session on an already linked WhatsApp Web profile (adapted from LabWatch)."""

from __future__ import annotations

import os
import socket
import sys
from contextlib import contextmanager
from pathlib import Path

from wa.text import header_matches

WEB_URL = "https://web.whatsapp.com"
DEFAULT_PROFILE = "~/Library/Application Support/iNBest/WhatsApp"
LOAD_TIMEOUT = 90_000
ACTION_TIMEOUT = 15_000

# WhatsApp ships obfuscated class names, so each role is an ordered list of
# candidates. This dict is the one place to fix when the DOM shifts.
SELECTORS = {
    "chat_list": ["#pane-side", 'div[aria-label="Chat list"]'],
    "qr": ['[data-testid="link-device-qr-code"]', 'canvas[aria-label*="QR"]', "div[data-ref]"],
    "search": [
        'input[data-tab="3"]',
        'input[role="textbox"][aria-label*="Search" i]',
        'input[placeholder*="Search" i]',
        'div[contenteditable="true"][data-tab="3"]',
    ],
    "composer": [
        'div[contenteditable="true"][data-tab="10"]',
        'footer div[contenteditable="true"]',
        'div[role="textbox"][aria-label*="message"]',
    ],
}


class WhatsAppError(Exception):
    pass


class LoggedOut(WhatsAppError):
    def __init__(self) -> None:
        super().__init__("WhatsApp is logged out: re-link the profile (scan the QR with the phone)")


def profile_path(option: str | None) -> Path:
    raw = option or os.environ.get("WA_PROFILE") or DEFAULT_PROFILE
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise WhatsAppError(f"no WhatsApp profile at {path}")
    return path


def _lock_target(profile: Path) -> tuple[str, int | None] | None:
    lock = profile / "SingletonLock"
    if not lock.is_symlink():
        return None
    host, _, pid = os.readlink(lock).rpartition("-")
    return host, int(pid) if pid.isdigit() else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def prepare_profile(profile: Path) -> None:
    """Clear Chromium's lock files only when its holder is dead; else refuse.

    The lock is a symlink to "<hostname>-<pid>". It is stale only when the hostname
    is this machine and the pid is gone. A lock from another host cannot be checked,
    so it is treated as held.
    """
    target = _lock_target(profile)
    if target is None:
        return
    host, pid = target
    if host == socket.gethostname() and (pid is None or not _pid_alive(pid)):
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            (profile / name).unlink(missing_ok=True)
        print(f"wa: cleared stale profile lock (pid {pid} is gone)", file=sys.stderr)
        return
    raise locked_error(profile, pid)


def locked_error(profile: Path, pid: int | None) -> WhatsAppError:
    who = f"pid {pid}" if pid else "another host"
    return WhatsAppError(
        f"profile {profile} is in use by a running browser ({who}). Another "
        "job shares this profile; wait for it to finish and retry."
    )


def first_selector(page, role: str, timeout: int = ACTION_TIMEOUT) -> str:
    """The first candidate selector for `role` that becomes visible, else raise."""
    for selector in SELECTORS[role]:
        try:
            page.wait_for_selector(selector, timeout=timeout, state="visible")
            return selector
        except Exception as exc:  # noqa: BLE001 (playwright TimeoutError)
            if type(exc).__name__ != "TimeoutError":
                raise WhatsAppError(f"browser died while looking for {role!r}: {exc}") from None
            timeout = 2_000
    if role != "qr" and is_logged_out(page):
        raise LoggedOut()
    raise WhatsAppError(f"no selector matched for {role!r}; WhatsApp's DOM may have changed")


def is_logged_out(page) -> bool:
    for selector in SELECTORS["qr"]:
        try:
            page.wait_for_selector(selector, timeout=2_000, state="visible")
            return True
        except Exception as exc:
            if type(exc).__name__ != "TimeoutError":
                raise
    return False


def _launch(p, profile: Path, headless: bool):
    options = {"headless": headless, "locale": "en-US"}
    for channel in ("chrome", None):
        try:
            extra = {"channel": channel} if channel else {}
            return p.chromium.launch_persistent_context(str(profile), **extra, **options)
        except Exception as exc:
            if (
                _lock_target(profile) is not None
                or "SingletonLock" in str(exc)
                or "ProcessSingleton" in str(exc)
            ):
                target = _lock_target(profile)
                raise locked_error(profile, target[1] if target else None) from None
            if channel is None:
                raise
            print(
                f"wa: Chrome unavailable ({str(exc).splitlines()[0]}); using bundled Chromium",
                file=sys.stderr,
            )


@contextmanager
def open_page(profile: Path, headless: bool = True):
    """Yield a WhatsApp Web page with the chat list loaded; raise LoggedOut otherwise."""
    from .browser import session_page

    with session_page(profile, headless=headless) as page:
        page.goto(WEB_URL, timeout=LOAD_TIMEOUT, wait_until="domcontentloaded")
        first_selector(page, "chat_list", timeout=LOAD_TIMEOUT)
        page.keyboard.press("Escape")
        yield page


def login(profile=None, *, timeout_ms=300_000):
    from .browser import session_page

    folder = Path(profile or os.environ.get("WA_PROFILE") or DEFAULT_PROFILE).expanduser()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with session_page(folder, headless=False) as page:
        page.goto(WEB_URL, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT)
        page.locator("#pane-side").wait_for(state="visible", timeout=timeout_ms)
    return folder


@contextmanager
def chat_page(group, profile=None, headless=True):
    with open_page(profile_path(profile), headless=headless) as page:
        open_chat(page, group)
        yield page


def conversation_titles(page) -> list[str]:
    """Every candidate name in the open conversation's header (title attr and text)."""
    try:
        found = page.eval_on_selector_all(
            "#main header span",
            "els => els.flatMap(e => { const c = e.cloneNode(true); c.querySelectorAll('img[alt]').forEach(i => i.replaceWith(i.alt)); return [e.title || '', c.textContent || '']; })",
        )
    except Exception:  # noqa: BLE001
        return []
    titles: list[str] = []
    for value in found or []:
        value = value.strip()
        if value and value not in titles:
            titles.append(value)
    return titles


def open_chat(page, chat: str) -> None:
    """Search for `chat`, open it, and verify the header. Raises on any mismatch."""
    search = first_selector(page, "search")
    page.click(search)
    page.fill(search, "")
    page.keyboard.insert_text(chat)
    page.wait_for_timeout(1_500)
    page.keyboard.press("Enter")
    first_selector(page, "composer")
    titles = conversation_titles(page)
    if not titles:
        raise WhatsAppError(f"could not read the conversation header for {chat!r}")
    if not header_matches(titles, chat):
        shown = ", ".join(repr(t) for t in titles[:3])
        raise WhatsAppError(f"header reads {shown}, expected {chat!r}; refusing to continue")
