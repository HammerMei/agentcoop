"""Unit tests for gateway/configtool/model.py — EditableConfig.

These pin the keystone design decision from docs/design/config-tool.md: the
config TUI reads/writes the PRE-MERGE raw document, never GatewayConfig, and
never through a code path that expands $VAR env references (that would risk
writing resolved secrets back to disk in a later save-capable phase).
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from gateway.config import GatewayConfig
from gateway.config_validate import validate_config
from gateway.configtool.model import EditableConfig, Provenance, StatusIndex


class _EditableConfigTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent_dir = Path(self.tmp) / "work"
        self.agent_dir.mkdir()

    def _write(self, yaml_text: str) -> Path:
        path = Path(self.tmp) / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return path


class TestEditableConfigLoad(_EditableConfigTestBase):
    def test_load_returns_raw_document(self):
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
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
        cfg = EditableConfig.load(path)
        self.assertEqual(cfg.path, path)
        self.assertEqual(len(cfg.connectors_raw), 1)
        self.assertEqual(cfg.connectors_raw[0]["name"], "rc")
        self.assertIn("default", cfg.agents_raw)
        self.assertEqual(len(cfg.watchers_raw), 1)

    def test_env_var_reference_is_never_expanded(self):
        """Regression for the keystone decision: EditableConfig must load via
        plain yaml.safe_load, never GatewayConfig.from_file. docs/design/
        config-tool.md decision 6, final revision: GatewayConfig.from_file()
        itself no longer expands $VAR either (a value that looks like a
        placeholder is a plain literal, same as everywhere else) — so this
        now also holds true via the real loader, confirmed below as a
        cross-check that EditableConfig.load() didn't accidentally start
        relying on that no-longer-distinctive behavior."""
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL_NEVER_SET_12345", username: bot, password: pw}}
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
        cfg = EditableConfig.load(path)
        self.assertEqual(
            cfg.connectors_raw[0]["server"]["url"], "$RC_URL_NEVER_SET_12345"
        )
        # Cross-check: the real loader treats it identically now (a plain
        # literal, not raised on, not resolved) — EditableConfig.load()'s
        # own reasons for using plain yaml.safe_load (provenance, raw
        # rooms: groupings — see module docstring) still stand regardless.
        real_cfg = GatewayConfig.from_file(path)
        self.assertEqual(
            real_cfg.connectors[0].raw["server"]["url"], "$RC_URL_NEVER_SET_12345"
        )

    def test_nonexistent_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            EditableConfig.load(Path(self.tmp) / "does-not-exist.yaml")

    def test_non_mapping_top_level_raises_value_error(self):
        path = Path(self.tmp) / "config.yaml"
        path.write_text("- just\n- a\n- list\n")
        with self.assertRaises(ValueError):
            EditableConfig.load(path)

    def test_reload_picks_up_on_disk_changes(self):
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
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
        cfg = EditableConfig.load(path)
        self.assertEqual(len(cfg.watchers_raw), 1)

        path.write_text(
            path.read_text()
            + "  - name: w2\n    rooms: {include: [dev]}\n"
        )
        cfg.reload()
        self.assertEqual(len(cfg.watchers_raw), 2)


class TestEditableConfigTemplates(_EditableConfigTestBase):
    def test_templates_strips_description(self):
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
            agent_templates:
              standard:
                description: "Shared claude settings"
                type: claude
                working_directory: {self.agent_dir}
            agents:
              default:
                inherits: standard
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        cfg = EditableConfig.load(path)
        standard = cfg.templates("agent")["standard"]
        self.assertNotIn("description", standard)
        self.assertEqual(standard["type"], "claude")

    def test_templates_enforces_forbidden_keys_like_the_real_loader(self):
        path = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: "$RC_URL", username: bot, password: pw}
            watcher_templates:
              standard:
                name: not-allowed
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
        cfg = EditableConfig.load(path)
        with self.assertRaises(ValueError):
            cfg.templates("watcher")


class TestEditableConfigTemplatesCaching(_EditableConfigTestBase):
    """Code review item 8: templates() is cached per kind (see
    EditableConfig._templates_cache) instead of re-running
    _parse_templates_block on every call. These tests pin the two things
    that matter about a cache: repeated calls return the equivalent value,
    and load()/reload() — the only ways `document` changes — invalidate it."""

    def _cfg(self) -> tuple[EditableConfig, Path]:
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
            agent_templates:
              standard:
                type: claude
                working_directory: {self.agent_dir}
            agents:
              default:
                inherits: standard
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        return EditableConfig.load(path), path

    def test_repeated_calls_return_the_same_cached_object(self):
        cfg, _ = self._cfg()
        first = cfg.templates("agent")
        second = cfg.templates("agent")
        self.assertIs(first, second)

    def test_reload_invalidates_the_cache(self):
        cfg, path = self._cfg()
        first = cfg.templates("agent")
        self.assertEqual(first["standard"]["type"], "claude")

        path.write_text(
            path.read_text().replace("type: claude", "type: opencode")
        )
        cfg.reload()
        second = cfg.templates("agent")
        self.assertEqual(second["standard"]["type"], "opencode")
        self.assertIsNot(first, second)

    def test_a_failed_lookup_is_not_cached_as_a_false_success(self):
        path = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: "$RC_URL", username: bot, password: pw}
            watcher_templates:
              standard:
                name: not-allowed
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
        cfg = EditableConfig.load(path)
        with self.assertRaises(ValueError):
            cfg.templates("watcher")
        # Calling again must still raise — a cache bug could swallow this
        # into a stale/absent cached value instead of re-validating.
        with self.assertRaises(ValueError):
            cfg.templates("watcher")


class TestEditableConfigProvenance(_EditableConfigTestBase):
    def _cfg(self) -> EditableConfig:
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "$RC_URL", username: bot, password: pw}}
            agent_templates:
              standard:
                type: claude
                working_directory: {self.agent_dir}
                timeout: 1800
                permissions: {{enabled: true, timeout: 300}}
            agents:
              inherits-everything:
                inherits: standard
              overrides-timeout:
                inherits: standard
                timeout: 500
              suppresses-timeout:
                inherits: standard
                timeout: null
              no-template-at-all:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w1
                connector: rc
                agent: inherits-everything
                rooms:
                  include: [general]
        """)
        return EditableConfig.load(path)

    def test_field_absent_from_entry_is_inherited(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["inherits-everything"]
        self.assertEqual(
            cfg.field_provenance("agent", entry, "timeout"),
            Provenance.INHERITED,
        )

    def test_field_explicitly_set_is_explicit(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["overrides-timeout"]
        self.assertEqual(
            cfg.field_provenance("agent", entry, "timeout"),
            Provenance.EXPLICIT,
        )

    def test_explicit_null_over_a_template_value_is_suppressing(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["suppresses-timeout"]
        self.assertEqual(
            cfg.field_provenance("agent", entry, "timeout"),
            Provenance.EXPLICIT_SUPPRESSING,
        )

    def test_field_absent_and_template_doesnt_set_it_is_default(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["suppresses-timeout"]
        # 'session_prefix' has no entry in the 'standard' template here, so
        # even though this entry DOES have inherits: standard, that template
        # doesn't cover this field — falls through to the code-level
        # dataclass default. Distinct from genuinely inheriting a value
        # (Provenance.DEFAULT, not INHERITED).
        self.assertEqual(
            cfg.field_provenance("agent", entry, "session_prefix"),
            Provenance.DEFAULT,
        )

    def test_field_absent_with_no_inherits_at_all_is_also_default(self):
        cfg = self._cfg()
        entry = cfg.agents_raw["no-template-at-all"]
        # No inherits: key at all — the OTHER way to land on
        # Provenance.DEFAULT, collapsed into the same enum value as the case
        # above (see Provenance's own docstring in model.py).
        self.assertEqual(
            cfg.field_provenance("agent", entry, "timeout"),
            Provenance.DEFAULT,
        )

    def test_merged_entry_reflects_real_resolve_inherits(self):
        cfg = self._cfg()
        merged = cfg.merged_entry("agent", cfg.agents_raw["overrides-timeout"])
        self.assertEqual(merged["timeout"], 500)  # entry's own override wins
        self.assertEqual(merged["type"], "claude")  # inherited from the template
        # nested dict merges too (permissions comes from the template wholesale)
        self.assertEqual(merged["permissions"], {"enabled": True, "timeout": 300})


class TestEditableConfigValidatedView(_EditableConfigTestBase):
    def test_validated_view_returns_real_gateway_config(self):
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
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
        cfg = EditableConfig.load(path)
        view = cfg.validated_view()
        self.assertIsInstance(view, GatewayConfig)
        self.assertEqual(len(view.watcher_rules), 1)
        self.assertEqual(view.watcher_rules[0].name, "w1")

    def test_validated_view_raises_same_as_from_file_on_invalid_config(self):
        path = self._write("""\
            connectors:
              - name: rc
                type: rocketchat
                server: {url: http://localhost:3000, username: bot, password: pw}
            agents:
              default:
                type: claude
            watcher_rules:
              - name: w1
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        cfg = EditableConfig.load(path)
        with self.assertRaises(ValueError):
            cfg.validated_view()


class TestEditableConfigDirtyTracking(_EditableConfigTestBase):
    def _cfg(self) -> EditableConfig:
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
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
        return EditableConfig.load(path)

    def test_freshly_loaded_config_is_not_dirty(self):
        cfg = self._cfg()
        self.assertFalse(cfg.dirty)

    def test_mark_dirty_sets_the_flag_and_clears_the_templates_cache(self):
        cfg = self._cfg()
        cached = cfg.templates("agent")  # populate the cache (empty here)
        cfg.document["agent_templates"] = {"standard": {"type": "opencode"}}
        cfg.mark_dirty()
        self.assertTrue(cfg.dirty)
        self.assertIsNot(cfg.templates("agent"), cached)
        self.assertEqual(cfg.templates("agent")["standard"]["type"], "opencode")

    def test_reload_clears_dirty(self):
        cfg = self._cfg()
        cfg.mark_dirty()
        self.assertTrue(cfg.dirty)
        cfg.reload()
        self.assertFalse(cfg.dirty)


class TestEditableConfigSave(_EditableConfigTestBase):
    """EditableConfig.save() — docs/design/config-tool.md decision 5:
    validate-before-write via a same-directory temp file, backup, atomic
    rename. The $VAR-survives-save test is the security keystone (advisor
    flagged this explicitly): if save() ever wrote a RESOLVED secret back to
    config.yaml, that's an incident, not a bug.
    """

    # A secret field (password), not url: config_validate's URL-format check
    # (added alongside the "does this look like a URL" validation) would
    # otherwise reject this placeholder as a malformed server.url.
    ENV_VAR_NAME = "RC_PASSWORD_FOR_CONFIGTOOL_SAVE_TEST"

    def setUp(self):
        super().setUp()
        os.environ[self.ENV_VAR_NAME] = "hunter2"
        self.addCleanup(os.environ.pop, self.ENV_VAR_NAME, None)

    def _valid_cfg_text(self) -> str:
        return f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: "${self.ENV_VAR_NAME}"}}
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
        """

    def test_save_preserves_the_unresolved_env_var_placeholder(self):
        path = self._write(self._valid_cfg_text())
        cfg = EditableConfig.load(path)
        cfg.save()
        # Read back with plain yaml (never the env-expanding loader) — the
        # literal "$VAR" string must survive, not the resolved secret.
        raw = yaml.safe_load(path.read_text())
        self.assertEqual(
            raw["connectors"][0]["server"]["password"], f"${self.ENV_VAR_NAME}"
        )

    def test_save_writes_a_timestamped_backup_of_the_prior_contents(self):
        path = self._write(self._valid_cfg_text())
        original_text = path.read_text()
        cfg = EditableConfig.load(path)
        cfg.save()
        backup_dir = path.parent / ".config-backups"
        backups = list(backup_dir.glob("config.yaml.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original_text)
        # secrets-adjacent files: backup dir owner-only, backup file owner-only
        self.assertEqual(oct(backup_dir.stat().st_mode)[-3:], "700")
        self.assertEqual(oct(backups[0].stat().st_mode)[-3:], "600")

    def test_two_saves_in_one_second_keep_two_backups(self):
        path = self._write(self._valid_cfg_text())
        first_text = path.read_text()
        cfg = EditableConfig.load(path)
        cfg.save()
        cfg.document["agents"]["default"]["timeout"] = 7
        cfg.mark_dirty()
        cfg.save()
        backups = sorted((path.parent / ".config-backups").glob("config.yaml.bak.*"))
        self.assertEqual(len(backups), 2, "the second save must not overwrite the first snapshot")
        self.assertEqual(backups[0].read_text(), first_text)

    def test_the_temp_file_is_owner_only_from_the_first_byte(self):
        # The chmod on config.yaml after the replace is not enough: under
        # umask 022 the temp file held every secret world-readable meanwhile.
        path = self._write(self._valid_cfg_text())
        cfg = EditableConfig.load(path)
        modes: list[int] = []
        real_dump = yaml.dump

        def spying_dump(data, stream, **kw):
            modes.append(os.fstat(stream.fileno()).st_mode & 0o777)
            return real_dump(data, stream, **kw)

        from unittest.mock import patch
        with patch("gateway.configtool.model.yaml.dump", side_effect=spying_dump):
            cfg.save()
        self.assertEqual(modes, [0o600])

    def test_saving_through_a_symlink_writes_the_target_and_keeps_the_link(self):
        # Docker mode 1 symlinks config.yaml to a bind mount; a repointed link
        # is followed on reload. Replacing the LINK with a file would leave the
        # real config untouched and every later edit through the link lost.
        real_dir = Path(self.tmp) / "real"
        real_dir.mkdir()
        real = real_dir / "config.yaml"
        real.write_text(textwrap.dedent(self._valid_cfg_text()))
        link = Path(self.tmp) / "config.yaml"
        link.symlink_to(real)
        cfg = EditableConfig.load(link)
        cfg.document["agents"]["default"]["timeout"] = 9
        cfg.mark_dirty()
        cfg.save()
        self.assertTrue(link.is_symlink(), "the link is still a link")
        self.assertEqual(yaml.safe_load(real.read_text())["agents"]["default"]["timeout"], 9)
        self.assertTrue((real_dir / ".config-backups").is_dir(), "backup beside the target")
        self.assertFalse((Path(self.tmp) / ".config-backups").exists())
        self.assertEqual(real.stat().st_mode & 0o777, 0o600)

    def test_a_stale_temp_file_from_an_interrupted_save_does_not_block_the_next(self):
        path = self._write(self._valid_cfg_text())
        path.with_name("config.yaml.tmp").write_text("left behind")
        cfg = EditableConfig.load(path)
        cfg.save()
        self.assertFalse(path.with_name("config.yaml.tmp").exists())

    def test_save_chmods_config_yaml_to_owner_only(self):
        path = self._write(self._valid_cfg_text())
        cfg = EditableConfig.load(path)
        cfg.save()
        self.assertEqual(oct(path.stat().st_mode)[-3:], "600")

    def test_save_clears_the_dirty_flag(self):
        path = self._write(self._valid_cfg_text())
        cfg = EditableConfig.load(path)
        cfg.mark_dirty()
        self.assertTrue(cfg.dirty)
        cfg.save()
        self.assertFalse(cfg.dirty)

    def test_save_leaves_no_leftover_temp_file(self):
        path = self._write(self._valid_cfg_text())
        cfg = EditableConfig.load(path)
        cfg.save()
        self.assertFalse((path.parent / "config.yaml.tmp").exists())

    def test_save_refuses_an_invalid_document_and_leaves_disk_untouched(self):
        path = self._write(self._valid_cfg_text())
        original_text = path.read_text()
        cfg = EditableConfig.load(path)
        del cfg.document["agents"]["default"]["working_directory"]
        cfg.mark_dirty()

        with self.assertRaises(ValueError):
            cfg.save()

        # The real file must be byte-identical to before the failed save —
        # no partial/temp/backup artifacts left behind either.
        self.assertEqual(path.read_text(), original_text)
        self.assertFalse((path.parent / "config.yaml.tmp").exists())
        self.assertFalse((path.parent / ".config-backups").exists())
        # dirty must stay set: the in-memory edit was never actually saved.
        self.assertTrue(cfg.dirty)

    def test_save_raises_file_not_found_if_the_config_file_is_gone(self):
        path = self._write(self._valid_cfg_text())
        cfg = EditableConfig.load(path)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            cfg.save()


class TestEditableConfigScopedSaveGate(_EditableConfigTestBase):
    """User-reported: the previous all-or-nothing gate meant a config with
    TWO independently-broken connectors could never be fixed (or even
    deleted) through the TUI — saving a fix to connector1 was rejected
    because connector2 was still broken, and vice versa. save() now only
    blocks on a genuinely NEW problem (gateway/config.py's collect_config(),
    which — unlike a single caught GatewayConfig.from_file() exception —
    surfaces every independently-broken connector/agent/watcher, not just
    whichever one from_file() would have hit first)."""

    def _two_broken_connectors_text(self) -> str:
        return f"""\
            connectors:
              - name: conn1
                type: rocketchat
                server: {{url: "http://localhost:3000", username: "", password: ""}}
              - name: conn2
                type: rocketchat
                server: {{url: "http://localhost:3000", username: "", password: ""}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: w-conn1
                connector: conn1
                agent: default
                rooms:
                  include: [general]
              - name: w-conn2
                connector: conn2
                agent: default
                rooms:
                  include: [dev]
        """

    def test_fixing_one_connector_saves_despite_the_others_pre_existing_error(self):
        path = self._write(self._two_broken_connectors_text())
        cfg = EditableConfig.load(path)
        conn1 = next(c for c in cfg.document["connectors"] if c["name"] == "conn1")
        conn1["server"]["username"] = "bot1"
        conn1["server"]["password"] = "pw1"
        cfg.mark_dirty()

        cfg.save()  # must not raise

        raw = yaml.safe_load(path.read_text())
        fixed = next(c for c in raw["connectors"] if c["name"] == "conn1")
        still_broken = next(c for c in raw["connectors"] if c["name"] == "conn2")
        self.assertEqual(fixed["server"]["username"], "bot1")
        # conn2's pre-existing, untouched problem survives exactly as it was.
        self.assertEqual(still_broken["server"]["username"], "")
        self.assertEqual(still_broken["server"]["password"], "")

    def test_deleting_the_fixed_connector_succeeds_despite_the_others_error(self):
        path = self._write(self._two_broken_connectors_text())
        cfg = EditableConfig.load(path)
        cfg.document["connectors"] = [
            c for c in cfg.document["connectors"] if c["name"] != "conn1"
        ]
        cfg.document["watcher_rules"] = [
            w for w in cfg.document["watcher_rules"] if w["connector"] != "conn1"
        ]
        cfg.mark_dirty()

        cfg.save()  # must not raise

        raw = yaml.safe_load(path.read_text())
        names = {c["name"] for c in raw["connectors"]}
        self.assertEqual(names, {"conn2"})

    def test_a_genuinely_new_error_is_still_blocked_even_on_an_already_broken_config(self):
        """The scoped gate narrows what's IGNORED — it must not become a
        blanket pass. Introducing a brand-new problem (here: breaking the
        agent) while conn2 is already broken must still be refused."""
        path = self._write(self._two_broken_connectors_text())
        cfg = EditableConfig.load(path)
        del cfg.document["agents"]["default"]["working_directory"]
        cfg.mark_dirty()

        with self.assertRaises(ValueError) as ctx:
            cfg.save()
        self.assertIn("working_directory is required", str(ctx.exception))

        # Disk must be untouched — same guarantee the existing
        # "refuses an invalid document" test already pins.
        raw = yaml.safe_load(path.read_text())
        self.assertIn("working_directory", raw["agents"]["default"])

    def test_fixing_an_unrelated_agent_does_not_reveal_a_hidden_connector_error(self):
        """PR review finding, chained from a bug in collect_config() itself:
        an invalid `default_agent` used to make collect_config() discard
        EVERY already-parsed connector/agent — so `before`'s findings never
        included a completely unrelated connector's own, already-real empty-
        credentials problem. Fixing the (unrelated) default_agent problem
        made that connector problem appear for the FIRST time in `after`,
        misclassifying an old, silent problem as "new" and blocking a
        perfectly good, unrelated fix."""
        path = self._write(f"""\
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
              - name: w1
                connector: rc1
                agent: other_agent
                rooms:
                  include: [general]
        """)
        cfg = EditableConfig.load(path)
        cfg.document["agents"]["broken_default"]["working_directory"] = str(self.agent_dir)
        cfg.mark_dirty()

        cfg.save()  # must not raise

        raw = yaml.safe_load(path.read_text())
        # rc1's pre-existing (untouched, unrelated) empty credentials survive
        # exactly as they were — this save neither fixed nor was blocked by them.
        self.assertEqual(raw["connectors"][0]["server"]["username"], "")

    def test_save_does_not_crash_when_a_broken_entry_has_a_non_string_name(self):
        """PR review finding: a watcher's own `name:` might itself be
        malformed (e.g. a list) on an entry that ALSO fails for an unrelated
        reason (here: no `room`/`rooms`) — gateway/config.py's
        collect_config() used to pass that malformed value straight through
        as ConfigIssue.entity_name, which _new_errors_introduced_by_this_save()
        above then puts into a set of tuples, raising an uncaught
        `TypeError: unhashable type: 'list'` instead of the clean ValueError
        every caller expects."""
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: "http://localhost:3000", username: bot, password: pw}}
              - name: rc2
                type: rocketchat
                server: {{url: "http://localhost:3001", username: bot2, password: pw2}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: [a, b]
                agent: default
                connector: rc
        """)
        cfg = EditableConfig.load(path)
        # An entirely unrelated, otherwise-harmless edit — must not trip
        # over the pre-existing malformed watcher name while diffing
        # before/after findings.
        cfg.document["connectors"][1]["server"]["username"] = "bot2-renamed"
        cfg.mark_dirty()

        cfg.save()  # must not raise TypeError

        raw = yaml.safe_load(path.read_text())
        assert raw["connectors"][1]["server"]["username"] == "bot2-renamed"


class TestStatusIndexNonStringEntityName(_EditableConfigTestBase):
    """PR review finding: StatusIndex.__init__() groups findings by
    `(entity_kind, entity_name)` — a dict key that must be hashable.
    gateway/config_validate.py's _lint_config() used to attach a
    truthy-but-non-string 'name'/'inherits' value (e.g. a YAML list)
    straight onto a Finding.entity_name unchecked, which crashed HERE with
    an uncaught TypeError: unhashable type the first time the config TUI
    tried to build a StatusIndex from --lint findings — not at
    validate_config() itself (dataclass construction doesn't type-check),
    which is why this needs its own test beyond test_config_validate.py's
    Finding-shape assertions."""

    def test_a_non_string_connector_name_does_not_crash_status_index(self):
        path = self._write(f"""\
            connectors:
              - name: [a, b]
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
                reply_in_thread: false
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
        """)
        result = validate_config(str(path), lint=True)
        StatusIndex(result.findings)  # must not raise TypeError

    def test_a_non_string_watcher_name_does_not_crash_status_index(self):
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: [a, b]
                agent: default
                connector: rc
                rooms:
                  include: [general]
                session_id: null
        """)
        result = validate_config(str(path), lint=True)
        StatusIndex(result.findings)  # must not raise TypeError




class TestMoveWatcherRule(_EditableConfigTestBase):
    """move_watcher_rule() — rule order is load-bearing (first match wins,
    design §2.1), so the Rules tab must be able to express it without a trip
    to $EDITOR. A move is a plain neighbour swap on the raw document list;
    persisting it stays the caller's job, same as every other mutation."""

    def _cfg(self) -> EditableConfig:
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: first
                connector: rc
                agent: default
                rooms:
                  include: [general]
              - name: second
                connector: rc
                agent: default
                rooms:
                  include: [dev]
              - name: third
                connector: rc
                agent: default
                rooms:
                  include: [ops]
        """)
        return EditableConfig.load(path)

    def _names(self, cfg: EditableConfig) -> list[str]:
        return [w["name"] for w in cfg.document["watcher_rules"]]

    def test_move_down_swaps_with_the_next_rule_and_returns_the_new_index(self):
        cfg = self._cfg()
        self.assertEqual(cfg.move_watcher_rule(0, +1), 1)
        self.assertEqual(self._names(cfg), ["second", "first", "third"])
        self.assertTrue(cfg.dirty)

    def test_move_up_swaps_with_the_previous_rule(self):
        cfg = self._cfg()
        self.assertEqual(cfg.move_watcher_rule(2, -1), 1)
        self.assertEqual(self._names(cfg), ["first", "third", "second"])

    def test_moving_past_either_edge_is_a_no_op(self):
        cfg = self._cfg()
        self.assertIsNone(cfg.move_watcher_rule(0, -1))
        self.assertIsNone(cfg.move_watcher_rule(2, +1))
        self.assertEqual(self._names(cfg), ["first", "second", "third"])
        self.assertFalse(cfg.dirty)

    def test_a_stale_index_is_a_no_op_not_an_error(self):
        """The table can be painted before an external shrink — a move
        against an index that no longer exists must find nothing, matching
        how row lookups elsewhere degrade."""
        cfg = self._cfg()
        self.assertIsNone(cfg.move_watcher_rule(7, -1))
        self.assertFalse(cfg.dirty)

    def test_the_moved_order_round_trips_through_save(self):
        cfg = self._cfg()
        cfg.move_watcher_rule(0, +1)
        cfg.save()
        reloaded = EditableConfig.load(cfg.path)
        self.assertEqual(self._names(reloaded), ["second", "first", "third"])


class TestStatusIndexRuleBridge(_EditableConfigTestBase):
    """status_for_rule() — rule rows are index-keyed, but a rule's findings
    are filed under THREE entity_name spellings depending on the producer
    (its own name; collect_config's "(index i)"; _lint_config's
    "watchers[i]"). The previous Watchers tab's key mismatch is exactly how
    broken rows displayed OK — a rule row must surface findings whichever
    spelling they arrived under."""

    def _status_for(self, watchers_yaml: str, index: int) -> str:
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
{watchers_yaml}
        """)
        cfg = EditableConfig.load(path)
        result = validate_config(str(path), lint=True)
        status = StatusIndex(result.findings)
        raw = cfg.document["watcher_rules"][index]
        entry = raw if isinstance(raw, dict) else {}
        return status.status_for_rule(index, entry)

    def test_a_shadowing_warning_filed_under_the_rule_name_reaches_its_row(self):
        """The keystone case: shadowing warnings are attributed to
        rule.name (config_validate), and the row is index-keyed — a rule
        with a shadowing warning must NOT display OK."""
        status = self._status_for(
            "              - name: broad\n"
            "                agent: default\n"
            "                connector: rc\n"
            "                rooms:\n"
            "                  include: [\"*\"]\n"
            "              - name: shadowed\n"
            "                agent: default\n"
            "                connector: rc\n"
            "                rooms:\n"
            "                  include: [general]\n",
            1,
        )
        self.assertEqual(status, "warning")

    def test_an_unnamed_broken_entry_is_found_under_its_index_spelling(self):
        """collect_config attributes a nameless entry's error to
        "(index i)" — the bridge must pick it up for the same row."""
        status = self._status_for(
            "              - rooms:\n"
            "                  include: [general]\n",
            0,
        )
        self.assertEqual(status, "error")

    def test_a_named_broken_entry_is_found_under_its_name(self):
        status = self._status_for(
            "              - name: w1\n"
            "                rooms:\n"
            "                  include: []\n",
            0,
        )
        self.assertEqual(status, "error")

    def test_a_clean_rule_is_ok(self):
        status = self._status_for(
            "              - name: w1\n"
            "                agent: default\n"
            "                connector: rc\n"
            "                rooms:\n"
            "                  include: [general]\n",
            0,
        )
        self.assertEqual(status, "ok")


class TestStatusIndexStrippedNameSpelling(_EditableConfigTestBase):
    def test_a_padded_rule_name_still_surfaces_parser_attributed_findings(self):
        """Codex review of #129 (round 3): the parser canonicalizes (strips)
        a rule's name before attributing shadowing warnings to it — a row
        whose raw spelling is padded must still surface them."""
        path = self._write(f"""\
            connectors:
              - name: rc
                type: rocketchat
                server: {{url: http://localhost:3000, username: bot, password: pw}}
            agents:
              default:
                type: claude
                working_directory: {self.agent_dir}
            watcher_rules:
              - name: broad
                connector: rc
                agent: default
                rooms:
                  include: ["*"]
              - name: " shadowed "
                connector: rc
                agent: default
                rooms:
                  include: [general]
        """)
        cfg = EditableConfig.load(path)
        result = validate_config(str(path), lint=True)
        status = StatusIndex(result.findings)
        self.assertEqual(status.status_for_rule(1, cfg.document["watcher_rules"][1]), "warning")
