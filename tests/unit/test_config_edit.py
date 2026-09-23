"""`gateway/config_edit.py`: the patch semantics and the plan-then-write path
behind `coop config add/remove/patch` (coop-keeper design §3.10).

The merge rules are pure and tested on plain dicts; `edit_document` is tested
against a real file in a temp dir, because what it promises is about the file:
a dry run leaves it alone, a write goes through the TUI's atomic save with a
backup, a stale digest is refused, and nothing it reports carries a value read
from a credential file.

Run with:
    uv run python -m pytest tests/unit/test_config_edit.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from gateway.config_edit import (
    REDACTED,
    DocumentError,
    PatchError,
    apply_fragment,
    edit_document,
    fragment_from_paths,
    merge_named_list,
    merge_patch,
    parse_entry,
    parse_set,
    prepare_fragment,
    read_document,
    read_secret_file,
)
from tests.helpers import gateway_config_text, write_secret_file


class TestMergePatch(unittest.TestCase):

    def test_mappings_merge_null_deletes_and_anything_else_replaces(self):
        target = {"a": {"x": 1, "y": 2}, "b": [1, 2], "c": "keep"}
        out = merge_patch(target, {"a": {"y": None, "z": 3}, "b": [9], "d": {"n": 1}})
        self.assertEqual(out, {"a": {"x": 1, "z": 3}, "b": [9], "c": "keep", "d": {"n": 1}})
        self.assertEqual(target["a"], {"x": 1, "y": 2}, "the input is not modified")

    def test_a_mapping_patch_over_a_scalar_replaces_it(self):
        self.assertEqual(merge_patch({"a": 5}, {"a": {"b": 1}}), {"a": {"b": 1}})

    def test_deleting_an_absent_key_is_a_no_op(self):
        self.assertEqual(merge_patch({"a": 1}, {"zzz": None}), {"a": 1})


class TestMergeNamedList(unittest.TestCase):

    def setUp(self):
        self.existing = [{"name": "rc", "type": "rocketchat", "server": {"url": "u", "password": "p"}},
                         {"name": "mm", "type": "mattermost"}]

    def test_a_new_name_is_appended_and_an_existing_one_merged_in_place(self):
        out = merge_named_list(self.existing, [
            {"name": "rc", "server": {"url": "new"}, "description": "d"},
            {"name": "voice", "type": "voice"},
        ], "connectors")
        self.assertEqual([e["name"] for e in out], ["rc", "mm", "voice"])
        self.assertEqual(out[0]["server"], {"url": "new", "password": "p"}, "merged, not replaced")
        self.assertEqual(out[0]["description"], "d")
        self.assertEqual(self.existing[0]["server"]["url"], "u", "the input is not modified")

    def test_op_add_refuses_an_existing_name_and_is_stripped_from_the_entry(self):
        out = merge_named_list(self.existing, [{"name": "x", "op": "add", "type": "voice"}], "connectors")
        self.assertEqual(out[-1], {"name": "x", "type": "voice"})
        with self.assertRaises(PatchError) as ctx:
            merge_named_list(self.existing, [{"name": "rc", "op": "add"}], "connectors")
        self.assertIn("'rc' already exists", str(ctx.exception))

    def test_op_remove_deletes_that_entry_and_only_it(self):
        out = merge_named_list(self.existing, [{"name": "rc", "op": "remove"}], "connectors")
        self.assertEqual(out, [{"name": "mm", "type": "mattermost"}])

    def test_op_remove_refuses_an_absent_name_or_extra_fields(self):
        with self.assertRaises(PatchError) as ctx:
            merge_named_list(self.existing, [{"name": "nope", "op": "remove"}], "watcher_rules")
        self.assertIn("no entry named 'nope'", str(ctx.exception))
        with self.assertRaises(PatchError) as ctx:
            merge_named_list(self.existing, [{"name": "rc", "op": "remove", "type": "x"}], "connectors")
        self.assertIn("other fields", str(ctx.exception))

    def test_an_unknown_op_and_a_nameless_entry_are_refused(self):
        with self.assertRaises(PatchError) as ctx:
            merge_named_list(self.existing, [{"name": "rc", "op": "replace"}], "connectors")
        self.assertIn("unknown op 'replace'", str(ctx.exception))
        with self.assertRaises(PatchError) as ctx:
            merge_named_list(self.existing, [{"type": "voice"}], "connectors")
        self.assertIn("needs a string 'name'", str(ctx.exception))

    def test_a_name_is_matched_never_parsed(self):
        # `@`, `:`-free but odd names are looked up literally; nothing splits them.
        existing = [{"name": "bob@mm-labpig", "x": 1}]
        out = merge_named_list(existing, [{"name": "bob@mm-labpig", "y": 2}], "connectors")
        self.assertEqual(out, [{"name": "bob@mm-labpig", "x": 1, "y": 2}])

    def test_a_file_whose_block_is_not_a_list_is_refused(self):
        with self.assertRaises(PatchError):
            merge_named_list({"a": 1}, [{"name": "x"}], "connectors")
        self.assertEqual(merge_named_list(None, [{"name": "x"}], "connectors"), [{"name": "x"}])


class TestApplyFragment(unittest.TestCase):

    def test_named_lists_merge_by_name_while_mappings_follow_merge_patch(self):
        doc = {"connectors": [{"name": "rc", "type": "rocketchat"}],
               "agents": {"a": {"type": "claude"}, "b": {"type": "claude"}},
               "watcher_rules": [{"name": "w1", "connector": "rc", "agent": "a"}]}
        out = apply_fragment(doc, {
            "connectors": [{"name": "mm", "type": "mattermost"}],
            "agents": {"b": None, "c": {"type": "opencode"}},
            "watcher_rules": [{"name": "w1", "op": "remove"}],
        })
        self.assertEqual([c["name"] for c in out["connectors"]], ["rc", "mm"])
        self.assertEqual(sorted(out["agents"]), ["a", "c"])
        self.assertEqual(out["watcher_rules"], [])

    def test_the_sentinel_is_refused_at_the_one_place_every_write_passes(self):
        # `add` builds its fragment from arguments and never goes through
        # prepare_fragment; the guard here is what catches a `***` password file.
        with self.assertRaises(PatchError) as ctx:
            apply_fragment({}, {"connectors": [{"name": "rc", "server": {"password": REDACTED}}]})
        self.assertIn("connectors[0].server.password", str(ctx.exception))

    def test_a_null_block_removes_it_and_a_non_mapping_fragment_is_refused(self):
        out = apply_fragment({"connectors": [{"name": "rc"}], "agents": {}}, {"connectors": None})
        self.assertEqual(out, {"agents": {}})
        with self.assertRaises(PatchError):
            apply_fragment({}, ["not", "a", "mapping"])


class TestPathsAndEntry(unittest.TestCase):

    def test_set_values_are_yaml_so_500_is_an_integer_and_quoted_500_a_string(self):
        self.assertEqual(parse_set("timeout=500"), ("timeout", 500))
        self.assertEqual(parse_set('timeout="500"'), ("timeout", "500"))
        self.assertEqual(parse_set("rooms.include=[a, b]"), ("rooms.include", ["a", "b"]))
        self.assertEqual(parse_set("server.url=http://x:1/y=z"), ("server.url", "http://x:1/y=z"))
        with self.assertRaises(PatchError):
            parse_set("no-equals-sign")

    def test_an_unparseable_value_is_refused_without_being_echoed(self):
        with self.assertRaises(PatchError) as ctx:
            parse_set("server.password=[unclosed")
        self.assertNotIn("unclosed", str(ctx.exception))
        with self.assertRaises(PatchError) as ctx:
            parse_set("hunter2")  # no '=': which half is the value is unknowable
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_entry_scopes_every_set_and_unset_to_one_list_entry(self):
        fragment = fragment_from_paths(
            [("server.url", "u"), ("timeout", 5)], ["server.token"], parse_entry("connector:bob@mm"))
        self.assertEqual(fragment, {"connectors": [
            {"name": "bob@mm", "server": {"url": "u", "token": None}, "timeout": 5}]})
        self.assertEqual(parse_entry("rule:w1"), ("watcher_rules", "w1"))
        self.assertEqual(parse_entry("rule:a:b"), ("watcher_rules", "a:b"), "the name is not parsed")
        self.assertIsNone(parse_entry(None))
        for bad in ("agent:x", "connector:", "connector", ":x"):
            with self.assertRaises(PatchError, msg=bad):
                parse_entry(bad)

    def test_entry_must_name_an_existing_entry_when_the_document_is_known(self):
        doc = {"connectors": [{"name": "rc"}], "watcher_rules": []}
        fragment = fragment_from_paths([("timeout", 5)], [], parse_entry("connector:rc"), doc)
        self.assertEqual(fragment, {"connectors": [{"name": "rc", "timeout": 5}]})
        with self.assertRaises(PatchError) as ctx:
            fragment_from_paths([("timeout", 5)], [], parse_entry("rule:nope"), doc)
        self.assertEqual(str(ctx.exception), "--entry: no rule named 'nope'")

    def test_without_entry_the_paths_are_top_level_and_a_later_set_wins(self):
        fragment = fragment_from_paths([("a.b", 1), ("a.b", 2), ("a.c", 3)], ["d"], None)
        self.assertEqual(fragment, {"a": {"b": 2, "c": 3}, "d": None})

    def test_set_to_null_is_an_explicit_deletion_not_a_silent_no_op(self):
        fragment = fragment_from_paths([parse_set("a.b=null"), parse_set("a.c=~")], [], None)
        self.assertEqual(fragment, {"a": {"b": None, "c": None}})
        self.assertEqual(apply_fragment({"a": {"b": 1, "c": 2, "d": 3}}, fragment), {"a": {"d": 3}})
        with self.assertRaises(PatchError):
            fragment_from_paths([("a..b", 1)], [], None)


class TestCredentialValues(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _file(self, content: str, name="pw") -> str:
        return write_secret_file(self.tmp, content, name)

    def test_one_trailing_newline_is_stripped_and_nothing_else(self):
        self.assertEqual(read_secret_file(self._file("s3cret\n")), "s3cret")
        self.assertEqual(read_secret_file(self._file("s3cret\r\n")), "s3cret")
        self.assertEqual(read_secret_file(self._file("s3cret\n\n")), "s3cret\n")
        self.assertEqual(read_secret_file(self._file(" padded ")), " padded ")

    def test_a_file_that_is_not_text_is_refused_by_path_not_content(self):
        path = self.tmp / "binary"
        path.write_bytes(b"\xff\xfe\x00secret")
        with self.assertRaises(PatchError) as ctx:
            read_secret_file(str(path))
        self.assertIn("is not text", str(ctx.exception))
        self.assertNotIn("secret", str(ctx.exception))

    def test_an_empty_or_missing_file_is_refused_by_path_not_content(self):
        with self.assertRaises(PatchError) as ctx:
            read_secret_file(self._file("\n"))
        self.assertIn("is empty", str(ctx.exception))
        with self.assertRaises(PatchError) as ctx:
            read_secret_file(str(self.tmp / "absent"))
        self.assertIn("absent", str(ctx.exception))

    def test_from_file_mappings_are_replaced_by_the_files_content(self):
        pw = self._file("hunter2\n")
        out = prepare_fragment({"connectors": [{"name": "rc", "server": {
            "password": {"from_file": pw}, "url": "u"}}]})
        self.assertEqual(out["connectors"][0]["server"], {"password": "hunter2", "url": "u"})

    def test_from_file_beside_other_keys_is_refused(self):
        with self.assertRaises(PatchError) as ctx:
            prepare_fragment({"server": {"password": {"from_file": "x", "extra": 1}}})
        self.assertIn("server.password", str(ctx.exception))

    def test_the_masked_sentinel_is_refused_wherever_it_appears(self):
        with self.assertRaises(PatchError) as ctx:
            prepare_fragment({"connectors": [{"name": "rc", "server": {"password": REDACTED}}]})
        self.assertIn("connectors[0].server.password", str(ctx.exception))
        # ...including when it arrives through a credential file.
        with self.assertRaises(PatchError):
            prepare_fragment({"a": {"from_file": self._file(REDACTED + "\n")}})


class TestFragmentFileAndJsonKeys(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_a_fragment_file_is_read_with_yaml_encoding_detection(self):
        from gateway.config_edit import read_fragment_file
        path = self.tmp / "f.yaml"
        path.write_bytes("agents: {b\u00f6b: {timeout: 1}}\n".encode("utf-16"))
        self.assertEqual(read_fragment_file(str(path)), {"agents": {"b\u00f6b": {"timeout": 1}}})
        path.write_bytes(b"\xff\xfe\x00\x00 not: [valid")
        with self.assertRaises(PatchError) as ctx:
            read_fragment_file(str(path))
        self.assertIn("not valid YAML", str(ctx.exception))
        (self.tmp / "empty.yaml").write_text("")
        self.assertEqual(read_fragment_file(str(self.tmp / "empty.yaml")), {})
        with self.assertRaises(PatchError):
            read_fragment_file(str(self.tmp / "absent.yaml"))

    def test_non_string_keys_are_rendered_for_json(self):
        import datetime

        from gateway.config_edit import json_safe_keys
        doc = {datetime.date(2026, 1, 1): "v", 1: [{2: "x"}], "s": {True: 1}}
        out = json_safe_keys(doc)
        self.assertEqual(out, {"2026-01-01": "v", "1": [{"2": "x"}], "s": {"True": 1}})
        json.dumps(out)


class TestTaggedScalarsNeverEcho(unittest.TestCase):
    """PyYAML's constructors leak bare ValueError/KeyError/AttributeError for a
    tagged scalar, with the VALUE in the message. Every entry point loads
    through `load_yaml`, so the value — here always a credential — never
    reaches an error. The tags enumerate the constructor families PyYAML has;
    a new one fails here, not in a review round."""

    TAGGED = ("!!int hunter2", "!!float hunter2", "!!bool hunter2", "!!timestamp hunter2",
              "!!binary hunter2!!", "!!python/object:os.system hunter2")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_the_file_the_set_value_and_the_fragment_file(self):
        from gateway.config_edit import read_fragment_file
        for tagged in self.TAGGED:
            with self.subTest(tagged=tagged):
                text = f"server:\n  password: {tagged}\n"
                (self.tmp / "c.yaml").write_text(text)
                with self.assertRaises(DocumentError) as ctx:
                    read_document(self.tmp / "c.yaml")
                self.assertNotIn("hunter2", str(ctx.exception))
                with self.assertRaises(PatchError) as ctx:
                    parse_set(f"server.password={tagged}")
                self.assertNotIn("hunter2", str(ctx.exception))
                (self.tmp / "f.yaml").write_text(text)
                with self.assertRaises(PatchError) as ctx:
                    read_fragment_file(str(self.tmp / "f.yaml"))
                self.assertNotIn("hunter2", str(ctx.exception))


class TestReadDocument(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_the_digest_is_over_the_bytes_and_an_empty_file_is_an_empty_document(self):
        import hashlib
        path = self.tmp / "config.yaml"
        path.write_text("# only a comment\n")
        document, digest = read_document(path)
        self.assertEqual(document, {})
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_a_yaml_error_names_the_place_but_never_the_offending_line(self):
        # str(YAMLError) quotes the source line — which here is a credential.
        (self.tmp / "bad.yaml").write_text("server:\n  password: hunter2: oops\n")
        with self.assertRaises(DocumentError) as ctx:
            read_document(self.tmp / "bad.yaml")
        self.assertIn("line 2", str(ctx.exception))
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_a_file_that_does_not_exist_yet_is_the_empty_deployment(self):
        import hashlib
        document, digest = read_document(self.tmp / "absent.yaml")
        self.assertEqual(document, {})
        self.assertEqual(digest, hashlib.sha256(b"").hexdigest())

    def test_invalid_and_non_mapping_files_are_document_errors(self):
        (self.tmp / "bad.yaml").write_text("a: [")
        with self.assertRaises(DocumentError) as ctx:
            read_document(self.tmp / "bad.yaml")
        self.assertIn("invalid YAML", str(ctx.exception))
        (self.tmp / "list.yaml").write_text("- a\n")
        with self.assertRaises(DocumentError) as ctx:
            read_document(self.tmp / "list.yaml")
        self.assertIn("mapping", str(ctx.exception))


class TestEditDocument(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.runtime = self.tmp / "runtime"
        self.runtime.mkdir()
        self.path = self.tmp / "config.yaml"
        # A rocketchat connector, because the outcome's masking is what these
        # tests are about; `gateway_config_text` builds script connectors.
        self.path.write_text(gateway_config_text(working_directory=self.tmp).replace(
            "- name: script\n  type: script",
            "- name: script\n  type: rocketchat\n  server: {url: http://rc:3000, "
            "username: bot, password: hunter2}"))
        self.original = self.path.read_bytes()
        self._state = patch("gateway.core.state.RUNTIME_DIR", self.runtime)
        self._state.start()
        self.addCleanup(self._state.stop)

    def _edit(self, fragment, **kw):
        return edit_document(str(self.path), lambda doc: apply_fragment(doc, fragment), **kw)

    def test_a_dry_run_reports_the_masked_result_and_leaves_the_file_alone(self):
        outcome = self._edit({"agents": {"default": {"timeout": 7}}}, dry_run=True)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.config["agents"]["default"]["timeout"], 7)
        self.assertEqual(outcome.config["connectors"][0]["server"]["password"], REDACTED)
        self.assertEqual(outcome.file_digest, read_document(self.path)[1], "the digest of the file read")
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertNotIn("hunter2", json.dumps(outcome.to_dict()))

    def test_a_write_goes_through_the_atomic_save_with_a_backup_and_reports_the_new_digest(self):
        before = read_document(self.path)[1]
        outcome = self._edit({"agents": {"default": {"timeout": 7}}}, dry_run=False, if_digest=before)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertFalse(outcome.dry_run)
        on_disk = yaml.safe_load(self.path.read_text())
        self.assertEqual(on_disk["agents"]["default"]["timeout"], 7)
        self.assertEqual(on_disk["connectors"][0]["server"]["password"], "hunter2",
                         "the real value is written; only the REPORT is masked")
        self.assertEqual(outcome.file_digest, read_document(self.path)[1])
        self.assertNotEqual(outcome.file_digest, before)
        backups = list((self.tmp / ".config-backups").iterdir())
        self.assertEqual(len(backups), 1, backups)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("hunter2", json.dumps(outcome.to_dict()))

    def test_a_stale_digest_is_refused_and_nothing_is_written(self):
        outcome = self._edit({"agents": {"default": {"timeout": 7}}}, dry_run=False, if_digest="0" * 64)
        self.assertFalse(outcome.ok)
        self.assertIn("has changed since", outcome.error)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse((self.tmp / ".config-backups").exists())

    def test_a_change_during_the_run_is_refused_before_the_write(self):
        def mutate(doc):
            self.path.write_text(self.path.read_text() + "\n# edited meanwhile\n")
            return apply_fragment(doc, {"agents": {"default": {"timeout": 7}}})
        outcome = edit_document(str(self.path), mutate, dry_run=False)
        self.assertFalse(outcome.ok)
        self.assertIn("changed while this edit ran", outcome.error)
        self.assertNotIn("timeout", self.path.read_text())

    def test_an_invalid_result_is_refused_with_validate_style_findings(self):
        outcome = self._edit({"watcher_rules": [{"name": "w1", "agent": "nobody"}]}, dry_run=False)
        self.assertFalse(outcome.ok)
        self.assertIn("does not validate", outcome.error)
        self.assertEqual(outcome.findings[0]["level"], "error")
        self.assertIn("nobody", outcome.findings[0]["message"])
        self.assertEqual(set(outcome.findings[0]), {"level", "entity_kind", "entity_name", "field", "message"})
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_a_validator_crash_on_a_wrongly_typed_value_is_a_refusal_not_a_traceback(self):
        # `server.url: []` reaches a connector parser's `.rstrip` — the
        # validator raises rather than reporting, and that is still ok:false.
        outcome = self._edit({"connectors": [{"name": "script", "server": {"url": []}}]}, dry_run=True)
        self.assertFalse(outcome.ok)
        self.assertIn("could not check the result", outcome.error)
        self.assertIn("AttributeError", outcome.error)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_a_validator_crash_on_a_tagged_credential_in_the_file_does_not_echo_it(self):
        # The file itself carries `password: !!int hunter2`: read_document
        # refuses it before validation, and the refusal names no value.
        self.path.write_text(self.path.read_text().replace("password: hunter2", "password: !!int hunter2"))
        outcome = self._edit({"agents": {"default": {"timeout": 1}}}, dry_run=True)
        self.assertFalse(outcome.ok)
        self.assertNotIn("hunter2", json.dumps(outcome.to_dict()))

    def test_a_patch_error_is_reported_not_raised(self):
        outcome = self._edit(["not a mapping"], dry_run=True)
        self.assertFalse(outcome.ok)
        self.assertIn("mapping", outcome.error)

    def test_the_first_write_creates_the_file_with_no_backup_and_mode_0600(self):
        # Bootstrap (§3.2, §7 test 1): the first plan's patch on a machine
        # with no config.yaml. Planned against "nothing there" — the digest of
        # no bytes — and written without a backup, since there is nothing to back up.
        import hashlib
        path = self.tmp / "fresh" / "config.yaml"
        fragment = {"agents": {"a": {"type": "claude", "working_directory": str(self.tmp)}}}
        planned = edit_document(str(path), lambda d: apply_fragment(d, fragment), dry_run=True)
        self.assertTrue(planned.ok, planned.error)
        self.assertEqual(planned.file_digest, hashlib.sha256(b"").hexdigest())
        self.assertFalse(path.exists(), "a dry run creates nothing")
        modes_seen: list[int] = []
        real_dump = yaml.dump

        def spying_dump(data, stream, **kw):
            modes_seen.append(os.fstat(stream.fileno()).st_mode & 0o777)
            return real_dump(data, stream, **kw)

        with patch("gateway.config_edit.yaml.dump", side_effect=spying_dump):
            outcome = edit_document(str(path), lambda d: apply_fragment(d, fragment),
                                    dry_run=False, if_digest=planned.file_digest)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(yaml.safe_load(path.read_text()), fragment)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(modes_seen, [0o600], "0600 from the first byte, not chmod'd after the dump")
        self.assertFalse((path.parent / ".config-backups").exists())
        self.assertEqual(outcome.file_digest, read_document(path)[1])
        # Once the file exists, the "nothing there" digest is stale.
        again = edit_document(str(path), lambda d: d, dry_run=False, if_digest=planned.file_digest)
        self.assertFalse(again.ok)
        self.assertIn("has changed since", again.error)

    def test_extra_keys_ride_along_in_the_report(self):
        outcome = self._edit({}, dry_run=True, extra=lambda merged: {"entry": {"name": "x"}})
        self.assertEqual(outcome.to_dict()["entry"], {"name": "x"})

    def test_the_dry_run_temp_file_does_not_survive(self):
        self._edit({"agents": {"default": {"timeout": 7}}}, dry_run=True)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["config.yaml", "runtime"])


if __name__ == "__main__":
    unittest.main()
