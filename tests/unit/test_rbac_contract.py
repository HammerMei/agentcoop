"""Tests for RBAC contract unification (P2-5).

Covers:
  - supports_per_message_env default is True on AgentBackend
  - OpenCodeBackend returns False
  - MessageProcessor skips env_for_role when backend returns False
  - MessageProcessor passes env when backend returns True
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.agents import AgentBackend
from gateway.agents.response import AgentResponse
from gateway.config import AgentConfig
from gateway.core.config import CoreConfig
from gateway.core.connector import IncomingMessage, Room, User, UserRole
from gateway.core.message_processor import MessageProcessor

# ── Helpers ────────────────────────────────────────────────────────────────────

class _EnvAwareBackend(AgentBackend):
    """Backend that records the env passed to send()."""

    def __init__(self, supports_env: bool = True):
        self._supports_env = supports_env
        self.last_env: dict | None = None

    @property
    def supports_per_message_env(self) -> bool:
        return self._supports_env

    async def create_session(self, working_directory, extra_args=None, session_title=None):
        return "ses_001"

    async def send(self, session_id, prompt, working_directory, timeout,
                   attachments=None, env=None, append_system_prompt_file=None):
        self.last_env = env
        return AgentResponse(text="ok")


def _make_processor(agent: AgentBackend) -> MessageProcessor:
    agent_cfg = AgentConfig(timeout=10)
    config = CoreConfig(agents={"default": agent_cfg})
    connector = MagicMock()
    connector.send_text = AsyncMock()
    connector.format_prompt_prefix = MagicMock(return_value="")
    connector.notify_typing = AsyncMock()
    room = Room(id="room_1", name="test-room")
    return MessageProcessor(
        session_id="ses_001",
        room=room,
        working_directory="/tmp",
        watcher_id="test-watcher",
        connector=connector,
        agent=agent,
        config=config,
        agent_name="default",
    )


def _make_msg(role: UserRole = UserRole.OWNER) -> IncomingMessage:
    return IncomingMessage(
        id="msg_001",
        timestamp="2026-01-01T00:00:00Z",
        room=Room(id="room_1", name="test-room"),
        sender=User(id="u1", username="testuser"),
        text="hello",
        role=role,
    )


# ── Capability flag ──────────────────────────────────────────────────────────

class TestSupportsPerMessageEnv(unittest.TestCase):
    """Backend capability flag defaults and overrides."""

    def test_base_class_default_is_true(self):
        class StubBackend(AgentBackend):
            async def create_session(self, *a, **kw): return "s"
            async def send(self, *a, **kw): return AgentResponse(text="ok")
        self.assertTrue(StubBackend().supports_per_message_env)

    def test_opencode_backend_returns_false(self):
        from gateway.agents.opencode.adapter import OpenCodeBackend
        backend = OpenCodeBackend(
            command="opencode", new_session_args=[], timeout=10,
        )
        self.assertFalse(backend.supports_per_message_env)

    def test_claude_backend_returns_true(self):
        from gateway.agents.claude.adapter import ClaudeBackend
        backend = ClaudeBackend(
            command="claude", new_session_args=[], timeout=10,
        )
        self.assertTrue(backend.supports_per_message_env)


# ── MessageProcessor env behaviour ───────────────────────────────────────────

class TestProcessorEnvBehaviour(unittest.IsolatedAsyncioTestCase):
    """MessageProcessor respects the backend's supports_per_message_env flag."""

    async def test_env_passed_when_backend_supports_it(self):
        """Default backend (supports_per_message_env=True) gets COOP_ROLE in env."""
        agent = _EnvAwareBackend(supports_env=True)
        proc = _make_processor(agent)
        proc.start()
        try:
            await proc.enqueue(_make_msg(UserRole.OWNER))
            # Give the consumer loop a chance to process
            await asyncio.sleep(0.05)
        finally:
            await proc.stop()

        self.assertIsNotNone(agent.last_env)
        self.assertEqual(agent.last_env.get("COOP_ROLE"), "owner")

    async def test_env_none_when_backend_does_not_support_it(self):
        """Backend with supports_per_message_env=False gets env=None (no COOP_ROLE)."""
        agent = _EnvAwareBackend(supports_env=False)
        proc = _make_processor(agent)
        proc.start()
        try:
            await proc.enqueue(_make_msg(UserRole.OWNER))
            await asyncio.sleep(0.05)
        finally:
            await proc.stop()

        self.assertIsNone(agent.last_env)

    async def test_guest_role_env_when_supported(self):
        """Guest messages get COOP_ROLE=guest when backend supports env."""
        agent = _EnvAwareBackend(supports_env=True)
        proc = _make_processor(agent)
        proc.start()
        try:
            await proc.enqueue(_make_msg(UserRole.GUEST))
            await asyncio.sleep(0.05)
        finally:
            await proc.stop()

        self.assertIsNotNone(agent.last_env)
        self.assertEqual(agent.last_env.get("COOP_ROLE"), "guest")


if __name__ == "__main__":
    unittest.main()
