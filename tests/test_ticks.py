from wa.sender import ticked as _ticked


def test_sent_delivered_and_read_count():
    assert _ticked(["wds-ic-read"])
    assert _ticked(["msg-dblcheck", "tail-out"])
    assert _ticked(["Delivered"])


def test_substrings_do_not_count():
    assert not _ticked(["unread", "thread", "send"])
    assert not _ticked(["sent-folder-icon"])


def test_still_uploading_does_not_count():
    assert not _ticked(["msg-time", "msg-check"])
    assert not _ticked(["wds-ic-pending"])
    assert not _ticked([])


def test_confirmation_requires_a_matching_row_after_the_last_known_row(monkeypatch):
    from wa import sender

    def row(key, matches, icon):
        return {"id": key, "matches": matches, "icons": [icon]}

    old = row("old", True, "read")
    pages = iter(
        [
            [row("earlier", True, "read"), old, row("other", False, "read")],
            [old, row("new", True, "pending")],
            [row("new", True, "delivered")],
        ]
    )
    polls = []

    class Page:
        def wait_for_timeout(self, ms):
            polls.append(ms)

    monkeypatch.setattr(sender, "_outgoing", lambda page, needle: next(pages))
    sender.confirm_sent(Page(), "Fixture", {"old"}, 1)
    assert len(polls) == 2
