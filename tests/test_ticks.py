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
