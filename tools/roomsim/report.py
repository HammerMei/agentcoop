"""What a run produces for people to read (section 8.1): an annotated transcript, per-bot
totals, and the rule table that the enforcement toggle exists for.

The rule table has one row per mechanism: ``opportunities`` is how often a bot faced
the rule; ``complied`` how often it obeyed on its own; ``violated`` how often it broke
it when nothing stopped it (``enforce: false``); ``rejected`` how often it wrote a reply
the gateway refused (``enforce: true``); ``held`` how often the gateway never started
the turn, so the bot chose nothing. With enforcement off that same turn runs with its
starting thread marked closed, and the bot's choice there is ``held, complied`` or
``held, violated``. The same scenario run in both modes gives two pairs side by side:
``violated`` against ``rejected``, and ``held`` against ``held, complied`` plus
``held, violated``: what agents follow by themselves, and what the gateway has to
enforce.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from .gateway import SimGateway
from .sim import Simulation

_MARKERS = {
    "turn_start": lambda e: f"→ {e.bot} turn ({e.source}, thread {e.thread}{', exempt' if e.exempt else ''})",
    "turn_held": lambda e: f"‖ {e.bot} held by {e.gate} ({e.source}, thread {e.thread})",
    "turn_end": lambda e: f"← {e.bot} turn {e.outcome}",
    "invalid": lambda e: f"✗ {e.bot} invalid reply #{e.attempt}: {e.reason}",
    "notice": lambda e: f"⚠ {e.bot} failure notice ({e.cause})",
    "control_applied": lambda e: f"⚙ {e.control} {e.value} by {e.by} in {e.thread}",
    "classifier_decision": lambda e: f"? post {e.post}: add {e.add}{' fallback ' + e.fallback if e.fallback else ''}",
    "classifier_expired": lambda e: f"? post {e.post}: decision for {e.bot} expired",
    "job_scheduled": lambda e: f"⏰ {e.bot} scheduled job {e.job} in {e.thread}",
    "job_fired": lambda e: f"⏰ {e.bot} job {e.job} fired",
    "backoff": lambda e: f"⏳ {e.bot} backoff until {e.until:.0f}",
    "degraded": lambda e: f"⛔ {e.bot} degraded after {e.streak} invalid turns",
    "rule": lambda e: f"§ {e.bot} {e.mechanism} {e.outcome} in {e.thread}",
}


def transcript(sim: Simulation, show_rules: bool = False) -> str:
    """Posts in server order, with the gateway events between them."""
    lines = []
    for event in sim.trace.events:
        if event.kind == "post":
            post = sim.room.by_seq(event.seq)
            lines.append(f"[{event.time:7.1f}] #{post.seq} [{post.thread}] {post.author}: {post.text}")
        elif event.kind in _MARKERS and (show_rules or event.kind != "rule"):
            lines.append(f"[{event.time:7.1f}]      {_MARKERS[event.kind](event)}")
    return "\n".join(lines)


def totals(gateways: Iterable[SimGateway]) -> str:
    head = ("| gateway | bot | turns | posts | words | tokens in | tokens out | cache read | cache write "
            "| cost USD | sweeps | failures | invalid |")
    rows = [head, "|" + "---|" * 13]
    for gw in gateways:
        for bot, t in gw.totals.items():
            rows.append(f"| {gw.name} | {bot} | {t['turns']} | {t['posts']} | {t['words']} | {t['tokens_in']} "
                        f"| {t['tokens_out']} | {t['cache_read']} | {t['cache_write']} | {t['cost_usd']:.4f} "
                        f"| {t['sweeps']} | {t['failures']} | {t['invalid']} |")
    return "\n".join(rows)


def rules(gateways: Iterable[SimGateway]) -> str:
    merged: dict[str, dict[str, int]] = {}
    for gw in gateways:
        for mechanism, count in gw.rules.items():
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


def classifier_summary(sim: Simulation) -> str:
    calls = sim.trace.count("classifier_call")
    retries = sim.trace.count("classifier_retry")
    fallbacks = len([e for e in sim.trace.of("classifier_decision") if e.fallback])
    expired = sim.trace.count("classifier_expired")
    line = f"classifier: {calls} calls, {retries} retries, {fallbacks} fallbacks, {expired} expired decisions"
    attempts = sum(g.classifier_totals["calls"] for g in sim.gateways)
    if attempts:
        failures = sum(g.classifier_totals["failures"] for g in sim.gateways)
        latency = sum(g.classifier_totals["latency"] for g in sim.gateways) / attempts
        cost = sum(g.classifier_totals["cost_usd"] for g in sim.gateways)
        tokens_out = sum(g.classifier_totals["tokens_out"] for g in sim.gateways)
        line += (f"; {attempts} attempts, {failures} failed, mean latency {latency:.1f}s, "
                 f"{tokens_out} tokens out, {cost:.4f} USD")
    return line


def report(sim: Simulation, title: str = "", show_rules: bool = False) -> str:
    gateways = sim.gateways
    mode = "enforced" if all(g.params.enforce for g in gateways) else "measure-only"
    parts = [f"# {title or sim.room.name} ({mode})", "", "## Transcript", "", transcript(sim, show_rules), "",
             "## Totals", "", totals(gateways), "", "## Rules", "", rules(gateways), "",
             classifier_summary(sim), ""]
    return "\n".join(parts)


def as_json(sim: Simulation) -> dict:
    return {
        "room": sim.room.name,
        "posts": [{"seq": p.seq, "time": p.time, "thread": p.thread, "author": p.author, "kind": p.kind,
                   "text": p.text, "mentions": sorted(p.mentions)} for p in sim.room.posts],
        "trace": [{"time": e.time, "kind": e.kind, **e.fields} for e in sim.trace.events],
        "status": [g.status() for g in sim.gateways],
    }


def dump_json(sim: Simulation) -> str:
    return json.dumps(as_json(sim), indent=1, default=str)
