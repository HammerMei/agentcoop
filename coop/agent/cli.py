"""The agent-facing commands (design §3.3), thin clients of the doorbell API:

```
coop read  (--wake W | --room R)
coop send  (--wake W | --unsolicited) [--room R | --thread T] [--to a,b] [--control mute=a,b] [TEXT]
coop done  --wake W [--answered T1,T2] [--failed REASON]
coop bio   --file bio.md
coop attachment ID [--out PATH]
```

Until the ``coop`` entry point moves, they run as ``python -m coop.agent <command>``.
The gateway address and the token come from ``--gateway`` / ``--token`` or the
environment (``COOP_GATEWAY``, ``COOP_TOKEN``). A failure prints one ``[ERROR]`` line
on stderr and exits 1; a usage error exits 2. A ``send`` a room that does not enforce
let through against a rule prints a ``[WARNING]`` line per rule and exits 0.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..errors import (
    CertificateMismatch,
    DoorbellError,
    InvalidReply,
    LostResponse,
    NoLiveLease,
    RefusedSend,
)
from ..room.model import ALL, Attachment, Post, Reply, Wake
from ..room.platform import RefusedPost
from ..room.prompt import one_line, render_hint
from .client import HttpDoorbell

PROG = "coop"


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog=PROG, description="the agent's side of the room")
    top.add_argument("--gateway", default=os.environ.get("COOP_GATEWAY"), help="doorbell address (COOP_GATEWAY)")
    top.add_argument("--token", default=os.environ.get("COOP_TOKEN"), help="bearer token (COOP_TOKEN)")
    top.add_argument("--fingerprint", default=os.environ.get("COOP_FINGERPRINT"),
                     help="the gateway certificate's SHA-256 fingerprint to pin (COOP_FINGERPRINT)")
    sub = top.add_subparsers(dest="command", required=True)

    read = sub.add_parser("read", help="the hint and the posts of a wake, or a room's posts after the cursor")
    where = read.add_mutually_exclusive_group(required=True)
    where.add_argument("--wake", metavar="W")
    where.add_argument("--room", metavar="R")
    read.add_argument("--after", metavar="ID", help="with --room: start after this post instead of the cursor")

    send = sub.add_parser("send", help="one reply, as the bot")
    mode = send.add_mutually_exclusive_group(required=True)
    mode.add_argument("--wake", metavar="W", help="the wake this reply answers")
    mode.add_argument("--unsolicited", action="store_true", help="a send outside any wake")
    place = send.add_mutually_exclusive_group()
    place.add_argument("--room", metavar="R", help="a new top-level post in room R")
    place.add_argument("--thread", metavar="T", help="a reply in thread T")
    send.add_argument("--to", metavar="a,b", help="who the reply is for")
    send.add_argument("--control", action="append", default=[], metavar="mute=a,b",
                      help="a control word (mute, unmute); repeatable")
    send.add_argument("text", nargs="?", help="the reply text; `-` reads it from stdin")

    done = sub.add_parser("done", help="end the turn")
    done.add_argument("--wake", metavar="W", required=True)
    done.add_argument("--answered", metavar="T1,T2", help="threads answered without a send")
    done.add_argument("--failed", metavar="REASON")

    bio = sub.add_parser("bio", help="push the bot's bio")
    bio.add_argument("--file", required=True, type=Path)

    attachment = sub.add_parser("attachment", help="download one attachment by the handle read printed")
    attachment.add_argument("id")
    attachment.add_argument("--out", type=Path, help="where to write it (default: its name, here)")
    return top


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


async def run(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.gateway or not args.token:
        parser().error("--gateway and --token (or COOP_GATEWAY and COOP_TOKEN) are required")
    try:
        if plain_http(args.gateway):
            print(f"[WARNING] {args.gateway} is plain http: the token travels in clear; keep it on loopback "
                  "or a tunnel to loopback", file=sys.stderr)
        async with HttpDoorbell(args.gateway, args.token, fingerprint=args.fingerprint) as doorbell:
            return await COMMANDS[args.command](doorbell, args)
    except (DoorbellError, NoLiveLease, InvalidReply, RefusedSend, RefusedPost, LostResponse,
            CertificateMismatch, httpx.HTTPError, OSError, ValueError) as exc:  # ValueError: a malformed address
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


def plain_http(address: str) -> bool:
    """Design §3.2: a plain ``http://`` address is accepted for loopback, a tunnel to
    loopback and test deployments, and the utility warns when it connects to one."""
    return urlsplit(address).scheme == "http"


# ── commands ────────────────────────────────────────────────────────────────


async def cmd_read(doorbell: HttpDoorbell, args) -> int:
    trigger = None
    if args.wake is not None and args.after is not None:
        parser().error("read --after goes with --room")
    if args.wake is not None:
        wake, posts = await doorbell.read(args.wake)
        trigger = wake.trigger.post_id if wake.trigger is not None else None
        print(reminder(wake))
        print(render_hint(wake.hint))
        print()
    else:
        posts = await doorbell.read_room(args.room, args.after)
    for post in posts:
        print(post_line(post, trigger))
        for found in post.attachments:
            print(attachment_line(found))
    return 0


async def cmd_send(doorbell: HttpDoorbell, args) -> int:
    text = sys.stdin.read() if args.text == "-" else args.text
    text = text.strip() if text else None
    control = parse_controls(args.control) if args.control else None
    if text is None and control is None:
        parser().error("send: text or --control")
    if text is not None and not args.to:
        parser().error("send: text needs --to")
    if args.unsolicited and args.room is None and args.thread is None:
        parser().error("send --unsolicited: --room or --thread")
    # --room is a new top-level post there (its own thread); --thread a reply; a wake's
    # send with neither goes to the wake's starting thread, which the gateway fills in
    reply = Reply(thread=args.thread, to=names(args.to) if args.to else None, text=text, control=control,
                  wake_id=None if args.unsolicited else args.wake)
    sent = await doorbell.send(reply, room=args.room)
    for broken in sent.warnings:
        # the room does not enforce: posted, counted, and said (design §3.5)
        print(f"[WARNING] posted, but it broke a rule this room does not enforce: {broken}", file=sys.stderr)
    if sent.post is not None:
        print(post_line(sent.post))
    return 0


async def cmd_done(doorbell: HttpDoorbell, args) -> int:
    ok = await doorbell.done(args.wake, tuple(names(args.answered)) if args.answered else (), args.failed)
    if not ok:
        print(f"[WARNING] wake {args.wake} holds no live lease: nothing moved", file=sys.stderr)
    return 0


async def cmd_bio(doorbell: HttpDoorbell, args) -> int:
    version = await doorbell.bio(args.file.read_text())
    print(f"bio v{version}")
    return 0


async def cmd_attachment(doorbell: HttpDoorbell, args) -> int:
    name, data = await doorbell.attachment(args.id)
    out = args.out if args.out is not None else Path(Path(name).name or args.id)
    out.write_bytes(data)
    print(out)
    return 0


COMMANDS = {"read": cmd_read, "send": cmd_send, "done": cmd_done, "bio": cmd_bio, "attachment": cmd_attachment}


# ── rendering ───────────────────────────────────────────────────────────────


def reminder(wake: Wake) -> str:
    return (f"you are {wake.bot} in {wake.room}, rules v{wake.rules_version}, "
            f"reply with `send --wake {wake.wake_id}` or `done`")


def post_line(post: Post, trigger: str | None = None) -> str:
    """``[thread] #id author (role): text``; the post that caused the wake is marked."""
    labels = [label for label in (post.author_role, "trigger" if post.id == trigger else "") if label]
    role = f" ({', '.join(labels)})" if labels else ""
    return f"[{post.thread}] #{post.id} {post.author}{role}: {one_line(post.text)}"


def attachment_line(found: Attachment) -> str:
    """One line per attachment; the name and type pass through ``one_line`` like post
    text, so a file name cannot forge a header line either (design §3.3)."""
    return (f"  attachment {found.id}: {one_line(found.name)} {one_line(found.content_type)} "
            f"{human_size(found.size)} -> coop attachment {found.id}")


def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def names(csv: str) -> list[str]:
    return [n.strip() for n in csv.split(",") if n.strip()]


def parse_controls(items: list[str]) -> dict:
    control: dict = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key or not value:
            parser().error(f"--control takes key=names, not {item!r}")
        control[key] = ALL if value == ALL else names(value)
    return control


if __name__ == "__main__":
    sys.exit(main())
