"""Offline checks for `wa-files`: row parsing, dates, filtering. No browser."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from wa import files as wa_files
from wa.files import FileItem

TODAY = date(2026, 11, 13)  # a Friday


def test_parse_label() -> None:
    assert wa_files.parse_label('Download "Factura 12.pdf"') == ("Factura 12.pdf", False)
    assert wa_files.parse_label('View "plan.pdf"') == ("plan.pdf", True)
    assert wa_files.parse_label("Download") is None
    assert wa_files.parse_label("React") is None


def test_parse_pre_and_size() -> None:
    assert wa_files.parse_pre("[10:32 AM, 11/12/2026] Test Contact: ") == (
        date(2026, 11, 12),
        "10:32 AM",
        "Test Contact",
    )
    assert wa_files.parse_pre("garbage") == (None, "", "")
    assert wa_files.parse_size("PDF • 3 pages • 245 kB 10:32 AM") == "245 kB"
    assert wa_files.parse_size("xlsx • 1.2 MB") == "1.2 MB"
    assert wa_files.parse_size("no size") == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("TODAY", date(2026, 11, 13)),
        ("Yesterday", date(2026, 11, 12)),
        ("MONDAY", date(2026, 11, 9)),
        ("FRIDAY", date(2026, 11, 6)),
        ("11/2/2026", date(2026, 11, 2)),
        ("November 2, 2026", date(2026, 11, 2)),
        ("hola", None),
    ],
)
def test_parse_divider(text: str, expected: date | None) -> None:
    assert wa_files.parse_divider(text, TODAY) == expected


ROWS = [
    {"id": "top-img", "pre": "", "labels": [], "images": 1, "text": "9:00 AM"},
    {"divider": "YESTERDAY"},
    {
        "id": "doc1",
        "pre": "[9:15 AM, 11/12/2026] Ana: ",
        "labels": ['View "plan.pdf"', 'View "plan.pdf"'],
        "images": 0,
        "text": "plan.pdf PDF • 2 pages • 180 kB 9:15 AM",
    },
    {"divider": "TODAY"},
    {"id": "img1", "pre": "", "labels": [], "images": 1, "text": "11:02 AM"},
    {
        "id": "reply",
        "pre": "[11:05 AM, 11/13/2026] Test Member: ",
        "labels": [],
        "images": 0,
        "quoted_doc": True,
        "text": "Document\nya lo vi",
    },
    {
        "id": "doc2",
        "pre": "[11:10 AM, 11/13/2026] Test Member: ",
        "labels": ['Download "costos.xlsx"'],
        "images": 0,
        "text": "costos.xlsx XLSX • 12 kB",
    },
]


def test_items_from_rows() -> None:
    items = wa_files.items_from_rows(ROWS, TODAY)
    by_id = {it.id: it for it in items}
    assert set(by_id) == {"top-img", "doc1", "img1", "doc2"}  # quoted reply isn't a file
    assert by_id["top-img"].day is None  # nothing dated above it yet
    assert by_id["doc1"].viewer and by_id["doc1"].type == "pdf"
    assert (by_id["doc1"].sender, by_id["doc1"].size) == ("Ana", "180 kB")
    assert by_id["img1"].day == TODAY and by_id["img1"].time == "11:02 AM"
    assert by_id["img1"].type == "image"
    assert not by_id["doc2"].viewer and by_id["doc2"].type == "xlsx"


def test_merge_fills_missing_day() -> None:
    known: dict[str, FileItem] = {}
    assert wa_files.merge(known, wa_files.items_from_rows(ROWS, TODAY)) == 4
    later = [{"divider": "11/11/2026"}, ROWS[0]]
    assert wa_files.merge(known, wa_files.items_from_rows(later, TODAY)) == 0
    assert known["top-img"].day == date(2026, 11, 11)


def test_in_range_keeps_undated() -> None:
    items = wa_files.items_from_rows(ROWS, TODAY)
    kept = {it.id for it in wa_files.in_range(items, TODAY)}
    assert kept == {"top-img", "img1", "doc2"}


def test_out_dir_default_and_repo_guard(tmp_path: Path) -> None:
    default = wa_files.out_dir(None, "Team Test")
    assert default.parent == Path("~/Downloads/whatsapp").expanduser()
    assert "/" not in default.name
    assert wa_files.out_dir(str(tmp_path), "g") == tmp_path
    with pytest.raises(ValueError, match="inside the repo"):
        wa_files.out_dir(str(wa_files.REPO_ROOT / "video" / "x"), "g")


def test_already_saved(tmp_path: Path) -> None:
    doc = FileItem("d", "document", "a/b.pdf", TODAY, "1:00 PM", "", "", False)
    img = FileItem("abcdefghij", "image", "", TODAY, "1:00 PM", "", "", False)
    assert not wa_files.already_saved(doc, tmp_path)
    (tmp_path / "a_b.pdf").write_text("x")
    (tmp_path / (img.target_name() + ".jpeg")).write_text("x")
    assert wa_files.already_saved(doc, tmp_path)
    assert wa_files.already_saved(img, tmp_path)


def test_format_table() -> None:
    out = wa_files.format_table(wa_files.items_from_rows(ROWS, TODAY))
    assert out.splitlines()[0].split() == ["date", "time", "sender", "type", "size", "name"]
    assert "costos.xlsx" in out and "(image)" in out


def test_row_sender() -> None:
    assert wa_files.row_sender({"sender": "Maybe Contact"}) == "Contact"
    assert wa_files.row_sender({"sender": "", "outgoing": True}) == "You"
    assert wa_files.row_sender({}) == ""
    rows = [
        {
            "id": "x",
            "pre": "",
            "sender": "Maybe Contact",
            "labels": ['View "o.pdf"'],
            "text": "PDF•678 kB\n1:31 PM",
        }
    ]
    (item,) = wa_files.items_from_rows(rows, TODAY)
    assert (item.sender, item.size, item.time) == ("Contact", "678 kB", "1:31 PM")
