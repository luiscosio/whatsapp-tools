"""Open verified recipients by group title or phone and confirm each new chunk."""

import logging
import time
import unicodedata

from .browser import session_page
from .session import WhatsAppError as BaseError
from .session import profile_path

ZWSP = "\u200b"
log = logging.getLogger(__name__)
WEB_URL = "https://web.whatsapp.com"

#: Milliseconds. Generous: the web client is slow to hydrate on a cold start.
LOAD_TIMEOUT = 60_000
ACTION_TIMEOUT = 15_000

#: Milliseconds to wait for WhatsApp to acknowledge a sent message. Separate
#: from ACTION_TIMEOUT, which bounds finding an element that is either there or
#: not: this bounds the client rendering a message we already pressed Enter on,
#: and a long digest takes visibly longer to appear than a short one. Borrowing
#: the 15s element budget made big digests report themselves undelivered while
#: sitting in the group, which is the worst of both answers.
CONFIRM_TIMEOUT = 60_000

#: Seconds between chunks. Bursts look like automation, and WhatsApp answers
#: that by silently refusing later messages: a 4-chunk burst delivered 1-3 and
#: rejected the 4th with "Something went wrong". Slower is the whole mitigation.
CHUNK_DELAY = 4.0

#: Seconds between recipients, for the same reason.
RECIPIENT_DELAY = 6.0

#: Recipients per run. A digest going to a dozen chats is a configuration
#: mistake, and this bounds the damage.
MAX_RECIPIENTS = 10

# WhatsApp ships obfuscated class names and removed production data-testid
# attributes years ago. These are the semi-stable anchors, each an ORDERED list
# of candidates so a single rename degrades instead of breaking outright. This
# dict is the ONE place to fix when the DOM shifts; a role that matches nothing
# is reported as source_health.EMPTY ("extraction broken?"), which is how
# selector rot reaches a human instead of turning into silence.
#
# locale is pinned to en-US at launch because aria-label values are localized.
_SELECTORS = {
    "chat_list": ["#pane-side", 'div[aria-label="Chat list"]'],
    # The login screen's own markers. `data-testid` survives here even though
    # WhatsApp stripped it from the chat UI; the container and its canvas both
    # carry the QR payload, confirmed 2026-08-18. The canvas aria-label reads
    # "Scan this QR code to link a device!", so match on "QR" rather than the
    # whole sentence. `qrcode` is the pre-2026 id, kept as a last resort.
    "qr": [
        '[data-testid="link-device-qr-code"]',
        'canvas[aria-label*="QR"]',
        "div[data-ref]",
        '[data-testid="qrcode"]',
    ],
    # WhatsApp swapped search from a contenteditable div to a real <input>
    # (observed 2026-07-25); data-tab="3" survived the change, the element type
    # did not. Input forms first, the old div forms kept as fallbacks.
    "search": [
        'input[data-tab="3"]',
        'input[role="textbox"][aria-label*="Search" i]',
        'input[placeholder*="Search" i]',
        'div[contenteditable="true"][data-tab="3"]',
        'div[role="textbox"][title="Search input textbox"]',
    ],
    "composer": [
        'div[contenteditable="true"][data-tab="10"]',
        'footer div[contenteditable="true"]',
        'div[role="textbox"][aria-label*="message"]',
    ],
    "conversation_title": ["header span[title]", "#main header span[title]"],
    "outgoing": ["div.message-out", 'div[data-id*="true_"]'],
}

_PHONE_ERROR_JS = """
() => {
  const text = [...document.querySelectorAll('[role="dialog"]')]
    .map(e => e.innerText || '').join(' ').toLowerCase();
  const markers = [
    'phone number shared via url is invalid',
    "isn't on whatsapp",
    'is not on whatsapp',
    'not a valid phone number',
  ];
  return markers.find(marker => text.includes(marker)) || '';
}
"""

# Close controls only. Never a "Continue"/"Allow"/"OK" button: this runs against
# whatever dialog WhatsApp happens to show, and agreeing to an unread prompt on
# the user's account is worse than a failed send.
_DIALOG_CLOSE = [
    '[role="dialog"] [aria-label="Close"]',
    '[role="dialog"] button[aria-label*="close" i]',
    '[role="dialog"] [data-icon="x"]',
    '[role="dialog"] [data-icon="x-alt"]',
]

_MODAL_TEXT_JS = """
() => {
  const d = [...document.querySelectorAll('[role="dialog"][aria-modal="true"]')]
    .find(e => e.offsetParent !== null);
  return d ? (d.innerText || '').trim().split('\\n')[0] : '';
}
"""


class WhatsAppError(BaseError):
    """Raised inside the browser helpers; never escapes the channel."""


class WhatsAppLoggedOut(WhatsAppError):
    """The session is gone and only a human with the phone can restore it.

    Its own type because every other failure here is worth retrying and this
    one is not: the next run, and the one after, fail identically until
    somebody scans a QR code.
    """


#: One wording for every place that reports it, because `send()` keys the
#: health status off this exception and the operator keys their next move off
#: the sentence.
_LOGGED_OUT = "logged out; run `wa login` and scan the QR"


def _first_match(page, role: str, timeout: int = ACTION_TIMEOUT):
    """Return the first selector for `role` that resolves, or raise.

    Tries candidates in order so a rename costs one fallback rather than the
    whole channel.
    """
    last = None
    for selector in _SELECTORS[role]:
        try:
            page.wait_for_selector(selector, timeout=timeout, state="visible")
            return selector
        except Exception as e:  # noqa: BLE001 (playwright's TimeoutError, duck-typed for tests)
            last = e
            # Only a timeout means "the page answered, and the element is not
            # there". Anything else means the page never got to answer: the tab
            # or the browser died under us. Both used to surface as "no selector
            # matched", which reads as selector rot and sends the next reader to
            # _SELECTORS — the wrong file, and a live session looks broken.
            # Falling through is pointless too: a dead page fails every
            # remaining candidate, one full timeout at a time.
            if type(e).__name__ != "TimeoutError":
                raise WhatsAppError(
                    f"browser died while looking for {role!r} ({type(e).__name__}: {str(e).splitlines()[0]})"
                ) from None
    # Every candidate timed out, which has two very different causes. Selector
    # rot is the rare one. The common one is that the session died: WhatsApp
    # drops a linked device without warning and swaps the whole app back to the
    # QR screen, where of course nothing matches. Reporting that as "no selector
    # matched for 'search'" is how a dead session got triaged as a DOM change
    # on 2026-08-18 — the reader is sent to _SELECTORS, finds nothing wrong,
    # and the digest keeps failing. Ask the page which one it is before naming
    # a cause. The check is skipped for "qr" itself, where it would be circular.
    if role != "qr" and _is_logged_out(page):
        raise WhatsAppLoggedOut(_LOGGED_OUT)
    raise WhatsAppError(f"no selector matched for {role!r} (last: {type(last).__name__})")


def _is_logged_out(page) -> bool:
    """True if the QR screen is showing.

    Raced against the chat list rather than waited on: a logged-out session
    must not present as three consecutive 30-second timeouts, which tells you
    nothing and burns two minutes of a cron run.
    """
    for selector in _SELECTORS["qr"]:
        try:
            page.wait_for_selector(selector, timeout=2_000, state="visible")
            return True
        except Exception as exc:
            if type(exc).__name__ != "TimeoutError":
                raise
    return False


def _modal_heading(page) -> str:
    """First line of any visible modal dialog, or '' if none is up."""
    try:
        return page.evaluate(_MODAL_TEXT_JS) or ""
    except Exception:  # noqa: BLE001 (a missing dialog must not fail the send)
        return ""


def _dismiss_blocking_dialog(page) -> None:
    """Clear a modal parked over the chat list before anything is clicked.

    Observed 2026-08-11 after a fresh login: a dialog with aria-modal="true"
    outlives the chat-list wait and intercepts every click, so the search box
    times out at Playwright's default 30s and the send dies with a bare
    "Page.click: Timeout" that names nothing. Escape first, then the dialog's
    own close control. Whatever is still standing gets logged by its heading,
    so the next reader adds a selector instead of guessing from a stack trace.

    Runs once, before the recipient loop, on purpose: _open_conversation's
    invalid-number check reads [role="dialog"] itself and has to keep seeing
    the dialog its own deeplink opens.
    """
    for _ in range(3):
        heading = _modal_heading(page)
        if not heading:
            return
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        if not _modal_heading(page):
            log.info(f"whatsapp: dismissed dialog {heading!r}")
            return
        for selector in _DIALOG_CLOSE:
            try:
                page.click(selector, timeout=2_000)
                page.wait_for_timeout(400)
                break
            except Exception as exc:
                if type(exc).__name__ != "TimeoutError":
                    raise
        if not _modal_heading(page):
            log.info(f"whatsapp: dismissed dialog {heading!r}")
            return
    still_up = _modal_heading(page)
    if still_up:
        log.warning(
            f"whatsapp: a modal dialog is still over the UI ({still_up!r}); "
            "clicks will time out. Add its close control to _DIALOG_CLOSE."
        )


def _conversation_titles(page) -> list[str]:
    """Every candidate name in the open conversation's header.

    Returns a list rather than one string on purpose. WhatsApp moved the chat
    name out of the header's `title` attribute and into a span's text
    (observed 2026-07-25); `title` now carries a tooltip, so the old read
    returned "click here for group info" and the send was refused as a
    mismatch. Collecting every candidate and asking whether the expected name
    is among them survives that kind of shuffle, where betting on one selector
    and one attribute does not.
    """
    try:
        found = (
            page.eval_on_selector_all(
                "#main header span",
                "els => els.map(e => ((e.getAttribute('title') || '') + '\\u0000' + "
                "(e.textContent || '')).split('\\u0000'))",
            )
            or []
        )
    except Exception:  # noqa: BLE001 (an unreadable header is a refusal, not a crash)
        return []
    titles = []
    for pair in found:
        for value in pair if isinstance(pair, list) else [pair]:
            value = (value or "").strip()
            if value and value not in titles:
                titles.append(value)
    return titles


def _phone_digits(phone) -> str | None:
    """The digits of a configured phone number, or None when there are not
    7-15 of them (E.164 allows at most 15)."""
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    return digits if 7 <= len(digits) <= 15 else None


def _header_matches(titles: list[str], *, chat: str, label: str, digits: str | None) -> bool:
    """Whether the open chat's header names the recipient we meant.

    A named chat must appear verbatim. A phone recipient's header shows either
    the number itself (compared digit for digit, so formatting does not
    matter) or the saved contact name, which is accepted when it equals the
    configured `chat` or `label`.
    """
    titles = [unicodedata.normalize("NFC", value) for value in titles]
    chat = unicodedata.normalize("NFC", chat)
    label = unicodedata.normalize("NFC", label)
    if chat and chat in titles:
        return True
    if digits is None:
        return False
    if label and label in titles:
        return True
    return any("".join(c for c in t if c.isdigit()) == digits for t in titles)


def _open_conversation(page, recipient: dict) -> tuple[bool, str]:
    """Open `recipient`'s chat. Returns (ok, detail).

    Verifies the conversation header before returning, for phone and named
    recipients alike. This is the real hazard: a `?phone=` deeplink can leave
    the previous chat on screen, and search-by-name is ambiguous when two
    chats have similar names. Sending into the wrong chat is worse than not
    sending at all, so we never type into a composer we have not verified.
    """
    phone = str(recipient.get("phone") or "").strip()
    chat = str(recipient.get("chat") or "").strip()
    label = str(recipient.get("label") or chat or phone).strip()
    digits = None

    if phone:
        digits = _phone_digits(phone)
        if digits is None:
            return False, f"invalid phone number for {label}: expected 7-15 digits"
        page.goto(f"{WEB_URL}/send?phone={digits}", timeout=LOAD_TIMEOUT)
        # WhatsApp can leave the previous chat visible behind its invalid-number
        # dialog. A composer alone is therefore not proof that THIS deeplink
        # opened; sending through it would target the previous conversation.
        page.wait_for_timeout(1_200)
        try:
            phone_error = page.evaluate(_PHONE_ERROR_JS)
        except Exception:  # noqa: BLE001 (the composer/header checks still fail soft)
            phone_error = ""
        if phone_error:
            return False, f"conversation did not open for {label}: {phone_error}"
    elif chat:
        search = _first_match(page, "search")
        page.click(search)
        page.fill(search, "")
        page.keyboard.insert_text(chat)
        page.wait_for_timeout(1_200)  # let the result list settle
        page.keyboard.press("Enter")
    else:
        return False, "recipient has neither phone nor chat"

    try:
        _first_match(page, "composer")
    except WhatsAppLoggedOut:
        # Not this recipient's problem, and folding it into a per-recipient
        # failure buries it: the delivery line would read "0/1 recipient(s)"
        # and the one fact that matters — nobody can send anything until a
        # human scans a QR — survives only in a warning nobody reads.
        raise
    except WhatsAppError as e:
        return False, f"conversation did not open for {label}: {e}"

    titles = _conversation_titles(page)
    if not titles:
        return False, f"could not read the conversation header for {label}"
    if not _header_matches(titles, chat=chat, label=label, digits=digits):
        shown = ", ".join(repr(t) for t in titles[:3])
        expected = chat or phone
        # The wording is load-bearing: a scheduled routine greps for it.
        return False, f"header reads {shown}, expected {expected!r} — refusing to send"

    return True, label


def _type_and_send(page, chunk: str) -> bool:
    """Type one chunk into the open conversation and send it.

    Line by line with Shift+Enter, because Enter sends. insert_text goes
    through CDP Input.insertText and handles the digest's non-BMP content
    correctly: 🇨🇳 is a regional-indicator pair, 1️⃣ a keycap sequence, 🏗️
    carries a variation selector, and keyboard.type() garbles all three.
    """
    probe = _match_probe(chunk)
    # The same digest header appears every day. Snapshot the matching row ids
    # before typing so yesterday's delivered row cannot make today's failed
    # send look successful while the new row is still absent. An empty probe
    # matches every row, so a chunk with nothing distinctive in it is still
    # confirmed: by a new row appearing and reaching a sent state.
    previous_ids = _matching_row_ids(page, probe)

    composer = _first_match(page, "composer")
    page.click(composer)

    lines = chunk.split("\n")
    for n, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
        if n < len(lines) - 1:
            page.keyboard.press("Shift+Enter")
    page.keyboard.press("Enter")

    if previous_ids is None:
        log.warning(
            "whatsapp: could not snapshot existing messages; refusing to claim this send was confirmed"
        )
        return False
    return _confirm_sent(page, probe, CONFIRM_TIMEOUT, previous_ids=previous_ids)


#: WhatsApp's own per-message status, read from the row's aria-labels. A failed
#: send still RENDERS in the thread, which is what made every text-based check
#: useless: the message is visibly there and was never transmitted.
_SENT_MARKERS = frozenset({"sent", "delivered", "read", "msg-check", "msg-dblcheck"})
_FAILED_MARKERS = ("went wrong", "not sent", "failed to send", "msg-error")


def _matching_rows(page, probe: str) -> list[dict] | None:
    """Message rows containing `probe`, or None when the DOM read itself fails."""
    try:
        rows = page.evaluate(_STATUS_JS, probe)
    except Exception:  # noqa: BLE001
        return None
    return rows if isinstance(rows, list) else []


def _matching_row_ids(page, probe: str) -> set[str] | None:
    rows = _matching_rows(page, probe)
    if rows is None:
        return None
    return {str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id") is not None}


def _confirm_sent(
    page, probe: str, timeout_ms: int = CONFIRM_TIMEOUT, *, previous_ids: set[str] | None = None
) -> bool:
    """True only once WhatsApp itself marks the message sent.

    Polls the row containing `probe` for its status icon. Three outcomes:
    an error label ("Something went wrong. Click to learn more.") fails
    immediately, a sent/delivered/read label succeeds, and a pending clock
    keeps waiting until the timeout.
    """
    previous_ids = previous_ids or set()
    deadline = time.monotonic() + timeout_ms / 1000
    last = "no matching row"
    while time.monotonic() < deadline:
        states = _matching_rows(page, probe)
        if states:
            for state in reversed(states):
                if not isinstance(state, dict):
                    continue
                row_id = str(state.get("id") or "")
                if not row_id or row_id in previous_ids:
                    continue
                labels = tuple(
                    str(label).strip().lower() for label in (state.get("icons") or []) if str(label).strip()
                )
                combined = " ".join(labels)
                if any(marker in combined for marker in _FAILED_MARKERS):
                    log.warning(f"whatsapp: WhatsApp rejected the message ({combined.strip()})")
                    return False
                if any(label in _SENT_MARKERS for label in labels):
                    return True
                last = combined or "pending"
                break
        page.wait_for_timeout(500)
    log.warning(f"whatsapp: send not confirmed within {timeout_ms}ms (last state: {last})")
    return False


#: Kept as a module constant so the quoting stays sane and the fake page in the
#: tests can recognise it.
_STATUS_JS = """
(needle) => {
  const candidates = [...document.querySelectorAll('#main div[data-id]')]
    .filter(r => (r.innerText || '').includes(needle));
  // Keep the innermost matching data-id node. Parent containers can include
  // several messages and therefore several status icons, which would let a
  // later delivered message confirm the wrong row.
  const rows = candidates.filter(
    r => !candidates.some(other => other !== r && r.contains(other)));
  const seen = new Set();
  const result = [];
  for (const row of rows) {
    const id = row.getAttribute('data-id');
    if (!id || seen.has(id)) continue;
    seen.add(id);
    result.push({
      id,
      icons: [...row.querySelectorAll('[aria-label],[data-icon]')]
        .map(e => e.getAttribute('aria-label') || e.getAttribute('data-icon'))
        .filter(Boolean),
    });
  }
  return result;
}
"""


def _drop_image_rendered(text: str) -> str:
    """Remove the characters WhatsApp swaps for <img> before they reach a probe.

    Emoji render as <img alt="🔴">, and innerText does NOT include alt text, so
    any probe containing one can never match the row it was taken from. Every
    severity heading starts with one ("🔴 *CRITICAL (48)*"), which silently made
    each chunk after the first unconfirmable — and since the send loop stops at
    the first unconfirmed chunk, the rest of the digest was never sent at all.

    Unicode category So (Symbol, other) covers 🔴 🟡 ⭐ 🌱; VS16 and ZWJ are the
    joiners that decorate them. Zero-width spaces are deliberately KEPT: the
    renderer inserts them to defuse WhatsApp's own markup (new_<ZWSP>url), and
    they survive into innerText, so the probe needs them to match.
    """
    return "".join(ch for ch in text if ch not in ("️", "‍") and unicodedata.category(ch) != "So")


def _strip_markup(line: str) -> str:
    """Drop the delimiters WhatsApp consumes; keep the ones it renders.

    markup.py escapes every literal *_~` in body text by following it with a
    zero-width space, so the two are distinguishable: a delimiter WITH a ZWSP
    after it is text the thread shows, one WITHOUT is emphasis the thread eats.
    Dropping both — which stripping '_' wholesale did — turned an escaped
    'new_<ZWSP>url' into 'new<ZWSP>url' and stopped it matching the rendered row.
    """
    return "".join(ch for i, ch in enumerate(line) if ch not in "*_~`" or line[i + 1 : i + 2] == ZWSP)


def _match_probe(chunk: str) -> str:
    """A short distinctive slice of `chunk` to look for in the conversation.

    Taken from the first substantial line, minus the markup the thread eats and
    the emoji it turns into images, so that what is left is text innerText will
    actually contain. A heading that is only a short label once its emoji is
    gone ("🟢 *INFO (3)*" -> "INFO (3)") falls through to the next line, which
    is what the length floor is for.

    With no line that long, the longest one is used. A short probe is still
    safe because rows that existed before typing are excluded by id. With no
    text at all (a chunk of emoji), the probe is "" and matches any new row.
    """
    fallback = ""
    for line in chunk.splitlines():
        cleaned = _drop_image_rendered(_strip_markup(line.strip())).strip()
        if len(cleaned) >= 12:
            return cleaned[:40]
        if len(cleaned) > len(fallback):
            fallback = cleaned
    return fallback[:40]


def _send_to_recipient(page, recipient: dict, chunks: list[str]) -> tuple[int, bool]:
    """Open one recipient's chat and send every chunk in order.

    Returns (chunks confirmed sent, whether the conversation opened). Stops at
    the first unconfirmed chunk: a digest that breaks off mid-way beats one
    whose pieces arrive out of order or after WhatsApp has started refusing
    them.
    """
    ok, label = _open_conversation(page, recipient)
    if not ok:
        log.warning(f"whatsapp: {label}")
        return 0, False
    for chunk_index, chunk in enumerate(chunks):
        if chunk_index:
            time.sleep(CHUNK_DELAY)
        if not _type_and_send(page, chunk):
            # The conversation IS holding everything up to this point, so say
            # how far it got. A partial send that reports as a flat failure
            # reads as "nobody received anything".
            log.warning(
                f"whatsapp: stopped after {chunk_index}/{len(chunks)} chunk(s) "
                f"to {label}; chunk {chunk_index + 1} was not confirmed. "
                f"The first {chunk_index} are in the conversation."
            )
            return chunk_index, True
    return len(chunks), True


def _send_chunks_sync(
    profile_dir: str,
    recipients: list[dict],
    chunks: list[str],
    *,
    headless: bool = True,
    browser_channel: str = "chrome",
    timeout_ms: int = LOAD_TIMEOUT,
) -> tuple[dict[int, int], str]:
    """Drive the browser. Returns ({recipient index: chunks sent}, detail).

    Keyed by index, not label: two recipients may share a label, and keying by
    it let one's success overwrite the other's failure.

    This is the mock seam, mirroring web_visual._screenshot_targets_sync:
    everything above it (config, rendering, packing, health, audit, capping) is
    tested for real by patching this one function.
    """
    results: dict[int, int] = {}
    #: Chunks reached by recipients whose chat opened but whose send stopped.
    partial: list[int] = []
    with session_page(profile_path(profile_dir), headless=headless, browser_channel=browser_channel) as page:
        page.goto(WEB_URL, timeout=timeout_ms, wait_until="domcontentloaded")
        if _is_logged_out(page):
            raise WhatsAppLoggedOut(_LOGGED_OUT)
        _first_match(page, "chat_list", timeout=timeout_ms)
        _dismiss_blocking_dialog(page)
        for index, recipient in enumerate(recipients):
            if index:
                time.sleep(RECIPIENT_DELAY)
            sent, opened = _send_to_recipient(page, recipient, chunks)
            results[index] = sent
            if opened and sent < len(chunks):
                partial.append(sent)

    delivered = sum(1 for sent in results.values() if sent == len(chunks))
    detail = f"{delivered}/{len(results)} recipient(s)"
    if partial:
        detail += f", stopped after {min(partial)}/{len(chunks)} chunk(s)"
    return results, detail
