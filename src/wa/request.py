"""JSON interface for project commands that run in their own Python environments."""

import json
import sys
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

from . import batch, files, history, recipients, session
from .reader import collect


def _date(value):
    return date.fromisoformat(value) if value else None


def dispatch(request, live=False):
    op = request["operation"]
    profile = request.get("profile")
    headed = request.get("headed", False)
    group = request.get("group")
    if op in {"send", "batch", "recipients"} and not live:
        raise ValueError("A send requires --yes")
    if op == "login":
        return str(session.login(profile, timeout_ms=request.get("timeout_ms", 300_000)))
    if op == "check":
        with session.open_page(session.profile_path(profile), headless=not headed):
            return True
    if op == "recipients":
        counts, detail = recipients._send_chunks_sync(
            profile,
            request["recipients"],
            request["chunks"],
            headless=not headed,
            browser_channel=request.get("browser_channel", "chrome"),
        )
        return {"counts": counts, "detail": detail}
    if not isinstance(group, str) or not group.strip():
        raise ValueError("An exact group title is required")
    if op == "group":
        with session.chat_page(group, profile, not headed):
            return True
    if op == "send":
        media = request.get("media")
        batch.send(
            request["text"], Path(media) if media else None, group=group, profile=profile, headless=not headed
        )
        return True
    if op == "batch":
        items = [(text, Path(media) if media else None) for text, media in request["items"]]
        reports = []
        ok = batch.send_batch(
            items,
            group=group,
            profile=profile,
            headless=not headed,
            retries=request.get("retries", batch.BATCH_RETRIES),
            report=lambda n, err: reports.append((n, err)),
        )
        return {"ok": ok, "reports": reports}
    if op == "history":
        return history.read_messages(
            group=group,
            profile=profile,
            since=_date(request.get("since")),
            max_scrolls=request.get("max_scrolls", 100),
            headed=headed,
        )
    if op == "files":
        items = files.run(
            group=group,
            profile=profile,
            headless=not headed,
            since=_date(request["since"]),
            download=request.get("download", False),
            out=request.get("out"),
            name=request.get("name"),
            repo_root=request.get("repo_root"),
            default_out=request.get("default_out"),
        )
        return {"count": len(items), "table": files.format_table(items)}
    if op == "delivered":
        with session.chat_page(group, profile, not headed) as page:
            page.wait_for_timeout(request.get("settle_ms", 12_000))
            needle = " ".join(request["needle"].casefold().split())
            return bool(needle) and any(
                needle in " ".join(m.text.casefold().split()) for m in collect(page, None, 0)
            )
    raise ValueError(f"Unknown WhatsApp operation: {op}")


def run(args):
    request = json.load(sys.stdin)
    with redirect_stdout(sys.stderr):
        result = dispatch(request, live=args.yes)
    print(json.dumps(result, ensure_ascii=False))
    return 0
