"""Every config field's reload action, asserted at the operator's seam (#144).

For each field of `ConnectorConfig`, `AgentConfig`, `WatcherRule`,
`GatewayConfig` and `SchedulerConfig`: one `config reload --dry-run` through
the control server with only that field changed, asserting the plan shows the
action `RELOAD_ACTIONS` classifies it as — a connector or agent restart, a
rule reconciliation, a value swap, or a rename read as remove + add. A field
added without a row in `MUTATIONS` fails here, locally.

`tests/unit/test_config_diff.py` checks the table is complete and that the diff
notices each field; this file checks that the notice reaches the plan an
operator reads.
"""

from __future__ import annotations

import dataclasses
import unittest

import pytest
import yaml

from gateway.config import AgentConfig, ConnectorConfig, GatewayConfig, SchedulerConfig
from gateway.config_diff import RELOAD_ACTIONS
from gateway.core.watcher_rule import WatcherRule
from tests.helpers import boot_gateway_service, isolate_runtime_dir, write_gateway_config

pytestmark = pytest.mark.integration


def _base(tmp) -> dict:
    """Two script connectors, two claude agents, one rule — so a rule can move
    connector or agent without an entity being added or removed."""
    return {
        "connectors": [{"name": "script", "type": "script"},
                       {"name": "second", "type": "script"}],
        "agents": {"default": {"type": "claude", "working_directory": str(tmp)},
                   "other": {"type": "claude", "working_directory": str(tmp)}},
        "watcher_rules": [{"name": "w1", "agent": "default", "connector": "script",
                           "rooms": {"include": ["script"]}}],
    }


def _connector(doc: dict) -> dict:
    return doc["connectors"][0]


def _agent(doc: dict) -> dict:
    return doc["agents"]["default"]


def _rule(doc: dict) -> dict:
    return doc["watcher_rules"][0]


# (dataclass, field) -> a mutation of the base document changing only that field.
# The expected plan outcome comes from RELOAD_ACTIONS, not repeated here.
MUTATIONS = {
    # A rename must carry its rules along, or the file is invalid (unknown connector).
    (ConnectorConfig, "name"): lambda d, t: (_connector(d).update(name="renamed"),
                                            _rule(d).update(connector="renamed")),
    (ConnectorConfig, "type"): lambda d, t: _connector(d).update(
        type="mattermost", server={"url": "http://localhost:8065", "team": "t", "token": "x"}),
    (ConnectorConfig, "raw"): lambda d, t: _connector(d).update(timezone="UTC"),
    (ConnectorConfig, "context_inject_files"): lambda d, t: _connector(d).update(
        context_inject_files=[str(t / "ctx.md")]),
    (AgentConfig, "name"): None,  # the dict key; a rename is remove + add, covered by
    #                               the connector's name row — an agent cannot be renamed
    #                               without every rule naming it changing too.
    (AgentConfig, "type"): lambda d, t: _agent(d).update(type="opencode"),
    (AgentConfig, "command"): lambda d, t: _agent(d).update(command="claude-next"),
    (AgentConfig, "new_session_args"): lambda d, t: _agent(d).update(new_session_args=["--x"]),
    (AgentConfig, "working_directory"): lambda d, t: _agent(d).update(
        working_directory=str(t / "elsewhere")),
    (AgentConfig, "session_prefix"): lambda d, t: _agent(d).update(session_prefix="p"),
    (AgentConfig, "lazy_instruction_loading"): lambda d, t: _agent(d).update(
        lazy_instruction_loading=False),
    (AgentConfig, "context_inject_files"): lambda d, t: _agent(d).update(
        context_inject_files=[str(t / "ctx.md")]),
    (AgentConfig, "owner_allowed_tools"): lambda d, t: _agent(d).update(
        owner_allowed_tools=[{"tool": "Read"}]),
    (AgentConfig, "guest_allowed_tools"): lambda d, t: _agent(d).update(
        guest_allowed_tools=[{"tool": "Read"}]),
    (AgentConfig, "timeout"): lambda d, t: _agent(d).update(timeout=400),
    (AgentConfig, "permissions"): lambda d, t: _agent(d).update(permissions={"enabled": True}),
    (WatcherRule, "name"): lambda d, t: _rule(d).update(name="w1-renamed"),
    (WatcherRule, "connector"): lambda d, t: _rule(d).update(connector="second"),
    (WatcherRule, "agent"): lambda d, t: _rule(d).update(agent="other"),
    (WatcherRule, "rooms"): lambda d, t: _rule(d).update(rooms={"include": ["script", "ops"]}),
    (WatcherRule, "session_idle_days"): lambda d, t: _rule(d).update(session_idle_days=3),
    (WatcherRule, "session_expire_days"): lambda d, t: _rule(d).update(session_expire_days=3),
    (WatcherRule, "context_inject_files"): lambda d, t: _rule(d).update(
        context_inject_files=[str(t / "ctx.md")]),
    (WatcherRule, "history_handoff"): lambda d, t: _rule(d).update(
        history_handoff={"enabled": False}),
    (GatewayConfig, "connectors"): None,     # sections: their entries' rows cover them
    (GatewayConfig, "agents"): None,
    (GatewayConfig, "watcher_rules"): None,
    (GatewayConfig, "scheduler"): None,
    (GatewayConfig, "max_queue_depth"): lambda d, t: d.update(max_queue_depth=7),
    (SchedulerConfig, "completed_job_ttl_days"): lambda d, t: d.update(
        scheduler={"completed_job_ttl_days": 1}),
}


class TestEveryFieldReachesThePlan(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp, self.runtime = isolate_runtime_dir(self)
        (self.tmp / "ctx.md").write_text("context\n")
        (self.tmp / "elsewhere").mkdir()
        self.doc = _base(self.tmp)
        config = write_gateway_config(self.tmp, text=yaml.safe_dump(self.doc, sort_keys=False))
        self.service = await boot_gateway_service(self, self.tmp, self.runtime, config)

    def test_every_field_has_a_mutation(self):
        for cls, table in RELOAD_ACTIONS.items():
            declared = {(cls, f.name) for f in dataclasses.fields(cls)}
            self.assertEqual(declared - set(MUTATIONS), set(), f"{cls.__name__}: unclassified here")
            self.assertEqual(set(table), {f for c, f in MUTATIONS if c is cls}, "stale row")

    async def test_each_field_change_shows_its_classified_action_in_a_dry_run(self):
        for (cls, field), mutate in MUTATIONS.items():
            if mutate is None:
                continue
            with self.subTest(entity=cls.__name__, field=field):
                doc = yaml.safe_load(yaml.safe_dump(self.doc))  # deep copy
                mutate(doc, self.tmp)
                (self.tmp / "config.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
                plan = await self.service._control.dispatch_command({
                    "cmd": "config-reload", "dry_run": True,
                    "config_path": str(self.tmp / "config.yaml")})
                self.assertTrue(plan["ok"], plan)
                changes = plan["changes"]
                action = RELOAD_ACTIONS[cls][field]
                if action == "identity":
                    self.assertEqual(changes["connectors"]["removed"], ["script"])
                    self.assertEqual(changes["connectors"]["added"], ["renamed"])
                elif action == "restart-connector":
                    self.assertEqual(changes["connectors"]["changed"], ["script"])
                    self.assertEqual([w["action"] for w in plan["watchers"]], ["restart"])
                elif action == "restart-agent":
                    self.assertEqual(changes["agents"]["changed"], ["default"])
                    self.assertEqual([w["action"] for w in plan["watchers"]], ["restart"])
                elif action == "reconcile":
                    self.assertTrue(changes["rules"]["changed"] or changes["rules"]["added"]
                                    or changes["rules"]["removed"], changes)
                    self.assertFalse(changes["connectors"]["changed"] or changes["agents"]["changed"],
                                     "a rule edit restarts nothing")
                elif action == "value":
                    self.assertEqual(len(changes["values"]), 1, changes)
                    self.assertEqual(plan["watchers"], [])
                else:
                    self.fail(f"unexpected action {action!r}")


if __name__ == "__main__":
    unittest.main()
