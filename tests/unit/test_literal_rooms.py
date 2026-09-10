"""A connector that cannot discover rooms may only be given literal ones.

Design §2.6: each connector declares whether its transport delivers **unsolicited
inbound** messages. Mattermost gets every channel the bot belongs to on one socket;
Rocket.Chat can subscribe-all via `__my_messages__`. Script's messages arrive by
direct injection that bypasses the connector, and Voice's rooms arrive as HTTP path
segments — neither has a stream to discover rooms from.

A rule is a pattern matched against rooms *as they turn up*. On a connector with no
inbound stream, a wildcard include or a DM opt-in therefore describes rooms nothing
will ever offer: the rule loads, looks correct, and silently never fires. That is why
this is a load error and not a warning — and `RoomPattern.is_literal` was added for
this check and, until now, never consulted.

The declaration exists twice by necessity — as `Connector.supports_unsolicited_inbound()`
for runtime and as `TYPES_WITH_UNSOLICITED_INBOUND` for the loader, which only ever
sees a type string — so one test here walks every type the connector factory knows
and binds them together.

Run with:
    uv run python -m pytest tests/unit/test_literal_rooms.py -v
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from gateway import config as config_module
from gateway.config import (
    GatewayConfig,
    _parse_one_watcher_rule,
    collect_config,
)
from gateway.core.config import AgentConfig, ConnectorConfig
from gateway.core.connector import (
    SUPPORTED_CONNECTOR_TYPES,
    TYPES_WITH_UNSOLICITED_INBOUND,
)

AGENTS = {"a1": AgentConfig(name="a1"), "a2": AgentConfig(name="a2")}


def parse(entry, *, connectors, index=0):
    # `agent` and `connector` are both required on every rule now, and these
    # cases are about neither — defaults supplied here so each entry below stays
    # about the room matcher. An entry that names its own connector (several do,
    # deliberately) still wins, since it is spread in afterwards.
    entry = {"agent": "a1", "connector": connectors[0].name, **entry}
    return _parse_one_watcher_rule(
        entry,
        index,
        connectors=connectors,
        connector_names={c.name for c in connectors},
        agents=AGENTS,
        config_dir=Path("/tmp"),
        templates={},
        seen_rule_names=set(),
    )


VOICE_FIRST = [
    ConnectorConfig(name="voice", type="voice", raw={}),
    ConnectorConfig(name="mm", type="mattermost", raw={}),
]
MM_FIRST = [
    ConnectorConfig(name="mm", type="mattermost", raw={}),
    ConnectorConfig(name="voice", type="voice", raw={}),
]


class TestTheTwoDeclarationsAgree(unittest.TestCase):
    """The set the loader reads and the method the runtime calls must not drift.

    Two declarations of one fact is the shape that has bitten this loader repeatedly.
    They cannot be collapsed — enforcement happens in `gateway/config.py`, which only
    sees a type string, and `gateway/connectors/` imports `gateway.config`, so reading
    the classes from the loader would invert the dependency and pull the websocket
    stack into `coop config validate`. So they are bound here instead of by a comment.
    """

    # Every type gateway/connectors/__init__.py's factory accepts.
    FACTORY_TYPES = ("rocketchat", "mattermost", "script", "voice")

    def _connector_class(self, type_name: str):
        if type_name == "rocketchat":
            from gateway.connectors.rocketchat import RocketChatConnector
            return RocketChatConnector
        if type_name == "mattermost":
            from gateway.connectors.mattermost import MattermostConnector
            return MattermostConnector
        if type_name == "script":
            from gateway.connectors.script import ScriptConnector
            return ScriptConnector
        if type_name == "voice":
            from gateway.connectors.voice import VoiceConnector
            return VoiceConnector
        raise AssertionError(f"unhandled type {type_name}")

    def test_every_factory_type_agrees_with_the_loader_set(self):
        for type_name in self.FACTORY_TYPES:
            with self.subTest(type=type_name):
                cls = self._connector_class(type_name)
                declared = cls.supports_unsolicited_inbound(cls)  # unbound, no I/O
                self.assertEqual(
                    declared,
                    type_name in TYPES_WITH_UNSOLICITED_INBOUND,
                    f"{cls.__name__} declares {declared} but the loader set says "
                    f"{type_name in TYPES_WITH_UNSOLICITED_INBOUND}",
                )

    def test_the_factory_type_list_here_is_complete(self):
        """If the factory learns a fifth type, this list must grow with it — else the
        agreement above silently stops covering the new connector."""
        source = (
            Path(__file__).resolve().parents[2]
            / "gateway" / "connectors" / "__init__.py"
        ).read_text()
        for type_name in self.FACTORY_TYPES:
            self.assertIn(f'cc.type == "{type_name}"', source)
        self.assertEqual(
            source.count("if cc.type =="),
            len(self.FACTORY_TYPES),
            "the connector factory handles a type this test does not know about",
        )

    def test_the_abc_default_is_fail_closed(self):
        """A new connector is restricted until it declares otherwise: the silent
        failure (a pattern that never matches) is worse than the loud one."""
        from gateway.core.connector import Connector

        self.assertFalse(Connector.supports_unsolicited_inbound(Connector))


class TestRulesOnAConnectorWithoutInbound(unittest.TestCase):
    def _err(self, entry, connectors=MM_FIRST) -> str:
        with self.assertRaises(ValueError) as cm:
            parse(entry, connectors=connectors)
        return str(cm.exception)

    def test_a_wildcard_include_is_rejected(self):
        """Asserts the message's CONTRACT, not its prose.

        The previous version pinned the phrase "no unsolicited inbound stream",
        which is exactly the wording the message was rewritten to remove — so it
        locked the error into explaining the gateway's internals. What a reader
        actually needs is: which connector, which value is wrong, and which field
        to edit. Pin that instead; the sentence is then free to improve.
        """
        msg = self._err({"name": "r", "connector": "voice", "rooms": {"include": ["*"]}})
        self.assertIn("voice", msg, "must name the connector type")
        self.assertIn("'*'", msg, "must quote the offending value")
        self.assertIn("rooms.include", msg, "must name the field to edit")

    def test_every_non_literal_form_is_rejected(self):
        for pattern in ("eng-*", "eng-?", "eng-[ab]", "*"):
            with self.subTest(pattern=pattern):
                msg = self._err({
                    "name": "r", "connector": "voice", "rooms": {"include": [pattern]},
                })
                self.assertIn(pattern, msg)

    def test_a_single_bad_pattern_among_literals_is_still_rejected(self):
        """The check is per pattern, not "does it have at least one literal"."""
        msg = self._err({
            "name": "r", "connector": "voice",
            "rooms": {"include": ["hotline", "eng-*"]},
        })
        self.assertIn("eng-*", msg)

    def test_a_dm_opt_in_is_rejected(self):
        for flag in ("direct", "group_direct"):
            with self.subTest(flag=flag):
                msg = self._err({"name": "r", "connector": "voice", "rooms": {flag: True}})
                # Names the flag actually written, so a group_direct mistake is
                # not reported as a 'rooms.direct' one — the old assertion passed
                # only because it matched a substring of the generic wording.
                self.assertIn(f"rooms.{flag}", msg)
                self.assertIn("rooms.include", msg, "must say where to put them")

    def test_a_dm_opt_in_alongside_literal_rooms_is_still_rejected(self):
        msg = self._err({
            "name": "r", "connector": "voice",
            "rooms": {"include": ["hotline"], "direct": True},
        })
        self.assertIn("rooms.direct", msg)

    def test_the_script_connector_is_restricted_too(self):
        connectors = [
            ConnectorConfig(name="mm", type="mattermost", raw={}),
            ConnectorConfig(name="sc", type="script", raw={}),
        ]
        msg = self._err(
            {"name": "r", "connector": "sc", "rooms": {"include": ["eng-*"]}},
            connectors=connectors,
        )
        self.assertIn("'script'", msg)

    def test_a_type_the_factory_knows_but_the_set_does_not_is_restricted(self):
        """Fail-closed, and this is the case it protects: someone adds a fifth
        connector to the factory and forgets to declare whether it has a stream.

        Simulated by adding a name to the factory's supported list without adding
        it to the inbound set — which is exactly the state that mistake produces.
        The completeness test in this file catches the omission separately; this
        asserts what the loader does in the meantime, which is to restrict.
        """
        connectors = [
            ConnectorConfig(name="mm", type="mattermost", raw={}),
            ConnectorConfig(name="mystery", type="carrier-pigeon", raw={}),
        ]
        with mock.patch.object(
            config_module,
            "SUPPORTED_CONNECTOR_TYPES",
            (*SUPPORTED_CONNECTOR_TYPES, "carrier-pigeon"),
        ):
            msg = self._err(
                {"name": "r", "connector": "mystery", "rooms": {"include": ["eng-*"]}},
                connectors=connectors,
            )
        self.assertIn("carrier-pigeon", msg)

    def test_a_type_that_exists_nowhere_is_left_to_the_type_check(self):
        """The other half of the split, and the reason it exists.

        A misspelling is not a connector without a stream — it is not a connector
        at all. This check used to answer it with a lecture about `rooms.direct`
        that never mentioned the missing letter, and whose suggested remedy (use
        literal `rooms.include`) silenced the complaint while leaving the type
        wrong. `config validate` now reports the type itself, so this stays quiet.
        """
        connectors = [
            ConnectorConfig(name="mm", type="mattermost", raw={}),
            ConnectorConfig(name="oops", type="mattrmost", raw={}),
        ]
        rule = parse(
            {"name": "r", "connector": "oops", "rooms": {"direct": True}},
            connectors=connectors,
        )
        self.assertEqual(rule.connector, "oops", "the rule must still load")

    def test_the_error_names_the_rule_and_the_connector(self):
        msg = self._err({"name": "hotline", "connector": "voice", "rooms": {"include": ["*"]}})
        self.assertIn("hotline", msg)
        self.assertIn("voice", msg)
        # No design-doc section number: a §-reference sends a reader who just
        # wanted their config to work into an architecture document.
        self.assertNotIn("§", msg)
        self.assertNotIn("2.6", msg)


class TestTheResolvedConnectorIsWhatCounts(unittest.TestCase):
    """A rule with no `connector:` falls back to `connectors[0]`.

    Checking the *written* connector would miss this entirely: the rule names nothing,
    so there is no type in the entry to look at.
    """

    def test_a_defaulted_connector_that_lacks_inbound_is_enforced(self):
        with self.assertRaises(ValueError) as cm:
            parse({"name": "r", "rooms": {"include": ["eng-*"]}}, connectors=VOICE_FIRST)
        self.assertIn("connector 'voice'", str(cm.exception))

    def test_a_defaulted_connector_that_has_inbound_is_untouched(self):
        rule = parse({"name": "r", "rooms": {"include": ["eng-*"]}}, connectors=MM_FIRST)
        self.assertEqual(rule.connector, "mm")

    def test_naming_a_capable_connector_beats_an_incapable_default(self):
        rule = parse(
            {"name": "r", "connector": "mm", "rooms": {"include": ["eng-*"]}},
            connectors=VOICE_FIRST,
        )
        self.assertEqual(rule.connector, "mm")


class TestWhatMustKeepWorking(unittest.TestCase):
    def test_literal_rooms_on_a_connector_without_inbound_are_accepted(self):
        rule = parse(
            {"name": "r", "connector": "voice", "rooms": {"include": ["hotline", "支援"]}},
            connectors=MM_FIRST + [ConnectorConfig(name="voice", type="voice", raw={})],
        )
        self.assertEqual([p.raw for p in rule.rooms.include], ["hotline", "支援"])

    def test_two_literal_rules_on_one_voice_connector_serve_two_agents(self):
        """§2.6 calls this load-bearing rather than a convenience: a voice path
        segment is also its only agent selector, so two rules with literal rooms on
        one connector are how one port serves two agents. The enforcement must not
        break it."""
        connectors = [ConnectorConfig(name="voice", type="voice", raw={})]
        seen: set[str] = set()
        rules = [
            _parse_one_watcher_rule(
                {"name": name, "connector": "voice", "agent": agent,
                 "rooms": {"include": [room]}},
                i,
                connectors=connectors,
                connector_names={"voice"},
                agents=AGENTS,
                config_dir=Path("/tmp"),
                templates={},
                seen_rule_names=seen,
            )
            for i, (name, agent, room) in enumerate(
                [("sales", "a1", "sales"), ("support", "a2", "support")]
            )
        ]
        self.assertEqual([(r.agent, r.rooms.include[0].raw) for r in rules],
                         [("a1", "sales"), ("a2", "support")])

    def test_patterns_and_dms_are_untouched_on_a_capable_connector(self):
        rule = parse(
            {"name": "r", "connector": "mm",
             "rooms": {"include": ["eng-*"], "direct": True, "group_direct": True}},
            connectors=MM_FIRST,
        )
        self.assertTrue(rule.rooms.direct)
        self.assertTrue(rule.rooms.group_direct)

    def test_a_literal_rule_on_a_voice_connector_loads(self):
        """The positive case the enforcement must not over-reach into: a rule
        naming its rooms literally is exactly what §2.6 requires of a
        connector with no unsolicited inbound, and the eager-start loop is
        what materializes it at boot."""
        cfg = GatewayConfig.from_file(_write_config("""\
            - name: hotline
              agent: default
              connector: voice
              rooms: {include: [hotline]}
            """))
        self.assertEqual([r.name for r in cfg.watcher_rules], ["hotline"])


def _write_config(watchers_block: str) -> str:
    body = textwrap.dedent("""\
        connectors:
          - name: voice
            agent: default
            type: voice
            server: {port: 8099}
          - name: mm
            agent: default
            type: mattermost
            server: {url: http://localhost:8065, token: t, team: lab}
        agents:
          default:
            type: claude
            working_directory: /tmp
        watcher_rules:
        """) + textwrap.indent(textwrap.dedent(watchers_block), "  ")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(body)
        return f.name


class TestThroughBothLoaders(unittest.TestCase):
    """A parser-level raise is not the contract; what the two loaders do with it is."""

    BAD = """\
    - name: everything
      agent: default
      connector: voice
      rooms: {include: ["*"]}
    """

    def test_from_file_fails_fast(self):
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(_write_config(self.BAD))
        # Contract, not prose — see test_a_wildcard_include_is_rejected.
        self.assertIn("cannot discover rooms", str(cm.exception))
        self.assertIn("rooms.include", str(cm.exception))

    def test_collect_config_attributes_it_and_keeps_going(self):
        cfg, issues = collect_config(_write_config("""\
            - name: everything
              agent: default
              connector: voice
              rooms: {include: ["*"]}
            - name: fine
              agent: default
              connector: mm
              rooms: {include: ["eng-*"]}
            """))
        self.assertEqual(len(issues), 1, [i.message for i in issues])
        self.assertEqual(issues[0].entity_kind, "watcher")
        self.assertEqual(issues[0].entity_name, "everything")
        self.assertEqual([r.name for r in cfg.watcher_rules], ["fine"])

    def test_a_literal_voice_rule_loads_through_from_file(self):
        cfg = GatewayConfig.from_file(_write_config("""\
            - name: hotline
              agent: default
              connector: voice
              rooms: {include: [hotline]}
            """))
        self.assertEqual([r.name for r in cfg.watcher_rules], ["hotline"])


if __name__ == "__main__":
    unittest.main()
