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
    print(json.dumps({"type": "result", "session_id": "named-before-the-sleep"}), flush=True)
    child = subprocess.Popen([sys.executable, "-c", "import time\\nwhile True:\\n open(%r, 'a').write('.')\\n time.sleep(0.1)" % str(here / "heartbeat")])
    (here / "child.pid").write_text(str(child.pid))
    time.sleep(30)
elif mode == "stubborn":
    # exits at once; its grandchild ignores SIGTERM and holds stdout open
    subprocess.Popen([sys.executable, "-c", "import signal, time\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\ntime.sleep(60)"])
elif mode == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "thread-" + ("resumed" if sys.argv[1:3] == ["exec", "resume"] else "fresh")}))
    print(json.dumps({"type": "turn.completed"}))
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


@pytest.fixture(params=[2])
async def served(request):
    """A doorbell on a real port; the lease is the test's (2 s: the ``sleeps`` fake is
    cut short; 10 s: a fake that runs two ``coop`` commands finishes on a loaded runner)."""
    sb = Sandbox(clock=AsyncioClock(), attach_agent=False, params=replace(FAST, lease_seconds=request.param))
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


@pytest.mark.parametrize("served", [10], indirect=True)
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
    assert heartbeat.exists()  # the grandchild ran
    size = heartbeat.stat().st_size
    await asyncio.sleep(0.5)
    assert heartbeat.stat().st_size == size  # and stopped with the group
    assert json.loads((tmp_path / "state" / "sessions.json").read_text()) == {"team": "named-before-the-sleep"}  # what the command printed before it was stopped is kept


async def test_a_session_that_did_not_resume_is_dropped_and_said(served, tmp_path, capsys):
    sb, doorbell, url = served
    runner = runner_for(doorbell, url, tmp_path, "fails")
    runner.sessions["team"] = "stale-session"
    await sb.human("@a hello", "t1")
    await runner.take(await doorbell.poll())
    err = capsys.readouterr().err
    assert "session stale-session for team did not resume; dropped" in err and "coop reset <agent> team" in err
    assert "team" not in runner.sessions and json.loads((tmp_path / "state" / "sessions.json").read_text()) == {}
    assert "no done from agent (exit 2)" in err


async def test_a_stubborn_grandchild_holding_stdout_is_killed_with_the_group_and_the_utility_moves_on(served, tmp_path, capsys):
    sb, doorbell, url = served
    runner = runner_for(doorbell, url, tmp_path, "stubborn")
    await sb.human("@a hello", "t1")
    started = time.monotonic()
    await runner.take(await doorbell.poll())
    assert time.monotonic() - started < 20  # the lease (2 s) + the grace (5 s) + the bounded read (5 s), never 60
    assert "lease expired" in capsys.readouterr().err  # the kill reached the grandchild: the pipes closed, the read ended


async def test_codex_names_its_thread_on_the_first_turn_resumes_it_and_a_reset_forgets_it(served, tmp_path):
    sb, doorbell, url = served
    runner = runner_for(doorbell, url, tmp_path, "codex", kind="codex")
    await sb.human("@a hello", "t1")
    await runner.take(await doorbell.poll())
    argv = json.loads((tmp_path / "codex" / "argv.json").read_text())
    assert argv[:2] == ["exec", "--json"] and runner.sessions == {"team": "thread-fresh"}
    await sb.human("@a more", "t1")
    await runner.take(await doorbell.poll())
    argv = json.loads((tmp_path / "codex" / "argv.json").read_text())
    assert argv[:3] == ["exec", "resume", "--json"] and "thread-fresh" in argv and runner.sessions == {"team": "thread-resumed"}
    sb.gw.reset("a")
    await sb.human("@a again", "t1")
    wake = await doorbell.poll()
    assert wake.reset
    runner.adapter = replace(runner.adapter, argv=lambda p, t: [sys.executable, "-c", "import sys; sys.exit(3)"])
    await runner.take(wake)  # the reset's turn fails before a thread is named: the old one is gone all the same
    assert runner.sessions == {} and json.loads((tmp_path / "state" / "sessions.json").read_text()) == {}


async def test_stop_cancels_the_poll_ends_the_command_and_fails_its_wake_as_stopped(served, tmp_path, capsys):
    sb, doorbell, url = served
    runner = runner_for(doorbell, url, tmp_path, "sleeps")
    await sb.human("@a hello", "t1")
    serving = asyncio.ensure_future(runner.serve(wait=5))
    await asyncio.sleep(0.8)  # the wake is taken, the command sleeping
    assert runner._process is not None
    runner.stop()
    assert await asyncio.wait_for(serving, 10) == 0
    assert "utility stopped" in capsys.readouterr().err
    assert sb.trace.last("turn_end", bot="a").outcome == "backend_failure"


async def test_a_refused_done_is_said_and_does_not_stop_the_utility(served, tmp_path, capsys, monkeypatch):
    sb, doorbell, url = served
    runner = runner_for(doorbell, url, tmp_path, "silent")

    async def refused(*a, **kw):
        raise DoorbellError(504, "the platform did not answer")

    monkeypatch.setattr(doorbell, "done", refused)
    await sb.human("@a hello", "t1")
    await runner.take(await doorbell.poll())  # does not raise
    assert "was refused (504" in capsys.readouterr().err


async def test_rules_that_cannot_be_fetched_fail_the_wake_and_do_not_stop_the_utility(served, tmp_path, capsys, monkeypatch):
    sb, doorbell, url = served
    runner = runner_for(doorbell, url, tmp_path, "silent")

    async def down(*a, **kw):
        import httpx

        raise httpx.ConnectError("down")

    monkeypatch.setattr(doorbell, "rules", down)
    await sb.human("@a hello", "t1")
    await runner.take(await doorbell.poll())
    assert "rules could not be fetched" in capsys.readouterr().err
    assert sb.trace.last("turn_end", bot="a").outcome == "backend_failure"


# ── the runner's parts, with a stub doorbell ────────────────────────────────


# The doubles below serve this file only (a stub doorbell and a wake with defaults for
# the runner's parts, a fake CLI script): no other suite drives the utility.
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
    import httpx

    stub.polls = [OSError("connection refused"), httpx.ConnectError("All connection attempts failed"), None,
                  DoorbellError(401, "bad token")]
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
