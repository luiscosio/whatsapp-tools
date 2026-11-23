"""List and download attachments from a verified conversation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .session import chat_page

REPO_ROOT = Path.cwd()
DEFAULT_DAYS = 7
DEFAULT_OUT = "~/Downloads/whatsapp"
SCROLL_PAUSE = 900  # ms after each scroll step for rows to render
MAX_IDLE_STEPS = 6  # scroll steps without new rows (at the top) before giving up

# One entry per rendered row, in DOM order: date dividers and messages.
ROWS_JS = r"""
() => {
  const main = document.querySelector('#main');
  if (!main) return [];
  // Prefix match only; parse_label() checks the quoted name.
  const docSel = '[title^="Download "],[aria-label^="Download "],[title^="View "],[aria-label^="View "]';
  const dateRe = /^(today|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}\/\d{1,2}\/\d{4}|[a-z]+ \d{1,2}, \d{4})$/i;
  // Date dividers ("Today", "11/12/2026") are leaf nodes outside the message rows.
  const dividers = [...main.querySelectorAll('span, div')]
    .filter(e => e.children.length === 0 && !e.closest('[role="row"]') && dateRe.test((e.innerText || '').trim()));
  const rows = [...main.querySelectorAll('[role="row"]')];
  const nodes = [...dividers, ...rows].sort(
    (a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
  const out = [];
  for (const r of nodes) {
    if (r.getAttribute('role') !== 'row') {
      out.push({divider: r.innerText.trim()});
      continue;
    }
    const idEl = r.querySelector('[data-id]');
    if (!idEl) continue;
    const quoted = [...r.querySelectorAll('[aria-label="Quoted message"]')];
    const inQuote = e => quoted.some(q => q.contains(e));
    const pre = r.querySelector('[data-pre-plain-text]');
    const maybe = [...r.querySelectorAll('[aria-label^="Maybe "]')].find(e => !inQuote(e));
    out.push({
      id: idEl.getAttribute('data-id') || '',
      pre: pre ? pre.getAttribute('data-pre-plain-text') : '',
      sender: maybe ? maybe.getAttribute('aria-label') : '',
      outgoing: !!r.querySelector('.message-out, [data-icon="tail-out"]'),
      labels: [...r.querySelectorAll(docSel)].filter(e => !inQuote(e))
        .map(e => e.getAttribute('title') || e.getAttribute('aria-label') || ''),
      images: [...r.querySelectorAll('img[src^="blob:"]')]
        .filter(e => !inQuote(e) && (e.naturalWidth || e.width) >= 100).length,
      quoted_doc: quoted.some(q => /document/i.test(q.innerText || '')),
      text: (r.innerText || '').trim().slice(0, 500),
    });
  }
  return out;
}
"""

SCROLLER_READY_JS = """
() => [...document.querySelectorAll('#main *')]
  .some(e => e.scrollHeight > e.clientHeight + 50 && getComputedStyle(e).overflowY !== 'visible')
"""

SCROLL_UP_JS = """
() => {
  // The message list scroller: the tallest scrollable element inside #main.
  const els = [...document.querySelectorAll('#main *')]
    .filter(e => e.scrollHeight > e.clientHeight + 50 && getComputedStyle(e).overflowY !== 'visible');
  const el = els.sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
  if (!el) return -1;
  el.scrollTop = Math.max(0, el.scrollTop - el.clientHeight * 0.7);
  return el.scrollTop;
}
"""

LABEL_RE = re.compile(r'^(Download|View)\s+"(.+)"\s*$', re.DOTALL)
SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?\s?(?:bytes|B|kB|KB|MB|GB))\b")
PRE_RE = re.compile(r"^\[(?P<time>[^,\]]+),\s*(?P<date>[^\]]+)\]\s*(?P<sender>.*?):\s*$")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}(?:\s?[AP]M)?)\b", re.IGNORECASE)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass
class FileItem:
    id: str
    kind: str  # "document" or "image"
    name: str  # file name for documents; "" for images until saved
    day: date | None
    time: str
    sender: str
    size: str
    viewer: bool  # document opens a viewer (View "...") before downloading

    @property
    def type(self) -> str:
        if self.kind == "image":
            return "image"
        ext = Path(self.name).suffix.lstrip(".").lower()
        return ext or "document"

    def target_name(self) -> str:
        """The file name used in --out. Images get a stable synthetic name."""
        if self.kind == "document":
            return safe_name(self.name)
        stamp = (self.day.isoformat() if self.day else "unknown") + "_" + self.time.replace(":", "")
        return safe_name(f"image_{stamp}_{self.id[-8:]}")


def safe_name(name: str) -> str:
    """A file name safe on macOS/Linux: no path separators or control chars."""
    clean = re.sub(r"[/\\:\x00-\x1f]", "_", name).strip().strip(".")
    return clean or "file"


def parse_label(label: str) -> tuple[str, bool] | None:
    """`Download "a.pdf"` -> ("a.pdf", False); `View "a.pdf"` -> ("a.pdf", True)."""
    m = LABEL_RE.match(label.strip())
    if not m:
        return None
    return m.group(2), m.group(1) == "View"


def parse_size(text: str) -> str:
    m = SIZE_RE.search(text)
    return m.group(1) if m else ""


def parse_date(text: str) -> date | None:
    """Dates as WhatsApp Web shows them with locale en-US: 11/13/2026, also
    13/11/2026 when the day can't be a month, and 2026-11-13."""
    text = text.strip()
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_pre(pre: str) -> tuple[date | None, str, str]:
    """`[10:32 AM, 11/13/2026] Test Member: ` -> (date, "10:32 AM", "Test Member")."""
    m = PRE_RE.match(pre.strip())
    if not m:
        return None, "", ""
    return parse_date(m.group("date")), m.group("time").strip(), m.group("sender").strip()


def parse_divider(text: str, today: date) -> date | None:
    """Date divider rows: TODAY, YESTERDAY, a weekday (this past week) or a date."""
    t = text.strip().lower()
    if t == "today":
        return today
    if t == "yesterday":
        return today - timedelta(days=1)
    if t in WEEKDAYS:
        back = (today.weekday() - WEEKDAYS.index(t)) % 7 or 7
        return today - timedelta(days=back)
    return parse_date(text)


def row_sender(row: dict) -> str:
    """Sender for a row without data-pre-plain-text (media and document
    bubbles): the "Maybe <name>" label of an unsaved contact, or "You"."""
    hint = row.get("sender", "")
    if hint.startswith("Maybe "):
        return hint[len("Maybe ") :].strip()
    if hint:
        return hint.strip()
    return "You" if row.get("outgoing") else ""


def items_from_rows(rows: list[dict], today: date) -> list[FileItem]:
    """Turn one snapshot of rendered rows (DOM order) into file items.

    A row without its own date takes the last divider or dated row above it;
    rows above the first known date keep day=None (a later, higher snapshot
    fills them in). Quoted documents are not files of their own."""
    items: list[FileItem] = []
    day: date | None = None
    for r in rows:
        if "divider" in r:
            day = parse_divider(r["divider"], today) or day
            continue
        row_day, row_time, sender = parse_pre(r.get("pre", ""))
        sender = sender or row_sender(r)
        day = row_day or day
        if not row_time:
            times = TIME_RE.findall(r.get("text", ""))
            row_time = times[-1] if times else ""
        for n, label in enumerate(r.get("labels", [])):
            parsed = parse_label(label)
            if not parsed:
                continue
            name, viewer = parsed
            items.append(
                FileItem(
                    id=f"{r['id']}#{n}" if n else r["id"],
                    kind="document",
                    name=name,
                    day=day,
                    time=row_time,
                    sender=sender,
                    size=parse_size(r.get("text", "")),
                    viewer=viewer,
                )
            )
        if not r.get("labels") and r.get("images"):
            items.append(FileItem(r["id"], "image", "", day, row_time, sender, "", False))
    # Dedupe labels that appear twice on one bubble (title and aria-label).
    seen: dict[tuple[str, str], FileItem] = {}
    for it in items:
        seen.setdefault((it.id.split("#")[0], it.name or it.id), it)
    return list(seen.values())


def merge(known: dict[str, FileItem], new: list[FileItem]) -> int:
    """Add new items by id; fill in day/sender that an earlier snapshot lacked.
    Returns how many ids were new."""
    added = 0
    for it in new:
        old = known.get(it.id)
        if old is None:
            known[it.id] = it
            added += 1
            continue
        old.day = old.day or it.day
        old.sender = old.sender or it.sender
        old.time = old.time or it.time
        old.size = old.size or it.size
    return added


def in_range(items: list[FileItem], since: date) -> list[FileItem]:
    """Items on or after `since`; undated ones are kept (better listed than lost)."""
    return [it for it in items if it.day is None or it.day >= since]


def out_dir(out: str | None, group: str, *, repo_root=None, default_out=None) -> Path:
    """Where downloads go; refuses a folder inside the repo."""
    path = (
        Path(os.path.expanduser(out))
        if out
        else Path(os.path.expanduser(default_out or DEFAULT_OUT)) / (safe_name(group))
    )
    resolved = path.resolve()
    root = Path(repo_root or REPO_ROOT).resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError(f"--out {path} is inside the repo; pick a folder outside it")
    return path


def already_saved(item: FileItem, folder: Path) -> bool:
    if not folder.exists():
        return False
    target = item.target_name()
    if item.kind == "document":
        return (folder / target).exists()
    return any(p.stem == target for p in folder.iterdir())


def format_table(items: list[FileItem]) -> str:
    rows = [
        (
            it.day.isoformat() if it.day else "?",
            it.time or "?",
            it.sender or "?",
            it.type,
            it.size or "-",
            it.name or "(image)",
        )
        for it in items
    ]
    head = ("date", "time", "sender", "type", "size", "name")
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(5)]
    lines = []
    for r in [head, *rows]:
        lines.append("  ".join(c.ljust(w) for c, w in zip(r[:5], widths, strict=True)) + "  " + r[5])
    return "\n".join(lines)


def sort_key(it: FileItem) -> tuple:
    return (it.day or date.min, it.time)


# --- browser part (not unit-tested) ---------------------------------------


def _save(page, item: FileItem, folder: Path) -> Path:
    """Open the file's viewer (or its download button) and save it. Read-only:
    only file/viewer buttons are clicked, Escape closes the viewer."""
    row = page.locator(f'#main [data-id="{item.id.split("#")[0]}"]').first
    try:
        if item.kind == "document":
            q = '"' + item.name.replace('"', '\\"') + '"'
            if item.viewer:
                row.locator(f'[title="View {q[1:-1]}"],[aria-label="View {q[1:-1]}"]').first.click()
                btn = page.locator('[aria-label="Download"]').first
                btn.wait_for(state="visible", timeout=15_000)
                with page.expect_download(timeout=60_000) as dl:
                    btn.click()
            else:
                with page.expect_download(timeout=60_000) as dl:
                    row.locator(
                        f'[title="Download {q[1:-1]}"],[aria-label="Download {q[1:-1]}"]'
                    ).first.click()
            target = folder / item.target_name()
        else:
            row.locator('img[src^="blob:"]').last.click()
            btn = page.locator('[aria-label="Download"]').first
            btn.wait_for(state="visible", timeout=15_000)
            with page.expect_download(timeout=60_000) as dl:
                btn.click()
            ext = Path(dl.value.suggested_filename).suffix or ".jpg"
            target = folder / (item.target_name() + ext)
        dl.value.save_as(target)
        return target
    finally:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)


def run(
    *,
    group: str,
    since: date,
    download: bool,
    out: str | None,
    name: str | None,
    profile: str | None = None,
    headless: bool = True,
    today: date | None = None,
    repo_root=None,
    default_out=None,
) -> list[FileItem]:
    today = today or date.today()
    folder = out_dir(out, group, repo_root=repo_root, default_out=default_out) if download else None

    def wanted(it: FileItem) -> bool:
        if it.day is not None and it.day < since:
            return False
        return not name or name.casefold() in (it.name or "").casefold()

    known: dict[str, FileItem] = {}
    done: set[str] = set()
    row_ids: set[str] = set()
    saved = skipped = 0
    with chat_page(group, profile, headless) as page:
        # The message list renders a few seconds after the header; wait for its scroller.
        page.wait_for_function(SCROLLER_READY_JS, timeout=60_000)
        idle = 0
        while True:
            page.wait_for_timeout(SCROLL_PAUSE)
            rows = page.evaluate(ROWS_JS)
            snap = items_from_rows(rows, today)
            merge(known, snap)
            fresh = {r["id"] for r in rows if "id" in r} - row_ids
            row_ids |= fresh
            if download:
                for it in snap:
                    it = known[it.id]
                    if not wanted(it) or it.id in done:
                        continue
                    done.add(it.id)
                    if already_saved(it, folder):
                        skipped += 1
                        continue
                    folder.mkdir(parents=True, exist_ok=True)
                    try:
                        path = _save(page, it, folder)
                        saved += 1
                        print(f"saved {path.name}", flush=True)
                    except Exception as e:  # noqa: BLE001
                        print(f"couldn't save {it.name or it.id}: {str(e).splitlines()[0]}")
            dates = [it.day for it in known.values() if it.day]
            # Dividers count too: stop once we've scrolled past `since`.
            rows_days = [parse_divider(r["divider"], today) for r in rows if "divider" in r]
            oldest = min([d for d in dates + rows_days if d], default=None)
            if oldest and oldest < since:
                break
            top = page.evaluate(SCROLL_UP_JS)
            idle = 0 if fresh else idle + 1
            if top == -1 or (top == 0 and idle >= MAX_IDLE_STEPS):
                break
    items = sorted((it for it in known.values() if wanted(it)), key=sort_key)
    if download:
        print(f"{saved} saved, {skipped} already in {folder}")
    return items
