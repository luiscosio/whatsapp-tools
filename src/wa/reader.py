"""Read-only chat reader: expands "Read more", scrolls up, parses message rows."""

from __future__ import annotations

import base64
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError

from wa.text import parse_clock, parse_pre_plain

EXPAND_JS = """
() => {
  const hits = [...document.querySelectorAll('#main span, #main div, #main [role="button"]')]
    .filter(e => e.childElementCount === 0 && /^Read more$/i.test((e.innerText || '').trim()));
  hits.forEach(e => e.click());
  return hits.length;
}
"""

# One entry per message row, in DOM order (oldest first). Text rows carry
# `.copyable-text[data-pre-plain-text]`; media rows without a caption do not.
ROWS_JS = """
() => [...document.querySelectorAll('#main [role="row"]')].map(r => {
  const c = r.querySelector('.copyable-text[data-pre-plain-text]');
  let text = '';
  if (c) {
    const parts = [...c.querySelectorAll('.selectable-text')].filter(e => !e.parentElement.closest('.selectable-text'));
    text = parts.map(part => {
      const clone = part.cloneNode(true);
      clone.querySelectorAll('img[alt]').forEach(i => i.replaceWith(i.alt));
      clone.querySelectorAll('br').forEach(b => b.replaceWith('\\n'));
      return clone.textContent;
    }).join('\\n');
  }
  const holder = r.querySelector('[data-id]');
  const doc = r.querySelector('[data-icon^="document"], [data-icon*="pdf"], [data-icon*="xls"]');
  return {
    id: holder ? holder.getAttribute('data-id') : '',
    pre: c ? c.getAttribute('data-pre-plain-text') : '',
    text,
    lines: (r.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean),
    image: r.querySelectorAll('img[src^="blob:"]').length,
    document: !!doc,
    out: !!r.querySelector('.message-out, [data-icon="tail-out"]'),
  };
})
"""

# Scrolls the message pane up by 70% of its height. The pane is virtualized (only
# ~14 rows are in the DOM), so rows must be collected at every step. Returns
# whether it moved.
SCROLL_JS = """
() => {
  const row = document.querySelector('#main [role="row"]');
  let el = row && row.parentElement;
  while (el && !(el.scrollHeight > el.clientHeight && /auto|scroll/.test(getComputedStyle(el).overflowY))) {
    el = el.parentElement;
  }
  if (!el) return false;
  const before = el.scrollTop;
  el.scrollTop = Math.max(0, before - el.clientHeight * 0.7);
  return el.scrollTop !== before;
}
"""

BLOB_JS = """
async (src) => {
  const blob = await (await fetch(src)).blob();
  const buf = new Uint8Array(await blob.arrayBuffer());
  let s = '';
  for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return {type: blob.type, data: btoa(s)};
}
"""


@dataclass
class Message:
    id: str
    when: datetime | None
    sender: str
    text: str
    media: str = ""  # "image", "document" or ""
    exact_time: bool = True  # False when the date was inherited from a neighbouring row

    def to_json(self) -> dict:
        d = asdict(self)
        d["when"] = self.when.isoformat() if self.when else None
        return d


def parse_rows(rows: list[dict]) -> list[Message]:
    """Turn raw DOM rows (oldest first) into messages; drop date separators and system rows.

    Media rows have no data-pre-plain-text: their time is the bare clock shown in the
    bubble, placed on the date of the nearest dated row before it (or, for rows at the
    top of the loaded window, the first dated row after it).
    """
    messages: list[Message] = []
    waiting: list[tuple[Message, str]] = []  # media rows with no dated row before them yet
    last_date: datetime | None = None
    for row in rows:
        media = "image" if row["image"] else ("document" if row["document"] else "")
        parsed = parse_pre_plain(row["pre"]) if row["pre"] else None
        if parsed:
            when, sender = parsed
            messages.append(Message(row["id"], when, sender, row["text"].strip(), media))
            last_date = when
            for msg, clock in waiting:
                msg.when = parse_clock(clock, when)
            waiting = []
        elif media:
            clock = next((ln for ln in reversed(row["lines"]) if parse_clock(ln, datetime.min)), "")
            sender = "me" if row["out"] else next((ln for ln in row["lines"] if ln != clock), "")
            msg = Message(row["id"], None, sender, f"[{media}]", media, exact_time=False)
            if last_date and clock:
                msg.when = parse_clock(clock, last_date)
            elif clock:
                waiting.append((msg, clock))
            messages.append(msg)
    return messages


def _key(msg: Message) -> str:
    return msg.id or f"{msg.when}|{msg.sender}|{msg.text[:80]}"


def collect(
    page, since: datetime | None, max_scrolls: int, media_folder: Path | None = None
) -> list[Message]:
    """Walk the chat from the newest message upward until `since` is passed or the top is hit.

    The pane is virtualized, so every step's rows are merged by message id. With
    `media_folder`, images and documents inside the range are saved as they come into view.
    """
    # The header and composer mount before WhatsApp hydrates the message list.
    try:
        page.locator("#main [role=row]").first.wait_for(state="attached", timeout=15_000)
    except TimeoutError:
        pass
    found: dict[str, Message] = {}
    attempted: set[str] = set()
    idle = 0
    for step in range(max_scrolls + 1):
        page.evaluate(EXPAND_JS)
        page.wait_for_timeout(400)
        new = 0
        for msg in parse_rows(page.evaluate(ROWS_JS)):
            key = _key(msg)
            if key not in found:
                new += 1
            if key not in found or (found[key].when is None and msg.when):
                found[key] = msg
            in_range = since is None or msg.when is None or msg.when >= since
            if media_folder and msg.media and in_range and key not in attempted:
                attempted.add(key)
                save_one(page, msg, media_folder)
        dated = [m.when for m in found.values() if m.when]
        if since and dated and min(dated) < since:
            break
        moved = page.evaluate(SCROLL_JS)
        page.wait_for_timeout(1_200)
        idle = idle + 1 if (new == 0 or not moved) else 0
        if idle >= 3:  # at the top and WhatsApp loaded nothing older
            break
        if step and step % 10 == 0:
            print(f"wa: step {step}, {len(found)} messages", file=sys.stderr)
    else:
        print(f"wa: stopped after {max_scrolls} scrolls; older messages may remain", file=sys.stderr)
    messages = sorted(found.values(), key=lambda m: (m.when is None, m.when or datetime.min))
    return [m for m in messages if since is None or m.when is None or m.when >= since]


def save_one(page, msg: Message, folder: Path) -> Path | None:
    """Best effort: save an image (from its blob preview) or a document (via download)."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = msg.when.strftime("%Y%m%d-%H%M") if msg.when else "unknown"
    row = page.locator(f'#main [data-id="{msg.id}"]').first
    try:
        if msg.media == "image":
            src = row.locator('img[src^="blob:"]').first.get_attribute("src", timeout=3_000)
            blob = page.evaluate(BLOB_JS, src)
            ext = blob["type"].split("/")[-1] or "jpg"
            path = folder / f"{stamp}-{_slug(msg.sender)}-{_slug(msg.id)[-8:]}.{ext}"
            path.write_bytes(base64.b64decode(blob["data"]))
        else:
            with page.expect_download(timeout=15_000) as download:
                row.locator('[title^="Download"], [aria-label^="Download"]').first.click(timeout=3_000)
            path = folder / f"{stamp}-{_slug(msg.sender)}-{download.value.suggested_filename}"
            download.value.save_as(str(path))
    except Exception as exc:  # noqa: BLE001 (best effort)
        print(
            f"wa: could not save {msg.media} from {msg.sender} at {msg.when}: {type(exc).__name__}",
            file=sys.stderr,
        )
        return None
    print(f"wa: saved {path}", file=sys.stderr)
    return path


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-") or "unknown"
