"""`config reload` through the control server, on a real `GatewayService` (#144).

Seam: `ControlServer.dispatch_command` — the same boundary the CLI crosses —
on a service booted from a config file with Script connectors and mock
agents. Assertions are what an operator sees: the plan document, the exit
code, `list` afterwards, the state file, `config-show`, AUDIT lines.

Run with:
    uv run python -m pytest tests/integration/test_config_reload.py -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import pytest

from gateway.core.state import load_state
from tests.helpers import (
    boot_gateway_service,
    gateway_config_text,
    isolate_runtime_dir,
    write_gateway_config,
)

pytestmark = pytest.mark.integration

ALL = ["active", "idle", "paused", "failed"]


class _ReloadCase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp, self.runtime = isolate_runtime_dir(self)
        self.config_path = self.tmp / "config.yaml"

    def _text(self, **kw) -> str:
        kw.setdefault("working_directory", self.tmp)
        return gateway_config_text(**kw)

    async def _boot(self, text: str | None = None):
        config = write_gateway_config(self.tmp, text=text if text is not None else self._text())
        self.service = await boot_gateway_service(self, self.tmp, self.runtime, config)
        return self.service

    def _rewrite(self, text: str) -> None:
        self.config_path.write_text(text)

    async def _dispatch(self, **request) -> dict:
        return await self.service._control.dispatch_command(request)

    async def _reload(self, *, dry_run=False) -> dict:
        return await self._dispatch(cmd="config-reload", dry_run=dry_run,
                                    config_path=str(self.config_path))

    async def _rows(self) -> dict[str, dict]:
        result = await self._dispatch(cmd="list", states=ALL)
        self.assertTrue(result["ok"], result)
        return {row["watcher_name"]: row for row in result["data"]}


class TestNoChangeAndDryRun(_ReloadCase):

    async def test_an_unchanged_file_is_a_no_op_that_exits_zero(self):
        await self._boot()
        before = await self._rows()
        result = await self._reload()
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["applied"])
        self.assertEqual(result["watchers"], [])
        self.assertEqual(await self._rows(), before)

    async def test_a_dry_run_plans_but_changes_nothing(self):
        await self._boot()
        rows = await self._rows()
        self.assertEqual(rows["script:script"]["state"], "active")
        self._rewrite(self._text(rules=[{
            "name": "w1", "agent": "default", "connector": "script",
            "rooms": {"include": ["script"]}, "session_idle_days": 3}]))

        result = await self._reload(dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["changes"]["rules"]["changed"], ["w1"])
        self.assertEqual([(w["action"], w["handle"]) for w in result["watchers"]],
                         [("rematerialize", "script:script")])
        record = load_state("script")[0]
        self.assertEqual(record.rule["session_idle_days"], 15, "nothing was applied")
        self.assertEqual(self.service._config_digest,
                         self.service.describe_config()["digest"])
        self.assertNotEqual(result["digest"], self.service._config_digest,
                            "the plan carries the candidate's digest, the daemon keeps its own")

    async def test_a_description_only_edit_is_a_no_op(self):
        await self._boot()
        self._rewrite(self._text().replace("watcher_rules:\n- name: w1",
                                           "watcher_rules:\n- description: the room\n  name: w1"))
        result = await self._reload()
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["applied"])


class TestRuleChanges(_ReloadCase):

    async def test_an_edited_rule_rematerializes_the_record_and_keeps_its_session(self):
        await self._boot()
        before = (await self._rows())["script:script"]
        self._rewrite(self._text(rules=[{
            "name": "w1", "agent": "default", "connector": "script",
            "rooms": {"include": ["script"]}, "session_idle_days": 3}]))

        result = await self._reload()

        self.assertTrue(result["applied"], result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual([w["action"] for w in result["watchers"]], ["rematerialize"])
        after = (await self._rows())["script:script"]
        self.assertEqual(after["session_id"], before["session_id"], "the session survives")
        self.assertEqual(after["state"], "active", "the processor was restarted, not dropped")
        self.assertEqual(load_state("script")[0].rule["session_idle_days"], 3)
        self.assertEqual(self.service.describe_config()["digest"], result["digest"])

    async def test_a_rematerialized_watcher_that_fails_to_restart_is_not_a_clean_apply(self):
        await self._boot()
        sm = self.service._session_managers["script"]
        self._rewrite(self._text(rules=[{
            "name": "w1", "agent": "default", "connector": "script",
            "rooms": {"include": ["script"]}, "session_idle_days": 3}]))

        async def _start_fails(*a, **kw):
            raise RuntimeError("backend down")

        with patch.object(sm._lifecycle, "start_watcher_in_room", side_effect=_start_fails):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertIn("could not be restarted", result["degraded"][0]["error"])
        self.assertEqual(self.service._entries[0].degraded, "",
                         "the connector runs on; one room is down")
        self.assertEqual((await self._rows())["script:script"]["state"], "failed")
        self.assertEqual(load_state("script")[0].rule["session_idle_days"], 3,
                         "the record was re-materialized all the same")

    async def test_an_expiry_that_does_not_go_through_is_not_a_clean_apply(self):
        await self._boot()
        sm = self.service._session_managers["script"]
        self._rewrite(self._text(rules=[]))

        async def _reclaim_fails(*a, **kw):
            raise OSError("state file is read-only")

        with patch.object(sm._lifecycle, "reclaim_room", side_effect=_reclaim_fails):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertIn("could not be expired", result["degraded"][0]["error"])
        self.assertIn("script:script", await self._rows(), "the record is still installed")

    async def test_a_removed_rule_expires_the_record_and_logs_the_session_id(self):
        await self._boot()
        session = (await self._rows())["script:script"]["session_id"]
        self._rewrite(self._text(rules=[]))

        with self.assertLogs("agent-chat-gateway", level="WARNING") as logs:
            result = await self._reload()

        self.assertEqual([(w["action"], w["reason"], w["session_id"]) for w in result["watchers"]],
                         [("expire", "no-rule-matches", session)])
        self.assertNotIn("script:script", await self._rows())
        audit = [line for line in logs.output if "AUDIT: session released" in line]
        self.assertEqual(len(audit), 1, logs.output)
        self.assertIn(session, audit[0])

    async def test_an_added_rule_on_an_eager_connector_starts_its_room(self):
        await self._boot()
        self._rewrite(self._text(rules=[
            {"name": "w1", "agent": "default", "connector": "script",
             "rooms": {"include": ["script"]}},
            {"name": "w2", "agent": "default", "connector": "script",
             "rooms": {"include": ["ops"]}},
        ]))
        result = await self._reload()
        self.assertEqual(result["changes"]["rules"]["added"], ["w2"])
        rows = await self._rows()
        self.assertEqual(rows["script:ops"]["state"], "active")
        self.assertEqual(rows["script:script"]["state"], "active", "the other room was not touched")

    async def test_an_eager_room_a_new_rule_names_that_will_not_start_is_reported(self):
        await self._boot()
        sm = self.service._session_managers["script"]
        real_start = sm._lifecycle.start_watcher_in_room

        async def _ops_fails(wc, *a, **kw):
            if wc.name == "script:ops":
                raise RuntimeError("no such room")
            return await real_start(wc, *a, **kw)

        self._rewrite(self._text(rules=[
            {"name": "w1", "agent": "default", "connector": "script",
             "rooms": {"include": ["script"]}},
            {"name": "w2", "agent": "default", "connector": "script",
             "rooms": {"include": ["ops"]}}]))
        with patch.object(sm._lifecycle, "start_watcher_in_room", side_effect=_ops_fails):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertIn("no such room", result["degraded"][0]["error"])
        self.assertEqual((await self._rows())["script:script"]["state"], "active")

    async def test_reordering_rules_is_applied_as_a_reconciliation(self):
        await self._boot(self._text(rules=[
            {"name": "w1", "agent": "default", "connector": "script",
             "rooms": {"include": ["script"]}},
            {"name": "w2", "agent": "default", "connector": "script",
             "rooms": {"include": ["script", "ops"]}},
        ]))
        self._rewrite(self._text(rules=[
            {"name": "w2", "agent": "default", "connector": "script",
             "rooms": {"include": ["script", "ops"]}},
            {"name": "w1", "agent": "default", "connector": "script",
             "rooms": {"include": ["script"]}},
        ]))
        result = await self._reload()
        self.assertTrue(result["changes"]["rules"]["reordered"])
        self.assertEqual([(w["action"], w["from_rule"], w["to_rule"]) for w in result["watchers"]
                          if w["handle"] == "script:script"],
                         [("rematerialize", "w1", "w2")])
        self.assertEqual(load_state("script")[0].rule_name if load_state("script")[0].room_id
                         == "script" else None, "w2")


class TestAgentChanges(_ReloadCase):

    async def test_a_changed_agent_is_rebuilt_and_its_processors_restarted(self):
        await self._boot()
        old_backend = self.service._agents["default"]
        before = (await self._rows())["script:script"]
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(self.tmp), "timeout": 99}}))

        result = await self._reload()

        self.assertEqual(result["changes"]["agents"]["changed"], ["default"])
        self.assertEqual([(w["action"], w["reason"]) for w in result["watchers"]],
                         [("restart", "agent 'default' restarts")])
        self.assertIsNot(self.service._agents["default"], old_backend)
        after = (await self._rows())["script:script"]
        self.assertEqual(after["state"], "active")
        self.assertEqual(after["session_id"], before["session_id"],
                         "same backend identity — the session is reused")
        self.assertEqual(self.service._core_config.agent_config("default").timeout, 99)

    async def test_a_changed_working_directory_starts_a_fresh_session_and_logs_the_old(self):
        await self._boot()
        old_session = (await self._rows())["script:script"]["session_id"]
        other = self.tmp / "elsewhere"
        other.mkdir()
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(other)}}))

        with self.assertLogs("agent-chat-gateway", level="WARNING") as logs:
            result = await self._reload()

        self.assertEqual(result["exit_code"], 0, result)
        after = (await self._rows())["script:script"]
        self.assertNotEqual(after["session_id"], old_session)
        self.assertEqual(after["state"], "active")
        new_backend = self.service._agents["default"]
        self.assertEqual([s["working_directory"] for s in new_backend.created_sessions],
                         [str(other)], "one fresh session, in the new directory")
        audit = [line for line in logs.output if "AUDIT: session released" in line
                 and old_session in line]
        self.assertEqual(len(audit), 1, logs.output)
        self.assertIn("abandoned at provisioning", audit[0])


class TestAgentRestartOrdering(_ReloadCase):

    async def test_processors_stop_before_their_backend_and_start_after_the_new_one(self):
        await self._boot()
        old_backend = self.service._agents["default"]
        sm = self.service._session_managers["script"]
        order: list[str] = []
        real_stop_backend = old_backend.stop
        real_stop_processor = sm._lifecycle._stop_processor

        async def _stop_backend():
            order.append("backend.stop")
            await real_stop_backend()

        async def _stop_processor(name):
            order.append("processor.stop")
            await real_stop_processor(name)

        old_backend.stop = _stop_backend
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(self.tmp), "timeout": 99}}))
        with patch.object(sm._lifecycle, "_stop_processor", side_effect=_stop_processor):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(order, ["processor.stop", "backend.stop"],
                         "the processor drains against a live backend, then the backend stops")
        after = (await self._rows())["script:script"]
        self.assertEqual(after["state"], "active")
        new_backend = self.service._agents["default"]
        self.assertIsNot(new_backend, old_backend)

    async def test_a_removed_agents_processors_drain_before_its_backend_stops(self):
        """Its records expire or move under the new rules, and each of those stops
        the processor — which drains by processing. The backend must still be up."""
        await self._boot(self._text(
            agents={"default": {"type": "claude", "working_directory": str(self.tmp)},
                    "other": {"type": "claude", "working_directory": str(self.tmp)}}))
        old_backend = self.service._agents["default"]
        sm = self.service._session_managers["script"]
        order: list[str] = []
        real_stop_backend, real_stop_processor = old_backend.stop, sm._lifecycle._stop_processor

        async def _stop_backend():
            order.append("backend.stop")
            await real_stop_backend()

        async def _stop_processor(name):
            order.append("processor.stop")
            await real_stop_processor(name)

        old_backend.stop = _stop_backend
        self._rewrite(self._text(
            agents={"other": {"type": "claude", "working_directory": str(self.tmp)}},
            rules=[{"name": "w1", "agent": "other", "connector": "script",
                    "rooms": {"include": ["script"]}}]))
        with patch.object(sm._lifecycle, "_stop_processor", side_effect=_stop_processor):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["changes"]["agents"]["removed"], ["default"])
        self.assertEqual(order[:2], ["processor.stop", "backend.stop"])
        self.assertNotIn("default", self.service._agents)
        after = (await self._rows())["script:script"]
        self.assertEqual(after["agent_name"], "other")
        self.assertEqual(after["state"], "active", "moved to the other agent and started there")

    async def test_a_room_moved_off_a_changed_agent_is_started_on_its_new_one(self):
        await self._boot(self._text(
            agents={"default": {"type": "claude", "working_directory": str(self.tmp)},
                    "other": {"type": "claude", "working_directory": str(self.tmp)}}))
        self._rewrite(self._text(
            agents={"default": {"type": "claude", "working_directory": str(self.tmp),
                                "timeout": 99},
                    "other": {"type": "claude", "working_directory": str(self.tmp)}},
            rules=[{"name": "w1", "agent": "other", "connector": "script",
                    "rooms": {"include": ["script"]}}]))
        result = await self._reload()
        self.assertEqual(result["exit_code"], 0, result)
        after = (await self._rows())["script:script"]
        self.assertEqual((after["agent_name"], after["state"]), ("other", "active"))

    async def test_a_backend_that_will_not_stop_is_replaced_and_reported(self):
        """No refusal, no rollback: the agent is replaced, the old backend is a
        tracked leftover, the plan says so and exits 2 (owner, 2026-09-05)."""
        await self._boot()
        old_backend = self.service._agents["default"]

        async def _stuck():
            raise RuntimeError("sidecar refuses to die")

        old_backend.stop = _stuck
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(self.tmp), "timeout": 99}}))
        result = await self._reload()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["exit_code"], 2)
        self.assertTrue(result["applied"])
        agent_findings = [d for d in result["degraded"] if d["kind"] == "agent"]
        self.assertEqual([d["name"] for d in agent_findings], ["default"])
        self.assertIn("did not stop after 3 attempts", agent_findings[0]["error"])
        self.assertIn("check the process now", agent_findings[0]["error"])
        self.assertIsNot(self.service._agents["default"], old_backend, "replaced regardless")
        self.assertEqual(self.service._core_config.agent_config("default").timeout, 99,
                         "the config IS applied")
        self.assertEqual([(n, k) for n, k, _ in self.service._runtime_manager.leftovers],
                         [("default", "backend")])
        status = await self._dispatch(cmd="config-show", include_config=False)
        self.assertIn("previous backend did not stop", status["degraded"][0]["error"])
        self.assertEqual((await self._rows())["script:script"]["state"], "active",
                         "the room runs on the new backend")

    async def test_a_stop_that_succeeds_on_retry_is_not_degraded(self):
        await self._boot()
        backend = self.service._agents["default"]
        real_stop = backend.stop
        calls = {"n": 0}

        async def _flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("busy")
            await real_stop()

        backend.stop = _flaky
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(self.tmp), "timeout": 99}}))
        result = await self._reload()

        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(calls["n"], 3, "two failures, then the retry that stopped it")
        self.assertEqual(self.service._runtime_manager.leftovers, [])

    async def test_a_broker_that_will_not_stop_is_the_agents_failure(self):
        from unittest.mock import AsyncMock, MagicMock
        await self._boot()
        broker = MagicMock(stop=AsyncMock(side_effect=RuntimeError("port still bound")))
        self.service._runtime_manager._brokers["default"] = broker
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(self.tmp), "timeout": 99}}))
        result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        agent_findings = [d for d in result["degraded"] if d["kind"] == "agent"]
        self.assertEqual([d["name"] for d in agent_findings], ["default"])
        self.assertIn("permission broker did not stop", agent_findings[0]["error"])
        self.assertEqual(broker.stop.await_count, 3)
        self.assertEqual([(n, k) for n, k, _ in self.service._runtime_manager.leftovers],
                         [("default", "permission broker")])

    async def test_a_removed_connector_is_removed_even_when_a_backend_will_not_stop(self):
        await self._boot(self._text(connectors=("script", "second"), rules=[
            {"name": "w1", "agent": "default", "connector": "script",
             "rooms": {"include": ["script"]}},
            {"name": "w2", "agent": "default", "connector": "second",
             "rooms": {"include": ["script"]}}]))
        backend = self.service._agents["default"]

        async def _stuck():
            raise RuntimeError("sidecar refuses to die")

        backend.stop = _stuck
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(self.tmp), "timeout": 99}}))
        result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertEqual([e.name for e in self.service._entries], ["script"])
        self.assertFalse((self.runtime / "state.second.json").exists())
        self.assertEqual([d["name"] for d in result["degraded"] if d["kind"] == "agent"],
                         ["default"])
        self.assertEqual(self.service.describe_config()["digest"], result["digest"])

    async def test_a_room_that_fails_to_start_after_its_agent_changed_is_reported(self):
        await self._boot()
        sm = self.service._session_managers["script"]
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(self.tmp), "timeout": 99}}))

        async def _start_fails(*a, **kw):
            raise RuntimeError("provisioning failed")

        with patch.object(sm._lifecycle, "start_watcher_in_room", side_effect=_start_fails):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertIn("provisioning failed", result["degraded"][0]["error"])
        self.assertEqual(self.service._entries[0].degraded, "", "the connector runs on")
        self.assertEqual((await self._rows())["script:script"]["state"], "failed")

    async def test_a_room_whose_stop_raised_late_is_still_started_again(self):
        """`_stop_processor` raises at the unsubscribe AFTER removing the processor;
        the room is stopped, noisily, and must come back like the others."""
        await self._boot()
        sm = self.service._session_managers["script"]

        async def _unsub_fails(*a, **kw):
            raise RuntimeError("unsubscribe timed out")

        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(self.tmp), "timeout": 99}}))
        with patch.object(sm._connector, "unsubscribe_room", side_effect=_unsub_fails):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertTrue(any("stopped with errors" in d["error"] for d in result["degraded"]),
                        result["degraded"])
        self.assertEqual((await self._rows())["script:script"]["state"], "active",
                         "reported, and still brought back on the new backend")

    async def test_a_kept_lifecycle_learns_which_agents_are_unavailable(self):
        await self._boot()
        sm = self.service._session_managers["script"]
        self.assertEqual(sm._lifecycle._blocked_agents, set())
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(self.tmp), "timeout": 99}}))

        async def _boom():
            raise RuntimeError("no such binary")

        from tests.helpers import MockAgentBackend

        def _broken(cfg):
            backend = MockAgentBackend(id_prefix="broken")
            backend.start = _boom
            return backend

        with patch("gateway.service._build_agent_backend", side_effect=_broken):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertEqual([(d["kind"], d["name"]) for d in result["degraded"]],
                         [("agent", "default")])
        self.assertIn("no such binary", result["degraded"][0]["error"])
        self.assertEqual(sm._lifecycle._blocked_agents, {"default"},
                         "the kept lifecycle refuses starts on the agent that did not come up")
        status = await self._dispatch(cmd="config-show", include_config=False)
        self.assertIn("no such binary", status["degraded"][0]["error"])

        # The binary is back; an unchanged reload retries and clears the gate.
        second = await self._reload()
        self.assertEqual(second["exit_code"], 0, second)
        self.assertEqual(sm._lifecycle._blocked_agents, set())
        self.assertEqual((await self._rows())["script:script"]["state"], "active")


class TestConnectorChanges(_ReloadCase):

    def _two(self, **kw) -> str:
        return self._text(connectors=("script", "second"), rules=[
            {"name": "w1", "agent": "default", "connector": "script",
             "rooms": {"include": ["script"]}},
            {"name": "w2", "agent": "default", "connector": "second",
             "rooms": {"include": ["script"]}},
        ], **kw)

    async def test_an_added_connector_is_built_connected_and_synced(self):
        await self._boot()
        self._rewrite(self._two())
        result = await self._reload()
        self.assertEqual(result["changes"]["connectors"]["added"], ["second"])
        self.assertEqual(result["exit_code"], 0, result)
        rows = await self._rows()
        self.assertEqual(rows["second:script"]["state"], "active")
        self.assertEqual([e.name for e in self.service._entries], ["script", "second"])
        self.assertEqual(set(self.service._session_managers), {"script", "second"})

    async def test_a_new_connectors_own_start_errors_are_reported_not_swallowed(self):
        await self._boot()
        self._rewrite(self._two())
        from gateway.core.session_manager import SessionManager
        real_sync = SessionManager.sync_only

        async def _sync(self_sm, *a, **kw):
            errors = await real_sync(self_sm, *a, **kw)
            if self_sm._connector_name == "second":
                errors = errors + ["Connector 'second': room 'script' failed to start: nope"]
            return errors

        with patch.object(SessionManager, "sync_only", _sync):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertIn("nope", result["degraded"][0]["error"])
        self.assertEqual(self.service._entries[1].degraded, "", "the connector itself is up")

    async def test_a_removed_connectors_records_go_through_the_shared_reclamation_tail(self):
        await self._boot(self._two())
        created = await self._dispatch(cmd="schedule-create", watcher="second:script",
                                       message="ping", cron="0 9 * * *", times=0)
        self.assertTrue(created["ok"], created)
        sm = self.service._session_managers["second"]
        real_reclaim = sm._lifecycle.reclaim_room
        self._rewrite(self._text())

        with patch.object(sm._lifecycle, "reclaim_room", wraps=real_reclaim) as reclaim:
            result = await self._reload()

        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(reclaim.await_count, 1, "each record through reclaim_room")
        self.assertEqual(reclaim.await_args.kwargs["reason"], "connector-removed")
        jobs = await self._dispatch(cmd="schedule-list", include_completed=True)
        self.assertEqual([j["status"] for j in jobs["jobs"]], ["cancelled"], jobs)
        self.assertIn("removed from config.yaml", jobs["jobs"][0]["cancel_reason"])

    async def test_a_removed_connector_expires_its_records_and_deletes_its_state_file(self):
        await self._boot(self._two())
        session = (await self._rows())["second:script"]["session_id"]
        self.assertTrue((self.runtime / "state.second.json").exists())
        self._rewrite(self._text())

        with self.assertLogs("agent-chat-gateway", level="WARNING") as logs:
            result = await self._reload()

        self.assertEqual(result["changes"]["connectors"]["removed"], ["second"])
        self.assertEqual([(w["action"], w["reason"], w["session_id"]) for w in result["watchers"]],
                         [("expire", "connector-removed", session)])
        self.assertFalse((self.runtime / "state.second.json").exists())
        self.assertNotIn("second:script", await self._rows())
        self.assertTrue(any("AUDIT: session released" in line and session in line
                            and "connector-removed" in line for line in logs.output))

    async def test_a_changed_connector_restarts_as_a_unit_with_records_kept(self):
        await self._boot()
        old = self.service._entries[0]
        before = (await self._rows())["script:script"]
        self._rewrite(self._text().replace("- name: script\n  type: script",
                                           "- name: script\n  type: script\n  context_inject_files: []\n  timezone: UTC"))
        # `timezone` lands in `raw`, which is a connector change.
        result = await self._reload()
        self.assertEqual(result["changes"]["connectors"]["changed"], ["script"], result)
        self.assertEqual([(w["action"], w["reason"]) for w in result["watchers"]],
                         [("restart", "connector restarts")])
        self.assertTrue(any("re-validated" in n for n in result["notes"]))
        self.assertIsNot(self.service._entries[0], old)
        self.assertIsNot(self.service._session_managers["script"], old.session_manager)
        after = (await self._rows())["script:script"]
        self.assertEqual(after["session_id"], before["session_id"])
        self.assertEqual(after["state"], "active")

    async def test_a_rename_with_copied_state_is_accepted_the_way_boot_accepts_it(self):
        """The layout boot's sweep-then-check order accepts: the old file goes, the
        copy under the new name is hydrated. Reload validates through the same
        prediction, so it is not refused as a duplicate session (#144)."""
        import shutil
        await self._boot()
        session = (await self._rows())["script:script"]["session_id"]
        # `shutdown` saves the file on the way out; copy what is on disk now.
        self.service._session_managers["script"]._lifecycle.save_state()
        shutil.copy(self.runtime / "state.script.json", self.runtime / "state.renamed.json")
        self._rewrite(self._text(connectors=("renamed",), rules=[{
            "name": "w1", "agent": "default", "connector": "renamed",
            "rooms": {"include": ["script"]}}]))

        dry = await self._reload(dry_run=True)
        self.assertTrue(dry["ok"], dry)
        self.assertEqual(dry["changes"]["connectors"]["removed"], ["script"])
        self.assertEqual(dry["changes"]["connectors"]["added"], ["renamed"])

        result = await self._reload()

        self.assertEqual(result["exit_code"], 0, result)
        self.assertFalse((self.runtime / "state.script.json").exists())
        # The copy keeps its OLD handle — repairing a copied handle is not a
        # supported flow (#148) — so the row is found by room, not by name.
        rows = [r for r in (await self._rows()).values()
                if r["connector"] == "renamed" and r["room_id"] == "script"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], session, "the copy carried it")
        self.assertEqual(rows[0]["state"], "active")

    async def test_a_connector_that_fails_to_connect_is_degraded_not_fatal(self):
        await self._boot()
        self._rewrite(self._two())
        from gateway.connectors import connector_factory as real_factory

        def _factory(cc):
            connector = real_factory(cc)
            if cc.name == "second":
                async def _boom():
                    raise ConnectionError("refused")
                connector.connect = _boom
            return connector

        with patch("gateway.service.connector_factory", side_effect=_factory):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertEqual([(d["kind"], d["name"]) for d in result["degraded"]],
                         [("connector", "second")])
        self.assertIn("refused", result["degraded"][0]["error"])
        status = await self._dispatch(cmd="config-show", include_config=False)
        self.assertEqual([d["name"] for d in status["degraded"]], ["second"])
        self.assertEqual((await self._rows())["script:script"]["state"], "active",
                         "the running connector was not touched")
        self.assertEqual([e.name for e in self.service._entries], ["script", "second"],
                         "the degraded entry stays in the mapping")
        self.assertEqual(self.service.describe_config()["digest"], result["digest"],
                         "the config IS active — the operator fixes and reloads again")

    async def test_only_the_new_connector_that_collides_is_degraded_at_the_identity_barrier(self):
        """Boot fails fast on a shared bot account; a reload cannot take the running
        connectors down for it, so the new connector whose addition makes the
        conflict is refused — and only that one."""
        from gateway.connectors import connector_factory as real_factory
        from gateway.core.bot_identity import BotIdentity

        identities = {
            "script": BotIdentity("rocketchat", "https://chat.example", "acct-1"),
            "second": BotIdentity("rocketchat", "https://chat.example", "acct-2"),
            "third": BotIdentity("rocketchat", "https://chat.example", "acct-1"),  # same as script
        }

        def _factory(cc):
            connector = real_factory(cc)
            connector.bot_identity = lambda: identities[cc.name]
            return connector

        with patch("gateway.service.connector_factory", side_effect=_factory):
            await self._boot()
            self._rewrite(self._text(connectors=("script", "second", "third"), rules=[
                {"name": "w1", "agent": "default", "connector": "script",
                 "rooms": {"include": ["script"]}},
                {"name": "w2", "agent": "default", "connector": "second",
                 "rooms": {"include": ["script"]}},
                {"name": "w3", "agent": "default", "connector": "third",
                 "rooms": {"include": ["script"]}},
            ]))
            result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertEqual([(d["kind"], d["name"]) for d in result["degraded"]],
                         [("connector", "third")])
        self.assertIn("same bot account", result["degraded"][0]["error"])
        rows = await self._rows()
        self.assertEqual(rows["second:script"]["state"], "active", "the other new one came up")
        self.assertEqual(rows["script:script"]["state"], "active", "the running one was not touched")

    async def test_a_rule_change_that_would_share_an_accounts_dms_is_refused_before_anything(self):
        """Two connectors on one Mattermost bot account, safe while their teams keep
        them apart; a rule-only reload opting both into `direct:` connects
        nothing, so the barrier would never run — the pre-check refuses instead.
        At the service seam: the loader refuses `direct:` on a script connector,
        so the candidate is built from rule objects."""
        from gateway.config_diff import diff_configs
        from gateway.core.bot_identity import BotIdentity
        from gateway.core.room_pattern import RoomPattern
        from gateway.core.watcher_rule import RoomMatcher, WatcherRule
        from tests.helpers import make_gateway_config

        await self._boot(self._two())
        for e in self.service._entries:
            e.connector.bot_identity = (
                lambda name=e.name: BotIdentity("mattermost", "https://mm.example", "acct-1",
                                                scope=f"team-{name}"))

        def _rule(name, connector, direct):
            return WatcherRule(name=name, connector=connector, agent="default",
                               rooms=RoomMatcher(include=(RoomPattern("script"),), direct=direct))

        active = self.service._config
        candidate = make_gateway_config(
            connectors=list(active.connectors), agents=dict(active.agents),
            rules=[_rule("w1", "script", True), _rule("w2", "second", True)])
        conflict = self.service._kept_identity_conflict(diff_configs(active, candidate), candidate)
        self.assertIsNotNone(conflict)
        self.assertIn("direct messages", conflict)

        safe = make_gateway_config(
            connectors=list(active.connectors), agents=dict(active.agents),
            rules=[_rule("w1", "script", True), _rule("w2", "second", False)])
        self.assertIsNone(self.service._kept_identity_conflict(diff_configs(active, safe), safe))

        # The wiring: a conflict refuses the reload — dry run and apply alike.
        digest = self.service.describe_config()["digest"]
        self._rewrite(self._text(connectors=("script", "second"), rules=[
            {"name": "w1", "agent": "default", "connector": "script",
             "rooms": {"include": ["script", "ops"]}},
            {"name": "w2", "agent": "default", "connector": "second",
             "rooms": {"include": ["script"]}}]))
        with patch.object(self.service, "_kept_identity_conflict", return_value="would share DMs"):
            dry = await self._reload(dry_run=True)
            result = await self._reload()
        for r in (dry, result):
            self.assertFalse(r["ok"], r)
            self.assertEqual(r["error"], "would share DMs")
        self.assertEqual(self.service.describe_config()["digest"], digest, "nothing changed")

    async def test_a_degraded_connector_is_retried_by_the_next_reload_even_if_unchanged(self):
        await self._boot()
        self._rewrite(self._two())
        from gateway.connectors import connector_factory as real_factory

        def _factory(cc):
            connector = real_factory(cc)
            if cc.name == "second":
                async def _boom():
                    raise ConnectionError("refused")
                connector.connect = _boom
            return connector

        with patch("gateway.service.connector_factory", side_effect=_factory):
            first = await self._reload()
        self.assertEqual(first["exit_code"], 2)

        # Nothing in the file changed; the server is simply reachable again.
        second = await self._reload()

        self.assertEqual(second["exit_code"], 0, second)
        self.assertEqual(second["changes"]["connectors"]["changed"], ["second"])
        self.assertFalse(any("degraded" in n for n in second["notes"]),
                         "a section that came back needs no history in the output")
        self.assertEqual((await self._rows())["second:script"]["state"], "active")
        self.assertEqual(self.service.describe_config()["degraded"], [])

    async def test_commands_against_a_degraded_connector_are_refused_with_the_cause(self):
        await self._boot()
        self._rewrite(self._two())
        from gateway.connectors import connector_factory as real_factory

        def _factory(cc):
            connector = real_factory(cc)
            if cc.name == "second":
                async def _boom():
                    raise ConnectionError("refused")
                connector.connect = _boom
            return connector

        with patch("gateway.service.connector_factory", side_effect=_factory):
            await self._reload()

        resumed = await self._dispatch(cmd="resume", connector="second",
                                       watcher_name="second:script")
        self.assertFalse(resumed["ok"])
        self.assertIn("degraded", resumed["error"])
        self.assertIn("refused", resumed["error"])
        self.assertNotIn("shutting down", resumed["error"])
        sent = await self._dispatch(cmd="send", connector="second", room="script", text="hi")
        self.assertFalse(sent["ok"])
        self.assertIn("degraded", sent["error"])
        listed = await self._dispatch(cmd="list", connector="second", states=ALL)
        self.assertTrue(listed["ok"], "list still answers for the records")

    async def test_an_apply_that_raises_leaves_a_consistent_fleet_and_the_old_config(self):
        """A defect mid-apply must not wedge the daemon or make it lie."""
        await self._boot()
        before = self.service.describe_config()["digest"]
        self._rewrite(self._two())
        sm = self.service._session_managers["script"]
        with patch.object(sm, "replace_rules", side_effect=RuntimeError("kaboom")):
            result = await self._reload()

        self.assertFalse(result["ok"])
        self.assertIn("kaboom", result["error"])
        self.assertEqual([e.name for e in self.service._entries], ["script", "second"],
                         "every connector the candidate names has an entry")
        self.assertTrue(self.service._entries[0].degraded,
                        "the kept manager may be half-applied — degraded until restarted")
        self.assertTrue(self.service._entries[1].degraded)
        self.assertFalse(sm._lifecycle.transitions_disarmed, "the kept manager is re-armed")
        self.assertIsNotNone(self.service._scheduler_task)
        status = await self._dispatch(cmd="config-show", include_config=False)
        self.assertEqual([d["name"] for d in status["degraded"]], ["script", "second"])
        self.assertEqual(status["digest"], before,
                         "the previous config stays active — the file is NOT applied")
        # The next reload re-diffs against the previous config, restarts the
        # degraded kept manager whole and lands everything.
        second = await self._reload()
        self.assertEqual(second["exit_code"], 0, second)
        self.assertEqual(second["changes"]["connectors"]["added"], ["second"])
        self.assertEqual(second["changes"]["connectors"]["changed"], ["script"])
        self.assertEqual((await self._rows())["second:script"]["state"], "active")
        self.assertEqual(self.service.describe_config()["digest"], second["digest"])
        self.assertEqual(len(self.service._entries), 2, "the placeholder was replaced, not doubled")

    async def test_a_leftover_entry_neither_config_names_is_removed_when_the_file_is_put_back(self):
        await self._boot()
        self._rewrite(self._two())
        sm = self.service._session_managers["script"]
        with patch.object(sm, "replace_rules", side_effect=RuntimeError("kaboom")):
            await self._reload()
        self.assertEqual([e.name for e in self.service._entries], ["script", "second"])

        self._rewrite(self._text())  # the operator gives up and restores the file
        result = await self._reload()

        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["changes"]["connectors"]["removed"], ["second"])
        self.assertEqual(result["changes"]["connectors"]["changed"], ["script"],
                         "the kept manager the failed apply may have half-updated is restarted")
        self.assertEqual([e.name for e in self.service._entries], ["script"])
        self.assertEqual(self.service.describe_config()["degraded"], [])
        self.assertEqual((await self._rows())["script:script"]["state"], "active")

    async def test_a_connector_whose_teardown_fails_is_replaced_and_kept_as_a_leftover(self):
        await self._boot()
        sm = self.service._session_managers["script"]
        entry = self.service._entries[0]
        self._rewrite(self._text().replace("- name: script\n  type: script",
                                           "- name: script\n  type: script\n  timezone: UTC"))

        async def _stuck():
            raise OSError("transport will not close")

        # The realistic failure: processors stopped, state saved, then the
        # transport will not close — `shutdown()` raises at its last step.
        sm._connector.disconnect = _stuck
        result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        finding = [d for d in result["degraded"] if d["kind"] == "connector"][0]
        self.assertIn("previous instance did not shut down after 3 attempts", finding["error"])
        self.assertIn("check the process now", finding["error"])
        self.assertIsNot(self.service._entries[0], entry, "replaced regardless")
        self.assertEqual(len(self.service._entries), 1)
        self.assertEqual([e.name for e, _ in self.service._leftover_entries], ["script"])
        self.assertEqual((await self._rows())["script:script"]["state"], "active")
        status = await self._dispatch(cmd="config-show", include_config=False)
        self.assertIn("previous instance did not shut down", status["degraded"][0]["error"])

        # Shutdown disconnects the leftover once more (retried) — and never saves it.
        disconnects = {"n": 0}

        async def _count():
            disconnects["n"] += 1
            raise OSError("still will not close")

        entry.connector.disconnect = _count
        with patch.object(entry.session_manager._lifecycle, "save_state") as save:
            await self.service.shutdown()
        self.assertEqual(disconnects["n"], 3, "three attempts, no other means")
        save.assert_not_called()

    async def test_a_leftover_that_disconnects_at_the_next_reload_leaves_the_degraded_list(self):
        await self._boot()
        sm = self.service._session_managers["script"]
        self._rewrite(self._text().replace("- name: script\n  type: script",
                                           "- name: script\n  type: script\n  timezone: UTC"))

        real_disconnect = sm._connector.disconnect
        calls = {"n": 0}

        async def _stuck_then_fine():
            calls["n"] += 1
            if calls["n"] <= 3:
                raise OSError("transport will not close")
            await real_disconnect()

        sm._connector.disconnect = _stuck_then_fine
        first = await self._reload()
        self.assertEqual(first["exit_code"], 2)
        self.assertEqual(len(self.service.describe_config()["degraded"]), 1)

        second = await self._reload()  # unchanged file; the leftover's disconnect now works
        self.assertEqual(second["exit_code"], 0, second)
        self.assertEqual(self.service._leftover_entries, [])
        self.assertEqual(self.service.describe_config()["degraded"], [])

    async def test_a_reconciliation_failure_degrades_the_entry_and_is_retried(self):
        await self._boot()
        sm = self.service._session_managers["script"]
        self._rewrite(self._text(rules=[{
            "name": "w1", "agent": "default", "connector": "script",
            "rooms": {"include": ["script"]}, "session_idle_days": 3}]))
        with patch.object(sm, "reconcile_live", side_effect=RuntimeError("engine broke")):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertIn("engine broke", self.service._entries[0].degraded)
        status = await self._dispatch(cmd="config-show", include_config=False)
        self.assertEqual([d["name"] for d in status["degraded"]], ["script"])

        second = await self._reload()  # file unchanged; the degraded entry is retried whole
        self.assertEqual(second["exit_code"], 0, second)
        self.assertEqual(second["changes"]["connectors"]["changed"], ["script"])
        self.assertEqual(self.service._entries[0].degraded, "")
        self.assertEqual(load_state("script")[0].rule["session_idle_days"], 3)

    async def test_an_added_connectors_static_era_records_are_planned_as_expired(self):
        from gateway.core.state import WatcherState, save_state
        await self._boot()
        save_state("second", [WatcherState(watcher_name="legacy", session_id="s-legacy",
                                           room_id="r-legacy")])
        self._rewrite(self._two())
        dry = await self._reload(dry_run=True)
        self.assertIn(("expire", "static-era record pruned at boot", "s-legacy"),
                      [(w["action"], w["reason"], w["session_id"]) for w in dry["watchers"]])

    async def test_a_scheduled_job_survives_its_connectors_restart(self):
        await self._boot()
        created = await self._dispatch(cmd="schedule-create", watcher="script:script",
                                       message="ping", cron="0 9 * * *", times=0)
        self.assertTrue(created["ok"], created)
        self._rewrite(self._text().replace("- name: script\n  type: script",
                                           "- name: script\n  type: script\n  timezone: UTC"))
        result = await self._reload()
        self.assertEqual(result["changes"]["connectors"]["changed"], ["script"])
        jobs = await self._dispatch(cmd="schedule-list")
        self.assertEqual([j["status"] for j in jobs["jobs"]], ["active"], jobs)
        self.assertIsNotNone(self.service._scheduler_task, "the scheduler is running again")


class TestSymlinkedConfig(_ReloadCase):

    async def test_a_repointed_config_symlink_is_followed_by_the_next_reload(self):
        import os
        real = self.tmp / "config.yaml"
        link = self.tmp / "current.yaml"
        write_gateway_config(self.tmp, text=self._text())
        os.symlink(real, link)
        from gateway.config import GatewayConfig
        self.service = await boot_gateway_service(
            self, self.tmp, self.runtime, GatewayConfig.from_file(str(link)), config_path=link)

        newer = self.tmp / "config.v2.yaml"
        newer.write_text(self._text(extra="max_queue_depth: 7\n"))
        os.unlink(link)
        os.symlink(newer, link)

        result = await self._dispatch(cmd="config-reload", dry_run=False, config_path=str(link))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["changes"]["values"][0]["new"], 7)
        self.assertEqual(self.service._core_config.max_queue_depth, 7)


class TestValuesAndRefusals(_ReloadCase):

    async def test_max_queue_depth_is_swapped_in_place_without_restarts(self):
        await self._boot()
        self._rewrite(self._text(extra="max_queue_depth: 7\n"))
        result = await self._reload()
        self.assertEqual(result["changes"]["values"],
                         [{"path": "max_queue_depth", "old": 100, "new": 7}])
        self.assertEqual(result["watchers"], [])
        self.assertEqual(self.service._core_config.max_queue_depth, 7)
        self.assertTrue(any("started from now on" in n for n in result["notes"]), result["notes"])

    async def test_scheduler_settings_are_swapped_in_place_without_restarts(self):
        await self._boot()
        self._rewrite(self._text(extra="scheduler:\n  completed_job_ttl_days: 1\n"))
        result = await self._reload()
        self.assertEqual(result["changes"]["values"],
                         [{"path": "scheduler.completed_job_ttl_days", "old": 7, "new": 1}])
        self.assertEqual(result["watchers"], [])
        self.assertEqual(self.service._job_scheduler.completed_job_ttl_days, 1)

    async def test_an_invalid_file_is_refused_with_findings_and_nothing_changes(self):
        await self._boot()
        digest = self.service.describe_config()["digest"]
        self._rewrite(self._text(rules=[{
            "name": "w1", "agent": "nobody", "connector": "script",
            "rooms": {"include": ["script"]}}]))
        result = await self._reload()
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertTrue(any("nobody" in f["message"] for f in result["validation"]["findings"]),
                        result)
        self.assertEqual(self.service.describe_config()["digest"], digest)
        self.assertEqual((await self._rows())["script:script"]["state"], "active")

    async def test_a_second_reload_while_one_applies_is_refused(self):
        await self._boot()
        async with self.service._reload_lock:
            result = await self._reload()
        self.assertFalse(result["ok"])
        self.assertIn("already in progress", result["error"])

    async def test_lifecycle_verbs_are_refused_during_the_apply_window(self):
        await self._boot()
        self.service._reloading = True
        try:
            result = await self._dispatch(cmd="pause", watcher_name="script:script")
        finally:
            self.service._reloading = False
        self.assertFalse(result["ok"])
        self.assertIn("reload is in progress", result["error"])
        listed = await self._dispatch(cmd="list", states=ALL)
        self.assertTrue(listed["ok"], "list is not a lifecycle verb")

    async def test_another_path_than_the_daemons_is_refused(self):
        await self._boot()
        result = await self._dispatch(cmd="config-reload", dry_run=True,
                                      config_path=str(self.tmp / "other.yaml"))
        self.assertFalse(result["ok"])
        self.assertIn("runs", result["error"])

    async def test_config_show_returns_the_redacted_active_config(self):
        await self._boot()
        result = await self._dispatch(cmd="config-show")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["digest"]), 64)
        self.assertEqual(result["config"]["connectors"][0]["name"], "script")
        import os
        self.assertEqual(result["config_path"], os.path.abspath(self.config_path),
                         "absolute but not resolved — a symlink stays a symlink")
        self.assertTrue(result["loaded_at"])


class TestShutdownAndReload(_ReloadCase):

    async def test_shutdown_waits_for_a_reload_that_is_applying(self):
        await self._boot()
        await self.service._reload_lock.acquire()  # a reload mid-apply
        shutting = asyncio.create_task(self.service.shutdown())
        await asyncio.sleep(0.05)
        self.assertFalse(shutting.done(), "teardown does not race the apply")
        self.service._reload_lock.release()
        await asyncio.wait_for(shutting, timeout=10)
        self.assertTrue(self.service._reload_lock.locked(), "held for good once shutting down")


class TestMembershipRemovalDuringReload(_ReloadCase):

    async def test_a_removal_that_arrives_while_quiesced_is_handled_after_rearm(self):
        await self._boot()
        sm = self.service._session_managers["script"]
        reclaimed: list[str] = []

        async def _reclaim(room_id, **kw):
            reclaimed.append(room_id)

        # Quiesce as the apply does, deliver the platform's "bot removed", re-arm.
        with patch.object(sm, "_reclaim_removed_room", side_effect=_reclaim):
            await sm.quiesce("a config reload is in progress")
            await sm._on_membership_removed("script")
            self.assertEqual(reclaimed, [], "deferred, not dropped, not acted on yet")
            await sm.rearm()
        self.assertEqual(reclaimed, ["script"])
        self.assertEqual(sm._deferred_removals, [])


class TestApplyIsQuiescent(_ReloadCase):

    async def test_kept_managers_are_rearmed_after_the_apply(self):
        await self._boot()
        self._rewrite(self._text(extra="max_queue_depth: 7\n"))
        await self._reload()
        sm = self.service._session_managers["script"]
        self.assertFalse(sm._lifecycle.transitions_disarmed)
        paused = await self._dispatch(cmd="pause", watcher_name="script:script")
        self.assertTrue(paused["ok"], paused)

    async def test_the_scheduler_is_paused_across_the_apply_and_restarted(self):
        await self._boot()
        seen: list[bool] = []
        original = self.service._install_entries

        def _spy(entries):
            seen.append(self.service._scheduler_task is None)
            original(entries)

        self._rewrite(self._text(extra="max_queue_depth: 7\n"))
        with patch.object(self.service, "_install_entries", side_effect=_spy):
            await self._reload()
        self.assertTrue(seen and all(seen), "no scheduler task ran during the apply")
        self.assertIsNotNone(self.service._scheduler_task)
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
