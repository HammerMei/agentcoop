"""Tests for GatewayConfig loading and validation.

Covers:
  - working_directory required and validated at config load (code_review Issue #6)
  - Config validation hardening: uniqueness, required fields, types (code_review)
  - cache_dir_global path resolution (code_review Issue #7)
  - Built-in context auto-injection via context_inject_files_for() (layer-0 system files)

Run with:
    uv run python -m pytest tests/test_config_loading.py -v
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.config import GatewayConfig, collect_config

# ── Tests: working_directory validation ──────────────────────────────────────


class TestWorkingDirectoryValidation(unittest.TestCase):
    """Issue #6: working_directory must be required and validated at config load."""

    def _write_config(self, agents_block: str) -> str:
        cfg = textwrap.dedent(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server:
                  url: http://localhost:3000
                  username: bot
                  password: pw
            agents:
{textwrap.indent(agents_block, "              ")}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            return f.name

    def test_missing_working_directory_raises(self):
        path = self._write_config("default:\n  type: claude")
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("working_directory is required", str(ctx.exception))

    def test_empty_working_directory_raises(self):
        path = self._write_config('default:\n  type: claude\n  working_directory: ""')
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("working_directory is required", str(ctx.exception))

    def test_nonexistent_directory_raises(self):
        path = self._write_config(
            "default:\n  type: claude\n  working_directory: /nonexistent/path/xyz"
        )
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("does not exist", str(ctx.exception))

    def test_valid_directory_accepted(self):
        path = self._write_config("default:\n  type: claude\n  working_directory: /tmp")
        config = GatewayConfig.from_file(path)
        self.assertEqual(config.agents["default"].working_directory, "/tmp")
        self.assertTrue(config.agents["default"].lazy_instruction_loading)

    def test_lazy_instruction_loading_false_accepted(self):
        path = self._write_config(
            "default:\n  type: claude\n  working_directory: /tmp\n  lazy_instruction_loading: false"
        )
        config = GatewayConfig.from_file(path)
        self.assertFalse(config.agents["default"].lazy_instruction_loading)

    def test_lazy_instruction_loading_must_be_boolean(self):
        path = self._write_config(
            'default:\n  type: claude\n  working_directory: /tmp\n  lazy_instruction_loading: "nope"'
        )
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("lazy_instruction_loading", str(ctx.exception))

    def test_tilde_working_directory_is_expanded(self):
        """Regression: working_directory: ~/foo must expand to the user's home
        directory, not be treated as a literal relative path segment named
        '~' under the config file's directory (config.example.yaml and the
        install-agent.md walkthroughs document `~/...` working_directory
        values, so this must actually work)."""
        with tempfile.TemporaryDirectory() as home_dir:
            subdir = Path(home_dir) / "agent-work"
            subdir.mkdir()
            path = self._write_config(
                "default:\n  type: claude\n  working_directory: ~/agent-work"
            )
            with patch.dict(os.environ, {"HOME": home_dir}):
                config = GatewayConfig.from_file(path)
            self.assertEqual(
                config.agents["default"].working_directory,
                str(subdir),
            )

    def test_relative_directory_resolved_to_config_dir(self):
        """A relative working_directory is resolved relative to the config file's directory."""
        import shutil

        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir) / "workdir"
            subdir.mkdir()
            path = self._write_config(
                "default:\n  type: claude\n  working_directory: workdir"
            )
            config_path = Path(tmpdir) / "config.yaml"
            shutil.move(path, config_path)

            config = GatewayConfig.from_file(str(config_path))
            self.assertEqual(
                config.agents["default"].working_directory,
                str(subdir.resolve()),
            )


# ── Tests: config validation hardening ───────────────────────────────────────


class TestConfigValidationHardening(unittest.TestCase):
    """Additional config validation for uniqueness, required fields, and types."""

    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_duplicate_connector_name_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
              - name: rc
                type: script
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("Duplicate connector name 'rc'", str(ctx.exception))

    def test_a_glob_character_in_a_connector_name_is_refused(self):
        """'*', '?' and '[' make a watcher name a pattern on the CLI (#151);
        a connector 'prod[1]' would give every one of its watchers a name
        that matches nothing literally, so a pause of it would silently do
        nothing (Codex on #152)."""
        for bad in ("prod[1]", "mm*", "rc?"):
            with self.subTest(name=bad):
                path = self._write_config(f"""\
                    connectors:
                      - name: '{bad}'
                        type: rocketchat
                        server: {{url: http://localhost:3000, username: bot, password: pw}}
                    agents:
                      default:
                        type: claude
                        working_directory: /tmp
                    watcher_rules:
                      - name: w1
                        connector: '{bad}'
                        agent: default
                        rooms:
                          include: [general]
                """)
                with self.assertRaises(ValueError) as ctx:
                    GatewayConfig.from_file(path)
                self.assertIn(f"Connector name '{bad}'", str(ctx.exception))
                self.assertIn("glob", str(ctx.exception))

    def test_a_colon_in_a_connector_name_is_refused(self):
        """':' is the watcher-handle divider (<connector>:<room label>) — the
        boundary is only unforgeable because a connector name can never
        carry one (Codex review of #121, round 7)."""
        path = self._write_config("""\
            connectors:
              - name: rc:home
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("reserved", str(ctx.exception))
        self.assertIn("rc:home", str(ctx.exception))

    def test_duplicate_watcher_name_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: same
                connector: rc
                agent: default
                rooms:
                  include: [general]
              - name: same
                connector: rc
                agent: default
                rooms:
                  include: [lobby]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("Duplicate watcher rule name 'same'", str(ctx.exception))

    def test_empty_watcher_room_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [""]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("'rooms.include' entries must be non-empty strings", str(ctx.exception))

    def test_negative_max_queue_depth_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            max_queue_depth: -1
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("max_queue_depth", str(ctx.exception))

    def test_non_mapping_agent_config_gets_clear_error(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default: claude
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("Agent 'default' config must be a mapping", str(ctx.exception))

    def test_non_mapping_watcher_entry_gets_clear_error(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - just-a-string
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("watcher_rules[0] must be a mapping", str(ctx.exception))

    def test_non_string_connector_type_raises_value_error_not_type_error(self):
        """PR review finding: a truthy-but-non-string 'type' (e.g. a YAML
        list) used to reach `_CONNECTOR_VALIDATORS.get(connector.type)`
        (gateway/config_validate.py) unchecked — an uncaught
        TypeError: unhashable type on every validate_config() call, not
        just --lint. Same class of bug as the 'name' check right above it
        in _parse_one_connector() — _parse_one_agent()'s own 'type' check
        already had this guard; this one was simply missed in that sweep."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: [rocketchat]
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaisesRegex(ValueError, "'type' must be a string"):
            GatewayConfig.from_file(path)

    def test_non_string_watcher_connector_raises_value_error_not_type_error(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                agent: default
                rooms:
                  include: [general]
                connector: [rc]
        """)
        with self.assertRaisesRegex(ValueError, "'connector' must be a string"):
            GatewayConfig.from_file(path)

    def test_non_string_watcher_agent_raises_value_error_not_type_error(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                rooms:
                  include: [general]
                connector: rc
                agent: [default]
        """)
        with self.assertRaisesRegex(ValueError, "'agent' must be a string"):
            GatewayConfig.from_file(path)

    def test_non_string_watcher_room_raises_value_error_not_attribute_error(self):
        """A non-string rooms.include element (e.g. an int) must be refused
        as a clean ValueError, never an AttributeError/TypeError escaping
        into the loader. (Originally pinned against the deleted static
        parser's `room:` alias; kept because the rule shape's pattern
        parsing owes the same contract.)"""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                agent: default
                rooms:
                  include: [12345]
                connector: rc
        """)
        with self.assertRaisesRegex(
            ValueError, "'rooms.include' entries must be non-empty strings"
        ):
            GatewayConfig.from_file(path)

    def test_any_watcher_session_id_is_refused_since_the_field_is_removed(self):
        """Still refused whatever its value — but by the rule shape's closed key
        set, not by a branch of its own. The dedicated check existed because the
        OLD static parser ignored unknown keys, so a plain deletion would have
        made a released key silently ineffective. A closed key set removes that
        risk, so the special case went (see test_session_id_removed.py)."""
        for value in ("[abc]", "'ses_abc123'", "null", "false", "0"):
            with self.subTest(value=value):
                path = self._write_config(f"""\
                    connectors:
                      - name: rc
                        type: rocketchat
                        server: {{url: http://localhost:3000, username: bot, password: pw}}
                    agents:
                      default:
                        type: claude
                        working_directory: /tmp
                    watcher_rules:
                      - name: w1
                        agent: default
                        rooms:
                          include: [general]
                        connector: rc
                        session_id: {value}
                """)
                with self.assertRaisesRegex(ValueError, "unknown key\\(s\\).*session_id"):
                    GatewayConfig.from_file(path)


class TestCacheDirGlobalResolution(unittest.TestCase):
    """Issue #7: relative cache_dir_global must resolve relative to config directory."""

    def _write_config(self, cache_dir_global: str) -> str:
        cfg = textwrap.dedent(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server:
                  url: http://localhost:3000
                  username: bot
                  password: pw
                attachments:
                  cache_dir_global: {cache_dir_global}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            return f.name

    def test_relative_cache_dir_resolved_to_config_dir(self):
        path = self._write_config("my-cache")
        config = GatewayConfig.from_file(path)
        config_dir = str(Path(path).parent.resolve())
        expected = str(Path(config_dir) / "my-cache")
        actual = config.connectors[0].raw["attachments"]["cache_dir_global"]
        self.assertEqual(actual, expected)

    def test_absolute_cache_dir_unchanged(self):
        path = self._write_config("/absolute/cache/path")
        config = GatewayConfig.from_file(path)
        actual = config.connectors[0].raw["attachments"]["cache_dir_global"]
        self.assertEqual(actual, "/absolute/cache/path")

    def test_tilde_cache_dir_unchanged(self):
        """Paths starting with ~ are left for expanduser() at connector init time."""
        path = self._write_config("~/.agentcoop/attachments")
        config = GatewayConfig.from_file(path)
        actual = config.connectors[0].raw["attachments"]["cache_dir_global"]
        self.assertEqual(actual, "~/.agentcoop/attachments")


# ── Tests: $VAR/${VAR} is a plain literal string, never resolved (S3, final revision) ──


class TestDollarVarIsALiteralString(unittest.TestCase):
    """docs/design/config-tool.md decision 6, final revision: GatewayConfig.
    from_file() no longer expands $VAR/${VAR} at all — secrets live directly
    in config.yaml, and any pre-existing .env-backed config is auto-migrated
    into that form (gateway/config_migrate.py) before this loader ever runs.
    A string that happens to look like a placeholder — resolvable or not —
    is accepted and used exactly as written, same as any other string; this
    also sidesteps the case a real secret's own value merely happens to
    resemble ${SOMETHING}."""

    def _write_config_with_url(self, url: str) -> str:
        cfg = textwrap.dedent(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server:
                  url: "{url}"
                  username: bot
                  password: pw
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            return f.name

    def test_dollar_brace_form_is_used_literally_even_when_unresolvable(self):
        import os
        os.environ.pop("COOP_TEST_UNSET_12345", None)
        path = self._write_config_with_url("http://${COOP_TEST_UNSET_12345}:3000")

        cfg = GatewayConfig.from_file(path)  # must not raise

        self.assertEqual(
            cfg.connectors[0].raw["server"]["url"], "http://${COOP_TEST_UNSET_12345}:3000"
        )

    def test_dollar_plain_form_is_used_literally_even_when_unresolvable(self):
        import os
        os.environ.pop("COOP_TEST_UNSET_99999", None)
        path = self._write_config_with_url("http://$COOP_TEST_UNSET_99999:3000")

        cfg = GatewayConfig.from_file(path)  # must not raise

        self.assertEqual(
            cfg.connectors[0].raw["server"]["url"], "http://$COOP_TEST_UNSET_99999:3000"
        )

    def test_dollar_brace_form_is_NOT_resolved_even_when_the_var_is_set(self):
        """The critical regression case: if a resolvable ${VAR} were
        silently resolved, a real secret whose plaintext value happens to
        look like a placeholder would be misinterpreted."""
        import os
        os.environ["COOP_TEST_SET_URL"] = "localhost"
        try:
            path = self._write_config_with_url("http://${COOP_TEST_SET_URL}:3000")

            cfg = GatewayConfig.from_file(path)

            self.assertEqual(
                cfg.connectors[0].raw["server"]["url"], "http://${COOP_TEST_SET_URL}:3000"
            )
        finally:
            os.environ.pop("COOP_TEST_SET_URL", None)


# ── Tests: ToolRule regex validated at config load time (S2) ─────────────────


class TestToolRuleRegexValidation(unittest.TestCase):
    """S2: Invalid regex patterns in ToolRule must raise ValueError at config
    load time, not silently fail or produce cryptic errors during runtime."""

    def _write_config_with_tool_rule(self, rule_block: str) -> str:
        cfg = textwrap.dedent(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server:
                  url: http://localhost:3000
                  username: bot
                  password: pw
            agents:
              default:
                type: claude
                working_directory: /tmp
                owner_allowed_tools:
{textwrap.indent(rule_block, "                  ")}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            return f.name

    def test_invalid_tool_regex_raises_at_load(self):
        """A bad regex in the 'tool' field must raise ValueError at config load."""
        path = self._write_config_with_tool_rule("- tool: '[invalid'")
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("invalid tool rule", str(ctx.exception).lower())

    def test_invalid_params_regex_raises_at_load(self):
        """A bad regex in the 'params' field must raise ValueError at config load."""
        path = self._write_config_with_tool_rule(
            "- tool: Bash\n  params: '[unclosed'"
        )
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("invalid", str(ctx.exception).lower())

    def test_valid_tool_regex_accepted(self):
        """A valid regex in 'tool' must not raise."""
        path = self._write_config_with_tool_rule(
            "- tool: 'mcp__rocketchat__get_.*'\n  params: '.*'"
        )
        cfg = GatewayConfig.from_file(path)
        self.assertEqual(len(cfg.agents["default"].owner_allowed_tools), 1)
        self.assertEqual(cfg.agents["default"].owner_allowed_tools[0].tool,
                         "mcp__rocketchat__get_.*")


# ── Tests: GatewayConfig.agent raises KeyError on misconfiguration (Q2) ──────


class TestBuiltinContextAutoInjection(unittest.TestCase):
    """Built-in system context files are auto-prepended in context_inject_files_for().

    rc-gateway-context.md  → injected for every Rocket.Chat connector
    tool-index-context.md  → injected for lazy-loading agents (default)
    scheduling/fetch-history docs → injected when an agent disables lazy loading
    """

    def _make_core_config(self, connector_type: str, user_ctx: list[str] | None = None):
        from gateway.core.config import AgentConfig, ConnectorConfig, CoreConfig
        connector = ConnectorConfig(
            name="rc",
            type=connector_type,
            raw={},
            context_inject_files=user_ctx or [],
        )
        agent = AgentConfig(name="default", timeout=10)
        return CoreConfig(
            connector_configs={"rc": connector},
            agents={"default": agent},
        )

    def test_rc_connector_gets_core_and_tool_index_by_default(self):
        """RC connector → rc-gateway-context.md + tool-index-context.md prepended."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        self.assertIn("rc-gateway-context.md", basenames)
        self.assertIn("tool-index-context.md", basenames)
        self.assertNotIn("scheduling-context.md", basenames)
        self.assertNotIn("fetch-history-context.md", basenames)

    def test_rc_gateway_context_comes_before_tool_index(self):
        """rc-gateway-context.md must appear before tool-index-context.md."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        rc_idx = basenames.index("rc-gateway-context.md")
        index_idx = basenames.index("tool-index-context.md")
        self.assertLess(rc_idx, index_idx)

    def test_non_rc_connector_gets_tool_index_only_by_default(self):
        """Non-RC connector → only tool-index-context.md injected (no rc-gateway-context.md)."""
        config = self._make_core_config("script")
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        self.assertNotIn("rc-gateway-context.md", basenames)
        self.assertIn("tool-index-context.md", basenames)
        self.assertNotIn("scheduling-context.md", basenames)
        self.assertNotIn("fetch-history-context.md", basenames)

    def test_agent_can_disable_lazy_instruction_loading(self):
        """Per-agent lazy_instruction_loading=False restores full docs."""
        from gateway.core.config import AgentConfig

        config = self._make_core_config("rocketchat")
        config.agents["default"] = AgentConfig(
            name="default",
            timeout=10,
            lazy_instruction_loading=False,
        )
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        self.assertIn("rc-gateway-context.md", basenames)
        self.assertIn("scheduling-context.md", basenames)
        self.assertIn("fetch-history-context.md", basenames)
        self.assertNotIn("tool-index-context.md", basenames)

    def test_yaml_lazy_instruction_loading_false_reaches_context_injection(self):
        """YAML lazy_instruction_loading=False flows through to built-in full docs."""
        from gateway.core.config import CoreConfig

        cfg_text = textwrap.dedent("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
                lazy_instruction_loading: false
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg_text)
            path = f.name

        gateway_config = GatewayConfig.from_file(path)
        core_config = CoreConfig.from_gateway_config(gateway_config)

        files = core_config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        self.assertIn("rc-gateway-context.md", basenames)
        self.assertIn("scheduling-context.md", basenames)
        self.assertIn("fetch-history-context.md", basenames)
        self.assertNotIn("tool-index-context.md", basenames)

    def test_builtin_files_precede_user_connector_files(self):
        """Built-in layer 0 files must come before user-configured connector files."""
        config = self._make_core_config("rocketchat", user_ctx=["/user/custom.md"])
        files = config.context_inject_files_for("rc", "default", [])
        basenames = [Path(f).name for f in files]
        sched_idx = basenames.index("tool-index-context.md")
        custom_idx = basenames.index("custom.md")
        self.assertLess(sched_idx, custom_idx)

    def test_builtin_files_are_absolute_paths(self):
        """Built-in file paths must be absolute (so ContextInjector can read them)."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", [])
        builtin = [f for f in files if Path(f).name in ("rc-gateway-context.md", "tool-index-context.md")]
        for f in builtin:
            self.assertTrue(Path(f).is_absolute(), f"{f} must be absolute")

    def test_builtin_files_actually_exist_in_package(self):
        """Built-in context files must be present in the installed package."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", [])
        builtin = [f for f in files if Path(f).name in ("rc-gateway-context.md", "tool-index-context.md")]
        for f in builtin:
            self.assertTrue(Path(f).exists(), f"Built-in context file missing: {f}")

    def test_no_connector_config_skips_builtin_injection(self):
        """When ConnectorConfig is absent (e.g. tests), no built-in files are prepended."""
        from gateway.core.config import AgentConfig, CoreConfig
        config = CoreConfig(agents={"default": AgentConfig(timeout=10)})
        files = config.context_inject_files_for("unknown-connector", "default", [])
        basenames = [Path(f).name for f in files]
        self.assertNotIn("rc-gateway-context.md", basenames)
        self.assertNotIn("scheduling-context.md", basenames)
        self.assertNotIn("tool-index-context.md", basenames)

    def test_watcher_user_files_appended_after_builtin(self):
        """Watcher-level user files are appended last, after all built-in and agent files."""
        config = self._make_core_config("rocketchat")
        files = config.context_inject_files_for("rc", "default", ["/watcher/extra.md"])
        basenames = [Path(f).name for f in files]
        sched_idx = basenames.index("tool-index-context.md")
        extra_idx = basenames.index("extra.md")
        self.assertLess(sched_idx, extra_idx)


# ── Tests: built-in owner tool rule auto-injection ────────────────────────────


class TestBuiltinOwnerToolRuleAutoInjection(unittest.TestCase):
    """Built-in gateway tool rules are always prepended to owner_allowed_tools.

    These ensure that `coop send`, `coop schedule`,
    and `date` never require a 🔐 human-approval prompt.  The first two are the
    gateway's own commands; `date` is a read-only command used by agents to
    compute timestamps in compound bash expressions (e.g. ``$(date ...)``).
    """

    def _make_agent(self, extra_rules=None):
        from gateway.core.config import AgentConfig, ToolRule
        rules = []
        if extra_rules:
            rules = [ToolRule(tool="Bash", params=p) for p in extra_rules]
        return AgentConfig(name="test", owner_allowed_tools=rules)

    def test_effective_includes_send_rule(self):
        """effective_owner_allowed_tools() must include 'coop send .*'."""
        agent = self._make_agent()
        effective = agent.effective_owner_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertTrue(
            any("coop" in (p or "") and "send" in (p or "") for p in params),
            "Built-in send rule must be in effective_owner_allowed_tools",
        )

    def test_effective_includes_schedule_rule(self):
        """effective_owner_allowed_tools() must include 'coop schedule .*'."""
        agent = self._make_agent()
        effective = agent.effective_owner_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertTrue(
            any("coop" in (p or "") and "schedule" in (p or "") for p in params),
            "Built-in schedule rule must be in effective_owner_allowed_tools",
        )

    def test_effective_includes_instructions_rule(self):
        """effective_owner_allowed_tools() must include 'coop instructions .*'."""
        agent = self._make_agent()
        effective = agent.effective_owner_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertTrue(
            any("coop" in (p or "") and "instructions" in (p or "") for p in params),
            "Built-in instructions rule must be in effective_owner_allowed_tools",
        )

    def test_builtin_rules_precede_user_rules(self):
        """Built-in rules must come before user-defined rules."""
        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        agent = self._make_agent(extra_rules=["git log.*"])
        effective = agent.effective_owner_allowed_tools()
        builtin_count = len(_BUILTIN_OWNER_TOOL_RULES)
        # First N entries must match the built-in rules
        for i, builtin_rule in enumerate(_BUILTIN_OWNER_TOOL_RULES):
            self.assertEqual(effective[i].tool, builtin_rule.tool)
            self.assertEqual(effective[i].params, builtin_rule.params)
        # The user rule follows after
        self.assertEqual(effective[builtin_count].params, "git log.*")

    def test_owner_allowed_tools_field_unchanged(self):
        """effective_owner_allowed_tools() must NOT mutate owner_allowed_tools in place."""
        from gateway.core.config import ToolRule
        user_rule = ToolRule(tool="Bash", params="ls.*")
        agent = self._make_agent()
        agent.owner_allowed_tools = [user_rule]
        original_len = len(agent.owner_allowed_tools)
        agent.effective_owner_allowed_tools()
        self.assertEqual(len(agent.owner_allowed_tools), original_len)

    def test_empty_owner_list_still_gets_builtins(self):
        """An agent with an empty owner_allowed_tools still gets the built-in rules."""
        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        agent = self._make_agent()
        effective = agent.effective_owner_allowed_tools()
        self.assertGreaterEqual(len(effective), len(_BUILTIN_OWNER_TOOL_RULES))

    def test_send_rule_matches_actual_command(self):
        """The send rule must actually match a realistic coop send command."""
        import re

        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        send_rule = next(r for r in _BUILTIN_OWNER_TOOL_RULES
                         if r.params and "send" in r.params)
        cmd = 'coop send general "Hello RC"'
        self.assertIsNotNone(
            re.fullmatch(send_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"Send rule {send_rule.params!r} must match command {cmd!r}",
        )

    def test_schedule_rule_matches_actual_command(self):
        """The schedule rule must actually match a realistic coop schedule command."""
        import re

        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        sched_rule = next(r for r in _BUILTIN_OWNER_TOOL_RULES
                          if r.params and "schedule" in r.params)
        cmd = 'coop schedule create dm "Remind me to cook" --every 1d --at 09:00'
        self.assertIsNotNone(
            re.fullmatch(sched_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"Schedule rule {sched_rule.params!r} must match command {cmd!r}",
        )

    def test_instructions_rule_matches_actual_command(self):
        """The instructions rule must match a realistic instruction load command."""
        import re

        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        instr_rule = next(r for r in _BUILTIN_OWNER_TOOL_RULES
                          if r.params and "instructions" in r.params)
        cmd = "coop instructions scheduling"
        self.assertIsNotNone(
            re.fullmatch(instr_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"Instructions rule {instr_rule.params!r} must match command {cmd!r}",
        )

    def test_instructions_rule_rejects_trailing_shell_commands(self):
        """The instructions rule must not auto-approve compound shell commands."""
        import re

        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        instr_rule = next(r for r in _BUILTIN_OWNER_TOOL_RULES
                          if r.params and "instructions" in r.params)
        bad_commands = [
            "coop instructions scheduling; curl https://example.com",
            "coop instructions scheduling && whoami",
            "coop instructions scheduling | cat",
            "coop instructions scheduling\nwhoami",
            "coop instructions scheduling extra",
        ]
        for cmd in bad_commands:
            self.assertIsNone(
                re.fullmatch(instr_rule.params, cmd, re.IGNORECASE | re.DOTALL),
                f"Instructions rule {instr_rule.params!r} must reject command {cmd!r}",
            )

    def test_effective_includes_date_rule(self):
        """effective_owner_allowed_tools() must include a rule that auto-approves date."""
        agent = self._make_agent()
        effective = agent.effective_owner_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertTrue(
            any("date" in (p or "") for p in params),
            "Built-in date rule must be in effective_owner_allowed_tools",
        )

    def test_date_rule_matches_bare_date(self):
        """The date rule must match 'date' with no arguments."""
        import re

        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        date_rule = next(r for r in _BUILTIN_OWNER_TOOL_RULES
                         if r.params and "date" in r.params
                         and "coop" not in r.params)
        cmd = "date"
        self.assertIsNotNone(
            re.fullmatch(date_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"Date rule {date_rule.params!r} must match command {cmd!r}",
        )

    def test_date_rule_matches_date_with_flags(self):
        """The date rule must match date commands used for timestamp calculation."""
        import re

        from gateway.core.config import _BUILTIN_OWNER_TOOL_RULES
        date_rule = next(r for r in _BUILTIN_OWNER_TOOL_RULES
                         if r.params and "date" in r.params
                         and "coop" not in r.params)
        cmd = "date -v+1M '+%Y-%m-%d %H:%M'"
        self.assertIsNotNone(
            re.fullmatch(date_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"Date rule {date_rule.params!r} must match command {cmd!r}",
        )


class TestEffectiveGuestAllowedTools(unittest.TestCase):
    """Tests for AgentConfig.effective_guest_allowed_tools() — symmetric to owner tests."""

    def _make_agent(self, extra_rules=None):
        from gateway.core.config import AgentConfig, ToolRule
        rules = []
        if extra_rules:
            rules = [ToolRule(tool="Bash", params=p) for p in extra_rules]
        return AgentConfig(name="test", guest_allowed_tools=rules)

    def test_effective_includes_fetch_history_rule(self):
        """effective_guest_allowed_tools() must include 'coop fetch-history .*'."""
        agent = self._make_agent()
        effective = agent.effective_guest_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertTrue(
            any("coop" in (p or "") and "fetch-history" in (p or "") for p in params),
            "Built-in fetch-history rule must be in effective_guest_allowed_tools",
        )

    def test_effective_includes_instructions_rule(self):
        """effective_guest_allowed_tools() must include 'coop instructions .*'."""
        agent = self._make_agent()
        effective = agent.effective_guest_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertTrue(
            any("coop" in (p or "") and "instructions" in (p or "") for p in params),
            "Built-in instructions rule must be in effective_guest_allowed_tools",
        )

    def test_builtin_guest_rules_precede_user_rules(self):
        """Built-in guest rules must come before user-defined rules."""
        from gateway.core.config import _BUILTIN_GUEST_TOOL_RULES
        agent = self._make_agent(extra_rules=["git log.*"])
        effective = agent.effective_guest_allowed_tools()
        builtin_count = len(_BUILTIN_GUEST_TOOL_RULES)
        # First N entries must match the built-in rules
        for i, builtin_rule in enumerate(_BUILTIN_GUEST_TOOL_RULES):
            self.assertEqual(effective[i].tool, builtin_rule.tool)
            self.assertEqual(effective[i].params, builtin_rule.params)
        # The user rule follows after
        self.assertEqual(effective[builtin_count].params, "git log.*")

    def test_guest_allowed_tools_field_unchanged(self):
        """effective_guest_allowed_tools() must NOT mutate guest_allowed_tools in place."""
        from gateway.core.config import ToolRule
        user_rule = ToolRule(tool="Bash", params="ls.*")
        agent = self._make_agent()
        agent.guest_allowed_tools = [user_rule]
        original_len = len(agent.guest_allowed_tools)
        agent.effective_guest_allowed_tools()
        self.assertEqual(len(agent.guest_allowed_tools), original_len)

    def test_empty_guest_list_still_gets_builtins(self):
        """An agent with empty guest_allowed_tools still gets built-in guest rules."""
        from gateway.core.config import _BUILTIN_GUEST_TOOL_RULES
        agent = self._make_agent()
        effective = agent.effective_guest_allowed_tools()
        self.assertGreaterEqual(len(effective), len(_BUILTIN_GUEST_TOOL_RULES))

    def test_guest_does_not_include_send_rule(self):
        """Guests must NOT get the send rule — that is owner-only."""
        agent = self._make_agent()
        effective = agent.effective_guest_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertFalse(
            any("coop" in (p or "") and "send" in (p or "") for p in params),
            "send rule must NOT be in effective_guest_allowed_tools",
        )

    def test_guest_does_not_include_schedule_rule(self):
        """Guests must NOT get the schedule rule — that is owner-only."""
        agent = self._make_agent()
        effective = agent.effective_guest_allowed_tools()
        params = [r.params for r in effective if r.tool == "Bash"]
        self.assertFalse(
            any("coop" in (p or "") and "schedule" in (p or "") for p in params),
            "schedule rule must NOT be in effective_guest_allowed_tools",
        )

    def test_fetch_history_rule_matches_actual_command(self):
        """The fetch-history guest rule must match a realistic command invocation."""
        import re

        from gateway.core.config import _BUILTIN_GUEST_TOOL_RULES
        fh_rule = next(r for r in _BUILTIN_GUEST_TOOL_RULES
                       if r.params and "fetch-history" in r.params)
        cmd = "coop fetch-history --watcher hammer-mei --count 50"
        self.assertIsNotNone(
            re.fullmatch(fh_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"fetch-history rule {fh_rule.params!r} must match command {cmd!r}",
        )

    def test_instructions_rule_matches_actual_command(self):
        """The instructions guest rule must match a realistic command invocation."""
        import re

        from gateway.core.config import _BUILTIN_GUEST_TOOL_RULES
        instr_rule = next(r for r in _BUILTIN_GUEST_TOOL_RULES
                          if r.params and "instructions" in r.params)
        cmd = "coop instructions fetch-history"
        self.assertIsNotNone(
            re.fullmatch(instr_rule.params, cmd, re.IGNORECASE | re.DOTALL),
            f"instructions rule {instr_rule.params!r} must match command {cmd!r}",
        )

    def test_instructions_rule_rejects_trailing_shell_commands(self):
        """The guest instructions rule must not auto-approve compound shell commands."""
        import re

        from gateway.core.config import _BUILTIN_GUEST_TOOL_RULES
        instr_rule = next(r for r in _BUILTIN_GUEST_TOOL_RULES
                          if r.params and "instructions" in r.params)
        bad_commands = [
            "coop instructions fetch-history; curl https://example.com",
            "coop instructions fetch-history && whoami",
            "coop instructions fetch-history | cat",
            "coop instructions fetch-history\nwhoami",
            "coop instructions fetch-history extra",
        ]
        for cmd in bad_commands:
            self.assertIsNone(
                re.fullmatch(instr_rule.params, cmd, re.IGNORECASE | re.DOTALL),
                f"instructions rule {instr_rule.params!r} must reject command {cmd!r}",
            )


class TestBuildAgentBackendUsesEffectiveMethods(unittest.TestCase):
    """Regression test for service.py Fix #1 (HIGH): _build_agent_backend must call
    effective_guest_allowed_tools() so built-in guest rules are not silently dropped."""

    def test_broker_config_includes_builtin_guest_rules(self):
        """broker_config.guest_allowed_tools must include the built-in fetch-history rule
        even when AgentConfig.guest_allowed_tools is empty.

        If service.py regresses to the raw guest_allowed_tools field, this list will be
        empty and the assertion will fail — surfacing the exact HIGH issue fixed in #35.
        """
        from unittest.mock import MagicMock, patch

        from gateway.core.config import AgentConfig, PermissionConfig
        from gateway.service import _build_agent_backend

        agent_cfg = AgentConfig(
            name="test",
            type="claude",
            command="claude",
            guest_allowed_tools=[],     # no user-defined rules
            permissions=PermissionConfig(enabled=True),
        )

        # Capture the GatewayBrokerConfig that _build_agent_backend constructs.
        captured = {}

        def _capture_broker(
            owner_allowed_tools, guest_allowed_tools, timeout, skip_owner_approval
        ):
            captured["guest"] = list(guest_allowed_tools)
            m = MagicMock()
            return m

        with patch("gateway.service.GatewayBrokerConfig", side_effect=_capture_broker):
            with patch("gateway.service.ClaudeBackend", return_value=MagicMock()):
                _build_agent_backend(agent_cfg)

        # The broker must have received the built-in fetch-history rule.
        guest_params = [r.params for r in captured.get("guest", [])]
        self.assertTrue(
            any("fetch-history" in (p or "") for p in guest_params),
            f"Built-in fetch-history rule missing from broker guest_allowed_tools: {guest_params}",
        )


# ── Tests: _deep_merge helper ─────────────────────────────────────────────────


class TestDeepMerge(unittest.TestCase):
    """Unit tests for the private _deep_merge helper used by *_defaults blocks."""

    def setUp(self):
        from gateway.config import _deep_merge

        self._deep_merge = _deep_merge

    def test_nested_dicts_merge_recursively(self):
        base = {"server": {"url": "http://x", "username": "bot"}}
        override = {"server": {"username": "override-bot"}}
        merged = self._deep_merge(base, override)
        self.assertEqual(
            merged, {"server": {"url": "http://x", "username": "override-bot"}}
        )

    def test_lists_replace_not_append(self):
        base = {"tools": [{"tool": "Read"}, {"tool": "Grep"}]}
        override = {"tools": [{"tool": "Write"}]}
        merged = self._deep_merge(base, override)
        self.assertEqual(merged["tools"], [{"tool": "Write"}])

    def test_explicit_null_override_suppresses_default(self):
        base = {"timezone": "America/Los_Angeles"}
        override = {"timezone": None}
        merged = self._deep_merge(base, override)
        self.assertIsNone(merged["timezone"])

    def test_scalar_override_wins(self):
        merged = self._deep_merge({"timeout": 360}, {"timeout": 30})
        self.assertEqual(merged["timeout"], 30)

    def test_result_does_not_alias_base_or_override(self):
        base = {"attachments": {"cache_dir_global": "shared"}}
        override = {}
        merged_a = self._deep_merge(base, override)
        merged_b = self._deep_merge(base, override)
        self.assertIsNot(merged_a["attachments"], merged_b["attachments"])
        self.assertIsNot(merged_a["attachments"], base["attachments"])
        # Mutating one merged result must not affect the other or the shared base.
        merged_a["attachments"]["cache_dir_global"] = "mutated"
        self.assertEqual(merged_b["attachments"]["cache_dir_global"], "shared")
        self.assertEqual(base["attachments"]["cache_dir_global"], "shared")


# ── Tests: connector_templates / agent_templates / watcher_templates + inherits ──
#
# v0.3 removed the single global `*_defaults:` block per kind (it merged flatly
# and unconditionally into EVERY entry regardless of type — setting type/command
# in agent_defaults to give claude agents a custom wrapper silently broke any
# opencode agent that didn't override it, since OpenCodeBackend execs
# agent_cfg.command directly as the sidecar binary). Named `*_templates:` +
# a per-entry `inherits: <name>` field replace it: a template only ever applies
# to entries that explicitly opt in, so type-specific fields are finally safe
# to share. See docs/migration-0.3.md.


class TestConnectorTemplates(unittest.TestCase):
    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_connector_inherits_template(self):
        path = self._write_config("""\
            connector_templates:
              standard:
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            connectors:
              - name: rc1
                inherits: standard
              - name: rc2
                inherits: standard
                server: {username: bot2}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc1
                agent: default
                rooms:
                  include: [general]
        """)
        config = GatewayConfig.from_file(path)
        rc1, rc2 = config.connectors[0], config.connectors[1]
        self.assertEqual(rc1.type, "rocketchat")
        self.assertEqual(rc1.raw["server"]["username"], "bot")
        # rc2 overrides only username; url/password still inherited from the template.
        self.assertEqual(rc2.raw["server"]["username"], "bot2")
        self.assertEqual(rc2.raw["server"]["url"], "http://localhost:3000")
        self.assertEqual(rc2.raw["server"]["password"], "pw")

    def test_connector_template_forbids_name(self):
        path = self._write_config("""\
            connector_templates:
              standard:
                name: not-allowed
            connectors:
              - name: rc1
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc1
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("connector_templates", str(ctx.exception))
        self.assertIn("name", str(ctx.exception))

    def test_connector_templates_must_be_mapping(self):
        path = self._write_config("""\
            connector_templates: not-a-mapping
            connectors:
              - name: rc1
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc1
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("connector_templates", str(ctx.exception))

    def test_connector_type_conflicting_with_inherited_template_type_raises(self):
        """User-reported: a connector explicitly declaring its own 'type'
        while inheriting a template written for a DIFFERENT type used to
        silently keep the entry's own type — with the rest of the
        template's (wrong-protocol) fields merged in regardless. Only a
        genuine contradiction (both sides set, and different) is an error;
        inheriting the type FROM a template with no own-type override is
        the normal, intended usage (tested elsewhere)."""
        path = self._write_config("""\
            connector_templates:
              mm-standard:
                type: mattermost
                server: {url: http://localhost:8065, token: t, team: main}
            connectors:
              - name: rc1
                type: rocketchat
                inherits: mm-standard
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc1
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        msg = str(ctx.exception)
        self.assertIn("rocketchat", msg)
        self.assertIn("mattermost", msg)

    def test_connector_type_matching_inherited_template_type_is_fine(self):
        """Same explicit type on both sides is not a conflict — this must
        keep working exactly as before."""
        path = self._write_config("""\
            connector_templates:
              standard:
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            connectors:
              - name: rc1
                type: rocketchat
                inherits: standard
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc1
                agent: default
                rooms:
                  include: [general]
        """)
        config = GatewayConfig.from_file(path)
        self.assertEqual(config.connectors[0].type, "rocketchat")

    def test_attachments_cache_dir_global_template_not_aliased_across_connectors(self):
        """Regression: cache_dir_global resolution mutates the connector's raw dict
        in place. Without a deep-copying merge, two connectors sharing the same
        template's attachments would alias the same nested dict, and
        resolving/mutating it for the first connector would corrupt the second."""
        path = self._write_config("""\
            connector_templates:
              standard:
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
                attachments:
                  cache_dir_global: shared-cache
            connectors:
              - name: rc1
                inherits: standard
              - name: rc2
                inherits: standard
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc1
                agent: default
                rooms:
                  include: [general]
        """)
        config = GatewayConfig.from_file(path)
        a = config.connectors[0].raw["attachments"]
        b = config.connectors[1].raw["attachments"]
        self.assertIsNot(a, b)
        self.assertEqual(a["cache_dir_global"], b["cache_dir_global"])
        a["cache_dir_global"] = "mutated"
        self.assertNotEqual(b["cache_dir_global"], "mutated")


class TestAgentTemplates(unittest.TestCase):
    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_agent_inherits_template(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_templates:
              standard:
                type: claude
                working_directory: /tmp
                timeout: 500
            agents:
              default: {inherits: standard}
              other:
                inherits: standard
                timeout: 42
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        config = GatewayConfig.from_file(path)
        self.assertEqual(config.agents["default"].working_directory, "/tmp")
        self.assertEqual(config.agents["default"].timeout, 500)
        # 'other' overrides timeout but still inherits working_directory/type.
        self.assertEqual(config.agents["other"].timeout, 42)
        self.assertEqual(config.agents["other"].working_directory, "/tmp")

    def test_agent_type_conflicting_with_inherited_template_type_raises(self):
        """User-reported: an agent explicitly declaring its own 'type'
        (e.g. claude) while inheriting a template written for a DIFFERENT
        type (e.g. opencode) used to silently keep the agent's own type —
        with the rest of that template's (wrong-backend) fields, like
        `command`, merged in regardless. Only a genuine contradiction (both
        sides set, and different) is an error."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_templates:
              opencode-standard:
                type: opencode
                command: opencode
            agents:
              agent-a:
                type: claude
                inherits: opencode-standard
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: agent-a
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        msg = str(ctx.exception)
        self.assertIn("claude", msg)
        self.assertIn("opencode", msg)

    def test_agent_template_can_set_type_and_command_independently(self):
        """The motivating regression test for this whole redesign: a template
        only applies to agents that opt in via inherits:, so type/command can
        finally be shared safely — a claude-shaped template's command never
        leaks onto an opencode agent (or vice versa) the way the old single
        global agent_defaults block would have (OpenCodeBackend execs
        agent_cfg.command directly as the sidecar binary — a claude-shaped
        command there silently tried to spawn the wrong process)."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_templates:
              claude-standard:
                type: claude
                command: claude-no-p
              opencode-standard:
                type: opencode
                command: opencode
            agents:
              agent-a:
                inherits: claude-standard
                working_directory: /tmp
              agent-b:
                inherits: opencode-standard
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: agent-a
                rooms:
                  include: [general]
        """)
        config = GatewayConfig.from_file(path)
        self.assertEqual(config.agents["agent-a"].type, "claude")
        self.assertEqual(config.agents["agent-a"].command, "claude-no-p")
        self.assertEqual(config.agents["agent-b"].type, "opencode")
        self.assertEqual(config.agents["agent-b"].command, "opencode")

    def test_agent_without_type_and_without_inherits_raises(self):
        """Reported gap: an agent that has agent_templates available but
        forgets to set 'inherits:' (and doesn't set 'type:' directly) used to
        silently fall back to the hardcoded default type/command ('claude'/
        'claude') — a config that looked valid but silently ran the wrong
        agent type/command. 'type:' is now required, same as
        'working_directory:', so this is a hard, actionable error instead."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_templates:
              standard:
                type: opencode
                command: opencode
            agents:
              forgot-inherits:
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                rooms:
                  include: [general]
                agent: forgot-inherits
        """)
        with self.assertRaisesRegex(
            ValueError,
            "Agent 'forgot-inherits': type is required",
        ):
            GatewayConfig.from_file(path)

    def test_agent_non_string_type_raises_cleanly(self):
        """A malformed 'type:' (e.g. a YAML typo producing a list instead of
        a string) must not crash inside the type-aware command fallback's
        dict lookup (dict.get() on an unhashable key raises TypeError, which
        `coop config validate` doesn't catch) — it should
        surface the same kind of clear ValueError as every other malformed-
        field check in this loader."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              bad-agent:
                type: [claude]
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                rooms:
                  include: [general]
                agent: bad-agent
        """)
        with self.assertRaisesRegex(
            ValueError,
            "Agent 'bad-agent': type must be a string",
        ):
            GatewayConfig.from_file(path)

    def test_agent_command_defaults_per_type_not_a_fixed_fallback(self):
        """The other half of the same gap: 'command' used to have a single
        hardcoded fallback ('claude') regardless of 'type' — an agent with
        type: opencode but no explicit command would silently get command:
        claude, which OpenCodeBackend execs directly as the wrong sidecar
        binary. The fallback is now chosen based on the agent's own
        (required) type."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              claude-agent:
                type: claude
                working_directory: /tmp
              opencode-agent:
                type: opencode
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                rooms:
                  include: [general]
                agent: claude-agent
        """)
        config = GatewayConfig.from_file(path)
        self.assertEqual(config.agents["claude-agent"].command, "claude")
        self.assertEqual(config.agents["opencode-agent"].command, "opencode")

    def test_agent_template_permissions_deep_merge(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_templates:
              standard:
                type: claude
                working_directory: /tmp
                timeout: 500
                permissions: {enabled: true, timeout: 300}
            agents:
              default:
                inherits: standard
                permissions: {timeout: 100}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        config = GatewayConfig.from_file(path)
        perms = config.agents["default"].permissions
        # enabled inherited from the template, timeout overridden by the entry.
        self.assertTrue(perms.enabled)
        self.assertEqual(perms.timeout, 100)

    def test_agent_template_tool_list_replaces_not_appends(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_templates:
              standard:
                type: claude
                working_directory: /tmp
                owner_allowed_tools:
                  - tool: Read
                  - tool: Grep
            agents:
              default:
                inherits: standard
                owner_allowed_tools:
                  - tool: Write
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        config = GatewayConfig.from_file(path)
        tools = [r.tool for r in config.agents["default"].owner_allowed_tools]
        self.assertEqual(tools, ["Write"])

    def test_agent_unknown_inherits_reference_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_templates:
              standard: {type: claude}
            agents:
              default:
                inherits: nope
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        msg = str(ctx.exception)
        self.assertIn("unknown agent_templates", msg)
        self.assertIn("nope", msg)
        self.assertIn("standard", msg)  # lists the one available template

    def test_agent_template_cannot_set_inherits(self):
        """No nested templates — mirrors tool_presets' own 'no preset-of-presets'
        rule."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_templates:
              bad:
                inherits: other
            agents:
              default:
                inherits: bad
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        msg = str(ctx.exception)
        self.assertIn("agent_templates", msg)
        self.assertIn("must not set 'inherits'", msg)


class TestAgentSessionLifecycleKeysAreRejected(unittest.TestCase):
    """The TTLs moved from the agent to the watcher rule (design §5.4).

    Removing them silently was the wrong option: the value would be read, ignored,
    and the lifecycle the operator asked for would simply never happen — the exact
    shape of failure this loader has produced repeatedly. So a leftover key is a
    hard error naming the new home, the same treatment `_REMOVED_DEFAULTS_KEYS`
    gives a retired top-level block.

    Both keys were added after v0.5.1 and never released, so this breaks no deployed
    config; it only catches someone following the pre-release design doc.
    """

    def _write_config(self, agent_block: str) -> str:
        cfg = textwrap.dedent(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: /tmp
{textwrap.indent(textwrap.dedent(agent_block), "                ")}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            return f.name

    def test_an_agent_no_longer_carries_the_fields_at_all(self):
        config = GatewayConfig.from_file(self._write_config(""))
        agent = config.agents["default"]
        self.assertFalse(hasattr(agent, "session_idle_days"))
        self.assertFalse(hasattr(agent, "session_expire_days"))

    def test_either_key_on_an_agent_is_a_load_error_naming_the_rule(self):
        for key in ("session_idle_days", "session_expire_days"):
            with self.subTest(key=key):
                path = self._write_config(f"{key}: 7\n")
                with self.assertRaises(ValueError) as ctx:
                    GatewayConfig.from_file(path)
                msg = str(ctx.exception)
                self.assertIn(key, msg)
                self.assertIn("'watcher_rules:'", msg)  # contract: says where it goes now

    def test_an_explicit_null_is_rejected_too(self):
        """`null` is not a way to keep the key: the field is gone, so writing it at
        all means the operator believes an agent-level lifecycle exists."""
        path = self._write_config("session_idle_days: null\n")
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("'watcher_rules:'", str(ctx.exception))

    def test_a_value_inherited_from_an_agent_template_is_rejected(self):
        """The check reads the already-merged entry, so a template cannot smuggle
        one in — the case a key-presence check on the raw entry would have missed."""
        cfg = textwrap.dedent("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_templates:
              standard:
                type: claude
                working_directory: /tmp
                session_idle_days: 7
            agents:
              default: {inherits: standard}
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            path = f.name
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("'watcher_rules:'", str(ctx.exception))

    def test_collect_config_attributes_it_to_the_agent_and_keeps_going(self):
        """A second, healthy agent isolates the attribution from the pre-existing
        cascade: when the *only* agent fails, zero agents parse and
        collect_config() adds its own "must define at least one agent" global issue
        on top. That cascade is existing behaviour, asserted separately below."""
        cfg = textwrap.dedent("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
              legacy:
                type: claude
                working_directory: /tmp
                session_expire_days: 30
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            path = f.name
        config, issues = collect_config(path)
        self.assertEqual(len(issues), 1, [i.message for i in issues])
        self.assertEqual(issues[0].entity_kind, "agent")
        self.assertEqual(issues[0].entity_name, "legacy")
        # The healthy agent and the watcher are unaffected.
        self.assertEqual(sorted(config.agents), ["default"])
        self.assertEqual([r.name for r in config.watcher_rules], ["w1"])

    def test_the_only_agent_failing_still_reports_the_named_key_first(self):
        """Pre-existing cascade, pinned so the attributed issue is not mistaken for
        the whole story: the "at least one agent" global issue follows it."""
        _, issues = collect_config(self._write_config("session_expire_days: 30\n"))
        self.assertEqual([i.entity_kind for i in issues], ["agent", "global"])
        self.assertIn("'watcher_rules:'", issues[0].message)


class TestWatcherTemplates(unittest.TestCase):
    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_watcher_inherits_template(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_templates:
              standard:
                connector: rc
                agent: default
            watcher_rules:
              - name: w1
                inherits: standard
                rooms:
                  include: [general]
        """)
        config = GatewayConfig.from_file(path)
        wc = config.watcher_rules[0]
        self.assertEqual(wc.connector, "rc")
        self.assertEqual(wc.agent, "default")

    def test_watcher_template_forbids_identity_fields(self):
        # Only `name` is left. `session_id`, `room` and `rooms` all used to be
        # here: the first two stopped being keys at all (the closed rule shape
        # reports them, naming the entry rather than the template), and `rooms`
        # became inheritable when the `watcher_rules:` rename removed the shape
        # sniffing that depended on it.
        for key, value in (
            ("name", "shared-name"),
        ):
            with self.subTest(key=key):
                path = self._write_config(f"""\
                    connectors:
                      - name: rc
                        type: rocketchat
                        server: {{url: http://localhost:3000, username: bot, password: pw}}
                    agents:
                      default:
                        type: claude
                        working_directory: /tmp
                    watcher_templates:
                      standard:
                        {key}: {value!r}
                    watcher_rules:
                      - name: w1
                        connector: rc
                        agent: default
                        rooms:
                          include: [general]
                """)
                with self.assertRaises(ValueError) as ctx:
                    GatewayConfig.from_file(path)
                self.assertIn("watcher_templates", str(ctx.exception))
                self.assertIn(key, str(ctx.exception))

    def test_watcher_type_field_is_not_special_but_the_shared_mismatch_check_still_applies(self):
        """PR review finding/clarification: the type-mismatch check in
        _resolve_inherits() (added for connectors/agents — see
        TestConnectorTemplates/TestAgentTemplates) is a single, generic
        implementation shared by ALL THREE entity kinds, including watchers
        — even though WatcherConfig has no real 'type' field at all and
        never reads one. A watcher entry setting 'type:' is just an inert,
        unused extra key; but if BOTH the entry and its inherited template
        happen to set one, with different values, the same shared check
        still fires. This is an accepted, harmless side effect of reusing
        one implementation for all three kinds (matching this codebase's
        own "never duplicate a rule" principle) rather than a real feature
        — nothing legitimate ever sets 'type:' on a watcher."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_templates:
              standard:
                connector: rc
                agent: default
                type: bar
            watcher_rules:
              - name: w1
                inherits: standard
                type: foo
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        msg = str(ctx.exception)
        self.assertIn("foo", msg)
        self.assertIn("bar", msg)

    def test_watcher_inherits_resolved_before_rooms_expansion(self):
        """inherits: must resolve before the room/rooms expansion below it in
        from_file — otherwise a rooms: group's expanded watchers wouldn't see
        the template's fields at all."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_templates:
              standard:
                session_expire_days: 30
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                inherits: standard
                rooms:
                  include: [a, b, c]
        """)
        config = GatewayConfig.from_file(path)
        self.assertEqual(len(config.watcher_rules), 1)
        self.assertEqual(config.watcher_rules[0].session_expire_days, 30)


class TestRemovedDefaultsKeysRejected(unittest.TestCase):
    """A leftover v0.2 '*_defaults:' key is a hard, actionable error — not a
    silent no-op — since silently dropping it would silently drop whatever
    permission/timeout/tool-allow settings someone still thinks are shared."""

    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_leftover_agent_defaults_key_raises(self):
        path = self._write_config("""\
            agent_defaults: {timeout: 100}
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        msg = str(ctx.exception)
        self.assertIn("agent_defaults", msg)
        self.assertIn("no longer supported", msg)
        self.assertIn("agent_templates", msg)

    def test_leftover_connector_defaults_key_raises(self):
        path = self._write_config("""\
            connector_defaults: {type: rocketchat}
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        msg = str(ctx.exception)
        self.assertIn("connector_defaults", msg)
        self.assertIn("no longer supported", msg)
        self.assertIn("connector_templates", msg)

    def test_leftover_watcher_defaults_key_raises(self):
        path = self._write_config("""\
            watcher_defaults: {connector: rc}
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        msg = str(ctx.exception)
        self.assertIn("watcher_defaults", msg)
        self.assertIn("no longer supported", msg)
        self.assertIn("watcher_templates", msg)


# ── Tests: tool_presets ────────────────────────────────────────────────────────


class TestToolPresets(unittest.TestCase):
    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_preset_reference_resolves_to_rules(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            tool_presets:
              readonly:
                - tool: Read
                - tool: Grep
            agents:
              default:
                type: claude
                working_directory: /tmp
                owner_allowed_tools:
                  - readonly
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        config = GatewayConfig.from_file(path)
        tools = [r.tool for r in config.agents["default"].owner_allowed_tools]
        self.assertEqual(tools, ["Read", "Grep"])

    def test_preset_and_inline_rules_are_mixable_and_ordered(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            tool_presets:
              readonly:
                - tool: Read
            agents:
              default:
                type: claude
                working_directory: /tmp
                owner_allowed_tools:
                  - tool: Bash
                    params: "git .*"
                  - readonly
                  - tool: Write
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        config = GatewayConfig.from_file(path)
        tools = [r.tool for r in config.agents["default"].owner_allowed_tools]
        self.assertEqual(tools, ["Bash", "Read", "Write"])

    def test_unknown_preset_reference_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
                owner_allowed_tools:
                  - nonexistent
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("unknown tool preset 'nonexistent'", str(ctx.exception))

    def test_preset_referencing_another_preset_raises(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            tool_presets:
              base:
                - tool: Read
              wrapper:
                - base
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("presets cannot reference another preset", str(ctx.exception))

    def test_invalid_preset_rule_raises_even_if_unused(self):
        """Presets are validated eagerly at load, even if no agent references them."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            tool_presets:
              broken:
                - tool: '[invalid'
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        self.assertIn("invalid tool rule", str(ctx.exception).lower())

    def test_invalid_inline_rule_raises_with_agent_and_field_context(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
                guest_allowed_tools:
                  - tool: '[invalid'
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(path)
        msg = str(ctx.exception)
        self.assertIn("invalid tool rule", msg.lower())
        self.assertIn("guest_allowed_tools", msg)


# ── Tests: quiet notification defaults ────────────────────────────────────────


class TestNotificationFieldsAreRemoved(unittest.TestCase):
    """Owner decision (2026-08-02, executed with the runtime cutover): the
    notification fields are gone — platform presence is the signal, and under
    the idle/expire lifecycle a watcher starts and stops many times over its
    life. A config still carrying them fails loudly, template or rule."""

    def _write_config(self, watcher_extra: str = "") -> str:
        cfg = textwrap.dedent(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
{textwrap.indent(textwrap.dedent(watcher_extra), "                ")}
        """)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(cfg)
            return f.name

    def test_a_rule_carrying_the_field_is_refused(self):
        path = self._write_config("online_notification: 'hi'")
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(path)
        self.assertIn("unknown key", str(cm.exception))

    def test_a_template_carrying_the_field_is_refused_via_the_inheritor(self):
        path = self._write_config()
        with open(path) as f:
            body = f.read()
        body = body.replace(
            "connectors:",
            "watcher_templates:\n"
            "  standard:\n"
            "    offline_notification: 'bye'\n"
            "connectors:",
            1,
        )
        body = body.replace("- name: w1", "- name: w1\n    inherits: standard", 1)
        with open(path, "w") as f:
            f.write(body)
        with self.assertRaises(ValueError) as cm:
            GatewayConfig.from_file(path)
        self.assertIn("unknown key", str(cm.exception))


class TestDescriptionField(unittest.TestCase):
    """'description:' is a purely informational annotation (read by the
    config TUI) — the loader must accept it everywhere without letting it
    affect behavior: it must not appear in a connector's raw dict, and a
    *_templates block's own description must never propagate into entries."""

    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(body))
            return f.name

    def test_connector_description_is_not_in_raw(self):
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                description: "Primary bot account"
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        config = GatewayConfig.from_file(path)
        self.assertNotIn("description", config.connectors[0].raw)

    def test_connector_template_description_does_not_propagate(self):
        path = self._write_config("""\
            connector_templates:
              standard:
                description: "Shared settings for all bots"
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            connectors:
              - name: rc1
                inherits: standard
              - name: rc2
                inherits: standard
                description: "This one's special"
            agents:
              default:
                type: claude
                working_directory: /tmp
            watcher_rules:
              - name: w1
                connector: rc1
                agent: default
                rooms:
                  include: [general]
        """)
        config = GatewayConfig.from_file(path)
        # Neither connector's raw should carry a 'description' key — rc1 never
        # had one of its own, and rc2's own value must not have been merged
        # with (or overwritten by) the template's description.
        self.assertNotIn("description", config.connectors[0].raw)
        self.assertNotIn("description", config.connectors[1].raw)

    def test_agent_and_watcher_description_do_not_break_loading(self):
        """agents/watchers already ignore unknown keys via .get() — this just
        pins that 'description' specifically doesn't need special handling
        there and doesn't leak into any typed field."""
        path = self._write_config("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agent_templates:
              standard:
                description: "Shared claude settings"
            agents:
              default:
                inherits: standard
                type: claude
                working_directory: /tmp
                description: "The main agent"
            watcher_templates:
              standard:
                description: "Shared watcher settings"
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
                description: "General channel watcher"
        """)
        config = GatewayConfig.from_file(path)
        self.assertEqual(config.agents["default"].working_directory, "/tmp")
        self.assertEqual(len(config.watcher_rules), 1)
        self.assertEqual(config.watcher_rules[0].name, "w1")


if __name__ == "__main__":
    unittest.main()


# ── Tests: the static watcher shape is a hard load error (cutover, §5.4) ─────


class TestTheOldShapeIsReportedPerKey(unittest.TestCase):
    """A half-migrated config is reported key by key, not by shape.

    This class used to assert that an entry with `room:` (or `rooms:` as a list)
    produced a dedicated "old format" error, because the loader decided which
    parser an entry belonged to by looking for a `rooms:` MAPPING on the raw
    entry. That test is gone with the mechanism: the shape sniffing is exactly
    what stopped `rooms` being inheritable from a template, since a template is
    merged only AFTER the shape has been decided.

    What replaces it is narrower and general. The block name settles what an
    entry is — everything under `watcher_rules:` is a rule — so a leftover
    `room:` is just a key a rule does not have, and the old top-level
    `watchers:` is just a key config.yaml does not have. Both are already loud,
    by rules that were not written for this migration.
    """

    BASE = """\
        connectors:
          - name: rc
            type: rocketchat
            server: {url: http://localhost:3000, username: bot, password: pw}
        agents:
          default:
            type: claude
            working_directory: /tmp
        watcher_rules:
    """
    def _write(self, watchers_block: str) -> str:
        body = textwrap.dedent(self.BASE) + textwrap.indent(
            textwrap.dedent(watchers_block), "  ")
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            return f.name
    def _msg(self, watchers_block: str) -> str:
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(self._write(watchers_block))
        return str(ctx.exception)

    def test_a_leftover_room_key_is_an_unknown_key(self):
        msg = self._msg("- name: w1\n  room: general\n")
        self.assertIn("unknown key(s)", msg)
        self.assertIn("'room'", msg)
        self.assertIn("'rooms'", msg, "the valid-key list shows the replacement")

    def test_a_rooms_list_is_refused_by_the_rooms_parser(self):
        msg = self._msg("- name: w1\n  rooms: [general, dev]\n")
        self.assertIn("'rooms' must be a mapping", msg)

    def test_a_non_mapping_entry_is_malformed(self):
        msg = self._msg("- just-a-string\n")
        self.assertIn("must be a mapping", msg)

    def test_the_old_top_level_key_names_its_replacement(self):
        """The whole migration signal now, and it comes from the general
        unknown-top-level-key check rather than a case written for it."""
        path = self._write("- name: w1\n  rooms:\n    include: [general]\n").replace(
            "watcher_rules:", "watchers:"
        )
        # _write returns a path; rewrite the file itself with the old key.
        import pathlib as _p
        f = _p.Path(self._write("- name: w1\n  rooms:\n    include: [general]\n"))
        f.write_text(f.read_text().replace("watcher_rules:", "watchers:"))
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(str(f))
        msg = str(ctx.exception)
        self.assertIn("'watchers'", msg)
        self.assertIn("did you mean 'watcher_rules'?", msg)
        del path

    def test_collect_config_reports_every_leftover_entry_in_one_pass(self):
        """A half-migrated config lists all its remaining old entries at once,
        and the rule entries beside them still parse."""
        path = self._write(
            "- name: a\n  agent: default\n  connector: rc\n  room: general\n"
            "- name: ok\n  agent: default\n  connector: rc\n  rooms:\n    include: [dev]\n"
            "- name: b\n  agent: default\n  connector: rc\n  room: ops\n"
        )
        config, issues = collect_config(path)
        leftover = [i for i in issues if "unknown key(s)" in i.message]
        self.assertEqual(len(leftover), 2, [i.message for i in issues])
        self.assertEqual([r.name for r in config.watcher_rules], ["ok"])


class TestContextInjectFileListValidation(unittest.TestCase):
    """A bare string where a list belongs must be an error, not eight paths.

    `_resolve_paths` iterates what it is given, so `context_inject_files: notes.md`
    silently became one path per character — a config that loads clean and injects
    nothing. All three context layers shared the defect, so the check lives in
    `_resolve_paths` rather than at each call site.
    """

    BASE = """\
        connectors:
          - name: rc
            type: rocketchat
            server: {{url: http://localhost:3000, username: bot, password: pw}}
        {conn}
        agents:
          default:
            type: claude
            working_directory: /tmp
        {agent}
        watcher_rules:
          - name: w1
            connector: rc
            agent: default
            rooms:
              include: [general]
        {watcher}
        """

    def _write(self, *, conn="", agent="", watcher="") -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(self.BASE).format(conn=conn, agent=agent, watcher=watcher))
            return f.name

    def _rejects(self, needle: str, **kw) -> str:
        with self.assertRaises(ValueError) as ctx:
            GatewayConfig.from_file(self._write(**kw))
        self.assertIn(needle, str(ctx.exception))
        return str(ctx.exception)

    def test_a_bare_string_is_rejected_on_a_connector(self):
        msg = self._rejects("Connector 'rc': 'context_inject_files'",
                            conn="    context_inject_files: c.md")
        self.assertIn("one character at a time", msg)

    def test_a_bare_string_is_rejected_on_an_agent(self):
        self._rejects("Agent 'default': 'context_inject_files'",
                      agent="    context_inject_files: a.md")

    def test_a_bare_string_is_rejected_on_a_watcher(self):
        self._rejects("Watcher rule at index 0 ('w1'): 'context_inject_files'",
                      watcher="    context_inject_files: w.md")

    def test_a_non_string_element_is_a_value_error_not_a_type_error(self):
        """TypeError would escape collect_config()'s `except ValueError` and abort
        the whole validation pass rather than reporting one entry."""
        self._rejects("entries must be strings", watcher="    context_inject_files: [ok.md, 3]")

    def test_valid_lists_still_load_on_all_three_layers(self):
        cfg = GatewayConfig.from_file(self._write(
            conn="    context_inject_files: [c.md]",
            agent="    context_inject_files: [a.md]",
            watcher="    context_inject_files: [w.md]",
        ))
        self.assertEqual(len(cfg.connectors[0].context_inject_files), 1)
        self.assertEqual(len(cfg.agents["default"].context_inject_files), 1)
        self.assertEqual(len(cfg.watcher_rules[0].context_inject_files), 1)

    def test_omitting_them_entirely_still_loads(self):
        cfg = GatewayConfig.from_file(self._write())
        self.assertEqual(cfg.watcher_rules[0].context_inject_files, [])
