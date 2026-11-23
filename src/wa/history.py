"""Read visible history and retain undated rows and completeness information."""

from datetime import date

from . import files
from .session import chat_page


def read_messages(*, group, profile=None, since=None, max_scrolls=100, headed=False):
    if not 1 <= max_scrolls <= 500:
        raise ValueError("max_scrolls must be between 1 and 500")
    target = group
    known = {}
    idle = 0
    stop = "scroll_limit"
    with chat_page(target, profile, not headed) as page:
        page.wait_for_function(files.SCROLLER_READY_JS, timeout=60_000)
        page.evaluate("""() => {
            const list = [...document.querySelectorAll('#main *')]
                .filter(e => e.scrollHeight > e.clientHeight + 50 &&
                    getComputedStyle(e).overflowY !== 'visible')
                .sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
            if (list) list.scrollTop = list.scrollHeight;
        }""")
        page.wait_for_timeout(900)
        for _ in range(max_scrolls):
            rows = page.evaluate("""() => [...document.querySelectorAll('#main [role="row"]')].map(r => ({
                id: r.querySelector('[data-id]')?.getAttribute('data-id'),
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
            if position == -1:
                stop = "scroller_unavailable"
                break
            if position == 0 and idle >= 6:
                stop = "visible_history_start"
                break
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
