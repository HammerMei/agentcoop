"""Run a simulator scenario through the room core with the fake agent:
``python -m tools.fakeagent tools/roomsim/scenarios/handoff.yaml [--enforce on|off]
[--json FILE] [--show-rules] [--param NAME=VALUE]``.

The run is deterministic and needs no model. Its transcript and rule table are the
simulator's (``python -m tools.roomsim``), for design §6.2's replay parity; the
``--json`` document holds the posts, the trace and every gateway's status.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .scenario import Scenario, load, run

_MARKERS = {
    "turn_start": lambda e: f"→ {e.bot} turn ({e.source}, thread {e.thread}{', exempt' if e.exempt else ''})",
    "lease_started": lambda e: f"  {e.bot} polled {e.wake}",
    "turn_held": lambda e: f"‖ {e.bot} held by {e.gate} ({e.source}, thread {e.thread})",
    "turn_end": lambda e: f"← {e.bot} turn {e.outcome}",
    "invalid": lambda e: f"✗ {e.bot} invalid turn: {e.reason}",
    "refused": lambda e: f"✗ {e.bot} send refused in {e.thread}: {e.reason}",
    "notice": lambda e: f"⚠ {e.bot} failure notice ({e.cause})",
    "control_applied": lambda e: f"⚙ {e.control} {e.value} by {e.by} in {e.thread}",
    "classifier_decision": lambda e: f"? post {e.post}: add {e.add}{' remove ' + str(e.remove) if e.remove else ''}"
    f"{' fallback ' + e.fallback if e.fallback else ''}",
    "classifier_expired": lambda e: f"? post {e.post}: decision for {e.bot} expired",
    "backoff": lambda e: f"⏳ {e.bot} backoff until {e.until:.0f}",
    "degraded": lambda e: f"⛔ {e.bot} degraded after {e.streak} invalid turns",
    "rule": lambda e: f"§ {e.bot} {e.mechanism} {e.outcome} in {e.thread}",
}


def transcript(scenario: Scenario, show_rules: bool = False) -> str:
    """Posts in server order, with the gateway events between them."""
    lines = []
    for event in scenario.trace.events:
        if event.kind == "post":
            post = scenario.room.by_id(event.id)
            lines.append(f"[{event.time:7.1f}] #{post.id} [{post.thread}] {post.author}: {post.text}")
        elif event.kind in _MARKERS and (show_rules or event.kind != "rule"):
            lines.append(f"[{event.time:7.1f}]      {_MARKERS[event.kind](event)}")
    return "\n".join(lines)


def rules(scenario: Scenario) -> str:
    merged: dict[str, dict[str, int]] = {}
    for core in scenario.cores:
        for mechanism, count in core.rules.items():
            row = merged.setdefault(mechanism, {"opportunities": 0, "complied": 0, "violated": 0, "rejected": 0,
                                               "held": 0, "held_complied": 0, "held_violated": 0})
            for key in row:
                row[key] += getattr(count, key)
    lines = ["| mechanism | opportunities | complied | violated | rejected | held | held, complied | held, violated |",
             "|---|---|---|---|---|---|---|---|"]
    for mechanism in sorted(merged):
        r = merged[mechanism]
        lines.append(f"| {mechanism} | {r['opportunities']} | {r['complied']} | {r['violated']} | {r['rejected']} "
                     f"| {r['held']} | {r['held_complied']} | {r['held_violated']} |")
    return "\n".join(lines)


def classifier_summary(scenario: Scenario) -> str:
    trace = scenario.trace
    fallbacks = len([e for e in trace.of("classifier_decision") if e.fallback])
    return (f"classifier: {trace.count('classifier_call')} calls, {trace.count('classifier_retry')} retries, "
            f"{fallbacks} fallbacks, {trace.count('classifier_expired')} expired decisions")


def report(scenario: Scenario, show_rules: bool = False) -> str:
    mode = "enforced" if all(c.params.enforce for c in scenario.cores) else "measure-only"
    parts = [f"# {scenario.name} ({mode})", "", "## Transcript", "", transcript(scenario, show_rules), "",
             "## Rules", "", rules(scenario), "", classifier_summary(scenario), ""]
    return "\n".join(parts)


def as_json(scenario: Scenario) -> dict:
    return {
        "room": scenario.room.name,
        "posts": [{"id": p.id, "time": p.time, "thread": p.thread, "author": p.author, "kind": p.kind,
                   "text": p.text, "mentions": sorted(p.mentions)} for p in scenario.room.posts],
        "trace": [{"time": e.time, "kind": e.kind, **e.fields} for e in scenario.trace.events],
        "status": [g.status() for g in scenario.gateways],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fakeagent", description=__doc__.split("\n")[0])
    parser.add_argument("scenario")
    parser.add_argument("--enforce", choices=["on", "off"], default=None)
    parser.add_argument("--json", help="write posts, trace and status to this JSON file")
    parser.add_argument("--show-rules", action="store_true", help="include rule events in the transcript")
    parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                        help="override a scenario param, e.g. catch_up_idle_seconds=60 (repeatable)")
    args = parser.parse_args(argv)
    enforce = None if args.enforce is None else args.enforce == "on"
    overrides = dict(p.split("=", 1) for p in args.param)
    scenario = asyncio.run(run(load(args.scenario, enforce, params=overrides)))
    print(report(scenario, args.show_rules))
    if args.json:
        Path(args.json).write_text(json.dumps(as_json(scenario), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
