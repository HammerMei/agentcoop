"""``coop agent run`` (design §3.4): the adapters as data, and the runner against
the doorbell API with fake CLIs — one that sends ``done`` itself through the real
``coop done``, one that does not, one the lease has to end."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from coop.agent import run as run_module
from coop.agent.adapters import ADAPTERS, Turn
from coop.agent.client import HttpDoorbell
from coop.agent.run import Runner, wake_text
from coop.errors import DoorbellError
from coop.room.clock import AsyncioClock
from coop.room.model import Hint, Wake

from .helpers import FAST, Doorbell, Sandbox

TOKENS = {"tok-a": "a"}

# ── the adapters ────────────────────────────────────────────────────────────


def test_claude_names_its_session_on_the_first_turn_and_resumes_it_after(tmp_path):
    rules = tmp_path / "rules.md"
    rules.write_text("be brief")
    claude = ADAPTERS["claude"]("claude")
    sid = claude.new_session()
    first = claude.command(Turn("hi", rules, sid, True))
    later = claude.command(Turn("again", rules, sid, False))
    assert first == ["claude", "-p", "--output-format", "json", "--append-system-prompt-file", str(rules),
                     "--system-prompt-snapshot", "off", "--dangerously-skip-permissions", "--session-id", sid, "hi"]
    assert later[-3:] == ["-r", sid, "again"] and "--session-id" not in later
    # the probe's shape: one JSON object, ``session_id`` the id we named
    assert claude.session_in(json.dumps({"type": "result", "session_id": sid, "result": "OK"})) == sid
    assert claude.session_in("not json") is None and claude.session_in(json.dumps({"result": "x"})) is None


def test_opencode_takes_a_ses_prefixed_session_we_name_and_the_rules_in_the_message(tmp_path):
    rules = tmp_path / "rules.md"
    rules.write_text("the rules")
    opencode = ADAPTERS["opencode"]("opencode")
    sid = opencode.new_session()
    assert sid.startswith("ses_coop")  # [probe] a session id must start with ``ses``
    argv = opencode.command(Turn("hi", rules, sid, True))
    assert argv[:-1] == ["opencode", "run", "--standalone", "--session", sid, "--format", "json", "--auto"]
    assert argv[-1] == "the rules\n\n---\n\nhi"
    assert opencode.command(Turn("again", rules, sid, False))[:-1] == argv[:-1]  # continue is the same call
    line = '{"type":"step_start","timestamp":1,"sessionID":"ses_ded7ad7e7ffeDVeptId3uUB1Dd","part":{}}'
    assert opencode.session_in(line + "\nnot json\n") == "ses_ded7ad7e7ffeDVeptId3uUB1Dd"


def test_codex_lets_the_cli_name_the_thread_and_resumes_it_with_the_rules_in_the_prompt(tmp_path):
    rules = tmp_path / "rules.md"
    rules.write_text("the rules")
    codex = ADAPTERS["codex"]("codex")
    assert codex.new_session is None
    first = codex.command(Turn("hi", rules, None, True))
    assert first == ["codex", "exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                     "the rules\n\n---\n\nhi"]
    later = codex.command(Turn("again", rules, "01a21285-1118-7213-827d-6fc3d9e7b107", False))
    assert later[:3] == ["codex", "exec", "resume"] and later[-2] == "01a21285-1118-7213-827d-6fc3d9e7b107"
    out = '{"type":"thread.started","thread_id":"01a21285-1118-7213-827d-6fc3d9e7b107"}\n{"type":"turn.started"}\n'
    assert codex.session_in(out) == "01a21285-1118-7213-827d-6fc3d9e7b107"


# ── the runner against the doorbell ─────────────────────────────────────────

FAKE_CLI = '''#!/usr/bin/env python3
"""A fake CLI: records its argv, then does what its name says."""
import json, os, re, subprocess, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
(here / "argv.json").write_text(json.dumps(sys.argv[1:]))
text = sys.argv[-1]
mode = here.name
wake = re.search(r"wake (w-[^ ]+) ", text).group(1)
if mode == "answers":
    subprocess.run([sys.executable, "-m", "coop", "read", "--wake", wake], capture_output=True, text=True, check=True)
    r = subprocess.run([sys.executable, "-m", "coop", "done", "--wake", wake, "--answered", "t1"], capture_output=True, text=True)
    (here / "done.txt").write_text(r.stdout + r.stderr + f"exit {r.returncode}")
    sid = sys.argv[sys.argv.index("--session-id") + 1] if "--session-id" in sys.argv else sys.argv[sys.argv.index("-r") + 1]
    print(json.dumps({"type": "result", "session_id": sid, "result": "done"}))
elif mode == "silent":
    print(json.dumps({"type": "result", "session_id": "x", "result": "forgot"}))
elif mode == "sleeps":
    child = subprocess.Popen([sys.executable, "-c", "import time\\nwhile True:\\n open(%r, 'a').write('.')\\n time.sleep(0.1)" % str(here / "heartbeat")])
    (here / "child.pid").write_text(str(child.pid))
    time.sleep(30)
elif mode == "fails":
    sys.exit(2)
'''


def fake_cli(tmp_path: Path, mode: str) -> str:
    d = tmp_path / mode
    d.mkdir()
    script = d / "cli.py"
    script.write_text(FAKE_CLI)
    script.chmod(0o755)
    return str(script)


@pytest.fixture
async def served():
    sb = Sandbox(clock=AsyncioClock(), attach_agent=False, params=replace(FAST, lease_seconds=2))
    async with Doorbell(sb.gateway, TOKENS, cancel_handlers=False) as server:
        url = f"http://{server.server.host}:{server.server.port}"
        async with HttpDoorbell(url, "tok-a") as doorbell:
            yield sb, doorbell, url


def runner_for(doorbell: HttpDoorbell, url: str, tmp_path: Path, mode: str, kind: str = "claude") -> Runner:
    program = fake_cli(tmp_path, mode)
    adapter = ADAPTERS[kind](sys.executable)  # the fake script is the first argument: see ``program`` below
    adapter = replace(adapter, argv=lambda p, turn, inner=adapter.argv: [sys.executable, program, *inner(p, turn)[1:]])
    env = {"COOP_GATEWAY": url, "COOP_TOKEN": "tok-a", "PATH": os.environ.get("PATH", ""),
           "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    return Runner(doorbell, adapter, tmp_path / "state", tmp_path, env)


async def test_a_turn_the_agent_closes_itself_is_normal_and_its_session_is_kept(served, tmp_path, capsys):
    sb, doorbell, url = served
    runner = runner_for(doorbell, url, tmp_path, "answers")
    await sb.human("@a hello", "t1")
    wake = await doorbell.poll()
    await runner.take(wake)
    argv = json.loads((tmp_path / "answers" / "argv.json").read_text())
    sid = argv[argv.index("--session-id") + 1]
    assert wake_text(wake) == argv[-1] and "tok-a" not in " ".join(argv)
    assert (tmp_path / "answers" / "done.txt").read_text().endswith("exit 0")
    assert sb.trace.last("turn_end", bot="a").outcome == "normal"
    assert json.loads((tmp_path / "state" / "sessions.json").read_text()) == {"team": sid}
    assert (tmp_path / "state" / "rules.md").read_text() == "rules" and runner.rules_version == sb.gateway.rules_version
    assert "[WARNING]" not in capsys.readouterr().err  # the runner's done was stale: the normal end of a turn
    await sb.human("@a more", "t1")
    await runner.take(await doorbell.poll())
    argv = json.loads((tmp_path / "answers" / "argv.json").read_text())
    assert argv[argv.index("-r") + 1] == sid and "--session-id" not in argv


async def test_a_command_that_exits_without_done_fails_the_turn_loudly(served, tmp_path, capsys):
    sb, doorbell, url = served
    runner = runner_for(doorbell, url, tmp_path, "silent")
    await sb.human("@a hello", "t1")
    wake = await doorbell.poll()
    await runner.take(wake)
    assert sb.trace.last("turn_end", bot="a").outcome == "backend_failure"
    assert "no done from agent (exit 0)" in capsys.readouterr().err
    assert runner.sessions["team"] == "x"  # a first turn keeps the id the output names: the CLI may rename ours


async def test_the_lease_ends_the_command_and_its_process_group(served, tmp_path, capsys):
    sb, doorbell, url = served
    runner = runner_for(doorbell, url, tmp_path, "sleeps")
    await sb.human("@a hello", "t1")
    wake = await doorbell.poll()
    started = time.monotonic()
    await runner.take(wake)
    assert time.monotonic() - started < 15  # the lease is 2 s, the grace 5 s; the fake would sleep 30
    assert "lease expired" in capsys.readouterr().err
    assert sb.trace.last("turn_end", bot="a").outcome == "backend_failure"
    heartbeat = tmp_path / "sleeps" / "heartbeat"
    size = heartbeat.stat().st_size if heartbeat.exists() else 0
    await asyncio.sleep(0.5)
    assert (heartbeat.stat().st_size if heartbeat.exists() else 0) == size  # the grandchild stopped with the group


async def test_a_session_that_did_not_resume_is_dropped_and_said(served, tmp_path, capsys):
    sb, doorbell, url = served
    runner = runner_for(doorbell, url, tmp_path, "fails")
    runner.sessions["team"] = "stale-session"
    await sb.human("@a hello", "t1")
    await runner.take(await doorbell.poll())
    err = capsys.readouterr().err
    assert "session stale-session for team did not resume; dropped" in err and "coop reset a team" in err
    assert "team" not in runner.sessions and json.loads((tmp_path / "state" / "sessions.json").read_text()) == {}
    assert "no done from agent (exit 2)" in err


# ── the runner's parts, with a stub doorbell ────────────────────────────────


class StubDoorbell:
    def __init__(self) -> None:
        self.rules_calls = 0
        self.done_calls: list[tuple[str, str | None]] = []
        self.polls: list[object] = []

    async def rules(self):
        self.rules_calls += 1
        return 7, f"rules v{self.rules_calls}"

    async def done(self, wake_id, answered=(), failed=None):
        self.done_calls.append((wake_id, failed))
        return True

    async def poll(self, wait=0):
        item = self.polls.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def a_wake(**more) -> Wake:
    fields = {"wake_id": "w-team-1", "room": "team", "bot": "a", "source": "addressed", "starting_thread": "t1",
              "hwm": None, "hint": Hint(source="addressed", starting_thread="t1", threads=()), "rules_version": 7}
    return Wake(**{**fields, **more})


async def test_a_reset_wake_starts_a_new_session_and_a_newer_rules_version_refetches_the_rules(tmp_path):
    stub = StubDoorbell()
    runner = Runner(stub, ADAPTERS["claude"]("claude"), tmp_path / "state", tmp_path)  # type: ignore[arg-type]
    runner.sessions["team"] = "old"
    assert runner._session_for(a_wake()) == ("old", False)
    chosen, first = runner._session_for(a_wake(reset=True))
    assert first and chosen != "old" and runner.sessions["team"] == chosen
    assert runner._session_for(a_wake(rules_version=8)) == (chosen, False)
    await runner._refresh_rules(a_wake())
    assert runner.rules.read_text() == "rules v1" and stub.rules_calls == 1
    await runner._refresh_rules(a_wake())  # same version: not fetched again
    assert stub.rules_calls == 1
    await runner._refresh_rules(a_wake(rules_version=8))
    assert runner.rules.read_text() == "rules v2"
    again = Runner(stub, ADAPTERS["claude"]("claude"), tmp_path / "state", tmp_path)  # type: ignore[arg-type]
    assert again.sessions["team"] == chosen and again.rules_version == 7  # the state file survives a restart


async def test_the_loop_stops_on_a_refused_token_and_waits_out_an_unreachable_gateway(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_module, "RETRY_SECONDS", (0.01,))
    stub = StubDoorbell()
    stub.polls = [OSError("connection refused"), None, DoorbellError(401, "bad token")]
    runner = Runner(stub, ADAPTERS["claude"]("claude"), tmp_path / "state", tmp_path)  # type: ignore[arg-type]
    assert await runner.serve(wait=0) == 1
    err = capsys.readouterr().err
    assert "polling again in 0 s" in err and "refuses the token" in err


def test_the_connection_defaults_come_from_agent_toml_under_the_environment(tmp_path, monkeypatch):
    from coop.agent import cli

    monkeypatch.setattr(cli, "AGENT_TOML", tmp_path / "agent.toml")
    monkeypatch.delenv("COOP_GATEWAY", raising=False)
    monkeypatch.delenv("COOP_TOKEN", raising=False)
    assert cli.defaults() == {}
    (tmp_path / "agent.toml").write_text('[connection]\ngateway = "http://g:1"\ntoken = "t"\ntransfer_timeout = 300\n')
    assert cli.defaults() == {"gateway": "http://g:1", "token": "t", "transfer_timeout": "300"}
    monkeypatch.setenv("COOP_TOKEN", "from-env")
    assert cli.defaults()["token"] == "from-env"
    args = cli.parser().parse_args(["read", "--room", "r"])
    assert (args.gateway, args.token, args.transfer_timeout) == ("http://g:1", "from-env", 300.0)
