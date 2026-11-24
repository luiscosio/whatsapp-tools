"""Read visible history and retain undated rows and completeness information."""

import time
from datetime import date

from . import files
from .session import chat_page

# WhatsApp renders these controls outside message bodies when a linked device
# is waiting for its phone. A group's own messages cannot set this state.
SYNC_STATE_JS = """() => {
    const controls = document.querySelectorAll('#main button, #main [role="button"]');
    for (const el of controls) {
        if (!el.getClientRects().length || el.closest('[data-id], [data-pre-plain-text]')) continue;
        const t = el.innerText || '';
        if (/syncing paused|open whatsapp on your phone|sincronización en pausa|abre whatsapp en tu/i.test(t)) return 'phone_sync_paused';
        if (/syncing older messages|sincronizando mensajes anteriores/i.test(t)) return 'history_syncing';
        if (/older messages.*phone|mensajes anteriores.*teléfono/i.test(t)) return 'older_history_on_phone';
    }
    return '';
}"""
SYNC_WAIT_SECONDS = 120


def read_messages(*, group, profile=None, since=None, max_scrolls=100, headed=False):
    if not 1 <= max_scrolls <= 500:
        raise ValueError("max_scrolls must be between 1 and 500")
    target = group
    known = {}
    idle = 0
    stop = "scroll_limit"
    with chat_page(target, profile, not headed) as page:
        deadline = time.monotonic() + SYNC_WAIT_SECONDS
        page.evaluate("""() => {
            const list = [...document.querySelectorAll('#main *')]
                .filter(e => e.scrollHeight > e.clientHeight + 50 &&
                    getComputedStyle(e).overflowY !== 'visible')
                .sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
            if (list) list.scrollTop = list.scrollHeight;
        }""")
        page.wait_for_timeout(900)
        scrolls = 0
        while scrolls < max_scrolls:
            rows = page.evaluate("""() => [...document.querySelectorAll('#main [role="row"]')].map(r => ({
                id: r.getAttribute('data-id') || r.querySelector('[data-id]')?.getAttribute('data-id'),
                pre: r.querySelector('[data-pre-plain-text]')?.getAttribute('data-pre-plain-text') || '',
                text: r.innerText || ''})).filter(r => r.id)""")
            fresh = 0
            dates = []
            for row in rows:
                day, at, sender = files.parse_pre(row["pre"])
                if day:
                    dates.append(day)
                if row["id"] not in known:
                    fresh += 1
                previous = known.get(row["id"])
                if previous and previous["text"] and not row["text"].strip():
                    continue
                known[row["id"]] = {
                    **row,
                    "date": day.isoformat() if day else (previous or {}).get("date"),
                    "time": at or (previous or {}).get("time"),
                    "sender": sender or (previous or {}).get("sender"),
                }
            if since and dates and min(dates) < since:
                stop = "date_boundary"
                break
            position = page.evaluate(files.SCROLL_UP_JS)
            idle = 0 if fresh else idle + 1
            if position == -1 or (position == 0 and idle >= 6):
                sync = page.evaluate(SYNC_STATE_JS)
                if (sync or not known) and time.monotonic() < deadline:
                    page.wait_for_timeout(2000)
                    continue
                stop = sync or ("visible_history_start" if known else "no_visible_messages")
                break
            scrolls += 1
            page.wait_for_timeout(900)
    items = [
        row
        for row in known.values()
        if not since or not row["date"] or date.fromisoformat(row["date"]) >= since
    ]
    return {
        "group": target,
        "messages": items,
        "stop_reason": stop,
        "complete": stop in {"date_boundary", "visible_history_start"},
        "scope": "History available on this linked WhatsApp device; unknown dates retained",
    }
