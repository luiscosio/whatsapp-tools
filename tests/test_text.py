from datetime import datetime

import pytest

from wa.reader import parse_rows
from wa.text import chunk_text, header_matches, match_probe, md_to_whatsapp, parse_clock, parse_pre_plain


def test_parse_pre_plain_pm_and_sender():
    assert parse_pre_plain("[10:03 PM, 11/10/2026] +1 202 555 0101: ") == (
        datetime(2026, 11, 10, 22, 3),
        "+1 202 555 0101",
    )


def test_parse_pre_plain_noon_midnight_and_narrow_nbsp():
    assert parse_pre_plain("[12:05 AM, 11/2/2026] Test Contact: ")[0] == datetime(2026, 11, 2, 0, 5)
    assert parse_pre_plain("[12:30 PM, 11/2/2026] Test Contact: ")[0] == datetime(2026, 11, 2, 12, 30)


def test_parse_pre_plain_sender_with_colon_and_24h():
    assert parse_pre_plain("[18:45, 1/5/2026] Ana: Gómez: ") == (datetime(2026, 1, 5, 18, 45), "Ana: Gómez")


@pytest.mark.parametrize("bad", ["", "no brackets", "[10:03 AM, 13/45/2026] X: "])
def test_parse_pre_plain_rejects_garbage(bad):
    assert parse_pre_plain(bad) is None


def test_parse_clock():
    assert parse_clock("11:17 AM", datetime(2026, 11, 10, 9, 0)) == datetime(2026, 11, 10, 11, 17)
    assert parse_clock("Test Contact", datetime(2026, 11, 10)) is None


def test_header_matches_is_exact():
    assert header_matches(["click here for group info", "Team Test"], "Team Test")
    assert not header_matches(["Team Test 2"], "Team Test")
    assert not header_matches([], "x")
    assert not header_matches(["x"], "")


def test_md_to_whatsapp():
    src = "# Title\n\nSome **bold** and [link](https://a.b/c).\n* item\n  * nested\n[https://x.y](https://x.y)\n"
    assert (
        md_to_whatsapp(src)
        == "*Title*\n\nSome *bold* and link: https://a.b/c.\n- item\n  - nested\nhttps://x.y"
    )


def test_match_probe_drops_markup_emoji_and_bullets():
    assert match_probe("🔴 *Resultado de las pruebas* de hoy") == "Resultado de las pruebas de hoy"
    assert (
        match_probe("- *ab*\n- una línea suficientemente larga para ser sonda")
        == "una línea suficientemente larga para ser"
    )
    assert match_probe("🔴") == ""


def test_chunk_text_short_and_empty():
    assert chunk_text("hola") == ["hola"]
    assert chunk_text("\n\n") == []


def test_chunk_text_splits_on_paragraphs_within_limit():
    text = "\n\n".join(["a" * 40] * 5)
    chunks = chunk_text(text, limit=100)
    assert chunks == ["\n\n".join(["a" * 40] * 2)] * 2 + ["a" * 40]


def test_chunk_text_splits_long_paragraph_by_lines_then_words():
    text = "\n".join(["linea " * 10] * 3) + "\n" + "x" * 250
    chunks = chunk_text(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


def _row(**kw):
    base = {"id": "", "pre": "", "text": "", "lines": [], "image": 0, "document": False, "out": False}
    return base | kw


def test_parse_rows_dates_media_and_skips_separators():
    rows = [
        _row(id="a", lines=["Test Contact", "9:55 AM"], image=1),  # before any dated row
        _row(id="sep", lines=["Yesterday"]),  # date separator
        _row(id="b", pre="[10:02 AM, 11/10/2026] Mario: ", text=" Listo "),
        _row(id="c", lines=["1:33 PM"], image=1, out=True),
    ]
    msgs = parse_rows(rows)
    assert [m.id for m in msgs] == ["a", "b", "c"]
    assert msgs[0].when == datetime(2026, 11, 10, 9, 55) and msgs[0].sender == "Test Contact"
    assert msgs[1].text == "Listo"
    assert msgs[2].when == datetime(2026, 11, 10, 13, 33) and msgs[2].sender == "me"
