"""AgentTurnRunner: executes one agent turn — send, handle response, post reply.

Extracted from MessageProcessor._process() so the processor is focused on queue
orchestration and session-map bookkeeping, while the turn runner owns:
  - Typing indicator bracket (on before send, off after)
  - Agent invocation with configured timeout
  - Usage logging
  - Response posting
  - Timeout / error handling with user-facing messages
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from ..agents import AgentBackend
from ..agents.errors import (
    AgentPermissionError,
    AgentRateLimitedError,
    AgentUnavailableError,
)
from ..agents.response import AgentResponse
from .agent_chain import AGENT_CHAIN_TERMINATION_TOKEN
from .config import CoreConfig
from .connector import Connector

logger = logging.getLogger("coop.core.turn_runner")


def _user_facing_agent_error_message(exc: Exception, session_id: str = "") -> str:
    """Return a production-safe chat message for agent execution failures.

    Keep detailed diagnostics in logs, but avoid leaking backend internals,
    local paths, HTTP details, or raw CLI errors back into the chat room.
    """
    ref_suffix = f" (ref: {session_id[:8]})" if session_id else ""
    if isinstance(exc, AgentRateLimitedError):
        return (
            "❌ Agent is temporarily unavailable due to a usage limit. "
            f"Please try again later.{ref_suffix}"
        )
    if isinstance(exc, AgentPermissionError):
        return (
            "❌ Agent could not complete the request because of a permission restriction."
            f"{ref_suffix}"
        )
    if isinstance(exc, AgentUnavailableError):
        return (
            f"❌ Agent is temporarily unavailable. Please try again later.{ref_suffix}"
        )
    return f"❌ Agent failed to process the request. Please try again.{ref_suffix}"


class AgentTurnRunner:
    """Runs a single agent turn: prompt → agent → reply (or error message).

    Stateless per-turn — the same runner instance is reused across multiple
    turns within a single MessageProcessor.
    """

    def __init__(
        self,
        agent: AgentBackend,
        connector: Connector,
        config: CoreConfig,
        agent_name: str = "",
        room_name: str = "",
    ) -> None:
        self._agent = agent
        self._connector = connector
        self._config = config
        self._agent_name = agent_name
        self._room_name = room_name

    async def run_turn(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        room_id: str,
        thread_id: str | None,
        file_paths: list[str] | None = None,
        role_env: dict[str, str] | None = None,
        is_agent_chain: bool = False,
        agent_chain_context: str = "",
        append_system_prompt_file: str | None = None,
        is_scheduled: bool = False,
    ) -> bool:
        """Execute one turn: send prompt to agent, post response (or error) to room.

        Two separate error boundaries:
          - **Stage 1** (agent execution): agent.stream() failure produces an error
            response object but does not touch the connector.
          - **Stage 2** (delivery): connector.send_text() failure is logged
            distinctly and does NOT attempt to send another error message through
            the same potentially broken transport (avoids recursive error loops).

        Typing indicators bracket both stages regardless of outcome.
        Intermediate events from agent.stream() are forwarded to
        connector.notify_agent_event() on a best-effort basis — errors there
        are silently swallowed and never abort the turn.

        When ``is_agent_chain=True`` and ``agent_chain_context`` is set, the
        context suffix is appended to the prompt before invoking the agent.
        If the agent responds with the termination token, the response is NOT
        delivered to the room and ``True`` is returned (terminated).

        Returns:
            True  — agent chain self-terminated (response was suppressed).
            False — normal delivery (or error delivery) occurred.
        """
        full_prompt = prompt
        if is_agent_chain and agent_chain_context:
            full_prompt = prompt + agent_chain_context

        await self._notify_typing(room_id, True)
        try:
            # Stage 1: execute agent turn (may emit intermediate events)
            response = await self._execute_agent(
                session_id,
                full_prompt,
                working_directory,
                room_id,
                thread_id,
                file_paths,
                role_env,
                append_system_prompt_file,
            )

            # Strip the termination token from agent responses — case-insensitive
            # substring match so LLM output variations like <END-OF-AGENT-CHAIN>
            # or the token embedded in surrounding text are still caught.
            #
            # The token may appear in both agent-chain and user-to-agent turns
            # (the LLM can output it regardless of context), so always strip it.
            #
            # Content on EITHER side of the token is preserved, e.g.:
            #   "Summary: XYZ\n\n<end-of-agent-chain>"  → deliver "Summary: XYZ"
            #   "<end-of-agent-chain>\nBye now"          → deliver "Bye now"
            #   "Pre\n<end-of-agent-chain>\nPost"        → deliver "Pre\n\nPost"
            # Suppressing the entire response would silently discard the agent's
            # last message — the same failure mode as the timeout bug.
            token_idx = response.text.lower().find(AGENT_CHAIN_TERMINATION_TOKEN)
            if token_idx != -1:
                pre_token_text = response.text[:token_idx].rstrip()
                token_end_idx = token_idx + len(AGENT_CHAIN_TERMINATION_TOKEN)
                post_token_text = response.text[token_end_idx:].strip()
                content_parts = [p for p in (pre_token_text, post_token_text) if p]
                stripped_text = "\n\n".join(content_parts)
                if is_agent_chain:
                    if stripped_text:
                        await self._deliver_response(
                            room_id,
                            dataclasses.replace(response, text=stripped_text),
                            thread_id,
                        )
                    logger.info(
                        "Agent chain self-terminated (session=%s room=%s sender turn suppressed)",
                        session_id[:8],
                        room_id,
                    )
                    return True
                elif is_scheduled and not stripped_text:
                    # The one silence that is almost never intended. A scheduled
                    # job fired, the agent ran a turn, and answered with the
                    # token alone — so the room gets nothing, `run_count`
                    # advances, and the job reads as healthy in `schedule list`.
                    # An operator staring at an empty channel had to correlate
                    # this INFO line with the fire by timestamp to find out.
                    logger.warning(
                        "Scheduled message in room %s produced no reply: the "
                        "agent answered with only <end-of-agent-chain> "
                        "(session=%s). The job's message is a prompt to the "
                        "agent and its reply is what gets posted — if the "
                        "message reads as a finished announcement, the agent "
                        "has nothing to add and stays silent.",
                        room_id, session_id[:8],
                    )
                    return False
                else:
                    # Not an agent chain — strip the token and deliver remaining content
                    logger.info(
                        "Stripped <end-of-agent-chain> token from non-chain response "
                        "(session=%s room=%s)",
                        session_id[:8],
                        room_id,
                    )
                    if stripped_text:
                        await self._deliver_response(
                            room_id,
                            dataclasses.replace(response, text=stripped_text),
                            thread_id,
                        )
                    return False

            # Stage 2: deliver response to chat room
            await self._deliver_response(room_id, response, thread_id)
            return False
        finally:
            await self._notify_typing(room_id, False)

    async def _execute_agent(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        room_id: str,
        thread_id: str | None,
        file_paths: list[str] | None,
        role_env: dict[str, str] | None,
        append_system_prompt_file: str | None = None,
    ) -> AgentResponse:
        """Iterate agent.stream() and return the final AgentResponse.

        Intermediate events are forwarded to connector.notify_agent_event() on a
        best-effort basis.  Exceptions from the agent are caught here and
        converted to error AgentResponse objects so the delivery stage always
        has something to post.
        """
        try:
            async for event in self._agent.stream(
                session_id=session_id,
                prompt=prompt,
                working_directory=working_directory,
                timeout=self._config.timeout_for(self._agent_name),
                attachments=file_paths,
                env=role_env,
                append_system_prompt_file=append_system_prompt_file,
            ):
                if event.kind == "final":
                    response = event.response or AgentResponse(
                        text="(empty response)", is_error=True
                    )
                    if response.usage:
                        logger.info(
                            "Agent usage [%s] in=%d out=%d cache_read=%d cost=%s",
                            self._room_name,
                            response.usage.input_tokens,
                            response.usage.output_tokens,
                            response.usage.cache_read_tokens,
                            f"${response.cost_usd:.4f}" if response.cost_usd else "n/a",
                        )
                    return response
                # Intermediate event — notify connector (best-effort, never aborts)
                try:
                    await self._connector.notify_agent_event(
                        room_id, event, thread_id=thread_id
                    )
                except Exception as notify_err:
                    logger.debug(
                        "notify_agent_event error (ignored): %s", notify_err
                    )

            # stream() ended without a final event — should not happen in practice
            logger.error(
                "Agent stream ended without a final event (session=%s)",
                session_id[:8],
            )
            return AgentResponse(
                text="❌ Agent response was empty. Please try again.",
                is_error=True,
            )
        except asyncio.TimeoutError:
            logger.error("Agent timed out for message: %s", prompt[:80])
            return AgentResponse(
                text="⏱️ Request timed out. Please try again.",
                is_error=True,
            )
        except Exception as e:
            logger.exception(
                "Agent error (session=%s room=%s): %s",
                session_id[:8],
                self._room_name,
                e,
            )
            return AgentResponse(
                text=_user_facing_agent_error_message(e, session_id=session_id),
                is_error=True,
            )

    async def _deliver_response(
        self,
        room_id: str,
        response: AgentResponse,
        thread_id: str | None,
    ) -> None:
        """Post an AgentResponse to the chat room.

        Connector delivery failures are logged with connector-specific context
        and do NOT attempt to send another error message through the same
        potentially broken transport — this prevents recursive error loops.
        """
        try:
            await self._connector.send_text(room_id, response, thread_id=thread_id)
        except Exception as e:
            logger.error(
                "Failed to deliver response to room %s: %s: %s (response text was: %s)",
                room_id,
                type(e).__name__,
                e,
                response.text[:100],
            )

    async def _notify_typing(self, room_id: str, is_typing: bool) -> None:
        try:
            await self._connector.notify_typing(room_id, is_typing)
        except Exception as e:
            logger.debug("Failed to send typing notification: %s", e)
