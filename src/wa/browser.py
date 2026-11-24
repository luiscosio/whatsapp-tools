"""Serialize browser jobs and reuse the Mac's linked Chrome session."""

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright

from .session import WhatsAppError, _launch, _lock_target, prepare_profile

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


@contextmanager
def _job_lock(profile, timeout=300):
    # The OS releases this advisory lock when a job exits, including a crash.
    if os.name == "nt":
        import msvcrt
    else:
        import fcntl

    with (profile / ".whatsapp-tools.lock").open("a+b") as handle:
        if os.name == "nt" and handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise WhatsAppError("Another WhatsApp job still holds the profile lock.")
                time.sleep(0.5)
        yield


def _connect(pw, profile):
    endpoint = profile / "DevToolsActivePort"
    try:
        port, route = endpoint.read_text().splitlines()[:2]
        if not (port.isdigit() and route.startswith("/devtools/browser/")):
            return None
        return pw.chromium.connect_over_cdp(f"ws://127.0.0.1:{int(port)}{route}", timeout=2000)
    except (OSError, ValueError):
        return None
    except Exception as exc:
        if type(exc).__module__.startswith("playwright"):
            return None
        raise


def _close_browser(browser, profile):
    browser.new_browser_cdp_session().send("Browser.close")
    deadline = time.monotonic() + 10
    while (profile / "SingletonLock").is_symlink() and time.monotonic() < deadline:
        time.sleep(0.05)


def _native_chrome(pw, profile, headless):
    browser = _connect(pw, profile)
    worker = profile / ".whatsapp-tools-headless.pid"
    if browser:
        agent = browser.new_browser_cdp_session().send("Browser.getVersion")["userAgent"]
        if ("HeadlessChrome/" in agent) == headless:
            return browser, False
        owner = _lock_target(profile)
        if headless or not (owner and worker.is_file() and worker.read_text().strip() == str(owner[1])):
            raise WhatsAppError("Close the dedicated WhatsApp browser before switching display modes.")
        # An explicit window or QR request can take over our idle background worker.
        _close_browser(browser, profile)
        worker.unlink(missing_ok=True)
    prepare_profile(profile)
    proc = subprocess.Popen(
        [
            str(CHROME),
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            *(["--headless=new", "about:blank"] if headless else ["--no-startup-window"]),
        ],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        browser = _connect(pw, profile)
        if browser:
            if headless:
                worker.write_text(str(proc.pid))
            return browser, True
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    raise WhatsAppError("Could not connect to the dedicated Chrome profile.")


@contextmanager
def session_page(profile, *, headless=True, browser_channel="chrome"):
    profile = Path(profile).expanduser().resolve()
    with _job_lock(profile), sync_playwright() as pw:
        if sys.platform == "darwin" and browser_channel == "chrome" and CHROME.is_file():
            browser, owned = _native_chrome(pw, profile, headless)
            page = browser.contexts[0].new_page()
            try:
                if not headless:
                    page.bring_to_front()
                yield page
            finally:
                if owned and not headless:
                    _close_browser(browser, profile)
                else:
                    page.close()
        else:
            prepare_profile(profile)
            if browser_channel in ("", "chrome"):
                ctx = _launch(pw, profile, headless)
            else:
                ctx = pw.chromium.launch_persistent_context(
                    str(profile), channel=browser_channel, headless=headless, locale="en-US"
                )
            try:
                yield ctx.pages[0] if ctx.pages else ctx.new_page()
            finally:
                ctx.close()
