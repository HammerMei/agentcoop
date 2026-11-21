"""The simulator's scenarios through the room core (design §6.2, replay parity): the
same YAML run by ``tools.roomsim`` and by ``tools.fakeagent`` produces the same
mechanism trace, for the mechanisms both still have."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.fakeagent.scenario import _params, load, run
from tools.roomsim import scenario as roomsim

SCENARIOS = Path("tools/roomsim/scenarios")

# Trace kinds both sides emit with the same meaning, and the fields compared. The
# simulator's post seqs become ``p<seq>`` ids here; ``invalid`` events differ by
# construction (no second attempt) and are left out; ``rule`` events are compared minus
# the hinted-thread counter, which is new.
PARITY = {
    "post": ("thread", "author", "post_kind"),
    "turn_start": ("bot", "source", "thread", "exempt", "delivered"),
    "turn_held": ("bot", "source", "thread", "gate"),
    "turn_end": ("bot", "outcome", "posts"),
    "cursor_saved": ("bot", "position"),
    "classifier_call": ("post",),
    "classifier_decision": ("post", "add", "remove", "fallback"),
    "classifier_expired": ("post", "bot"),
    "control_applied": ("thread", "by", "control", "value"),
    "control_ignored": ("thread", "by"),
    "notice": ("bot", "cause"),
    "backoff": ("bot", "until"),
    "degraded": ("bot", "streak"),
    "unfinished_added": ("bot", "thread"),
    "unfinished_removed": ("bot", "thread"),
    "sweep": ("bot", "reason", "candidates"),
    "rule": ("bot", "thread", "mechanism", "outcome"),
}


def _pid(value):
    return f"p{value}" if isinstance(value, int) else value


def _normalise_roomsim(event) -> dict:
    fields = dict(event.fields)
    if "seq" in fields:
        fields["position" if event.kind == "cursor_saved" else "post"] = fields.pop("seq")
        if event.kind != "cursor_saved":
            fields["post"] = _pid(fields["post"])
    if "post" in fields and event.kind.startswith("classifier"):
        fields["post"] = _pid(fields["post"])
    if "delivered" in fields:
        fields["delivered"] = [_pid(s) for s in fields["delivered"]]
    if "candidates" in fields:
        fields["candidates"] = [c.replace("post:", "post:p") if c.startswith("post:") else c for c in fields["candidates"]]
    return fields


def _comparable(events, normalise):
    found = []
    for e in events:
        if e.kind not in PARITY or (e.kind == "rule" and e.fields.get("mechanism") == "hinted_thread"):
            continue
        fields = normalise(e)
        found.append((e.time, e.kind, tuple((k, fields.get(k)) for k in PARITY[e.kind])))
    return found


def _no_jobs(path: Path) -> bool:
    text = path.read_text()
    return "schedule:" not in text and "follow_up" not in text


@pytest.mark.parametrize("enforce", [True, False], ids=["enforce", "measure"])
@pytest.mark.parametrize("path", sorted(p for p in SCENARIOS.glob("*.yaml") if _no_jobs(p)), ids=lambda p: p.stem)
async def test_a_scenario_traces_the_same_through_the_simulator_and_the_core(path, enforce):
    sim = await roomsim.run(roomsim.load(path, enforce=enforce))
    core = await run(load(path, enforce=enforce))
    assert _comparable(core.trace.events, lambda e: dict(e.fields)) == _comparable(sim.trace.events, _normalise_roomsim)


async def test_the_catch_up_corrections_scenario_corrects_both_claims_through_periodic_catch_up():
    scenario = await run(load(SCENARIOS / "catch_up_corrections.yaml"))
    by_author = {}
    for post in scenario.room.posts:
        by_author.setdefault(post.author, []).append(post)
    assert len(by_author["m"]) == 1 and len(by_author["v"]) == 1 and "x" not in by_author and "y" not in by_author
    catch_ups = [e for e in scenario.trace.of("turn_start") if e.source == "catch-up"]
    assert {e.bot for e in catch_ups if e.time == 60.0} == {"x", "y", "m", "v"}  # idle since the start
    assert [e.bot for e in catch_ups if e.time == 90.0] == ["b"]  # idle since its turn at 2: sees the corrections
    assert {e.bot for e in catch_ups if e.time == 150.0} == {"x", "y", "m", "v"}  # second wave: done


def test_scenario_params_accept_overrides_and_reject_unknown_values():
    assert _params({"catch_up_idle_seconds": 120}, None, {"catch_up_idle_seconds": "5"}).catch_up_idle_seconds == 5
    assert _params({"follow_up_retry_cap": 3}, None).failure_backoff_seconds == 30  # the simulator's param is ignored
    with pytest.raises(ValueError, match="unknown param"):
        _params(None, None, {"catch_up_order": "roster"})
    with pytest.raises(ValueError, match="true or false"):
        _params(None, None, {"classifier_enabled": "treu"})
    assert _params(None, None, {"classifier_enabled": "False"}).classifier_enabled is False
