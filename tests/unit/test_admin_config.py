"""Unit tests for gateway.admin.config: AdminProfile validation and
load_profiles/get_profile file handling."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.admin.config import (
    AdminConfigError,
    AdminProfile,
    get_profile,
    load_profiles,
)


class TestAdminProfileValidation(unittest.TestCase):
    def test_valid_mattermost_profile_with_token(self):
        profile = AdminProfile(
            name="mm", type="mattermost", server_url="https://x", team="t", token="tok"
        )
        self.assertEqual(profile.team, "t")

    def test_valid_rocketchat_profile_with_username_password(self):
        profile = AdminProfile(
            name="rc", type="rocketchat", server_url="https://x",
            username="admin", password="pw",
        )
        self.assertEqual(profile.username, "admin")

    def test_unknown_type_rejected(self):
        with self.assertRaises(AdminConfigError):
            AdminProfile(name="x", type="discord", server_url="https://x", token="t")

    def test_missing_server_url_rejected(self):
        with self.assertRaises(AdminConfigError):
            AdminProfile(name="x", type="rocketchat", server_url="", token="t")

    def test_missing_credentials_rejected(self):
        with self.assertRaises(AdminConfigError):
            AdminProfile(name="x", type="rocketchat", server_url="https://x")

    def test_mattermost_without_team_rejected(self):
        with self.assertRaises(AdminConfigError):
            AdminProfile(name="mm", type="mattermost", server_url="https://x", token="t")

    def test_rocketchat_without_team_is_fine(self):
        # RC has no team concept — team is a mattermost-only requirement.
        profile = AdminProfile(
            name="rc", type="rocketchat", server_url="https://x",
            username="admin", password="pw",
        )
        self.assertIsNone(profile.team)


class TestLoadProfiles(unittest.TestCase):
    def test_missing_file_raises(self):
        with self.assertRaises(AdminConfigError):
            load_profiles("/nonexistent/admin-profiles.yaml")

    def test_config_path_is_a_directory_raises_admin_config_error(self):
        # open() on a directory raises IsADirectoryError, not
        # FileNotFoundError — so it must land in the generic OSError arm.
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(AdminConfigError):
                load_profiles(d)

    def test_unsearchable_parent_dir_raises_admin_config_error(self):
        # EACCES is not in pathlib's _IGNORED_ERRNOS, so the old
        # Path.exists() pre-check re-raised PermissionError before any
        # try/except could convert it.
        with tempfile.TemporaryDirectory() as d:
            locked = Path(d) / "locked"
            locked.mkdir()
            (locked / "cfg.yaml").write_text("profiles: {}\n")
            os.chmod(locked, 0o000)
            try:
                with self.assertRaises(AdminConfigError):
                    load_profiles(locked / "cfg.yaml")
            finally:
                os.chmod(locked, 0o755)

    def test_overlong_path_raises_admin_config_error(self):
        # ENAMETOOLONG likewise escaped the old exists() pre-check.
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(AdminConfigError):
                load_profiles(Path(d) / ("x" * 600))

    def test_non_utf8_config_file_raises_admin_config_error(self):
        # A latin-1 accented password: text-mode read() raised
        # UnicodeDecodeError (a ValueError, so neither the OSError nor the
        # YAMLError arm caught it). Binary mode turns it into a ReaderError.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_bytes(
                "profiles:\n  rc-lab:\n    type: rocketchat\n"
                "    server_url: https://rc\n    username: admin\n"
                "    password: café\n".encode("latin-1")
            )
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_utf16_config_file_loads(self):
        # Binary mode means PyYAML sniffs the BOM, so a UTF-16 file (what a
        # Windows editor may produce) now parses instead of failing at all.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_bytes(
                "profiles:\n  rc-lab:\n    type: rocketchat\n"
                "    server_url: https://rc\n    username: admin\n"
                "    password: pw\n".encode("utf-16")
            )
            self.assertEqual(set(load_profiles(path)), {"rc-lab"})

    def test_utf8_non_ascii_config_file_still_loads(self):
        # Guard the flip side of the binary-mode switch: a normal UTF-8 file
        # with non-ASCII values must keep working.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text(
                "profiles:\n  rc-lab:\n    type: rocketchat\n"
                "    server_url: https://rc\n    username: admin\n"
                "    password: paßwörd鐵錘\n",
                encoding="utf-8",
            )
            self.assertEqual(load_profiles(path)["rc-lab"].password, "paßwörd鐵錘")

    def test_bad_explicit_yaml_tag_raises_admin_config_error(self):
        # PyYAML's SafeConstructor leaks raw non-YAMLError exceptions for
        # these: ValueError, ValueError, AttributeError, KeyError.
        for body in (
            "profiles: !!int abc\n",
            "profiles: !!float abc\n",
            "profiles: !!timestamp nonsense\n",
            "profiles: !!bool maybe\n",
        ):
            with self.subTest(body=body):
                with tempfile.TemporaryDirectory() as d:
                    path = Path(d) / "cfg.yaml"
                    path.write_text(body)
                    with self.assertRaises(AdminConfigError):
                        load_profiles(path)

    def test_deeply_nested_yaml_raises_admin_config_error(self):
        # PyYAML's composer recurses, so deep nesting raises RecursionError
        # (a RuntimeError, not a YAMLError).
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text("profiles: " + "[" * 2000 + "]" * 2000)
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_nul_byte_in_path_raises_admin_config_error(self):
        # open() raises ValueError (not OSError) for an embedded NUL, so this
        # relies on the broad backstop arm.
        with self.assertRaises(AdminConfigError):
            load_profiles("/tmp/a\x00b.yaml")

    def test_permission_error_reading_config_raises_admin_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text(
                "profiles:\n  rc-lab:\n    type: rocketchat\n"
                "    server_url: https://rc\n    username: admin\n    password: pw\n"
            )
            with patch("builtins.open", side_effect=PermissionError("denied")):
                with self.assertRaises(AdminConfigError):
                    load_profiles(path)

    def test_missing_profiles_section_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text("not_profiles: {}\n")
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_non_mapping_profile_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text("profiles:\n  bad: not-a-mapping\n")
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_malformed_yaml_syntax_raises_admin_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            # Unclosed bracket — a genuine YAML syntax error, not just bad content.
            path.write_text("profiles: [unterminated\n")
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_list_root_raises_admin_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text("- just\n- a\n- list\n")
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_scalar_root_raises_admin_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text("just a string\n")
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_non_mapping_profiles_section_raises_admin_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text("profiles:\n  - a\n  - b\n")
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_numeric_profile_name_raises_admin_config_error(self):
        # Unquoted "123:" parses as an int key, not a string — the classic
        # YAML "Norway problem" (also bites true/false/yes/no).
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text(
                "profiles:\n  123:\n    type: rocketchat\n"
                "    server_url: https://rc\n    username: admin\n    password: pw\n"
            )
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_boolean_profile_name_raises_admin_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text(
                "profiles:\n  true:\n    type: rocketchat\n"
                "    server_url: https://rc\n    username: admin\n    password: pw\n"
            )
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_unknown_field_raises_admin_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text(
                "profiles:\n  rc-lab:\n    type: rocketchat\n"
                "    server_url: https://rc\n    username: admin\n    password: pw\n"
                "    usernme: typo\n"
            )
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_redundant_name_key_raises_admin_config_error(self):
        # 'name' is already passed positionally (name=name) — a profile
        # body that also sets 'name' collides with it (TypeError: multiple
        # values for argument 'name'), not just an "unknown field".
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text(
                "profiles:\n  rc-lab:\n    name: something-else\n    type: rocketchat\n"
                "    server_url: https://rc\n    username: admin\n    password: pw\n"
            )
            with self.assertRaises(AdminConfigError):
                load_profiles(path)

    def test_env_var_path_used_when_no_explicit_path_given(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text(
                "profiles:\n  rc-lab:\n    type: rocketchat\n"
                "    server_url: https://rc\n    username: admin\n    password: pw\n"
            )
            with patch.dict("os.environ", {"COOP_ADMIN_CONFIG": str(path)}):
                profiles = load_profiles()
            self.assertEqual(set(profiles), {"rc-lab"})

    def test_explicit_path_overrides_env_var(self):
        with tempfile.TemporaryDirectory() as d:
            real_path = Path(d) / "real.yaml"
            real_path.write_text(
                "profiles:\n  rc-lab:\n    type: rocketchat\n"
                "    server_url: https://rc\n    username: admin\n    password: pw\n"
            )
            with patch.dict("os.environ", {"COOP_ADMIN_CONFIG": "/nonexistent/other.yaml"}):
                profiles = load_profiles(real_path)
            self.assertEqual(set(profiles), {"rc-lab"})

    def test_loads_multiple_profiles(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text(
                "profiles:\n"
                "  mm-lab:\n"
                "    type: mattermost\n"
                "    server_url: https://mm\n"
                "    team: t\n"
                "    token: tok\n"
                "  rc-lab:\n"
                "    type: rocketchat\n"
                "    server_url: https://rc\n"
                "    username: admin\n"
                "    password: pw\n"
            )
            profiles = load_profiles(path)
            self.assertEqual(set(profiles), {"mm-lab", "rc-lab"})
            self.assertEqual(profiles["mm-lab"].type, "mattermost")
            self.assertEqual(profiles["rc-lab"].type, "rocketchat")


class TestDuplicateKeys(unittest.TestCase):
    """PyYAML silently keeps the LAST value for a repeated key. For a tool that
    drives destructive operations against whichever server the config names,
    that turns a copy/paste into a silent change of target or credential."""

    def _load(self, body: str):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cfg.yaml"
            path.write_text(body)
            return load_profiles(path)

    def test_duplicate_field_within_a_profile_is_rejected(self):
        with self.assertRaises(AdminConfigError) as ctx:
            self._load(
                "profiles:\n  mm-lab:\n    type: mattermost\n    team: t\n"
                "    server_url: https://prod.example.com\n    token: prod-token\n"
                "    server_url: https://lab.example.com\n    token: lab-token\n"
            )
        # Must name the offending key and where it is, or the operator has to
        # diff the file by eye.
        self.assertIn("server_url", str(ctx.exception))
        self.assertIn("duplicate key", str(ctx.exception))

    def test_duplicate_profile_name_is_rejected(self):
        with self.assertRaises(AdminConfigError) as ctx:
            self._load(
                "profiles:\n  p:\n    type: rocketchat\n    server_url: https://first\n"
                "    username: a\n    password: b\n"
                "  p:\n    type: rocketchat\n    server_url: https://second\n"
                "    username: c\n    password: d\n"
            )
        self.assertIn("duplicate key", str(ctx.exception))

    def test_duplicate_top_level_key_is_rejected(self):
        with self.assertRaises(AdminConfigError):
            self._load(
                "profiles:\n  a:\n    type: rocketchat\n    server_url: https://x\n"
                "    username: u\n    password: p\n"
                "profiles:\n  b:\n    type: rocketchat\n    server_url: https://y\n"
                "    username: u\n    password: p\n"
            )

    def test_yaml_merge_keys_still_work(self):
        """Regression: the first version of the strict loader reimplemented the
        construction loop and so dropped SafeConstructor's flatten_mapping()
        call, breaking `<<: *anchor` entirely with "could not determine a
        constructor for the tag 'tag:yaml.org,2002:merge'". That idiom is a
        natural fit for this file (several profiles sharing a type and
        credentials), so it has to keep working."""
        profiles = self._load(
            "_defaults: &rc\n  type: rocketchat\n  username: admin\n  password: pw\n"
            "profiles:\n"
            "  rc-a:\n    <<: *rc\n    server_url: https://a.example.com\n"
            "  rc-b:\n    <<: *rc\n    server_url: https://b.example.com\n"
        )
        self.assertEqual(set(profiles), {"rc-a", "rc-b"})
        self.assertEqual(profiles["rc-a"].username, "admin")
        self.assertEqual(profiles["rc-b"].server_url, "https://b.example.com")

    def test_explicit_key_overriding_a_merged_one_is_not_a_duplicate(self):
        # Overriding an inherited value is the entire point of a merge key, and
        # YAML specifies the explicit key wins — so duplicate detection must run
        # on the literally-written keys, BEFORE merge expansion.
        profiles = self._load(
            "_defaults: &rc\n  type: rocketchat\n  username: shared\n  password: pw\n"
            "profiles:\n  rc-a:\n    <<: *rc\n    username: overridden\n"
            "    server_url: https://a.example.com\n"
        )
        self.assertEqual(profiles["rc-a"].username, "overridden")

    def test_multiple_merge_sources_still_work(self):
        profiles = self._load(
            "_a: &a\n  type: rocketchat\n  username: admin\n"
            "_b: &b\n  password: pw\n"
            "profiles:\n  rc-a:\n    <<: [*a, *b]\n    server_url: https://a.example.com\n"
        )
        self.assertEqual(profiles["rc-a"].password, "pw")
        self.assertEqual(profiles["rc-a"].username, "admin")

    def test_duplicate_field_inside_a_merged_mapping_is_still_rejected(self):
        # The merge key must not become a blanket exemption from the check.
        with self.assertRaises(AdminConfigError) as ctx:
            self._load(
                "_defaults: &rc\n  type: rocketchat\n  username: admin\n  password: pw\n"
                "profiles:\n  rc-a:\n    <<: *rc\n"
                "    server_url: https://one\n    server_url: https://two\n"
            )
        self.assertIn("duplicate key", str(ctx.exception))

    def test_a_config_without_duplicates_still_loads(self):
        profiles = self._load(
            "profiles:\n  rc-lab:\n    type: rocketchat\n    server_url: https://rc\n"
            "    username: admin\n    password: pw\n"
            "  mm-lab:\n    type: mattermost\n    server_url: https://mm\n"
            "    team: t\n    token: tok\n"
        )
        self.assertEqual(set(profiles), {"rc-lab", "mm-lab"})

    def test_same_key_in_DIFFERENT_profiles_is_not_a_duplicate(self):
        # Duplicate detection is per-mapping, not per-document — every profile
        # legitimately has its own `type`/`server_url`.
        profiles = self._load(
            "profiles:\n  a:\n    type: rocketchat\n    server_url: https://x\n"
            "    username: u\n    password: p\n"
            "  b:\n    type: rocketchat\n    server_url: https://y\n"
            "    username: u\n    password: p\n"
        )
        self.assertEqual(set(profiles), {"a", "b"})

    def test_unhashable_complex_key_is_a_clean_error_not_a_traceback(self):
        # A YAML complex key (`? [a, b]`) is unhashable. There is deliberately
        # no special-case guard for it — this pins that the generic backstop
        # still turns it into a clean AdminConfigError rather than a traceback.
        with self.assertRaises(AdminConfigError) as ctx:
            self._load("profiles:\n  ? [a, b]\n  : value\n")
        self.assertIn("unhashable", str(ctx.exception))

    def test_strict_loader_still_refuses_unsafe_tags(self):
        # Subclassing SafeLoader must not have widened the tag set: an
        # arbitrary-object tag has to remain a YAML error, not get constructed.
        with self.assertRaises(AdminConfigError):
            self._load("profiles: !!python/object/apply:os.system ['echo pwned']\n")


class TestGetProfile(unittest.TestCase):
    def test_returns_matching_profile(self):
        profiles = {
            "mm": AdminProfile(name="mm", type="mattermost", server_url="https://x", team="t", token="tok")
        }
        self.assertIs(get_profile(profiles, "mm"), profiles["mm"])

    def test_unknown_name_raises_with_available_names(self):
        profiles = {
            "mm": AdminProfile(name="mm", type="mattermost", server_url="https://x", team="t", token="tok")
        }
        with self.assertRaises(AdminConfigError) as ctx:
            get_profile(profiles, "nope")
        self.assertIn("mm", str(ctx.exception))


class TestFieldTypeValidation(unittest.TestCase):
    """__post_init__'s type gate: every field must be a str (or None).

    YAML happily supplies non-strings for what looks like a string —
    unquoted `123`, `true`, `2026-08-11`, or a nested mapping from a bad
    indent. These must all surface as AdminConfigError, because cli._run()
    guards the whole load_profiles/get_profile/admin_factory block with
    `except AdminConfigError` ONLY.
    """

    def test_non_string_server_url_rejected(self):
        # The regression this gate exists for: an int server_url is truthy,
        # so it passed `if not self.server_url`, and then
        # RocketChatREST/MattermostREST.__init__'s `server_url.rstrip("/")`
        # raised AttributeError from inside admin_factory() — a raw
        # traceback for an ordinary unquoted-YAML-scalar mistake.
        with self.assertRaises(AdminConfigError):
            AdminProfile(name="x", type="rocketchat", server_url=123, token="t")  # type: ignore[arg-type]

    def test_non_string_team_rejected(self):
        # Covers the non-server_url fields, which share the same loop.
        with self.assertRaises(AdminConfigError):
            AdminProfile(
                name="x", type="mattermost", server_url="https://x",
                team={"a": "b"}, token="t",  # type: ignore[arg-type]
            )

    def test_rejection_message_does_not_echo_the_secret(self):
        # An unquoted all-digit password parses as an int and lands in this
        # gate — the message must name the type only. _run() prints it
        # straight to stderr, and RocketChatREST.__repr__ already sets the
        # `password=***` convention.
        with self.assertRaises(AdminConfigError) as ctx:
            AdminProfile(
                name="x", type="rocketchat", server_url="https://x",
                username="admin", password=12345678,  # type: ignore[arg-type]
            )
        self.assertNotIn("12345678", str(ctx.exception))

    def test_none_still_gets_the_specific_required_message(self):
        # The gate deliberately lets None through so the semantic checks
        # below it keep producing their more specific messages. Simplifying
        # the guard to a bare `isinstance(value, str)` would regress this.
        with self.assertRaises(AdminConfigError) as ctx:
            AdminProfile(name="x", type="rocketchat", server_url=None, token="t")  # type: ignore[arg-type]
        self.assertIn("server_url is required", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
