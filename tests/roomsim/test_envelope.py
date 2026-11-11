"""Section 5.9: every invalid combination, the canonical post, ``all`` resolution."""

from __future__ import annotations

import pytest

from tools.roomsim.envelope import (
    ParseError,
    ValidationContext,
    canonical_text,
    parse_envelope,
    resolve_control,
    validate,
)
from tools.roomsim.model import ALL, Hint, ReplyItem, ThreadGroup

HINT = Hint(
    source="addressed",
    starting_thread="main",
    threads=(
        ThreadGroup("main", "must-reply"),
        ThreadGroup("opt", "optional"),
        ThreadGroup("closed", "closed", ("mute",)),
        ThreadGroup("unf", "must-reply"),
    ),
)
CTX = ValidationContext(
    bot="a", members=frozenset({"H", "a", "b", "x"}), agents=frozenset({"a", "b"}),
    orchestrator_in=frozenset({"opt"}),
)


def problems(env_json: str):
    structural, rules = validate(parse_envelope(env_json), HINT, CTX)
    return [(p.mechanism, p.thread, p.reason) for p in structural], [(p.mechanism, p.thread) for p in rules]


class TestParsing:
    def test_end_token_alone_is_one_done(self):
        env = parse_envelope("  <end-of-agent-chain>\n")
        assert [i.done for i in env.replies] == [True]

    def test_end_token_with_text_is_invalid(self):
        with pytest.raises(ParseError):
            parse_envelope("sure <end-of-agent-chain>")

    def test_fenced_block_and_whole_response(self):
        fenced = 'Here:\n```json\n{"replies": [{"done": true}]}\n```'
        assert parse_envelope(fenced).replies[0].done
        assert parse_envelope('{"replies": [{"done": true}]}').replies[0].done

    @pytest.mark.parametrize("text", ["no json here", '{"done": true}', '{"replies": "x"}', "```json\n{bad}\n```"])
    def test_replies_absent_or_unparsable(self, text):
        with pytest.raises(ParseError):
            parse_envelope(text)

    def test_schedule_is_parsed(self):
        env = parse_envelope('{"replies": [{"done": true}], "schedule": [{"in_seconds": 60, "text": "t"}]}')
        assert env.schedule[0].in_seconds == 60


class TestInvalidCombinations:
    @pytest.mark.parametrize(
        "body,reason",
        [
            ('{"thread": "main"}', "item has no action"),
            ('{"thread": "main", "done": true, "text": "x", "to": ["H"]}', "done with text or control"),
            ('{"thread": "main", "text": "   ", "to": ["H"]}', "empty text"),
            ('{"thread": "zzz", "done": true}', "thread not delivered nor listed"),
            ('{"thread": "opt", "control": {"mute": []}}', "mute list is empty"),
            ('{"thread": "opt", "control": {"orchestrator": "a"}}', "unsupported control ['orchestrator']"),
            ('{"thread": "opt", "control": {"mute": ["x"]}}', "mute names 'x', not an agent in the room"),
            ('{"thread": "opt", "control": {}}', "control is not an object"),
        ],
    )
    def test_structural_problems_are_rejected_in_every_mode(self, body, reason):
        structural, _ = problems('{"replies": [' + body + ', {"thread": "unf", "done": true}]}')
        assert (reason in [s[2] for s in structural]), structural

    def test_must_reply_thread_without_item(self):
        _, rules = problems('{"replies": [{"thread": "main", "done": true}]}')
        assert rules == [("must_reply", "unf")]

    def test_optional_and_closed_threads_need_no_item(self):
        structural, rules = problems('{"replies": [{"done": true}, {"thread": "unf", "done": true}]}')
        assert structural == [] and rules == []

    def test_text_or_control_in_closed_thread_is_a_rule_problem_per_cause(self):
        _, rules = problems('{"replies": [{"done": true}, {"thread": "unf", "done": true},'
                            ' {"thread": "closed", "to": ["H"], "text": "hi"}]}')
        assert rules == [("mute", "closed")]

    def test_done_in_closed_thread_is_fine(self):
        _, rules = problems('{"replies": [{"done": true}, {"thread": "unf", "done": true},'
                            ' {"thread": "closed", "done": true, "to": ["b"]}]}')
        assert rules == []

    def test_control_from_non_orchestrator_is_a_rule_problem(self):
        _, rules = problems('{"replies": [{"control": {"mute": ["b"]}}, {"thread": "unf", "done": true}]}')
        assert rules == [("authority", "main")]

    def test_control_without_to_is_accepted_from_the_orchestrator(self):
        structural, rules = problems('{"replies": [{"thread": "opt", "control": {"limit": {"posts": 2}}},'
                                     ' {"done": true}, {"thread": "unf", "done": true}]}')
        assert structural == [] and rules == []

    def test_recipient_rules(self):
        _, rules = problems('{"replies": [{"to": ["a"], "text": "me"}, {"thread": "unf", "to": ["ghost"], "text": "x"}]}')
        assert rules == [("recipients", "main"), ("recipients", "unf")]

    @pytest.mark.parametrize("body", ['{"thread": "main", "text": "x"}', '{"thread": "main", "text": "x", "to": []}'])
    def test_text_without_recipients_is_a_recipients_rule_not_a_schema_error(self, body):
        structural, rules = problems('{"replies": [' + body + ', {"thread": "unf", "done": true}]}')
        assert structural == [] and rules == [("recipients", "main")]

    @pytest.mark.parametrize("text", [
        '{"replies": [{"to": 5, "text": "x"}]}',
        '{"replies": [{"to": ["H"], "text": ["x"]}]}',
        '{"replies": [{"thread": {"a": 1}, "done": true}]}',
        '{"replies": [{"control": "mute"}]}',
        '{"replies": [{"done": true}], "schedule": [{"in_seconds": "soon"}]}',
    ])
    def test_wrong_types_are_parse_errors_not_crashes(self, text):
        with pytest.raises(ParseError):
            parse_envelope(text)

    def test_limit_shape_is_checked(self):
        structural, _ = problems('{"replies": [{"thread": "opt", "control": {"limit": {"posts": "ten"}}},'
                                 ' {"done": true}, {"thread": "unf", "done": true}]}')
        assert [s[2] for s in structural] == ["limit must map posts/words to integers"]

    def test_thread_defaults_to_the_starting_thread(self):
        env = parse_envelope('{"replies": [{"to": ["H"], "text": "x"}, {"thread": "unf", "done": true}]}')
        validate(env, HINT, CTX)
        assert env.replies[0].thread == "main"


class TestCanonicalForm:
    def test_control_lines_first_then_text_with_exact_mentions(self):
        text = canonical_text(ReplyItem(to=["H", "b"], text="@b go; @x is idle", control={"mute": ["x"]}))
        assert text == "mute: @x\n@H @b go; x is idle"

    def test_all_becomes_the_room_wide_mention(self):
        assert canonical_text(ReplyItem(to=[ALL], text="heads up")) == "@all heads up"
        assert canonical_text(ReplyItem(to=[ALL], text="@channel heads up")) == "@channel heads up"

    def test_resolve_mute_all_excludes_the_orchestrator_and_omits_nobody(self):
        assert resolve_control({"mute": ALL}, ["a", "b", "chair"], set(), "chair") == {"mute": ["a", "b"]}
        assert resolve_control({"mute": ALL}, ["chair"], set(), "chair") == {}

    def test_resolve_unmute_all_names_only_the_muted(self):
        assert resolve_control({"unmute": ALL, "limit": {"posts": 1}}, ["a", "b"], {"b"}, None) == {
            "unmute": ["b"], "limit": {"posts": 1}}
