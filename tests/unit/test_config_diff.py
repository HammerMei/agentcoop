"""The config-vs-config diff and digest behind `config reload` (#144).

Pure: two `GatewayConfig`s in, a `ConfigDiff` out. The per-field enumeration
is the one that matters most — a field added to any config entity without a
row in `RELOAD_ACTIONS` fails here, locally, instead of arriving at a reload
that silently does nothing about it.
"""

from __future__ import annotations

import dataclasses
import json
import textwrap
import unittest

from gateway.config import (
    AgentConfig,
    ConnectorConfig,
    GatewayConfig,
    SchedulerConfig,
)
from gateway.config_diff import (
    RELOAD_ACTIONS,
    ValueChange,
    config_digest,
    diff_configs,
    flatten_config,
    redacted_config,
)
from gateway.core.config import ToolRule
from gateway.core.watcher_rule import WatcherRule
from tests.helpers import make_connector_config as _connector
from tests.helpers import make_gateway_config as _config
from tests.helpers import make_rule


class TestEveryFieldIsClassified(unittest.TestCase):

    def test_no_config_field_is_unclassified(self):
        for cls in (ConnectorConfig, AgentConfig, WatcherRule, GatewayConfig, SchedulerConfig):
            with self.subTest(entity=cls.__name__):
                declared = {f.name for f in dataclasses.fields(cls)}
                table = set(RELOAD_ACTIONS[cls])
                self.assertEqual(declared - table, set(),
                                 f"a {cls.__name__} field has no reload classification")
                self.assertEqual(table - declared, set(), "stale entry")

    def test_every_classified_field_change_is_seen_by_the_diff(self):
        """The table says what happens; this checks the diff notices at all.
        One mutation per field, applied to a copy of the active config."""
        active = _config()
        mutations = {
            (ConnectorConfig, "type"): lambda c: dataclasses.replace(
                c.connectors[0], type="mattermost"),
            (ConnectorConfig, "raw"): lambda c: dataclasses.replace(
                c.connectors[0], raw={"server": {"url": "https://other"}}),
            (ConnectorConfig, "context_inject_files"): lambda c: dataclasses.replace(
                c.connectors[0], context_inject_files=["x.md"]),
        }
        for (cls, name), mutate in mutations.items():
            with self.subTest(field=name):
                candidate = _config(connectors=[mutate(active)])
                self.assertEqual(diff_configs(active, candidate).connectors.changed, ["rc"])
        agent_fields = [f for f in dataclasses.fields(AgentConfig) if f.name != "name"]
        for f in agent_fields:
            with self.subTest(field=f.name):
                agent = active.agents["a"]
                new_value = {
                    "type": "opencode", "command": "other", "new_session_args": ["--x"],
                    "working_directory": "/elsewhere", "session_prefix": "p",
                    "lazy_instruction_loading": not agent.lazy_instruction_loading,
                    "context_inject_files": ["x.md"],
                    "owner_allowed_tools": [ToolRule(tool="Z")],
                    "guest_allowed_tools": [ToolRule(tool="Z")],
                    "timeout": agent.timeout + 1,
                    "permissions": dataclasses.replace(
                        agent.permissions, enabled=not agent.permissions.enabled),
                }[f.name]
                candidate = _config(agents={"a": dataclasses.replace(agent, **{f.name: new_value})})
                self.assertEqual(diff_configs(active, candidate).agents.changed, ["a"])


class TestDiff(unittest.TestCase):

    def test_identical_configs_diff_to_nothing(self):
        self.assertFalse(diff_configs(_config(), _config()))

    def test_a_connector_rename_is_a_removal_plus_an_addition(self):
        diff = diff_configs(_config(), _config(connectors=[_connector(name="rc2")]))
        self.assertEqual(diff.connectors.removed, ["rc"])
        self.assertEqual(diff.connectors.added, ["rc2"])
        self.assertEqual(diff.connectors.changed, [])

    def test_a_rotated_token_is_a_connector_change(self):
        diff = diff_configs(_config(connectors=[_connector(token="old")]),
                            _config(connectors=[_connector(token="new")]))
        self.assertEqual(diff.connectors.changed, ["rc"])

    def test_an_agent_working_directory_change_restarts_the_agent(self):
        diff = diff_configs(_config(), _config(agents={"a": AgentConfig(name="a",
                                                                      working_directory="/x")}))
        self.assertEqual(diff.agents.changed, ["a"])

    def test_a_rule_edit_is_a_rules_change(self):
        rule = make_rule(room="eng-*", name="eng", connector="rc", agent="a")
        diff = diff_configs(_config(), _config(rules=[rule]))
        self.assertEqual(diff.rules.changed, ["eng"])
        self.assertTrue(diff.rules_changed)

    def test_reordering_rules_is_a_rules_change_with_no_entity_changed(self):
        r1 = make_rule(room="eng", name="eng", connector="rc", agent="a")
        r2 = make_rule(room="ops", name="ops", connector="rc", agent="a")
        diff = diff_configs(_config(rules=[r1, r2]), _config(rules=[r2, r1]))
        self.assertFalse(diff.rules)
        self.assertTrue(diff.rules_reordered)
        self.assertTrue(diff.rules_changed)
        self.assertTrue(diff)

    def test_top_level_values_are_reported_by_path(self):
        diff = diff_configs(
            _config(),
            _config(max_queue_depth=7, scheduler=SchedulerConfig(completed_job_ttl_days=1)))
        self.assertEqual(diff.values, [
            ValueChange("max_queue_depth", 100, 7),
            ValueChange("scheduler.completed_job_ttl_days", 7, 1),
        ])
        self.assertFalse(diff.connectors or diff.agents or diff.rules_changed)


class TestDigest(unittest.TestCase):

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _load(self, text: str, name="config.yaml") -> GatewayConfig:
        path = self.tmp / name
        path.write_text(textwrap.dedent(text))
        return GatewayConfig.from_file(str(path))

    def test_formatting_comments_and_descriptions_do_not_change_the_digest(self):
        a = self._load(f"""\
            connectors:
              - name: rc
                type: script
            agents:
              a:
                type: claude
                working_directory: {self.tmp}
            watcher_rules:
              - name: eng
                agent: a
                connector: rc
                rooms:
                  include: [eng]
        """)
        b = self._load(f"""\
            # a comment
            watcher_rules:
              - description: "the engineering room"
                rooms: {{include: [eng]}}
                connector: rc
                agent: a
                name: eng
            agents:
              a:
                description: main agent
                working_directory: {self.tmp}
                type: claude
            connectors:
              - type: script
                description: test connector
                name: rc
        """, name="b.yaml")
        self.assertEqual(config_digest(a), config_digest(b))
        self.assertFalse(diff_configs(a, b), "a description is not a change")

    def test_connector_order_changes_neither_the_diff_nor_the_digest(self):
        a = _config(connectors=[_connector(name="rc"), _connector(name="mm")])
        b = _config(connectors=[_connector(name="mm"), _connector(name="rc")])
        self.assertFalse(diff_configs(a, b))
        self.assertEqual(config_digest(a), config_digest(b), "the two notions of equality agree")

    def test_rule_order_still_changes_the_digest(self):
        r1 = make_rule(room="eng", name="eng", connector="rc", agent="a")
        r2 = make_rule(room="ops", name="ops", connector="rc", agent="a")
        self.assertNotEqual(config_digest(_config(rules=[r1, r2])),
                            config_digest(_config(rules=[r2, r1])))

    def test_equivalent_pattern_spellings_digest_alike_as_they_compare_alike(self):
        a = _config(rules=[make_rule(room="eng-*", name="eng", connector="rc", agent="a")])
        b = _config(rules=[make_rule(room="eng-**", name="eng", connector="rc", agent="a")])
        self.assertFalse(diff_configs(a, b), "== says they are the same rule")
        self.assertEqual(config_digest(a), config_digest(b), "so must the digest")

    def test_a_yaml_date_in_a_connectors_raw_block_still_digests(self):
        import datetime
        cfg = _config(connectors=[_connector(build_date=datetime.date(2026, 9, 5))])
        self.assertRegex(config_digest(cfg), r"^[0-9a-f]{64}$")
        self.assertEqual(dict(flatten_config(cfg))["connectors.rc.raw.server.build_date"],
                         "2026-09-05")

    def test_a_mapping_spelled_like_a_tag_cannot_forge_a_date(self):
        import datetime
        as_date = _config(connectors=[_connector(build_date=datetime.date(2026, 9, 5))])
        forged = _config(connectors=[_connector(
            build_date={"$type": "date", "$value": "2026-09-05"})])
        self.assertTrue(diff_configs(as_date, forged))
        self.assertNotEqual(config_digest(as_date), config_digest(forged),
                            "every leaf carries its type outside the value space")

    def test_the_dump_and_the_json_show_plain_values(self):
        cfg = _config()
        flat = dict(flatten_config(cfg))
        self.assertEqual(flat["max_queue_depth"], 100)
        self.assertEqual(flat["connectors.rc.type"], "rocketchat")
        self.assertEqual(redacted_config(cfg)["max_queue_depth"], 100)
        self.assertEqual(redacted_config(cfg)["watcher_rules"][0]["rooms"]["include"], ["eng"])

    def test_a_date_and_its_quoted_spelling_digest_differently_as_they_diff_differently(self):
        import datetime
        as_date = _config(connectors=[_connector(build_date=datetime.date(2026, 9, 5))])
        as_text = _config(connectors=[_connector(build_date="2026-09-05")])
        self.assertTrue(diff_configs(as_date, as_text), "a different raw dict restarts the connector")
        self.assertNotEqual(config_digest(as_date), config_digest(as_text))

    def test_a_changed_value_changes_the_digest(self):
        self.assertNotEqual(config_digest(_config(connectors=[_connector(token="old")])),
                            config_digest(_config(connectors=[_connector(token="new")])))

    def test_the_digest_is_sixty_four_hex_characters(self):
        digest = config_digest(_config())
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_flatten_redacts_secrets_at_any_depth_and_keys_connectors_by_name(self):
        cfg = _config(connectors=[_connector(auth={"password": "hunter2", "user": "bot"},
                                             token="t0k")])
        flat = dict(flatten_config(cfg))
        self.assertEqual(flat["connectors.rc.raw.server.auth.password"], "***")
        self.assertEqual(flat["connectors.rc.raw.server.token"], "***")
        self.assertEqual(flat["connectors.rc.raw.server.auth.user"], "bot")
        self.assertEqual(flat["connectors.rc.raw.server.url"], "https://rc.example")
        self.assertEqual(flat["watcher_rules[0].rooms.include[0]"], "eng")
        self.assertEqual(flat["max_queue_depth"], 100)
        self.assertNotIn("hunter2", json.dumps(flat))

    def test_a_mapping_under_a_secret_key_is_redacted_whole(self):
        cfg = _config(connectors=[_connector(client_secret={"value": "s3cret", "id": "app"},
                                             api_tokens=[{"value": "t0k"}])])
        flat = dict(flatten_config(cfg))
        self.assertEqual(flat["connectors.rc.raw.server.client_secret"], "***")
        self.assertEqual(flat["connectors.rc.raw.server.api_tokens"], "***")
        text = json.dumps(flat) + json.dumps(redacted_config(cfg))
        self.assertNotIn("s3cret", text)
        self.assertNotIn("t0k", text)

    def test_an_entity_named_like_a_secret_is_not_redacted(self):
        cfg = _config(connectors=[_connector(name="token-bot", token="t0k")],
                      agents={"secretary": AgentConfig(name="secretary")},
                      rules=[make_rule(room="eng", name="eng", connector="token-bot",
                                       agent="secretary")])
        flat = dict(flatten_config(cfg))
        self.assertEqual(flat["agents.secretary.type"], "claude")
        self.assertEqual(flat["connectors.token-bot.type"], "rocketchat")
        self.assertEqual(flat["connectors.token-bot.raw.server.token"], "***")
        doc = redacted_config(cfg)
        self.assertEqual(doc["agents"]["secretary"]["type"], "claude")
        self.assertEqual(doc["connectors"][0]["raw"]["server"]["token"], "***")

    def test_redacted_json_document_is_serializable_and_scrubbed(self):
        cfg = _config(connectors=[_connector(token="t0k")])
        doc = json.dumps(redacted_config(cfg))
        self.assertNotIn("t0k", doc)
        self.assertIn('"***"', doc)


if __name__ == "__main__":
    unittest.main()
