"""Sync tests for gateway/schema/config.schema.json.

The hand-written JSON Schema is not generated from the dataclasses/parser in
gateway/config.py, so it can silently drift from the format the parser
actually accepts. These tests are the drift tripwire: they validate the
canonical example config and the e2e fixture (both exercised elsewhere by
GatewayConfig.from_file-style tests) against the schema, and spot-check a
handful of known-invalid documents to confirm the schema is not accidentally
too permissive to catch anything at all.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
SCHEMA_PATH = REPO_ROOT / "gateway" / "schema" / "config.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    with open(SCHEMA_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def validator(schema: dict) -> jsonschema.Draft202012Validator:
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class TestSchemaIsValid:
    def test_schema_is_valid_draft_2020_12(self, schema):
        # Raises if the schema document itself is malformed.
        jsonschema.Draft202012Validator.check_schema(schema)


class TestExampleAndFixtureConfigsMatchSchema:
    """The two configs the gateway actually loads elsewhere in the test suite
    must validate cleanly — if this fails, either the schema drifted from a
    parser change, or the example/fixture drifted from the documented format."""

    def test_a_bare_watcher_rules_key_is_schema_valid(self, validator):
        """The loaders normalise a YAML null to an empty list; the schema used to
        admit only an array, so a config that started and passed `config
        validate` failed editor/CI validation (Codex, PR #140)."""
        doc = _load_yaml(REPO_ROOT / "config.example.yaml")
        doc["watcher_rules"] = None
        errors = list(validator.iter_errors(doc))
        assert not errors, "\n".join(str(e) for e in errors)

    def test_config_example_yaml_is_schema_valid(self, validator):
        doc = _load_yaml(REPO_ROOT / "config.example.yaml")
        errors = list(validator.iter_errors(doc))
        assert not errors, "\n".join(str(e) for e in errors)

    def test_e2e_fixture_config_is_schema_valid(self, validator):
        doc = _load_yaml(REPO_ROOT / "tests" / "e2e" / "acg-config" / "config.yaml")
        errors = list(validator.iter_errors(doc))
        assert not errors, "\n".join(str(e) for e in errors)

    def test_description_field_is_schema_valid_everywhere(self, validator):
        """'description:' is accepted on connectors, agents, watchers, and all
        three *_templates blocks — additionalProperties: false on agent/watcher
        means this must be explicit in the schema, not just implicitly allowed."""
        doc = _load_yaml(REPO_ROOT / "config.example.yaml")
        doc = copy.deepcopy(doc)
        doc["connectors"][0]["description"] = "Primary bot"
        doc["agents"]["my-agent"]["description"] = "The main agent"
        doc["watcher_rules"][0]["description"] = "General channel"
        doc["connector_templates"] = {"x": {"description": "Shared connector settings"}}
        doc["agent_templates"] = {"x": {"description": "Shared agent settings"}}
        doc["watcher_templates"] = {"x": {"description": "Shared watcher settings"}}
        errors = list(validator.iter_errors(doc))
        assert not errors, "\n".join(str(e) for e in errors)


class TestRuleShapedWatchersValidate:
    """The rule shape from docs/design/dynamic-watcher-design.md §2.1.

    Both shapes are accepted while the rule path is being built, so these run
    alongside the static-shape cases above rather than replacing them. `$defs`
    discriminates on the *type* of `rooms:` — object means rule, array means the
    old shorthand — so exactly one `oneOf` branch can ever match.
    """

    @pytest.fixture
    def base_doc(self) -> dict:
        return _load_yaml(REPO_ROOT / "config.example.yaml")

    def _with_watcher(self, base_doc: dict, entry: dict) -> dict:
        doc = copy.deepcopy(base_doc)
        doc["watcher_rules"] = [entry]
        return doc

    def _ok(self, validator, base_doc, entry):
        errors = list(validator.iter_errors(self._with_watcher(base_doc, entry)))
        assert not errors, "\n".join(str(e) for e in errors)

    def _rejected(self, validator, base_doc, entry):
        assert list(validator.iter_errors(self._with_watcher(base_doc, entry)))

    def test_a_rule_with_include_patterns(self, validator, base_doc):
        self._ok(validator, base_doc, {
            "name": "eng", "connector": "rc-main", "agent": "my-agent",
            "rooms": {"include": ["eng-*", "incident-*"], "except_for": ["eng-archive"]},
        })

    def test_a_dm_only_rule_needs_no_include(self, validator, base_doc):
        self._ok(validator, base_doc, {
            "name": "dms", "rooms": {"direct": True, "group_direct": True},
        })

    def test_per_rule_ttls(self, validator, base_doc):
        self._ok(validator, base_doc, {
            "name": "eng", "rooms": {"include": ["eng-*"]},
            "session_idle_days": 7, "session_expire_days": 30,
        })

    def test_the_reserved_object_dm_form_validates_so_adding_it_stays_additive(
        self, validator, base_doc
    ):
        """§5.4: the schema leaves room for `direct: {include: [...]}` even
        though the loader rejects it today, so a config written against a later
        loader still validates here rather than needing a schema change."""
        self._ok(validator, base_doc, {
            "name": "dms", "rooms": {"direct": {"include": ["alice"], "except_for": ["bob"]}},
        })

    def test_the_static_shape_is_rejected(self, validator, base_doc):
        """The schema matches the loader's cutover refusal (§5.4) — a static
        entry must fail schema validation too, not slip through one gate."""
        self._rejected(validator, base_doc, {"room": "general", "connector": "rc-main"})
        self._rejected(validator, base_doc, {"rooms": ["a", "b"], "connector": "rc-main"})

    def test_a_rule_without_a_name_is_rejected(self, validator, base_doc):
        self._rejected(validator, base_doc, {"rooms": {"include": ["eng-*"]}})

    def test_an_empty_rule_name_is_rejected(self, validator, base_doc):
        self._rejected(validator, base_doc, {"name": "", "rooms": {"include": ["a"]}})

    def test_a_typo_inside_rooms_is_rejected(self, validator, base_doc):
        self._rejected(validator, base_doc, {"name": "x", "rooms": {"includ": ["a"]}})

    def test_a_non_positive_ttl_is_rejected(self, validator, base_doc):
        self._rejected(validator, base_doc, {
            "name": "x", "rooms": {"include": ["a"]}, "session_idle_days": 0,
        })

    def test_a_stray_key_on_a_rule_is_rejected(self, validator, base_doc):
        self._rejected(validator, base_doc, {
            "name": "x", "rooms": {"include": ["a"]}, "sesion_idle_days": 7,
        })

    def test_mixing_a_room_with_a_rooms_block_is_rejected(self, validator, base_doc):
        """Neither branch matches: the static one forbids room+rooms together,
        and the rule one has additionalProperties: false so `room` is unknown."""
        self._rejected(validator, base_doc, {
            "name": "x", "room": "general", "rooms": {"include": ["a"]},
        })

    def test_session_id_is_not_accepted_on_a_rule(self, validator, base_doc):
        self._rejected(validator, base_doc, {
            "name": "x", "rooms": {"include": ["a"]}, "session_id": "abc",
        })


class TestEmptyDeploymentValidates:
    """The loaders accept a file with no connector, agent or rule (coop-keeper
    design §3.10); the schema must agree, or an editor would flag as invalid a
    file `coop config validate` accepts."""

    def test_an_empty_document_is_valid(self, validator):
        assert not list(validator.iter_errors({}))

    def test_explicitly_empty_blocks_are_valid(self, validator):
        assert not list(validator.iter_errors(
            {"connectors": [], "agents": {}, "watcher_rules": []}))

    def test_templates_only_is_valid(self, validator):
        assert not list(validator.iter_errors({
            "tool_presets": {"readonly": [{"tool": "Read"}]},
            "connector_templates": {"default": {"reply_in_thread": False}},
        }))


class TestSchemaCatchesKnownMistakes:
    """Negative controls — if these stop failing, the schema became too
    permissive (e.g. a stray additionalProperties: true) to catch anything."""

    @pytest.fixture
    def base_doc(self) -> dict:
        return _load_yaml(REPO_ROOT / "config.example.yaml")

    def test_typo_top_level_key_is_rejected(self, validator, base_doc):
        bad = copy.deepcopy(base_doc)
        bad["watchres"] = bad.pop("watcher_rules")
        assert list(validator.iter_errors(bad))

    def test_room_and_rooms_both_set_is_rejected(self, validator, base_doc):
        bad = copy.deepcopy(base_doc)
        bad["watcher_rules"][0]["room"] = "oops"
        assert list(validator.iter_errors(bad))

    def test_typo_in_tool_rule_key_is_rejected(self, validator, base_doc):
        bad = copy.deepcopy(base_doc)
        bad["agents"]["my-agent"]["owner_allowed_tools"].append({"toool": "Read"})
        assert list(validator.iter_errors(bad))

    def test_unknown_connector_type_is_rejected(self, validator, base_doc):
        bad = copy.deepcopy(base_doc)
        bad["connectors"][0]["type"] = "rocketchatt"
        assert list(validator.iter_errors(bad))

    def test_connector_template_setting_name_is_rejected(self, validator, base_doc):
        bad = copy.deepcopy(base_doc)
        bad["connector_templates"] = {"x": {"name": "not-allowed"}}
        assert list(validator.iter_errors(bad))

    def test_a_rule_entry_setting_session_id_is_rejected(self, validator, base_doc):
        """`session_id` is not a rule key, so the entry's closed property set
        catches it. The equivalent check on a `watcher_templates:` entry moved to
        the loader when `session_id` lost its dedicated rejection path: a
        template is merged into the entry, and the merged entry is what has a
        closed key set. See tests/unit/test_session_id_removed.py."""
        bad = copy.deepcopy(base_doc)
        bad["watcher_rules"][0]["session_id"] = "not-allowed"
        assert list(validator.iter_errors(bad))

    def test_a_watcher_template_may_supply_rooms(self, validator, base_doc):
        """The point of the `watcher_rules:` rename: `rooms` is inheritable, so a
        rule need not carry it and the schema must not require it."""
        ok = copy.deepcopy(base_doc)
        ok["watcher_templates"] = {"shared": {"rooms": {"direct": True}}}
        ok["watcher_rules"] = [{"name": "dms", "inherits": "shared"}]
        assert not list(validator.iter_errors(ok))

    def test_template_setting_inherits_is_rejected(self, validator, base_doc):
        """No nested templates — a template cannot itself set 'inherits'."""
        bad = copy.deepcopy(base_doc)
        bad["agent_templates"] = {"x": {"inherits": "y"}}
        assert list(validator.iter_errors(bad))

    def test_session_idle_days_zero_is_rejected(self, validator, base_doc):
        bad = copy.deepcopy(base_doc)
        bad["agents"]["my-agent"]["session_idle_days"] = 0
        assert list(validator.iter_errors(bad))


class TestNullableTTLFields:
    """The TTLs live on a watcher **rule**, not an agent (design §5.4).

    An explicit `null` must still validate even though the field is otherwise a
    positive integer — that is the loader-supported way to suppress a non-null value
    inherited from a `watcher_templates:` entry (`_deep_merge()`'s documented
    "explicit null suppresses a base value" contract), not just "omit it".

    The agent side asserts the opposite: `$defs/agent` sets
    `additionalProperties: false`, so a leftover TTL key there is a *schema* error as
    well as a loader error. Both halves are checked, because the schema is not
    enforced at load and the loader does not read the schema — neither one covers
    the other.
    """

    @pytest.fixture
    def base_doc(self) -> dict:
        return _load_yaml(REPO_ROOT / "config.example.yaml")

    @pytest.mark.parametrize("field", ["session_idle_days", "session_expire_days"])
    def test_explicit_null_on_a_rule_is_schema_valid(self, validator, base_doc, field):
        doc = copy.deepcopy(base_doc)
        doc["watcher_rules"] = [
            {"name": "eng", "rooms": {"include": ["eng-*"]}, field: None}
        ]
        errors = list(validator.iter_errors(doc))
        assert not errors, "\n".join(str(e) for e in errors)

    @pytest.mark.parametrize("field", ["session_idle_days", "session_expire_days"])
    def test_a_positive_integer_on_a_rule_is_schema_valid(self, validator, base_doc, field):
        doc = copy.deepcopy(base_doc)
        doc["watcher_rules"] = [
            {"name": "eng", "rooms": {"include": ["eng-*"]}, field: 7}
        ]
        errors = list(validator.iter_errors(doc))
        assert not errors, "\n".join(str(e) for e in errors)

    @pytest.mark.parametrize("field", ["session_idle_days", "session_expire_days"])
    def test_the_key_is_no_longer_accepted_on_an_agent(self, validator, base_doc, field):
        doc = copy.deepcopy(base_doc)
        doc["agents"]["my-agent"][field] = 7
        errors = list(validator.iter_errors(doc))
        assert errors, f"$defs/agent still accepts {field}"
