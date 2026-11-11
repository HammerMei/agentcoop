"""Run a scenario: ``python -m tools.roomsim scenarios/handoff.yaml [--bots scripted|claude]
[--enforce on|off] [--record FILE | --replay FILE] [--json FILE] [--workdir DIR]``.

Scripted runs are deterministic and need no model. A ``claude`` run sends each bot
turn through the gateway's ``ClaudeBackend`` with the permission broker off, one
session per bot; its wall-clock turn durations become simulated durations. ``--record``
stores every turn's input hash and output so ``--replay`` reproduces the run without a
model and fails loudly if the scenario has drifted.

A live bot session inherits the operator's Claude Code configuration (user-level
instructions, hooks, memory) unless ``--config-dir`` points ``CLAUDE_CONFIG_DIR`` at a
directory set up for the lab; the CLI must be logged in there once, interactively.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from .bots import BackendBot, Recorder, Replayer
from .report import dump_json, report
from .scenario import load, run


def _runner(args, members, room_name):
    if args.replay:
        return Replayer(json.loads(Path(args.replay).read_text()))
    if args.bots == "scripted":
        return None
    from gateway.agents.claude.adapter import ClaudeBackend

    # The built-in agent: the operator's configured persona must not reach the bots. A resumed
    # session keeps the agent it was created with, so the first turn's args are enough.
    backend = ClaudeBackend(command=args.claude_command, new_session_args=["--agent", "claude"],
                            timeout=args.timeout, broker_config=None)
    workdir = args.workdir or tempfile.mkdtemp(prefix="roomsim-")
    return BackendBot(backend, workdir, args.timeout, room_name, members)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="roomsim", description=__doc__.split("\n")[0])
    parser.add_argument("scenario")
    parser.add_argument("--bots", choices=["scripted", "claude"], default="scripted")
    parser.add_argument("--enforce", choices=["on", "off"], default=None)
    parser.add_argument("--record", help="write the turns' recording to this JSON file")
    parser.add_argument("--replay", help="replay turns from this recording instead of running bots")
    parser.add_argument("--json", help="write posts, trace and status to this JSON file")
    parser.add_argument("--workdir", help="working directory for live agent sessions")
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--config-dir", help="CLAUDE_CONFIG_DIR for the bot sessions (a lab login, not the operator's)")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--show-rules", action="store_true", help="include rule events in the transcript")
    args = parser.parse_args(argv)
    if args.config_dir:
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(args.config_dir).expanduser())

    enforce = None if args.enforce is None else args.enforce == "on"
    preview = load(args.scenario, enforce)
    runner = _runner(args, list(preview.sim.room.members.values()), preview.sim.room.name)
    recorder = Recorder(runner) if (args.record and runner is not None) else None
    if args.record and runner is None:
        recorder = Recorder(preview.sim.gateways[0].runner)
    scenario = load(args.scenario, enforce, recorder or runner)
    sim = asyncio.run(run(scenario))
    print(report(sim, scenario.name))
    if isinstance(runner, Replayer) and runner.unused():
        print(f"[ERROR] replay used {len(runner.records) - runner.unused()} of {len(runner.records)} recorded turns",
              file=sys.stderr)
        return 1
    if args.record and recorder is not None:
        Path(args.record).write_text(json.dumps(recorder.records, indent=1))
    if args.json:
        Path(args.json).write_text(dump_json(sim))
    return 0


if __name__ == "__main__":
    sys.exit(main())
