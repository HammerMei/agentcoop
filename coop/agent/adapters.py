"""The CLI agents the utility can drive (design §3.4): one command per wake, a
session or thread id kept per room, the room rules carried the way each CLI takes
them. Evidence tags as in the design: ``[help]`` from the installed CLI's help,
``[probe]`` run on this machine (2026-11-25: Claude Code 2.1.330, OpenCode 2.2.6,
Codex 0.167.0), ``[src]`` from the frozen gateway's adapter.

Each adapter is data: how to build the argv, where the session id is in the output.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Turn:
    """What one command is built from."""

    text: str  # the wake text (design §3.4)
    rules: Path  # the room-rules document, written by the utility
    session: str | None  # the room's session or thread id (``None``: the CLI names it on this first turn)
    first: bool  # no turn has run in this session yet


@dataclass(frozen=True)
class Adapter:
    kind: str
    program: str
    # ``None`` when the CLI names the session itself (read from the output after the
    # first turn); otherwise the utility chooses one before the first turn, so a
    # first turn the lease ended still has a session to resume.
    new_session: Callable[[], str] | None
    argv: Callable[[str, Turn], list[str]]
    # The session id the output names, or ``None``: ``str`` of the whole stdout.
    session_in: Callable[[str], str | None]
    # The rules travel in the prompt (OpenCode, Codex) or through a flag (Claude Code).
    rules_in_text: bool = False
    # A resume that cannot fail for a missing session (OpenCode's ``--session`` creates
    # one): a non-zero exit then is never read as "did not resume".
    resume_creates: bool = False

    def command(self, turn: Turn) -> list[str]:
        if self.rules_in_text:
            turn = Turn(f"{turn.rules.read_text()}\n\n---\n\n{turn.text}", turn.rules, turn.session, turn.first)
        return self.argv(self.program, turn)


def _claude_argv(program: str, turn: Turn) -> list[str]:
    # [probe] ``--session-id <uuid>`` on the first turn and ``-r <uuid>`` after give one
    # session the utility named; ``--output-format json`` prints one object whose
    # ``session_id`` is that id. [src] ``--system-prompt-snapshot off``: Claude Code
    # records the system prompt on a conversation's first request and replays it on
    # every resume unless the record is off, so the rules file is re-read each turn.
    # [help] ``--dangerously-skip-permissions`` is bypass mode (design §3.4).
    session = ["--session-id", str(turn.session)] if turn.first else ["-r", str(turn.session)]
    return [program, "-p", "--output-format", "json", "--append-system-prompt-file", str(turn.rules),
            "--system-prompt-snapshot", "off", "--dangerously-skip-permissions", *session, turn.text]


def _claude_session_in(stdout: str) -> str | None:
    try:
        doc = json.loads(stdout)
    except ValueError:
        return None
    value = doc.get("session_id") if isinstance(doc, dict) else None
    return value if isinstance(value, str) and value else None


def _opencode_argv(program: str, turn: Turn) -> list[str]:
    # [help] ``--session``: "Session ID to continue, or to create if it does not exist";
    # [probe] the id must start with ``ses`` (``ses_coop<hex>`` is taken; a bare name is
    # refused with 'Expected a string starting with "ses"'); the output lines carry it
    # as ``sessionID``. [help] ``--standalone``: a private server instead of the
    # background service, so ending the command ends the turn. [help] ``--auto``:
    # bypass mode. The rules travel in the message: an agent entry with the rules as
    # its ``system`` is not verified for ``run`` (design §3.4).
    return [program, "run", "--standalone", "--session", str(turn.session), "--format", "json", "--auto", turn.text]


def _opencode_session_in(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        value = doc.get("sessionID") if isinstance(doc, dict) else None
        if isinstance(value, str) and value.startswith("ses"):
            return value
    return None


def _codex_argv(program: str, turn: Turn) -> list[str]:
    # [probe] ``exec --json`` prints a ``thread.started`` line with ``thread_id``, and
    # ``exec resume <id> --json "<prompt>"`` continues it. [help] the prompt argument
    # is "initial instructions for the agent", so the rules travel in it; the utility
    # writes nothing into the working directory (an ``AGENTS.md`` there is the owner's).
    # [help] ``--dangerously-bypass-approvals-and-sandbox`` is bypass mode;
    # ``--skip-git-repo-check`` lets a working directory outside a repository run.
    flags = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
    if turn.first:
        return [program, "exec", *flags, turn.text]
    return [program, "exec", "resume", *flags, str(turn.session), turn.text]


def _codex_session_in(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get("type") == "thread.started" and isinstance(doc.get("thread_id"), str):
            return doc["thread_id"]
    return None


ADAPTERS: dict[str, Callable[[str], Adapter]] = {
    "claude": lambda program: Adapter("claude", program, lambda: str(uuid.uuid4()), _claude_argv, _claude_session_in),
    "opencode": lambda program: Adapter("opencode", program, lambda: f"ses_coop{secrets.token_hex(8)}", _opencode_argv,
                                        _opencode_session_in, rules_in_text=True, resume_creates=True),
    "codex": lambda program: Adapter("codex", program, None, _codex_argv, _codex_session_in, rules_in_text=True),
}
DEFAULT_PROGRAM = {"claude": "claude", "opencode": "opencode", "codex": "codex"}
