"""Tests for AgentSession — thin scripting wrapper around AgentBackend.

All tests use MockAgentBackend (canned responses, zero subprocess calls).
Covers: basic send/receive, options forwarding, error handling, and the
primary use case of agent-to-agent pipelines.

Run with:
    uv run python -m unittest tests.test_agent_session -v
"""

from __future__ import annotations

import asyncio
import unittest

from gateway.agents.response import AgentResponse
from gateway.agents.session import AgentSession
from tests.integration.test_integration import MockAgentBackend

# ── Basic send/receive ─────────────────────────────────────────────────────────

class TestAgentSessionBasic(unittest.IsolatedAsyncioTestCase):
    """Core send/receive behaviour."""

    async def test_single_round_trip(self):
        backend = MockAgentBackend(responses=["pong"])
        async with AgentSession(backend, "/tmp") as session:
            response = await session.send("ping")
        self.assertIsInstance(response, AgentResponse)
        self.assertEqual(response.text, "pong")
        self.assertEqual(str(response), "pong")  # __str__ delegates to .text

    async def test_multiple_sequential_sends(self):
        backend = MockAgentBackend(responses=["one", "two", "three"])
        async with AgentSession(backend, "/tmp") as session:
            results = [await session.send(f"msg-{i}") for i in range(3)]
        self.assertEqual([r.text for r in results], ["one", "two", "three"])

    async def test_session_id_assigned_on_enter(self):
        backend = MockAgentBackend()
        async with AgentSession(backend, "/tmp") as session:
            self.assertIsNotNone(session.session_id)
            self.assertTrue(session.session_id.startswith("mock-session-"))

    async def test_session_id_none_before_enter(self):
        session = AgentSession(MockAgentBackend(), "/tmp")
        self.assertIsNone(session.session_id)

    async def test_session_id_reused_across_sends(self):
        """All send() calls within the same context must use the same session_id."""
        backend = MockAgentBackend(responses=["a", "b"])
        async with AgentSession(backend, "/tmp") as session:
            sid = session.session_id
            await session.send("first")
            await session.send("second")

        self.assertEqual(backend.sent_messages[0]["session_id"], sid)
        self.assertEqual(backend.sent_messages[1]["session_id"], sid)

    async def test_working_directory_passed_to_create_and_send(self):
        backend = MockAgentBackend()
        async with AgentSession(backend, "/my/project") as session:
            await session.send("hello")

        self.assertEqual(backend.created_sessions[0]["working_directory"], "/my/project")
        self.assertEqual(backend.sent_messages[0]["working_directory"], "/my/project")

    async def test_timeout_passed_to_send(self):
        backend = MockAgentBackend()
        async with AgentSession(backend, "/tmp", timeout=42) as session:
            await session.send("hi")
        self.assertEqual(backend.sent_messages[0]["timeout"], 42)

    async def test_create_session_called_exactly_once(self):
        backend = MockAgentBackend()
        async with AgentSession(backend, "/tmp") as session:
            await session.send("a")
            await session.send("b")
        self.assertEqual(len(backend.created_sessions), 1)


# ── Options forwarding ─────────────────────────────────────────────────────────

class TestAgentSessionOptions(unittest.IsolatedAsyncioTestCase):
    """extra_args, session_title, attachments, env forwarded correctly."""

    async def test_extra_args_passed_to_create_session(self):
        backend = MockAgentBackend()
        async with AgentSession(backend, "/tmp", extra_args=["--agent", "assistance"]):
            pass
        self.assertEqual(backend.created_sessions[0]["extra_args"], ["--agent", "assistance"])

    async def test_no_extra_args_by_default(self):
        backend = MockAgentBackend()
        async with AgentSession(backend, "/tmp"):
            pass
        self.assertIsNone(backend.created_sessions[0]["extra_args"])

    async def test_session_title_passed_to_create_session(self):
        backend = MockAgentBackend()
        async with AgentSession(backend, "/tmp", session_title="my-pipeline"):
            pass
        self.assertEqual(backend.created_sessions[0]["session_title"], "my-pipeline")

    async def test_no_session_title_by_default(self):
        backend = MockAgentBackend()
        async with AgentSession(backend, "/tmp"):
            pass
        self.assertIsNone(backend.created_sessions[0]["session_title"])

    async def test_attachments_forwarded_to_send(self):
        backend = MockAgentBackend()
        async with AgentSession(backend, "/tmp") as session:
            await session.send("read this", attachments=["/tmp/file.txt"])
        self.assertEqual(backend.sent_messages[0]["attachments"], ["/tmp/file.txt"])

    async def test_env_forwarded_to_send(self):
        backend = MockAgentBackend()
        async with AgentSession(backend, "/tmp") as session:
            await session.send("hi", env={"COOP_ROLE": "owner"})
        self.assertEqual(backend.sent_messages[0]["env"], {"COOP_ROLE": "owner"})

    async def test_no_attachments_by_default(self):
        backend = MockAgentBackend()
        async with AgentSession(backend, "/tmp") as session:
            await session.send("hi")
        self.assertIsNone(backend.sent_messages[0]["attachments"])

    async def test_no_env_by_default(self):
        backend = MockAgentBackend()
        async with AgentSession(backend, "/tmp") as session:
            await session.send("hi")
        self.assertIsNone(backend.sent_messages[0]["env"])


# ── Error handling ─────────────────────────────────────────────────────────────

class TestAgentSessionErrors(unittest.IsolatedAsyncioTestCase):
    """send() before enter, timeout, and backend errors."""

    async def test_send_before_enter_raises_runtime_error(self):
        session = AgentSession(MockAgentBackend(), "/tmp")
        with self.assertRaises(RuntimeError) as ctx:
            await session.send("oops")
        self.assertIn("async with", str(ctx.exception))

    async def test_timeout_error_propagates(self):
        backend = MockAgentBackend()
        backend.side_effect = asyncio.TimeoutError
        with self.assertRaises(asyncio.TimeoutError):
            async with AgentSession(backend, "/tmp") as session:
                await session.send("slow query")

    async def test_backend_runtime_error_propagates(self):
        backend = MockAgentBackend()
        backend.side_effect = RuntimeError
        with self.assertRaises(RuntimeError):
            async with AgentSession(backend, "/tmp") as session:
                await session.send("broken request")


# ── Agent-to-agent pipelines ───────────────────────────────────────────────────

class TestAgentToAgentPipeline(unittest.IsolatedAsyncioTestCase):
    """Primary use case: chaining multiple AgentSessions manually.

    This is the cleaner replacement for the ScriptConnector.pipe_to() pattern
    when you need explicit control over what passes between agents.
    """

    async def test_two_agent_pipeline(self):
        """opencode summarises → claude reviews."""
        opencode = MockAgentBackend(responses=["Summary: project has 3 modules."])
        claude   = MockAgentBackend(responses=["Review: summary looks accurate."])

        async with (
            AgentSession(opencode, "/tmp") as oc,
            AgentSession(claude,   "/tmp") as cc,
        ):
            summary = await oc.send("Summarize the codebase")
            # str(summary) == summary.text — works transparently in f-strings
            review  = await cc.send(f"Review this summary:\n{summary}")

        self.assertEqual(summary.text, "Summary: project has 3 modules.")
        self.assertEqual(review.text,  "Review: summary looks accurate.")
        # claude must receive opencode's output in its prompt (via __str__)
        self.assertIn("Summary: project has 3 modules.", claude.sent_messages[0]["prompt"])

    async def test_three_agent_chain(self):
        """A → B → C: each agent builds on the previous output."""
        agent_a = MockAgentBackend(responses=["step-A done"])
        agent_b = MockAgentBackend(responses=["step-B done"])
        agent_c = MockAgentBackend(responses=["step-C done"])

        async with (
            AgentSession(agent_a, "/tmp") as a,
            AgentSession(agent_b, "/tmp") as b,
            AgentSession(agent_c, "/tmp") as c,
        ):
            out_a = await a.send("start")
            out_b = await b.send(str(out_a))   # use str() to relay text between agents
            out_c = await c.send(str(out_b))

        self.assertEqual(out_c.text, "step-C done")
        self.assertEqual(agent_b.sent_messages[0]["prompt"], "step-A done")
        self.assertEqual(agent_c.sent_messages[0]["prompt"], "step-B done")

    async def test_independent_sessions_have_no_cross_contamination(self):
        """Each AgentSession calls create_session on its own backend — no shared state.

        Note: MockAgentBackend counters are per-instance so both may produce
        "mock-session-0001".  The important invariant is that each backend is
        called exactly once and that send() on one session never touches the
        other backend.
        """
        opencode = MockAgentBackend(responses=["oc-reply"])
        claude   = MockAgentBackend(responses=["cc-reply"])

        async with (
            AgentSession(opencode, "/tmp") as oc,
            AgentSession(claude,   "/tmp") as cc,
        ):
            self.assertIsNotNone(oc.session_id)
            self.assertIsNotNone(cc.session_id)

            oc_reply = await oc.send("hello from oc")
            cc_reply = await cc.send("hello from cc")

        # Each backend created exactly one session
        self.assertEqual(len(opencode.created_sessions), 1)
        self.assertEqual(len(claude.created_sessions), 1)

        # Messages were delivered to the correct backend
        self.assertEqual(opencode.sent_messages[0]["prompt"], "hello from oc")
        self.assertEqual(claude.sent_messages[0]["prompt"], "hello from cc")
        self.assertEqual(oc_reply.text, "oc-reply")
        self.assertEqual(cc_reply.text, "cc-reply")

    async def test_pipeline_with_transform(self):
        """Demonstrate mid-pipeline prompt transformation."""
        extractor = MockAgentBackend(responses=["raw: foo bar baz"])
        formatter = MockAgentBackend(responses=["formatted: FOO BAR BAZ"])

        async with (
            AgentSession(extractor, "/tmp") as ex,
            AgentSession(formatter, "/tmp") as fm,
        ):
            raw     = await ex.send("Extract keywords from the doc")
            # Transform: wrap in a new instruction before forwarding
            relayed = f"Uppercase the following keywords:\n{raw}"
            result  = await fm.send(relayed)

        self.assertIn("raw: foo bar baz", formatter.sent_messages[0]["prompt"])
        self.assertEqual(result.text, "formatted: FOO BAR BAZ")

    async def test_multi_turn_within_single_session(self):
        """A single AgentSession supports multiple back-and-forth turns."""
        backend = MockAgentBackend(responses=["hi there", "doing well", "bye!"])

        async with AgentSession(backend, "/tmp") as session:
            r1 = await session.send("hello")
            r2 = await session.send("how are you?")
            r3 = await session.send("goodbye")

        self.assertEqual([r.text for r in [r1, r2, r3]], ["hi there", "doing well", "bye!"])
        # All turns must share the same session_id
        sids = {m["session_id"] for m in backend.sent_messages}
        self.assertEqual(len(sids), 1)

    async def test_response_is_agent_response_instance(self):
        """send() must always return an AgentResponse, never a bare str."""
        backend = MockAgentBackend(responses=["hello"])
        async with AgentSession(backend, "/tmp") as session:
            response = await session.send("hi")
        self.assertIsInstance(response, AgentResponse)
        self.assertFalse(response.is_error)

    async def test_str_of_response_equals_text(self):
        """AgentResponse.__str__ must equal .text for transparent pipeline use."""
        backend = MockAgentBackend(responses=["the answer"])
        async with AgentSession(backend, "/tmp") as session:
            response = await session.send("question")
        self.assertEqual(str(response), response.text)
        self.assertEqual(f"result: {response}", f"result: {response.text}")


if __name__ == "__main__":
    unittest.main()
