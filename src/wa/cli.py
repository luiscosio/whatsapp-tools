"""wa: read and send WhatsApp Web messages through a linked browser profile."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

from wa import reader, sender
from wa.session import LoggedOut, WhatsAppError, open_chat, open_page, profile_path
from wa.text import chunk_text, md_to_whatsapp

REPO_ROOT = Path.cwd()
MEDIA_ROOT = Path(os.environ.get("WA_MEDIA_ROOT") or REPO_ROOT / "baseline/recovery-evidence/whatsapp")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wa", description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--profile", help="linked Chrome profile dir (default: $WA_PROFILE, else the local iNBest profile)"
    )
    common.add_argument("--headed", action="store_true", help="show the browser window")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", parents=[common], help="report whether the profile is linked or logged out")

    sub.add_parser("login", parents=[common], help="link the Chrome profile by QR code")

    read = sub.add_parser("read", parents=[common], help="print messages from a chat (read only)")
    read.add_argument("--chat", required=True)
    read.add_argument("--since", type=lambda v: datetime.strptime(v, "%Y-%m-%d"), help="YYYY-MM-DD")
    read.add_argument("--out", type=Path, help="also write messages as JSON lines")
    read.add_argument(
        "--save-media",
        action="store_true",
        help="save images and documents under baseline/recovery-evidence/whatsapp/ (best effort)",
    )
    read.add_argument("--max-scrolls", type=int, default=100)

    send = sub.add_parser("send", parents=[common], help="send a message file (dry run unless --yes)")
    send.add_argument("--chat", required=True)
    send.add_argument(
        "--file", required=True, type=Path, help="message text; Markdown is converted to WhatsApp markup"
    )
    send.add_argument(
        "--media", type=Path, help="one image or video; the first 1000 characters become its caption"
    )
    send.add_argument(
        "--yes", action="store_true", help="actually send; without it only print what would be sent"
    )
    files = sub.add_parser("files", parents=[common], help="list or download chat attachments")
    files.add_argument("--chat", required=True)
    files.add_argument("--since", type=date.fromisoformat, required=True)
    files.add_argument("--download", action="store_true")
    files.add_argument("--out")
    files.add_argument("--name")
    rpc = sub.add_parser("request-v1", help="run a JSON request from a project CLI")
    rpc.add_argument("--yes", action="store_true", help="authorize outbound delivery")
    return parser


def cmd_files(args):
    from .files import format_table, run

    rows = run(
        group=args.chat,
        since=args.since,
        download=args.download,
        out=args.out,
        name=args.name,
        profile=args.profile,
        headless=not args.headed,
    )
    print(format_table(rows))
    return 0


def cmd_login(args):
    from wa.session import login

    print(f"linked: {login(args.profile)}")
    return 0


def cmd_check(args) -> int:
    profile = profile_path(args.profile)
    try:
        with open_page(profile, headless=not args.headed):
            pass
    except LoggedOut:
        print(f"logged out: {profile}")
        return 1
    print(f"linked: {profile}")
    return 0


def _format(msg: reader.Message) -> str:
    when = msg.when.strftime("%Y-%m-%d %H:%M") if msg.when else "unknown time"
    body = msg.text.replace("\n", "\n    ")
    return f"[{when}] {msg.sender}: {body}"


def cmd_read(args) -> int:
    profile = profile_path(args.profile)
    with open_page(profile, headless=not args.headed) as page:
        open_chat(page, args.chat)
        folder = (
            MEDIA_ROOT / re.sub(r"[^A-Za-z0-9]+", "-", args.chat).strip("-").lower()
            if args.save_media
            else None
        )
        messages = reader.collect(page, args.since, args.max_scrolls, folder)
    for msg in messages:
        print(_format(msg))
    if args.out:
        args.out.write_text("".join(json.dumps(m.to_json(), ensure_ascii=False) + "\n" for m in messages))
    dated = [m.when for m in messages if m.when]
    span = f"{min(dated):%Y-%m-%d %H:%M} to {max(dated):%Y-%m-%d %H:%M}" if dated else "no dated messages"
    print(f"wa: {len(messages)} messages, {span}", file=sys.stderr)
    return 0


def cmd_send(args) -> int:
    text = md_to_whatsapp(args.file.read_text())
    chunks = chunk_text(text)
    if not chunks:
        raise WhatsAppError(f"{args.file} is empty")
    if args.media and not args.media.is_file():
        raise WhatsAppError(f"media file not found: {args.media}")
    if not args.yes:
        print(
            f"DRY RUN (add --yes to send). Chat: {args.chat!r}, media: {args.media or 'none'}, {len(chunks)} message(s)."
        )
        for n, chunk in enumerate(chunks, 1):
            print(f"--- message {n}/{len(chunks)} ---\n{chunk}")
        return 0
    profile = profile_path(args.profile)
    with open_page(profile, headless=not args.headed) as page:
        sent = sender.send(page, args.chat, chunks, args.media)
    print(f"sent and confirmed: {sent} message(s) to {args.chat!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from . import request

    args = _parser().parse_args(argv)
    try:
        return {
            "request-v1": request.run,
            "files": cmd_files,
            "login": cmd_login,
            "check": cmd_check,
            "read": cmd_read,
            "send": cmd_send,
        }[args.command](args)
    except (WhatsAppError, ValueError, OSError) as exc:
        print(f"wa: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
