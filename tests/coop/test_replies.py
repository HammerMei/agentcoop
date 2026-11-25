"""Section 5.9 as one ``send`` at a time (design §3.5): every invalid combination,
the canonical post, ``all`` resolution. The envelope's parsing tests of
``tests/roomsim/test_envelope.py`` are gone with the envelope; what a ``send`` names is
checked here, what a turn must cover is checked at ``done`` in ``test_turn.py``."""

from __future__ import annotations

import pytest

from coop.room.model import ALL, Reply
from coop.room.replies import ValidationContext, canonical_text, check_reply, resolve_control

CTX = ValidationContext(
    bot="a", members=frozenset({"H", "a", "b", "x"}), agents=frozenset({"a", "b"}),
    orchestrator_in=frozenset({"opt"}),
)


def problems(reply: Reply, ctx: ValidationContext = CTX):
    structural, rules = check_reply(reply, ctx)
    return [(p.mechanism, p.thread, p.reason) for p in structural], [(p.mechanism, p.thread) for p in rules]


class TestInvalidCombinations:
    @pytest.mark.parametrize(
        "reply,reason",
        [
            (Reply("main"), "neither text, control nor a file"),
            (Reply("main", to=["H"], text="   "), "empty text"),
            (Reply("opt", control={"mute": []}), "mute list is empty"),
            (Reply("opt", control={"orchestrator": "a"}), "unsupported control ['orchestrator']"),
            (Reply("opt", control={"mute": ["x"]}), "mute names 'x', not an agent in the room"),
            (Reply("opt", control={}), "control is not an object"),
        ],
    )
    def test_structural_problems_are_refused_in_every_mode(self, reply, reason):
        structural, _ = problems(reply)
        assert reason in [s[2] for s in structural], structural

    def test_text_or_control_in_closed_thread_is_a_rule_problem_per_cause(self):
        ctx = ValidationContext("a", CTX.members, CTX.agents, CTX.orchestrator_in, closed_by=("mute", "rate_posts"))
        _, rules = problems(Reply("closed", to=["H"], text="hi"), ctx)
        assert rules == [("mute", "closed"), ("rate_posts", "closed")]

    def test_control_from_non_orchestrator_is_a_rule_problem(self):
        _, rules = problems(Reply("main", control={"mute": ["b"]}))
        assert rules == [("authority", "main")]

    def test_control_without_to_is_accepted_from_the_orchestrator(self):
        assert problems(Reply("opt", control={"mute": ["b"]})) == ([], [])

    def test_recipient_rules(self):
        assert problems(Reply("main", to=["a"], text="me"))[1] == [("recipients", "main")]
        assert problems(Reply("unf", to=["ghost"], text="x"))[1] == [("recipients", "unf")]

    def test_the_sender_among_other_recipients_is_still_a_recipients_rule(self):
        _, rules = problems(Reply("main", to=["H", "a"], text="and me"))
        assert rules == [("recipients", "main")]

    @pytest.mark.parametrize("to", [None, []])
    def test_text_without_recipients_is_a_recipients_rule_not_a_usage_error(self, to):
        structural, rules = problems(Reply("main", to=to, text="x"))
        assert structural == [] and rules == [("recipients", "main")]

    def test_limit_is_not_a_bot_control(self):  # 5.3: the rate limit is the owner's
        structural, _ = problems(Reply("opt", control={"limit": {"posts": 2}}))
        assert [s[2] for s in structural] == ["unsupported control ['limit']"]


class TestCanonicalForm:
    def test_control_lines_first_then_text_with_exact_mentions(self):
        text = canonical_text(Reply("main", to=["H", "b"], text="@b go; @x is idle", control={"mute": ["x"]}))
        assert text == "mute: @x\n@H @b go; x is idle"

    def test_all_becomes_the_room_wide_mention(self):
        assert canonical_text(Reply("main", to=[ALL], text="heads up")) == "@all heads up"
        assert canonical_text(Reply("main", to=[ALL], text="@channel heads up")) == "@channel heads up"

    def test_resolve_mute_all_excludes_the_orchestrator_and_omits_nobody(self):
        assert resolve_control({"mute": ALL}, ["a", "b", "chair"], set(), "chair") == {"mute": ["a", "b"]}
        assert resolve_control({"mute": ALL}, ["chair"], set(), "chair") == {}

    def test_resolve_unmute_all_names_only_the_muted(self):
        assert resolve_control({"unmute": ALL, "mute": ["a"]}, ["a", "b"], {"b"}, None) == {
            "unmute": ["b"], "mute": ["a"]}
