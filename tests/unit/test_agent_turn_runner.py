"""Tests for gateway.core.agent_turn_runner.AgentTurnRunner.

Covers:
  - Successful turn: agent called, response posted, typing bracketed
  - Timeout: timeout error message posted
  - Exception: error message posted
  - Usage logging on response with usage metadata
  - Intermediate events forwarded to connector.notify_agent_event()
  - notify_agent_event() error does not abort the turn
  - Default stream() (wraps send()) emits no intermediate events
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.agents import AgentBackend
from gateway.agents.errors import AgentPermissionError, AgentRateLimitedError
from gateway.agents.response import AgentEvent, AgentResponse
from gateway.config import AgentConfig
from gateway.core.agent_turn_runner import AgentTurnRunner
from gateway.core.config import CoreConfig

# ── Helpers ────────────────────────────────────────────────────────────────────


class _MockAgent(AgentBackend):
    """Mock that uses the default stream() wrapping send()."""

    def __init__(self, response: AgentResponse | Exception):
        self._response = response

    async def create_session(self, *a, **kw):
        return "ses_001"

    async def send(
        self,
        session_id,
        prompt,
        working_directory,
        timeout,
        attachments=None,
        env=None,
        append_system_prompt_file=None,
    ):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _StreamingMockAgent(AgentBackend):
    """Mock that overrides stream() to emit intermediate events."""

    def __init__(self, events: list[AgentEvent]):
        self._events = events

    async def create_session(self, *a, **kw):
        return "ses_001"

    async def send(self, *a, **kw):
        # Not called when stream() is overridden
        raise NotImplementedError

    async def stream(self, *a, **kw):
        for event in self._events:
            yield event


def _make_runner(agent: AgentBackend) -> tuple[AgentTurnRunner, MagicMock]:
    connector = MagicMock()
    connector.send_text = AsyncMock()
    connector.notify_typing = AsyncMock()
    connector.notify_agent_event = AsyncMock()
    config = CoreConfig(
        agents={"default": AgentConfig(timeout=10)},
    )
    runner = AgentTurnRunner(
        agent=agent,
        connector=connector,
        config=config,
        agent_name="default",
        room_name="test-room",
    )
    return runner, connector


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestAgentTurnRunner(unittest.IsolatedAsyncioTestCase):
    async def test_successful_turn_posts_response(self):
        agent = _MockAgent(AgentResponse(text="Hi there!"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id="t1",
        )

        connector.send_text.assert_called_once()
        call_args = connector.send_text.call_args
        self.assertEqual(call_args[0][0], "room_1")
        self.assertEqual(call_args[0][1].text, "Hi there!")
        self.assertEqual(call_args[1]["thread_id"], "t1")

    async def test_typing_notifications_bracket_execution(self):
        agent = _MockAgent(AgentResponse(text="ok"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        # notify_typing called twice: True (before send), False (after)
        calls = connector.notify_typing.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0][1])  # first call: is_typing=True
        self.assertFalse(calls[1][0][1])  # second call: is_typing=False

    async def test_timeout_posts_error_message(self):
        agent = _MockAgent(asyncio.TimeoutError())
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="slow query",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        connector.send_text.assert_called_once()
        response = connector.send_text.call_args[0][1]
        self.assertTrue(response.is_error)
        self.assertIn("timed out", response.text)

    async def test_exception_posts_error_message(self):
        agent = _MockAgent(RuntimeError("backend crashed"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="trigger error",
            working_directory="/tmp",
            room_id="room_1",
            thread_id="t1",
        )

        connector.send_text.assert_called_once()
        response = connector.send_text.call_args[0][1]
        self.assertTrue(response.is_error)
        self.assertIn("failed to process the request", response.text.lower())
        self.assertIn("ref: ses_001", response.text)

    async def test_rate_limited_exception_maps_to_friendly_message(self):
        agent = _MockAgent(AgentRateLimitedError("quota reached"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="trigger limit",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        response = connector.send_text.call_args[0][1]
        self.assertIn("usage limit", response.text.lower())
        self.assertIn("ref: ses_001", response.text)

    async def test_permission_exception_maps_to_friendly_message(self):
        agent = _MockAgent(AgentPermissionError("approval required"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="blocked",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        response = connector.send_text.call_args[0][1]
        self.assertIn("permission restriction", response.text.lower())
        self.assertIn("ref: ses_001", response.text)

    async def test_typing_off_sent_even_on_error(self):
        """Typing indicator is turned off even when the agent raises."""
        agent = _MockAgent(RuntimeError("boom"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="fail",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        # Last notify_typing call must be False
        last_call = connector.notify_typing.call_args_list[-1]
        self.assertFalse(last_call[0][1])

    async def test_file_paths_and_env_forwarded_to_agent(self):
        """file_paths and role_env are forwarded to agent.stream()."""
        captured: dict = {}

        class _CapturingAgent(AgentBackend):
            async def create_session(self, *a, **kw):
                return "ses_001"

            async def send(self, **kw):
                raise NotImplementedError

            async def stream(self, **kw):
                captured.update(kw)
                yield AgentEvent(kind="final", response=AgentResponse(text="ok"))

        agent = _CapturingAgent()

        connector = MagicMock()
        connector.send_text = AsyncMock()
        connector.notify_typing = AsyncMock()
        connector.notify_agent_event = AsyncMock()
        config = CoreConfig(
            agents={"default": AgentConfig(timeout=10)},
        )
        runner = AgentTurnRunner(agent, connector, config, "default", "room")

        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
            file_paths=["/tmp/a.txt"],
            role_env={"COOP_ROLE": "owner"},
        )

        self.assertEqual(captured.get("attachments"), ["/tmp/a.txt"])
        self.assertEqual(captured.get("env"), {"COOP_ROLE": "owner"})

    async def test_connector_send_failure_logged_not_recursive(self):
        """Connector send_text failure is logged, not recursively retried."""
        agent = _MockAgent(AgentResponse(text="Good reply"))
        runner, connector = _make_runner(agent)
        connector.send_text = AsyncMock(side_effect=ConnectionError("RC down"))

        # Must not raise — delivery failure is caught and logged
        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        # send_text was called exactly once — no recursive error posting
        connector.send_text.assert_called_once()

    async def test_agent_error_delivered_even_when_connector_healthy(self):
        """Agent error produces an error response that goes through delivery."""
        agent = _MockAgent(RuntimeError("agent crashed"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="fail",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        # Error response should have been delivered
        connector.send_text.assert_called_once()
        delivered = connector.send_text.call_args[0][1]
        self.assertTrue(delivered.is_error)
        self.assertIn("failed to process the request", delivered.text.lower())
        self.assertIn("ref: ses_001", delivered.text)

    async def test_connector_failure_after_agent_success_no_error_loop(self):
        """When agent succeeds but connector fails, no second send is attempted."""
        agent = _MockAgent(AgentResponse(text="Success"))
        runner, connector = _make_runner(agent)
        connector.send_text = AsyncMock(side_effect=ConnectionError("transport down"))

        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        # Only ONE send_text call (the failed delivery), not a second one
        # attempting to send "Error: transport down"
        self.assertEqual(connector.send_text.call_count, 1)

    # ── Streaming / notify_agent_event tests ─────────────────────────────────

    async def test_intermediate_events_forwarded_to_connector(self):
        """Intermediate AgentEvents are forwarded to connector.notify_agent_event."""
        events = [
            AgentEvent(kind="tool_call", text="🔧 Bash"),
            AgentEvent(kind="tool_call", text="🔧 Read"),
            AgentEvent(kind="final", response=AgentResponse(text="done")),
        ]
        agent = _StreamingMockAgent(events)
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id="t1",
        )

        # Final response delivered
        connector.send_text.assert_called_once()
        self.assertEqual(connector.send_text.call_args[0][1].text, "done")

        # Intermediate events forwarded (2 tool_call events, NOT the final)
        self.assertEqual(connector.notify_agent_event.call_count, 2)
        first_call = connector.notify_agent_event.call_args_list[0]
        self.assertEqual(first_call[0][1].kind, "tool_call")
        self.assertEqual(first_call[0][1].text, "🔧 Bash")
        self.assertEqual(first_call[1]["thread_id"], "t1")

    async def test_default_stream_emits_no_intermediate_events(self):
        """Default stream() (wrapping send()) emits no intermediate events."""
        agent = _MockAgent(AgentResponse(text="Hi"))
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        # No intermediate events — notify_agent_event should not be called
        connector.notify_agent_event.assert_not_called()
        connector.send_text.assert_called_once()

    async def test_notify_agent_event_error_does_not_abort_turn(self):
        """If notify_agent_event raises, the turn still completes normally."""
        events = [
            AgentEvent(kind="tool_call", text="🔧 Bash"),
            AgentEvent(kind="final", response=AgentResponse(text="ok")),
        ]
        agent = _StreamingMockAgent(events)
        runner, connector = _make_runner(agent)
        connector.notify_agent_event = AsyncMock(
            side_effect=ConnectionError("RC down")
        )

        # Must not raise — notify_agent_event error is swallowed
        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        # Final response still delivered despite notify_agent_event failure
        connector.send_text.assert_called_once()
        self.assertEqual(connector.send_text.call_args[0][1].text, "ok")

    async def test_stream_with_only_final_event_posts_response(self):
        """A stream with only a final event works identically to the default."""
        events = [AgentEvent(kind="final", response=AgentResponse(text="only final"))]
        agent = _StreamingMockAgent(events)
        runner, connector = _make_runner(agent)

        await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        connector.send_text.assert_called_once()
        self.assertEqual(connector.send_text.call_args[0][1].text, "only final")
        connector.notify_agent_event.assert_not_called()

    # ── Agent chain tests ─────────────────────────────────────────────────────

    async def test_agent_chain_termination_token_suppresses_delivery(self):
        """When agent responds with the termination token, response is not delivered."""
        from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN

        agent = _MockAgent(AgentResponse(text=AGENT_CHAIN_TERMINATION_TOKEN))
        runner, connector = _make_runner(agent)

        terminated = await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
            is_agent_chain=True,
            agent_chain_context="\n---\n[Agent chain: turn 1/5]",
        )

        self.assertTrue(terminated)
        connector.send_text.assert_not_called()

    async def test_agent_chain_normal_response_delivered_returns_false(self):
        """Normal agent chain response is delivered and run_turn returns False."""
        agent = _MockAgent(AgentResponse(text="Here is my analysis."))
        runner, connector = _make_runner(agent)

        terminated = await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
            is_agent_chain=True,
            agent_chain_context="\n---\n[Agent chain: turn 2/5]",
        )

        self.assertFalse(terminated)
        connector.send_text.assert_called_once()
        self.assertEqual(connector.send_text.call_args[0][1].text, "Here is my analysis.")

    async def test_termination_token_in_non_agent_chain_stripped_not_delivered(self):
        """Termination token alone in a non-chain turn is stripped; nothing delivered."""
        from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN

        agent = _MockAgent(AgentResponse(text=AGENT_CHAIN_TERMINATION_TOKEN))
        runner, connector = _make_runner(agent)

        # is_agent_chain=False (default) — token is stripped, no content to deliver
        terminated = await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        self.assertFalse(terminated)
        connector.send_text.assert_not_called()

    async def test_termination_token_with_preamble_in_non_agent_chain_strips_token(self):
        """Token with preamble in a non-chain turn: preamble delivered, token stripped."""
        from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN

        preamble = "Here is my answer."
        agent = _MockAgent(AgentResponse(text=f"{preamble}\n\n{AGENT_CHAIN_TERMINATION_TOKEN}"))
        runner, connector = _make_runner(agent)

        terminated = await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        self.assertFalse(terminated)
        connector.send_text.assert_called_once()
        delivered_text = connector.send_text.call_args[0][1].text
        self.assertEqual(delivered_text, preamble)
        self.assertNotIn(AGENT_CHAIN_TERMINATION_TOKEN, delivered_text)

    async def test_termination_token_at_start_post_text_delivered(self):
        """Token at start of response: text after token is delivered, token stripped."""
        from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN

        epilogue = "Bye now"
        agent = _MockAgent(AgentResponse(text=f"{AGENT_CHAIN_TERMINATION_TOKEN}\n{epilogue}"))
        runner, connector = _make_runner(agent)

        terminated = await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
            is_agent_chain=True,
            agent_chain_context="\n---\n[Agent chain: turn 1/5]",
        )

        self.assertTrue(terminated)
        connector.send_text.assert_called_once()
        delivered_text = connector.send_text.call_args[0][1].text
        self.assertEqual(delivered_text, epilogue)
        self.assertNotIn(AGENT_CHAIN_TERMINATION_TOKEN, delivered_text)

    async def test_termination_token_at_start_non_chain_post_text_delivered(self):
        """Token at start in non-chain turn: post-token text is delivered, token stripped."""
        from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN

        epilogue = "Bye now"
        agent = _MockAgent(AgentResponse(text=f"{AGENT_CHAIN_TERMINATION_TOKEN}\n{epilogue}"))
        runner, connector = _make_runner(agent)

        terminated = await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        self.assertFalse(terminated)
        connector.send_text.assert_called_once()
        delivered_text = connector.send_text.call_args[0][1].text
        self.assertEqual(delivered_text, epilogue)
        self.assertNotIn(AGENT_CHAIN_TERMINATION_TOKEN, delivered_text)

    async def test_termination_token_in_middle_both_sides_delivered(self):
        """Token in the middle: both pre- and post-token text are delivered."""
        from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN

        pre = "Hello"
        post = "Goodbye"
        agent = _MockAgent(AgentResponse(text=f"{pre}\n{AGENT_CHAIN_TERMINATION_TOKEN}\n{post}"))
        runner, connector = _make_runner(agent)

        terminated = await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
            is_agent_chain=True,
            agent_chain_context="\n---\n[Agent chain: turn 1/5]",
        )

        self.assertTrue(terminated)
        connector.send_text.assert_called_once()
        delivered_text = connector.send_text.call_args[0][1].text
        self.assertIn(pre, delivered_text)
        self.assertIn(post, delivered_text)
        self.assertNotIn(AGENT_CHAIN_TERMINATION_TOKEN, delivered_text)

    async def test_agent_chain_context_appended_to_prompt(self):
        """Agent chain context suffix is appended to the prompt before invoking agent."""
        captured: dict = {}

        class _CapturingAgent(AgentBackend):
            async def create_session(self, *a, **kw):
                return "ses_001"

            async def send(self, **kw):
                raise NotImplementedError

            async def stream(self, **kw):
                captured.update(kw)
                yield AgentEvent(kind="final", response=AgentResponse(text="ok"))

        agent = _CapturingAgent()
        connector = MagicMock()
        connector.send_text = AsyncMock()
        connector.notify_typing = AsyncMock()
        connector.notify_agent_event = AsyncMock()
        config = CoreConfig(
            agents={"default": AgentConfig(timeout=10)},
        )
        runner = AgentTurnRunner(agent, connector, config, "default", "room")

        await runner.run_turn(
            session_id="ses_001",
            prompt="base prompt",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
            is_agent_chain=True,
            agent_chain_context="\n---\n[Agent chain: turn 1/5]",
        )

        self.assertIn("base prompt", captured.get("prompt", ""))
        self.assertIn("[Agent chain: turn 1/5]", captured.get("prompt", ""))

    async def test_agent_chain_no_context_prompt_unchanged(self):
        """When agent_chain_context is empty, the prompt is not modified."""
        captured: dict = {}

        class _CapturingAgent(AgentBackend):
            async def create_session(self, *a, **kw):
                return "ses_001"

            async def send(self, **kw):
                raise NotImplementedError

            async def stream(self, **kw):
                captured.update(kw)
                yield AgentEvent(kind="final", response=AgentResponse(text="ok"))

        agent = _CapturingAgent()
        connector = MagicMock()
        connector.send_text = AsyncMock()
        connector.notify_typing = AsyncMock()
        connector.notify_agent_event = AsyncMock()
        config = CoreConfig(
            agents={"default": AgentConfig(timeout=10)},
        )
        runner = AgentTurnRunner(agent, connector, config, "default", "room")

        await runner.run_turn(
            session_id="ses_001",
            prompt="base prompt",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
            is_agent_chain=True,
            agent_chain_context="",  # empty — no append
        )

        self.assertEqual(captured.get("prompt"), "base prompt")

    async def test_run_turn_returns_false_for_normal_non_agent_chain_turn(self):
        """Non-agent-chain turns always return False."""
        agent = _MockAgent(AgentResponse(text="Hello"))
        runner, connector = _make_runner(agent)

        result = await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
        )

        self.assertFalse(result)

    async def test_termination_token_with_whitespace_stripped(self):
        """Termination token detection trims surrounding whitespace."""
        from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN

        agent = _MockAgent(AgentResponse(text=f"  {AGENT_CHAIN_TERMINATION_TOKEN}  \n"))
        runner, connector = _make_runner(agent)

        terminated = await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
            is_agent_chain=True,
            agent_chain_context="\n---\n[Agent chain: turn 1/5]",
        )

        self.assertTrue(terminated)
        connector.send_text.assert_not_called()

    async def test_termination_token_uppercase_variant_still_terminates(self):
        """Case-insensitive match catches uppercase/mixed-case LLM output variants."""
        agent = _MockAgent(AgentResponse(text="<END-OF-AGENT-CHAIN>"))
        runner, connector = _make_runner(agent)

        terminated = await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
            is_agent_chain=True,
            agent_chain_context="\n---\n[Agent chain: turn 1/5]",
        )

        self.assertTrue(terminated)
        connector.send_text.assert_not_called()

    async def test_termination_token_embedded_in_text_delivers_preamble(self):
        """Token embedded after meaningful content: preamble is delivered, token is suppressed.

        LLMs sometimes prefix the token with a closing sentence, e.g.:
            "Summary: XYZ\n\n<end-of-agent-chain>"
        The preamble must be delivered so the user sees it; the raw token must not appear.
        """
        from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN

        preamble = "Summary: the task is complete."
        text_with_preamble = f"{preamble}\n\n{AGENT_CHAIN_TERMINATION_TOKEN}"
        agent = _MockAgent(AgentResponse(text=text_with_preamble))
        runner, connector = _make_runner(agent)

        terminated = await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
            is_agent_chain=True,
            agent_chain_context="\n---\n[Agent chain: turn 1/5]",
        )

        self.assertTrue(terminated)
        # Preamble delivered, token never appears in chat
        connector.send_text.assert_called_once()
        delivered_response = connector.send_text.call_args[0][1]
        delivered_text = delivered_response.text
        self.assertIn(preamble, delivered_text)
        self.assertNotIn(AGENT_CHAIN_TERMINATION_TOKEN, delivered_text)

    async def test_termination_token_only_no_delivery(self):
        """Token with no preamble: nothing delivered, just silent termination."""
        from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN

        agent = _MockAgent(AgentResponse(text=AGENT_CHAIN_TERMINATION_TOKEN))
        runner, connector = _make_runner(agent)

        terminated = await runner.run_turn(
            session_id="ses_001",
            prompt="hello",
            working_directory="/tmp",
            room_id="room_1",
            thread_id=None,
            is_agent_chain=True,
            agent_chain_context="\n---\n[Agent chain: turn 1/5]",
        )

        self.assertTrue(terminated)
        connector.send_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestAScheduledTurnThatFallsSilentIsLoud(unittest.IsolatedAsyncioTestCase):
    """A scheduled job fired, the agent ran, and answered with the termination
    token alone. The room gets nothing, `run_count` advances, `schedule list`
    says healthy — and until this WARNING the only trace was an INFO line an
    operator had to correlate with the fire by timestamp. Measured on a live
    deployment: 25 consecutive runs, zero posts, one confused owner.
    """

    def _run(self, runner, **kw):
        return runner.run_turn(
            session_id="ses_001", prompt="🖥️ Computer Part of the Minute: CPU Cooler",
            working_directory="/tmp", room_id="room_1", thread_id=None, **kw,
        )

    async def test_a_token_only_reply_to_a_scheduled_prompt_warns_and_posts_nothing(self):
        from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN

        runner, connector = _make_runner(_MockAgent(AgentResponse(text=AGENT_CHAIN_TERMINATION_TOKEN)))

        with self.assertLogs("coop.core.turn_runner", "WARNING") as logs:
            terminated = await self._run(runner, is_scheduled=True)

        self.assertFalse(terminated, "not an agent chain — the caller's accounting is unchanged")
        connector.send_text.assert_not_called()
        joined = "\n".join(logs.output)
        self.assertIn("room_1", joined)
        self.assertIn("produced no reply", joined)
        self.assertIn("reply is what gets posted", joined, "says what to do about it")

    async def test_the_same_reply_to_an_ordinary_message_stays_at_info(self):
        """Silence in answer to a person or a broadcast is a legitimate choice
        the routing rules ask for; only a scheduled prompt makes it suspect."""
        import logging

        from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN

        runner, connector = _make_runner(_MockAgent(AgentResponse(text=AGENT_CHAIN_TERMINATION_TOKEN)))

        with self.assertLogs("coop.core.turn_runner", "INFO") as logs:
            await self._run(runner, is_scheduled=False)

        self.assertFalse([r for r in logs.records if r.levelno >= logging.WARNING], logs.output)
        connector.send_text.assert_not_called()

    async def test_a_scheduled_reply_with_content_is_delivered_without_a_warning(self):
        """The fence is on SILENCE, not on the token: a reply that says something
        and then terminates posts the something, as any reply does."""
        import logging

        from gateway.core.agent_chain import AGENT_CHAIN_TERMINATION_TOKEN

        runner, connector = _make_runner(
            _MockAgent(AgentResponse(text=f"CPU cooler: keeps the chip alive.\n\n{AGENT_CHAIN_TERMINATION_TOKEN}")))

        with self.assertLogs("coop.core.turn_runner", "INFO") as logs:
            await self._run(runner, is_scheduled=True)

        self.assertFalse([r for r in logs.records if r.levelno >= logging.WARNING], logs.output)
        connector.send_text.assert_called_once()
        self.assertNotIn("end-of-agent-chain", connector.send_text.call_args.args[1].text)

