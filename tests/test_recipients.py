import pytest

from wa import recipients as wa


class FakePage:
    """A duck-typed Playwright page. `visible` decides which selectors resolve."""

    def __init__(self, visible=(), title=None):
        self.visible = set(visible)
        self.title = title
        self.typed: list[str] = []
        self.keys: list[str] = []
        self.goto_urls: list[str] = []
        self.composer_text = ""
        # Tests about confirmation override this; everything else should not
        # pay the full poll timeout just to type a message.
        self.status_icons = ["tail-out", " Delivered "]
        self.status_id = 0
        self.phone_error = ""

    def wait_for_selector(self, selector, timeout=None, state=None):
        if selector not in self.visible:
            raise TimeoutError(f"no {selector}")
        return object()

    def goto(self, url, timeout=None):
        self.goto_urls.append(url)

    def click(self, selector):
        pass

    def fill(self, selector, value):
        pass

    def get_attribute(self, selector, attr):
        return self.title

    def eval_on_selector_all(self, selector, script):
        # Mirrors the real shape: one [title, text] pair per header span.
        return [["", self.title]] if self.title else []

    def wait_for_timeout(self, ms):
        pass

    def wait_for_function(self, script, arg=None, timeout=None):
        return True

    def evaluate(self, script, arg=None):
        """Stands in for the per-message status read. `status_icons` is what
        WhatsApp would report for the row containing the probe; None means no
        such row exists yet."""
        if script == wa._PHONE_ERROR_JS:
            return self.phone_error
        icons = getattr(self, "status_icons", None)
        return [{"id": f"message-{self.status_id}", "icons": icons}] if icons is not None else []

    class _Keyboard:
        def __init__(self, page):
            self.page = page

        def insert_text(self, text):
            self.page.typed.append(text)

        def press(self, key):
            self.page.keys.append(key)
            if key == "Enter":
                self.page.status_id += 1

    @property
    def keyboard(self):
        return FakePage._Keyboard(self)


COMPOSER = 'div[contenteditable="true"][data-tab="10"]'
SEARCH = 'div[contenteditable="true"][data-tab="3"]'
TITLE = "header span[title]"


def test_first_match_falls_through_to_a_later_candidate():
    """Selector rot should cost one fallback, not the channel."""
    second = wa._SELECTORS["composer"][1]
    page = FakePage(visible={second})
    assert wa._first_match(page, "composer", timeout=1) == second


def test_first_match_raises_when_nothing_resolves():
    with pytest.raises(wa.WhatsAppError, match="no selector matched"):
        wa._first_match(FakePage(), "composer", timeout=1)


def test_first_match_separates_a_dead_browser_from_selector_rot():
    """A crashed tab must not read as "no selector matched".

    That message points at _SELECTORS, so a healthy session gets diagnosed as
    DOM rot and the real cause (the browser going away) goes unlooked-for.
    """

    class _Dead(FakePage):
        def wait_for_selector(self, selector, timeout=None, state=None):
            raise RuntimeError("Target page, context or browser has been closed")

    with pytest.raises(wa.WhatsAppError, match="browser died") as caught:
        wa._first_match(_Dead(), "composer", timeout=1)
    assert "no selector matched" not in str(caught.value)


def test_first_match_reports_a_dead_session_instead_of_selector_rot():
    """The 2026-08-18 misdiagnosis, in one test.

    WhatsApp drops a linked device without warning and swaps the app back to
    the QR screen, where every selector legitimately fails. Reporting that as
    "no selector matched for 'search'" sent the operator to _SELECTORS, which
    were fine, while the digest kept failing for a reason no one was looking at.
    """
    page = FakePage(visible={wa._SELECTORS["qr"][0]})

    with pytest.raises(wa.WhatsAppLoggedOut) as caught:
        wa._first_match(page, "search", timeout=1)

    assert "wa login" in str(caught.value)
    assert "no selector matched" not in str(caught.value)


def test_first_match_still_reports_selector_rot_on_a_live_session():
    """The logged-out check must not swallow the failure it was added beside:
    a signed-in page whose role matches nothing is real DOM rot."""
    page = FakePage(visible={"#pane-side"})

    with pytest.raises(wa.WhatsAppError, match="no selector matched") as caught:
        wa._first_match(page, "search", timeout=1)

    assert not isinstance(caught.value, wa.WhatsAppLoggedOut)


def test_is_logged_out_detects_the_qr_screen():
    assert wa._is_logged_out(FakePage(visible={wa._SELECTORS["qr"][0]})) is True


def test_is_logged_out_is_false_when_signed_in():
    assert wa._is_logged_out(FakePage(visible={"#pane-side"})) is False


def test_open_conversation_by_phone_uses_the_deeplink():
    page = FakePage(visible={COMPOSER}, title="+1 (555) 123-4567")
    ok, label = wa._open_conversation(page, {"phone": "+1 555 123-4567", "label": "Jane"})
    assert ok and label == "Jane"
    assert page.goto_urls == ["https://web.whatsapp.com/send?phone=15551234567"]


def test_a_malformed_phone_is_rejected_before_navigation():
    page = FakePage(visible={COMPOSER})
    ok, detail = wa._open_conversation(page, {"phone": "not-a-number", "label": "Bad"})
    assert not ok and "invalid phone" in detail
    assert page.goto_urls == []


def test_an_unregistered_phone_cannot_fall_through_to_the_previous_chat():
    """WhatsApp leaves the old composer behind its error dialog. Seeing that
    composer must not authorize a send to whichever chat happened to be open."""
    page = FakePage(visible={COMPOSER})
    page.phone_error = "isn't on whatsapp"
    ok, detail = wa._open_conversation(page, {"phone": "+15551234567", "label": "Missing"})
    assert not ok and "did not open" in detail


def test_open_conversation_by_chat_searches_and_verifies_the_header():
    page = FakePage(visible={SEARCH, COMPOSER, TITLE}, title="SL5 Intel")
    ok, label = wa._open_conversation(page, {"chat": "SL5 Intel"})
    assert ok and label == "SL5 Intel"
    assert page.typed == ["SL5 Intel"] and "Enter" in page.keys


def test_a_header_mismatch_aborts_without_touching_the_composer():
    """The real hazard. Search-by-name is ambiguous, and sending a digest into
    the wrong chat is worse than not sending it."""
    page = FakePage(visible={SEARCH, COMPOSER, TITLE}, title="Fixture Group")
    ok, detail = wa._open_conversation(page, {"chat": "SL5 Intel"})
    assert not ok
    assert "refusing to send" in detail
    assert page.keys.count("Enter") == 1  # the search Enter, not a send


def test_a_conversation_that_never_opens_is_reported():
    page = FakePage(visible=set())
    ok, detail = wa._open_conversation(page, {"phone": "+15551234567", "label": "Jane"})
    assert not ok and "did not open" in detail


def test_an_unreadable_header_aborts():
    page = FakePage(visible={SEARCH, COMPOSER}, title=None)
    ok, detail = wa._open_conversation(page, {"chat": "SL5 Intel"})
    assert not ok and "conversation header" in detail


def test_a_recipient_with_neither_phone_nor_chat_is_rejected():
    ok, detail = wa._open_conversation(FakePage(), {"label": "nobody"})
    assert not ok and "neither phone nor chat" in detail


def test_type_and_send_uses_insert_text_never_type():
    """insert_text goes through CDP Input.insertText and handles non-BMP
    correctly. keyboard.type() dispatches per-character key events and garbles
    🇨🇳 (a regional-indicator pair), 1️⃣ (a keycap sequence) and 🏗️ (a
    variation selector), all of which are in every digest."""
    page = FakePage(visible={COMPOSER})
    # FakePage's keyboard deliberately has no type(); calling it would raise
    # AttributeError, so reaching the assertion IS the proof.
    assert not hasattr(page.keyboard, "type")
    wa._type_and_send(page, "🇨🇳 China (3)")
    assert page.typed == ["🇨🇳 China (3)"]


def test_type_and_send_uses_shift_enter_between_lines_and_enter_once():
    """Enter sends, so a multi-line chunk typed naively sends line by line."""
    page = FakePage(visible={COMPOSER})
    wa._type_and_send(page, "line one\nline two\nline three")
    assert page.typed == ["line one", "line two", "line three"]
    assert page.keys == ["Shift+Enter", "Shift+Enter", "Enter"]


def test_type_and_send_handles_blank_lines():
    page = FakePage(visible={COMPOSER})
    wa._type_and_send(page, "header\n\nbody")
    assert page.typed == ["header", "body"]
    assert page.keys == ["Shift+Enter", "Shift+Enter", "Enter"]


# ── check() ────────────────────────────────────────────────────────────────


def test_header_name_is_read_from_text_not_just_the_title_attribute():
    """WhatsApp moved the chat name out of the header's `title` attribute and
    into a span's text; `title` now carries a tooltip ("click here for group
    info"). Reading only the attribute refused every group send as a
    mismatch."""

    class _Page(FakePage):
        def eval_on_selector_all(self, selector, script):
            return [
                ["click here for group info", "click here for group info"],
                ["", "Deal Friends & C - SUITE"],
            ]

    page = _Page(visible={SEARCH, COMPOSER})
    ok, label = wa._open_conversation(page, {"chat": "Deal Friends & C - SUITE"})
    assert ok and label == "Deal Friends & C - SUITE"


def test_a_header_with_no_readable_name_refuses():
    class _Page(FakePage):
        def eval_on_selector_all(self, selector, script):
            return []

    ok, detail = wa._open_conversation(_Page(visible={SEARCH, COMPOSER}), {"chat": "Whatever"})
    assert not ok and "could not read" in detail


def test_send_succeeds_only_when_whatsapp_marks_it_sent():
    """Neither an empty composer nor the text being visible proves anything: a
    REJECTED message still renders in the thread. Only WhatsApp's own status
    counts."""
    page = FakePage(visible={COMPOSER})
    page.status_icons = ["tail-out", " Delivered "]
    assert wa._type_and_send(page, "*LabWatch Daily Report* — 1 new signals") is True


def test_a_rejected_message_is_a_failure_even_though_it_renders():
    """The bug this exists for. A 4-chunk burst had chunks 1-3 Delivered and
    chunk 4 marked "Something went wrong"; all four were reported as sent, and
    the message sat visibly in the thread having never been transmitted."""
    page = FakePage(visible={COMPOSER})
    page.status_icons = ["tail-out", "Something went wrong. Click to learn more.", "You:"]
    assert wa._type_and_send(page, "*LabWatch Daily Report* — 1 new signals") is False


def test_a_message_that_never_gets_a_status_is_a_failure():
    page = FakePage(visible={COMPOSER})
    page.status_icons = None
    assert wa._confirm_sent(page, "probe", timeout_ms=300) is False


def test_a_pending_message_is_not_yet_a_success():
    page = FakePage(visible={COMPOSER})
    page.status_icons = ["tail-out", " Pending "]
    assert wa._confirm_sent(page, "probe", timeout_ms=300) is False


def test_a_stale_delivered_row_cannot_confirm_a_new_send():
    """Daily report headers repeat. Until a new row exists, yesterday's matching
    delivered row must be ignored."""
    page = FakePage(visible={COMPOSER})
    page.status_icons = ["tail-out", "Delivered"]
    assert wa._confirm_sent(page, "probe", timeout_ms=300, previous_ids={"message-0"}) is False


def test_not_sent_is_not_a_sent_marker_substring():
    page = FakePage(visible={COMPOSER})
    page.status_icons = ["Message not sent"]
    assert wa._confirm_sent(page, "probe", timeout_ms=300) is False


def test_data_icon_sent_marker_is_accepted():
    page = FakePage(visible={COMPOSER})
    page.status_icons = ["tail-out", "msg-check"]
    assert wa._confirm_sent(page, "probe", timeout_ms=300) is True


def test_the_probe_strips_markup_because_the_thread_renders_it():
    """We type *bold*; the thread shows bold. Matching on the raw chunk would
    never find it."""
    assert wa._match_probe("*LabWatch Daily Report* — 3 signals").startswith("LabWatch Daily Report")
    assert "*" not in wa._match_probe("*LabWatch Daily Report* — 3 signals")


def test_the_probe_skips_short_leading_lines():
    assert wa._match_probe("(1/4)\n*LabWatch Daily Report*").startswith("LabWatch Daily Report")


def test_the_probe_strips_emoji_because_they_render_as_images():
    """WhatsApp swaps emoji for <img alt="🔴">, and innerText excludes alt text.

    Every severity heading leads with one, so a probe that kept the emoji could
    never match the row it came from — which made every chunk after the first
    unconfirmable, and stopped the rest of the digest from being sent at all.
    """
    probe = wa._match_probe("(2/5)\n🔴 *CRITICAL (48)*\n  • openai new_url")
    assert probe == "CRITICAL (48)"

    assert wa._match_probe("(5/5)\n🟡 *IMPORTANT (7)*\n  • mistral") == "IMPORTANT (7)"
    assert wa._match_probe("(3/5)\n🔴 *CRITICAL (48)* (cont.)") == "CRITICAL (48) (cont.)"


def test_the_probe_falls_through_a_heading_that_is_only_emoji_and_a_short_label():
    """'🟢 *INFO (3)*' is 8 characters once the emoji goes, under the floor, so
    the next line has to carry the probe."""
    body = f"  • openai new_{wa.ZWSP}url — https://openai.com/x"
    probe = wa._match_probe(f"(4/5)\n🟢 *INFO (3)*\n{body}")
    assert probe.startswith(f"• openai new_{wa.ZWSP}url")


def test_the_probe_keeps_an_escaped_delimiter_but_drops_an_emphasis_one():
    """markup.py marks a literal delimiter by following it with a ZWSP. The
    thread renders that one and eats the other, so the probe must too."""
    literal = f"  • openai new_{wa.ZWSP}url — https://openai.com/index/x"
    assert f"new_{wa.ZWSP}url" in wa._match_probe(literal)

    emphasis = "    _OpenAI publishes an abuse-disruption report_"
    assert wa._match_probe(emphasis).startswith("OpenAI publishes")
    assert "_" not in wa._match_probe(emphasis)


# ── a dead browser's profile lock ──────────────────────────────────────────


def _profile_with_lock(tmp_path, target: str | None):
    import os

    profile = tmp_path / "profile"
    profile.mkdir()
    if target is not None:
        os.symlink(target, profile / "SingletonLock")
        (profile / "SingletonCookie").write_text("cookie", encoding="utf-8")
    return profile


def test_phone_recipient_header_showing_the_number_is_accepted():
    page = FakePage(visible={COMPOSER}, title="+1 555-123-4567")
    ok, label = wa._open_conversation(page, {"phone": "+15551234567", "label": "Jane"})
    assert ok and label == "Jane"


def test_phone_recipient_header_showing_the_saved_contact_is_accepted():
    page = FakePage(visible={COMPOSER}, title="Jane Doe")
    ok, _ = wa._open_conversation(page, {"phone": "+15551234567", "label": "Jane", "chat": "Jane Doe"})
    assert ok


def test_phone_recipient_header_matching_the_label_is_accepted():
    page = FakePage(visible={COMPOSER}, title="Jane")
    ok, _ = wa._open_conversation(page, {"phone": "+15551234567", "label": "Jane"})
    assert ok


def test_phone_recipient_in_the_wrong_chat_is_refused():
    """The deeplink can leave the previous chat on screen. Without a header
    check the digest went to whoever that was."""
    page = FakePage(visible={COMPOSER}, title="Fixture Group")
    ok, detail = wa._open_conversation(page, {"phone": "+15551234567", "label": "Jane"})
    assert not ok
    assert "header reads 'Fixture Group', expected '+15551234567' — refusing to send" == detail
    assert "Enter" not in page.keys


def test_phone_recipient_with_an_unreadable_header_is_refused():
    page = FakePage(visible={COMPOSER}, title=None)
    ok, detail = wa._open_conversation(page, {"phone": "+15551234567", "label": "Jane"})
    assert not ok and "conversation header" in detail


def test_phone_digits_helper():
    assert wa._phone_digits("+1 (555) 123-4567") == "15551234567"
    assert wa._phone_digits("call-me") is None
    assert wa._phone_digits("123") is None


# ── chunks with nothing distinctive to probe ───────────────────────────────


def test_a_chunk_with_no_long_line_is_not_assumed_sent(monkeypatch):
    """It used to return True without looking. Now a new row with a sent mark
    has to appear, or the chunk is unconfirmed."""
    monkeypatch.setattr(wa, "CONFIRM_TIMEOUT", 300)
    page = FakePage(visible={COMPOSER})
    page.status_icons = None
    assert wa._type_and_send(page, "🔴\n(1/4)") is False


def test_a_chunk_with_no_long_line_is_confirmed_by_a_new_sent_row():
    page = FakePage(visible={COMPOSER})
    page.status_icons = ["Delivered"]
    assert wa._type_and_send(page, "🔴") is True


def test_short_lines_still_yield_a_probe():
    assert wa._match_probe("(1/4)\nhi") == "(1/4)"
    assert wa._match_probe("🔴") == ""


def test_an_unreadable_thread_refuses_to_confirm_a_probe_less_chunk():
    class _Blind(FakePage):
        def evaluate(self, script, arg=None):
            raise RuntimeError("detached")

    assert wa._type_and_send(_Blind(visible={COMPOSER}), "🔴") is False
