"""Unit tests for gateway/config.py's collect_config() — the fault-tolerant
counterpart to GatewayConfig.from_file() (which stays strict/fail-fast,
unchanged, for its existing production callers). These pin the specific
correctness properties an independent code review verified/caught while
this was built: partial progress is preserved across a structural failure
elsewhere, and a failed multi-room watcher entry never leaks a phantom
"duplicate name" collision onto a later, genuinely valid entry.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from gateway.config import GatewayConfig, collect_config


class _CollectConfigTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()

    def _write(self, yaml_text: str) -> str:
        path = Path(self.tmp) / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return str(path)


class TestCollectConfigPartialProgressPreserved(_CollectConfigTestBase):
    """PR review finding: several structural-failure branches used to
    `return None, issues` outright, discarding every connector/agent that
    had ALREADY parsed successfully — silently hiding an unrelated,
    already-real problem (e.g. a connector's empty credentials) behind a
    completely different structural issue elsewhere in the file."""

    def test_an_unknown_top_level_key_still_returns_the_good_connectors(self):
        """Renamed with its subject. This used to write an invalid
        `default_agent:` and assert the connectors survived it. That key no
        longer exists, so the same config now trips the UNKNOWN-top-level-key
        check instead — the assertion kept passing while testing something else,
        which is worse than failing. The structural point is the same and is
        what the name says now: a global-scope issue must not discard the
        per-entity parsing that already succeeded."""
        config_path = self._write(f"""\
            connectors:
              - name: rc1
                type: rocketchat
                server: {{url: "http://localhost:3000", username: "", password: ""}}
            agents:
              broken_default:
                type: claude
              other_agent:
                type: claude
                working_directory: {self.agent_dir}
            default_agent: broken_default
            watcher_rules:
              - connector: rc1
                agent: other_agent
                rooms:
                  include: [general]
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual([c.name for c in config.connectors], ["rc1"])
        self.assertTrue(
            any("does not use" in i.message and i.entity_kind == "global" for i in issues),
            [i.message for i in issues],
        )

    def test_all_connectors_failing_still_returns_the_good_agents(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc1
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.connectors, [])
        self.assertEqual(list(config.agents), ["default"])

    def test_zero_agents_still_returns_the_good_connectors(self):
        config_path = self._write("""\
            connectors:
              - name: rc1
                type: rocketchat
                server: {url: "http://localhost:3000", username: "", password: ""}
            agents:
              broken:
                type: claude
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual([c.name for c in config.connectors], ["rc1"])
        self.assertEqual(config.agents, {})

    def test_malformed_watchers_block_still_returns_good_connectors_and_agents(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc1
                type: rocketchat
                server: {{url: "http://localhost:3000", username: "", password: ""}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules: {{not: a-list}}
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual([c.name for c in config.connectors], ["rc1"])
        self.assertEqual(list(config.agents), ["default"])

    def test_malformed_watchers_block_still_keeps_a_valid_max_queue_depth_and_scheduler(self):
        """PR review finding: every structural early-return branch above
        used to hardcode max_queue_depth=100/scheduler=SchedulerConfig()
        instead of actually parsing them — silently discarding an
        otherwise-valid value behind a completely unrelated structural
        issue elsewhere in the file (the exact "don't hide an unrelated,
        already-successful value behind a different issue" bug this whole
        function exists to avoid for connectors/agents/watchers). These two
        fields have no entity dependency on watchers: at all."""
        config_path = self._write(f"""\
            connectors:
              - name: rc1
                type: rocketchat
                server: {{url: "http://localhost:3000", username: "", password: ""}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules: {{not: a-list}}
            max_queue_depth: 42
            scheduler:
              completed_job_ttl_days: 30
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.max_queue_depth, 42)
        self.assertEqual(config.scheduler.completed_job_ttl_days, 30)


class TestCollectConfigNonStringScalarFields(_CollectConfigTestBase):
    """PR review finding (round 6): the same class of bug round 5 fixed for
    a non-string 'name'/'inherits' (a truthy-but-wrong-type raw value
    slipping past a bare `if not x` check into a hash-based `in`/`.get()`,
    or a string method) was also live on the other raw scalar reference
    fields — connector 'type', watcher 'connector'/'agent' — reachable via
    BOTH
    from_file() (see test_config_loading.py's
    TestConfigValidationHardening for the strict-path pins) and
    collect_config(). Each must surface as a collected, per-entity/global
    ConfigIssue — never an uncaught TypeError/AttributeError that would
    abort collect_config() (or crash gateway/config_validate.py's
    validate_config() a layer up) for the WHOLE file."""

    def test_non_string_connector_type_is_a_collected_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: [rocketchat]
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertTrue(any("'type' must be a string" in i.message for i in issues))

    def test_non_string_watcher_connector_is_a_collected_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                agent: default
                rooms:
                  include: [general]
                connector: [rc]
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertTrue(any("'connector' must be a string" in i.message for i in issues))

    def test_non_string_watcher_agent_is_a_collected_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                rooms:
                  include: [general]
                connector: rc
                agent: [default]
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertTrue(any("'agent' must be a string" in i.message for i in issues))

    def test_non_string_watcher_room_is_a_collected_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                agent: default
                rooms:
                  include: [12345]
                connector: rc
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertTrue(any("'rooms.include' entries must be non-empty strings" in i.message for i in issues))

    def test_non_string_watcher_session_id_is_a_collected_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                agent: default
                rooms:
                  include: [general]
                connector: rc
                session_id: [abc]
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        # Inverted with the field's removal: the key is refused whatever its value,
        # and the refusal must still arrive as an attributed issue rather than an
        # exception that aborts the pass.
        watcher_issues = [i for i in issues if i.entity_kind == "watcher"]
        self.assertEqual(len(watcher_issues), 1, [i.message for i in issues])
        # `session_id` no longer has a rejection path of its own — it is simply
        # not a key, so the closed rule shape reports it like any other. What the
        # test is really about survives: refused whatever its value, and arriving
        # as an attributed issue rather than an exception that aborts the pass.
        self.assertIn("session_id", watcher_issues[0].message)
        self.assertIn("unknown key(s)", watcher_issues[0].message)


class TestCollectConfigNonStringNameHint(_CollectConfigTestBase):
    """PR review finding: a connector/watcher entry's own `name:` might
    itself be malformed (e.g. a list instead of a string) on an entry that
    ALSO fails for some unrelated reason — ConfigIssue.entity_name is typed
    `str | None` everywhere downstream, and EditableConfig's save-gate
    (model.py's _new_errors_introduced_by_this_save()) puts this value
    straight into a set of tuples, which raises an uncaught
    `TypeError: unhashable type` for anything non-hashable (a list).
    collect_config() must fall back to the same "(index i)" label an
    absent name already gets, rather than using the malformed value
    verbatim."""

    def test_non_string_connector_name_falls_back_to_index_label(self):
        config_path = self._write(f"""\
            connectors:
              - name: [a, b]
                type: rocketchat
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        connector_issues = [i for i in issues if i.entity_kind == "connector"]
        self.assertEqual(len(connector_issues), 1)
        self.assertEqual(connector_issues[0].entity_name, "(index 0)")
        # Never crashes building a set of these — the actual failure mode
        # PR review caught (EditableConfig._new_errors_introduced_by_this_save()).
        {(i.entity_kind, i.entity_name, i.message) for i in issues}

    def test_non_string_watcher_name_falls_back_to_index_label(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: [a, b]
                agent: default
                connector: rc
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        watcher_issues = [i for i in issues if i.entity_kind == "watcher"]
        self.assertEqual(len(watcher_issues), 1)
        self.assertEqual(watcher_issues[0].entity_name, "(index 0)")
        {(i.entity_kind, i.entity_name, i.message) for i in issues}


class TestCollectConfigQueueSchedulerSessionId(_CollectConfigTestBase):
    """PR review finding: collect_config()'s max_queue_depth/scheduler:/
    duplicate-session_id branches (unlike connectors/agents/watchers) had no
    dedicated test directly against collect_config() itself — only
    indirectly, via test_config_validate.py's lint regression test. Also
    pins that max_queue_depth and scheduler: are validated INDEPENDENTLY: a
    bad one falls back to its own default without discarding the other's
    genuinely valid value."""

    def _base_config(self, extra: str) -> str:
        # `extra` is interpolated into an already-dedented block below, so
        # it needs the SAME 12-space indentation as every other line here —
        # otherwise textwrap.dedent() (in _write()) sees an inconsistent
        # common prefix across lines and no-ops, breaking the YAML entirely.
        indented_extra = textwrap.indent(extra, " " * 12)
        return self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
{indented_extra}
        """)

    def test_invalid_max_queue_depth_falls_back_to_default_and_keeps_a_valid_scheduler(self):
        config_path = self._base_config(
            "max_queue_depth: -1\nscheduler: {completed_job_ttl_days: 30}"
        )
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.max_queue_depth, 100)
        self.assertEqual(config.scheduler.completed_job_ttl_days, 30)
        self.assertTrue(any("max_queue_depth" in i.message for i in issues))

    def test_invalid_scheduler_falls_back_to_default_and_keeps_a_valid_max_queue_depth(self):
        config_path = self._base_config("max_queue_depth: 42\nscheduler: not-a-mapping")
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.max_queue_depth, 42)
        self.assertEqual(config.scheduler.completed_job_ttl_days, 7)  # dataclass default
        self.assertTrue(any("scheduler" in i.message for i in issues))

    def test_each_watcher_carrying_session_id_is_its_own_attributed_issue(self):
        """Replaces the duplicate-sticky-session_id case: the field is removed, so two
        watchers cannot share one and the cross-watcher pass is gone with it. What
        matters now is that each offending entry is reported on its own — the property
        the old test was really pinning (an issue per entry, not one discard)."""
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
                session_id: sticky-1
              - name: w2
                connector: rc
                agent: default
                rooms:
                  include: [dev]
                session_id: sticky-1
              - name: w3
                connector: rc
                agent: default
                rooms:
                  include: [ops]
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(
            [(i.entity_kind, i.entity_name) for i in issues],
            [("watcher", "w1"), ("watcher", "w2")],
        )
        for issue in issues:
            self.assertIn("session_id", issue.message)
            self.assertIn("unknown key(s)", issue.message)
        # The clean entry either side still parses — the "not a discard" half.
        self.assertEqual([r.name for r in config.watcher_rules], ["w3"])


class TestCollectConfigOnTheFlyWatcherFields(_CollectConfigTestBase):
    """exclude_room / room: "*" (WatcherConfig), and the TTL keys that moved off the
    agent — docs/design/dynamic-watcher-design.md. Same class of requirement as the
    fields above: a bad value must surface as a collected, per-entity ConfigIssue
    through collect_config(), never an uncaught exception that aborts the whole
    file."""

    def test_a_ttl_key_left_on_an_agent_is_a_collected_agent_issue(self):
        """These moved to the watcher rule (design §5.4), and a leftover key is a
        hard error rather than a silently ignored one — so it must arrive here as an
        attributed issue, not as an exception that stops the pass."""
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
                session_idle_days: 30
            watcher_rules:
              - rooms:
                  include: [general]
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.agents, {})
        agent_issues = [i for i in issues if i.entity_kind == "agent"]
        self.assertEqual(len(agent_issues), 1, [i.message for i in issues])
        # Contract, not phrasing: the message was reworded for plain language.
        # What must survive is that it names the key, says where the setting
        # lives now, and does so as an attributed agent issue.
        msg = agent_issues[0].message
        self.assertIn("session_idle_days", msg)
        self.assertIn("'watcher_rules:'", msg)

    def test_wildcard_room_is_a_collected_watcher_issue(self):
        config_path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - connector: rc
                agent: default
                rooms:
                  include: ["*"]
        """)
        config, issues = collect_config(config_path)
        self.assertIsNotNone(config)
        # The static-era "not implemented yet" rejection died with its shape:
        # a wildcard include is exactly what a rule is for — on an inbound
        # connector it is valid config, refused only where nothing can ever
        # offer a room (test_literal_rooms).
        self.assertEqual(len(issues), 1, [i.message for i in issues])
        self.assertIn("'name' is required", issues[0].message)


if __name__ == "__main__":
    unittest.main()


class TestExplicitNullWatchersBlock(_CollectConfigTestBase):
    """A bare `watchers:` — the natural way to empty the block — used to
    reach `enumerate(None)` and raise a raw TypeError, because the guard
    checked truthiness BEFORE type. That crashed the daemon at startup and
    `coop config validate` with it, on a config an operator produces by
    deleting their rules. Found while testing the config TUI's rollback
    (Codex review of PR #129, round 11); the rule this violated is stated in
    this same file, on `_resolve_watcher_connector`."""

    def _with_watchers(self, literal: str) -> str:
        return self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:{literal}
        """)

    def test_an_explicit_null_is_treated_as_no_watchers(self):
        config, issues = collect_config(self._with_watchers(""))
        self.assertIsNotNone(config)
        self.assertEqual(config.watcher_rules, [])
        self.assertEqual(issues, [])

    def test_the_strict_loader_accepts_it_too(self):
        GatewayConfig.from_file(self._with_watchers(""))

    def test_a_falsy_non_list_still_gets_the_clean_message(self):
        """`0`/`""` took the same skipped-guard path as null; they are
        mistakes rather than an empty block, so they must be REPORTED, not
        silently treated as empty."""
        for literal in (" 0", ' ""'):
            with self.subTest(literal=literal):
                config, issues = collect_config(self._with_watchers(literal))
                self.assertTrue(
                    any("'watcher_rules:' must be a list" in i.message for i in issues),
                    [i.message for i in issues],
                )

    def test_a_truthy_non_list_is_unchanged(self):
        config, issues = collect_config(self._with_watchers(" 5"))
        self.assertTrue(any("'watcher_rules:' must be a list" in i.message for i in issues))
