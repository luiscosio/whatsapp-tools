"""Pure helpers: timestamps, header matching, markup, chunking. No browser here."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

# `data-pre-plain-text` in the en-US locale: "[10:02 AM, 11/10/2026] Sender Name: "
# (month/day/year; the space before AM/PM can be a narrow no-break space).
_PRE_RE = re.compile(
    r"^\[(\d{1,2}:\d{2}(?:\s*[AP]M)?),\s*(\d{1,2})/(\d{1,2})/(\d{4})\]\s*(.*):\s*$", re.DOTALL
)
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?:\s*[AP]M)?$", re.IGNORECASE)


def _clock(value: str) -> tuple[int, int]:
    value = " ".join(value.split()).upper()
    suffix = value[-2:] if value.endswith(("AM", "PM")) else ""
    hour, minute = (int(x) for x in value.removesuffix(suffix).strip().split(":"))
    if suffix == "PM" and hour != 12:
        hour += 12
    if suffix == "AM" and hour == 12:
        hour = 0
    return hour, minute


def parse_pre_plain(pre: str) -> tuple[datetime, str] | None:
    """Return (datetime, sender) from a data-pre-plain-text value, or None."""
    m = _PRE_RE.match(pre.strip())
    if not m:
        return None
    clock, month, day, year, sender = m.groups()
    try:
        hour, minute = _clock(clock)
        return datetime(int(year), int(month), int(day), hour, minute), sender.strip()
    except ValueError:
        return None


def parse_clock(text: str, on: datetime) -> datetime | None:
    """A bare "11:17 AM" (as shown on media rows) placed on the date of `on`."""
    text = text.strip()
    if not _TIME_RE.match(text):
        return None
    hour, minute = _clock(text)
    return on.replace(hour=hour, minute=minute, second=0, microsecond=0)


def header_matches(titles: list[str], chat: str) -> bool:
    """The open conversation's header must name the chat we meant, verbatim."""
    wanted = chat.strip()
    return bool(wanted) and any(t.strip() == wanted for t in titles)


# --- markdown to WhatsApp markup -------------------------------------------------

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*#*$")


def md_to_whatsapp(text: str) -> str:
    """Convert the Markdown subset used in message files to WhatsApp markup.

    Headings and **bold** become *bold*, [label](url) becomes "label: url" (WhatsApp
    auto-links bare URLs), "* item" bullets become "- item". Everything else is kept,
    including single *bold* and _italic_ that are already WhatsApp-native.
    """
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        heading = _HEADING_RE.match(line)
        if heading:
            line = f"*{heading.group(1)}*"
        line = _LINK_RE.sub(
            lambda m: m.group(2) if m.group(1) == m.group(2) else f"{m.group(1)}: {m.group(2)}", line
        )
        line = _BOLD_RE.sub(lambda m: f"*{m.group(1) or m.group(2)}*", line)
        line = re.sub(r"^(\s*)\*\s+", r"\1- ", line)
        out.append(line.rstrip())
    return "\n".join(out).strip("\n")


def strip_markup(line: str) -> str:
    return re.sub(r"[*_~`]", "", line)


def match_probe(chunk: str) -> str:
    """A short slice of `chunk` that the rendered row's text will contain.

    Markup delimiters are removed (WhatsApp consumes them) and so are emoji
    (rendered as images, absent from innerText). Takes the first line with 12 or
    more characters left, else the longest line.
    """
    fallback = ""
    for line in chunk.splitlines():
        line = re.sub(r"^\s*[-•]\s+", "", line.strip())
        cleaned = "".join(
            ch for ch in strip_markup(line) if ch not in "️‍" and unicodedata.category(ch) != "So"
        ).strip()
        if len(cleaned) >= 12:
            return cleaned[:40]
        if len(cleaned) > len(fallback):
            fallback = cleaned
    return fallback[:40]


# --- chunking --------------------------------------------------------------------

MAX_CHUNK = 3500


def chunk_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split on blank lines, then lines, then spaces, so no chunk exceeds `limit`.

    Lines are never cut mid-word unless a single word is longer than `limit`.
    """
    text = text.strip("\n")
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= limit:
            current = paragraph
            continue
        for piece in _split_long(paragraph, limit):
            if current and len(current) + 1 + len(piece) <= limit:
                current = f"{current}\n{piece}"
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def _split_long(paragraph: str, limit: int) -> list[str]:
    pieces: list[str] = []
    for line in paragraph.split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(line[:cut])
            line = line[cut:].lstrip(" ")
        pieces.append(line)
    return pieces
