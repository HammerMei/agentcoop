"""MessageProcessor: per-room message queue and session bookkeeping.

Platform-agnostic replacement for the old gateway/watcher.py.

Responsibilities after decomposition:
  - Queue orchestration (enqueue / consumer loop)
  - Lifecycle (start / stop / _stopping gate)
  - Session-map updates (role, permission thread)
  - Anonymous user rejection

Delegated to extracted collaborators:
  - Prompt construction       → :func:`prompt_builder.build_prompt`
  - Attachment path remapping → :func:`attachment_workspace.localize_attachment_paths`
  - Agent turn execution      → :class:`agent_turn_runner.AgentTurnRunner`
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING

from ..agents import AgentBackend
from ..agents.response import AgentResponse
from .agent_chain import build_agent_chain_context
from .agent_turn_runner import AgentTurnRunner, _user_facing_agent_error_message
from .attachment_workspace import localize_attachment_paths
from .config import CoreConfig, WatcherConfig
from .connector import Attachment, Connector, IncomingMessage, Room, UserRole, is_scheduled_message
from .injected_context_builder import InjectedContextBuilder
from .paths import watcher_prompt_key
from .prompt_builder import build_catchup_prompt, build_prompt
from .session_maps import SessionMaps
from .state import WatcherState, now_iso

if TYPE_CHECKING:
    from .permission import PermissionRegistry

logger = logging.getLogger("coop.core.processor")

# Sentinel placed on the queue by stop() to wake a blocked consumer.
_DRAIN_SENTINEL = object()


class MessageProcessor:
    """Per-room message queue: dequeues IncomingMessages, runs the agent, posts replies.

    One MessageProcessor is created per watched room/session pair.
    The Connector fires IncomingMessage objects into enqueue(); the processor
    serializes them and calls AgentBackend.send() one at a time.

    The processor knows nothing about:
      - Which platform the message came from
      - How to parse platform message formats
      - How to download attachments
      - How to resolve user roles
    All of that is the Connector's responsibility.
    """

    def __init__(
        self,
        session_id: str,
        room: Room,
        working_directory: str,
        watcher_id: str,
        connector: Connector,
        agent: AgentBackend,
        config: CoreConfig,
        agent_name: str = "",
        permission_registry: "PermissionRegistry | None" = None,
        session_role_map: dict[str, str] | None = None,
        session_permission_thread_map: "dict[str, str | None] | None" = None,
        session_maps: SessionMaps | None = None,
        context_injector: InjectedContextBuilder | None = None,
        watcher_state: WatcherState | None = None,
        watcher_config: WatcherConfig | None = None,
        connector_name: str = "",
        attachment_local_base: str | None = None,
        append_system_prompt_file: str | None = None,
    ) -> None:
        self._session_id = session_id
        self._room = room
        self._working_directory = working_directory
        self._watcher_id = watcher_id
        self._connector = connector
        self._agent = agent
        self._config = config
        self._agent_name = agent_name
        self._permission_registry = permission_registry
        self._session_role_map = session_role_map
        self._session_permission_thread_map = session_permission_thread_map
        self._session_maps = session_maps
        self._context_injector = context_injector
        self._watcher_state = watcher_state
        self._watcher_config = watcher_config
        self._connector_name = connector_name
        self._attachment_local_base = attachment_local_base
        # Path to re-supply on every turn via AgentBackend.send()/.stream()'s
        # append_system_prompt_file kwarg (e.g. Claude's
        # --append-system-prompt-file). None for backends with no such
        # mechanism (see AgentBackend.ensure_durable_instructions()).
        self._append_system_prompt_file = append_system_prompt_file

        self._turn_runner = AgentTurnRunner(
            agent=agent,
            connector=connector,
            config=config,
            agent_name=agent_name,
            room_name=room.name,
        )

        self._queue: asyncio.Queue[IncomingMessage | object] = asyncio.Queue(
            maxsize=self._config.max_queue_depth or 0
        )
        self._task: asyncio.Task | None = None
        # Track short-lived fire-and-forget tasks (e.g. queue-full notifications)
        # so they can be awaited/cancelled during stop() and don't float free.
        self._background_tasks: set[asyncio.Task] = set()

        # Processor lifecycle state machine:
        #   running  → enqueue() accepts, consumer loop runs normally
        #   draining → enqueue() rejects, consumer finishes queued messages then exits
        #   stopped  → everything halted
        self._state: str = "running"  # "running" | "draining" | "stopped"
        # True while the consumer loop is inside a turn — see has_work_in_flight.
        self._turn_in_flight: bool = False
        # Event set when the consumer finishes draining (or is forced to stop).
        self._drained = asyncio.Event()
        # Cooldown for queue-full notifications to prevent spam storms.
        self._last_queue_full_notify: float = 0.0
        self._queue_full_cooldown: float = 30.0  # seconds between notifications

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the consumer task."""
        self._task = asyncio.create_task(
            self._run(), name=f"processor-{self._watcher_id[:8]}"
        )
        logger.info(
            "MessageProcessor started: watcher=%s room=%s session=%s agent=%s cwd=%s",
            self._watcher_id[:8],
            self._room.name,
            self._session_id[:8],
            self._agent_name,
            self._working_directory,
        )

    async def stop(self, drain_timeout: float = 30.0) -> None:
        """Gracefully stop the processor: drain queued messages, then shut down.

        Stop protocol:
          1. Transition to ``draining`` — ``enqueue()`` immediately rejects new messages.
          2. Cancel the online-notification task (fire-and-forget startup side effect).
          3. Wait for the consumer to finish processing all already-queued messages
             (up to ``drain_timeout`` seconds).  If the timeout expires, the consumer
             task is force-cancelled — queued messages may be lost in this case.
          4. Cancel and await any in-flight background tasks.
          5. Transition to ``stopped``.

        Draining matters for not losing
        queued work — but note it does **not** affect the persisted watermark.
        Both connectors advance their watermark when a message is *accepted into
        the queue*, not when it is handled, so draining never moves it.
        ``_stop_processor()`` therefore captures it before unsubscribing, which
        is the only point where the connector still holds the room entry it
        lives in.

        Args:
            drain_timeout: Maximum seconds to wait for the queue to drain.
                Use 0 for immediate cancellation (force stop).
        """
        # Guard against double-stop (e.g., concurrent callers or accidental re-entry).
        if self._state != "running":
            return

        # Phase 1: stop accepting new messages.
        self._state = "draining"


        # Phase 2: wake the consumer (if blocked on empty queue) and wait for drain.
        if self._task:
            # Place a sentinel to unblock a consumer waiting on queue.get().
            try:
                self._queue.put_nowait(_DRAIN_SENTINEL)
            except asyncio.QueueFull:
                pass  # queue is full — consumer will see draining state after each message
            try:
                await asyncio.wait_for(self._drained.wait(), timeout=drain_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "Drain timeout (%.1fs) for watcher '%s' — force-cancelling consumer "
                    "(%d messages may be lost)",
                    drain_timeout,
                    self._watcher_id[:8],
                    self._queue.qsize(),
                )
            # Cancel the task (no-op if it already exited from drain completion).
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Phase 3: clean up background tasks.
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        self._state = "stopped"
        logger.info("MessageProcessor stopped: watcher=%s", self._watcher_id[:8])

    @property
    def watcher_id(self) -> str:
        """Which watcher this processor belongs to.

        Public because the dispatcher has to tell "this watcher is registering again"
        from "a second watcher wants the same room" (§4.1), and identity of the
        *object* cannot: a restart builds a new processor for the same watcher.
        """
        return self._watcher_id

    @property
    def is_accepting(self) -> bool:
        """True if the processor is running and has queue capacity."""
        return self._state == "running" and not self._queue.full()

    @property
    def has_work_in_flight(self) -> bool:
        """A turn is running or messages are queued — accepted work not yet done.

        The idle sweep's busy gate (§2.5): a room must not be dropped
        mid-conversation, and `last_activity_at` alone cannot say so — it is
        stamped at *acceptance*, so a turn still running reads as old activity.
        The pending-permission half of that gate lives with the sweep, which
        holds the registry; this property answers only for the processor.
        """
        return self._turn_in_flight or not self._queue.empty()

    # ── Inbound ───────────────────────────────────────────────────────────────

    async def enqueue(self, msg: IncomingMessage) -> bool:
        """Called by MessageDispatcher when a message arrives.

        Permission commands (approve/deny) are already intercepted at the
        Dispatcher level before routing, so this method only handles
        normal message queueing.

        Returns:
            True if the message was successfully queued for processing.
            False if the queue was full and the message was dropped — the caller
            must NOT advance the dedup watermark in this case, so the message
            can be re-delivered on reconnect.
        """
        if self._state != "running":
            logger.warning(
                "enqueue() on stopping processor for room '%s' — rejecting message from %s",
                self._room.name,
                msg.sender.username,
            )
            return False
        try:
            self._queue.put_nowait(msg)
            # The idle clock (§2.5), advanced on *accepted* work only — a refused
            # message is not activity, and counting it would keep a room the
            # gateway is dropping messages for alive forever. The clock's one
            # ADVANCING write site: inbound and scheduled injection both funnel
            # through this method, so the two cannot drift. (Creation initializes
            # the field — a birth stamp, not an advance — and recreation carries
            # it untouched: a restart is not activity.) In memory only, persisted
            # at the existing save points — a per-message disk write is the cost
            # §2.2 explicitly rejects — and a crash that loses the advance idles
            # the room *early*, which is the safe direction: its next message
            # wakes it and the same session resumes.
            if self._watcher_state is not None:
                self._watcher_state.last_activity_at = now_iso()
            return True
        except asyncio.QueueFull:
            logger.warning(
                "Queue full (depth=%d) for room '%s' — dropping message from %s",
                self._queue.maxsize,
                self._room.name,
                msg.sender.username,
            )
            # Rate-limit queue-full notifications to prevent spam storms under
            # bursty load.  Only the first drop in each cooldown window triggers
            # a user-visible message; subsequent drops are silently logged above.
            now = asyncio.get_running_loop().time()
            if now - self._last_queue_full_notify >= self._queue_full_cooldown:
                self._last_queue_full_notify = now
                task = asyncio.create_task(
                    self._connector.send_text(
                        msg.room.id,
                        AgentResponse(
                            text="⚠️ Server busy — your message was dropped. Please retry.",
                            is_error=True,
                        ),
                        thread_id=msg.thread_id,
                    ),
                    name=f"queue-full-notify-{self._room.id[:8]}",
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            return False

    # ── Consumer loop ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Consumer loop: process messages one at a time until drained or cancelled.

        In ``running`` state, blocks on ``queue.get()`` indefinitely.
        When ``stop()`` transitions to ``draining``, it places a
        ``_DRAIN_SENTINEL`` on the queue to wake the consumer.  The consumer
        then finishes all remaining real messages and exits, signalling
        ``_drained`` so ``stop()`` can proceed.
        """
        try:
            while True:
                msg = await self._queue.get()
                if msg is _DRAIN_SENTINEL:
                    # Drain any remaining real messages that were enqueued
                    # before the sentinel.
                    while not self._queue.empty():
                        remaining = self._queue.get_nowait()
                        if remaining is not _DRAIN_SENTINEL:
                            try:
                                await self._process(remaining)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                logger.exception("Unhandled error in processor loop")
                    break  # drain complete
                # Atomically drain any additional pending messages to build a
                # catch-up batch.  No await between get_nowait() calls so no
                # new messages can be interleaved during collection.
                batch: list[IncomingMessage] = [msg]
                while True:
                    try:
                        extra = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if extra is _DRAIN_SENTINEL:
                        # Sentinel consumed — process whatever batch we have,
                        # then exit.  The state==draining check below will
                        # catch the empty-queue condition.
                        break
                    batch.append(extra)

                self._turn_in_flight = True
                try:
                    if len(batch) == 1:
                        await self._process(batch[0])
                    else:
                        await self._process_batch(batch)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Unhandled error in processor loop")
                finally:
                    self._turn_in_flight = False
                # After each turn, check if we should exit (drain mode + empty).
                if self._state == "draining" and self._queue.empty():
                    break
        finally:
            self._drained.set()

    async def _process(self, msg: IncomingMessage) -> None:
        """Process a single IncomingMessage: build prompt, call agent, send reply.

        Orchestrates the extracted collaborators:
          1. Reject anonymous users (security gate)
          2. Localize attachment paths (AttachmentLocalizer)
          3. Build prompt (PromptBuilder)
          4. Update session maps (permission broker bookkeeping)
          5. Run agent turn (AgentTurnRunner)
        """
        if msg.role == UserRole.ANONYMOUS:
            logger.warning(
                "Dropping message from ANONYMOUS user in session %s — "
                "anonymous users are not permitted",
                self._session_id,
            )
            return

        file_paths = localize_attachment_paths(
            msg.attachments, self._attachment_local_base
        )

        try:
            await self._ensure_context_injected()
        except Exception as e:
            logger.exception(
                "Durable context retry failed during message processing "
                "(session=%s room=%s): %s",
                self._session_id[:8],
                self._room.name,
                e,
            )
            await self._connector.send_text(
                msg.room.id,
                AgentResponse(
                    text=_user_facing_agent_error_message(
                        e, session_id=self._session_id
                    ),
                    is_error=True,
                ),
                thread_id=msg.thread_id,
            )
            return

        prefix = self._connector.format_prompt_prefix(msg)
        prompt = build_prompt(msg.text, prefix, msg.warnings)

        logger.info("Processing [%s] %s", self._room.name, msg.text[:120])

        # Update session maps BEFORE the agent turn so the permission broker
        # sees the correct role and thread context if a tool-use fires mid-turn.
        if self._session_role_map is not None:
            if self._session_maps is not None:
                self._session_maps.update_role(self._session_id, msg.role.value)
            else:
                self._session_role_map[self._session_id] = msg.role.value
        if self._session_permission_thread_map is not None:
            permission_thread_id = msg.extra_context.get("permission_thread_id")
            if self._session_maps is not None:
                self._session_maps.update_permission_thread(
                    self._session_id,
                    permission_thread_id,
                )
            else:
                self._session_permission_thread_map[self._session_id] = (
                    permission_thread_id
                )

        role_env: dict[str, str] | None = None
        if self._agent.supports_per_message_env:
            role_env = self._config.env_for_role(msg.role, self._agent_name)

        # Agent chain: inject toll-call context if this is an agent-to-agent message
        is_agent_chain = msg.extra_context.get("is_agent_chain", False)
        agent_chain_context = ""
        if is_agent_chain:
            agent_chain_turn = msg.extra_context.get("agent_chain_turn", 1)
            agent_chain_max_turns = msg.extra_context.get("agent_chain_max_turns", 5)
            agent_chain_context = build_agent_chain_context(agent_chain_turn, agent_chain_max_turns)

        await self._turn_runner.run_turn(
            session_id=self._session_id,
            prompt=prompt,
            working_directory=self._working_directory,
            room_id=msg.room.id,
            thread_id=msg.thread_id,
            file_paths=file_paths or None,
            role_env=role_env,
            is_agent_chain=is_agent_chain,
            agent_chain_context=agent_chain_context,
            append_system_prompt_file=self._append_system_prompt_file,
            is_scheduled=is_scheduled_message(msg),
        )

    async def _process_batch(self, batch: list[IncomingMessage]) -> None:
        """Process a batch of IncomingMessages as a single catch-up turn.

        Called when the queue had >1 messages pending after the previous turn.
        The last non-anonymous message is the anchor (the agent responds to it);
        all preceding non-anonymous messages form the [CATCH-UP] history header.

        Design decisions:
          - ANONYMOUS messages are dropped (same gate as _process()).
          - Session maps, role_env, thread_id: taken from anchor.
          - Attachments: aggregated from all messages, dedup by local path.
          - Warnings: aggregated from all messages, surfaced on the anchor prompt.
          - History lines carry no warnings (context only).
          - Turn budget over-counting: agent-chain counters are incremented once
            per enqueued message (in filter_rc_message), so a batch of N agent
            messages charges N turns. This is a known limitation; will be
            addressed in a follow-up if observed to cause premature budget
            exhaustion in practice.
        """
        # Security gate: drop anonymous messages (same as _process).
        allowed = [m for m in batch if m.role != UserRole.ANONYMOUS]
        if not allowed:
            logger.warning(
                "Entire batch of %d messages contained only ANONYMOUS users "
                "— dropping (session=%s room=%s)",
                len(batch),
                self._session_id[:8],
                self._room.name,
            )
            return

        anchor = allowed[-1]
        history_msgs = allowed[:-1]

        try:
            await self._ensure_context_injected()
        except Exception as e:
            logger.exception(
                "Durable context retry failed during batch processing "
                "(session=%s room=%s): %s",
                self._session_id[:8],
                self._room.name,
                e,
            )
            await self._connector.send_text(
                anchor.room.id,
                AgentResponse(
                    text=_user_facing_agent_error_message(e, session_id=self._session_id),
                    is_error=True,
                ),
                thread_id=anchor.thread_id,
            )
            return

        # Aggregate attachments from all messages; dedup by local path.
        all_attachments: list[Attachment] = []
        seen_paths: set[str] = set()
        for m in allowed:
            for att in m.attachments:
                if att.local_path not in seen_paths:
                    all_attachments.append(att)
                    seen_paths.add(att.local_path)

        file_paths = localize_attachment_paths(all_attachments, self._attachment_local_base)

        # Aggregate warnings from all messages; surfaced on the anchor prompt.
        all_warnings = [w for m in allowed for w in m.warnings] or None

        # Build history lines using the same format_prompt_prefix as normal
        # messages — timestamps and addressing are already embedded.
        # No warnings on history lines (context only).
        history_lines = [
            build_prompt(m.text, self._connector.format_prompt_prefix(m))
            for m in history_msgs
        ]

        anchor_prefix = self._connector.format_prompt_prefix(anchor)
        anchor_prompt = build_prompt(anchor.text, anchor_prefix, all_warnings)
        prompt = build_catchup_prompt(history_lines, anchor_prompt)

        logger.info(
            "Processing batch [%s] %d messages (history=%d), anchor: %s",
            self._room.name,
            len(batch),
            len(history_msgs),
            anchor.text[:120],
        )

        # Update session maps from anchor (same logic as _process).
        if self._session_role_map is not None:
            if self._session_maps is not None:
                self._session_maps.update_role(self._session_id, anchor.role.value)
            else:
                self._session_role_map[self._session_id] = anchor.role.value
        if self._session_permission_thread_map is not None:
            permission_thread_id = anchor.extra_context.get("permission_thread_id")
            if self._session_maps is not None:
                self._session_maps.update_permission_thread(
                    self._session_id, permission_thread_id,
                )
            else:
                self._session_permission_thread_map[self._session_id] = permission_thread_id

        role_env: dict[str, str] | None = None
        if self._agent.supports_per_message_env:
            role_env = self._config.env_for_role(anchor.role, self._agent_name)

        is_agent_chain = anchor.extra_context.get("is_agent_chain", False)
        agent_chain_context = ""
        if is_agent_chain:
            agent_chain_turn = anchor.extra_context.get("agent_chain_turn", 1)
            agent_chain_max_turns = anchor.extra_context.get("agent_chain_max_turns", 5)
            agent_chain_context = build_agent_chain_context(agent_chain_turn, agent_chain_max_turns)

        # If messages in the batch span multiple threads, the response is sent
        # to the anchor's thread_id (the last non-anonymous message's thread).
        # This is intentional: the agent's reply goes to the most-recent context,
        # not scattered across every thread in the batch.
        await self._turn_runner.run_turn(
            session_id=self._session_id,
            prompt=prompt,
            working_directory=self._working_directory,
            room_id=anchor.room.id,
            thread_id=anchor.thread_id,
            file_paths=file_paths or None,
            role_env=role_env,
            is_agent_chain=is_agent_chain,
            agent_chain_context=agent_chain_context,
            append_system_prompt_file=self._append_system_prompt_file,
            is_scheduled=is_scheduled_message(anchor),
        )

    async def rename(self, handle: str, *, room_name: str) -> None:
        """The room was renamed; take the new handle and re-issue the identity header.

        The handle is display data, but it is display data the AGENT reads: the
        "Coop Session Identity" block names the watcher and the room (from
        `_watcher_config`), and it is handed to the backend on every turn, so a
        stale one sends the agent's own `schedule create` at a name that no
        longer resolves — or, once the platform reuses it, at another room
        (Codex, PR #140). The processor's own `watcher_id` is the room id and
        does not move. The durable file
        is rewritten under the same room-keyed path (`watcher_prompt_key` no
        longer includes the handle), so the path the session was started with
        stays valid and the next turn reads the new content.

        Raises whatever the backend raises; the lifecycle logs and moves on —
        the processor's own context-injection retry re-issues the header later.
        """
        # NOT `self._watcher_id`: that is the processor's identity, and it is the
        # ROOM ID (`_start_watcher` passes `watcher_id=room.id`) — the dispatcher
        # compares it to decide whether a replacement processor is the same
        # watcher's. Overwriting it with the handle made the next restart of
        # this room's processor look like another watcher's claim and be
        # refused. The handle lives in `_watcher_config.name`, which is what the
        # identity header reads.
        self._room = Room(id=self._room.id, name=room_name, type=self._room.type)
        if self._watcher_config is not None:
            self._watcher_config = dataclasses.replace(
                self._watcher_config, name=handle, room=room_name)
        if self._context_injector is None or self._watcher_config is None:
            return
        content = await self._context_injector.build(
            self._agent_name,
            self._connector_name,
            self._watcher_config,
            agent_username=self._connector.agent_username,
        )
        try:
            to_repeat = await self._agent.ensure_durable_instructions(
                self._session_id,
                self._working_directory,
                self._config.timeout_for(self._agent_name),
                content,
                path_key=watcher_prompt_key(self._connector_name, self._room.id),
                # A rename IS a re-delivery: the agent must learn the new handle,
                # so a backend that delivers by sending (OpenCode) sends again.
                # Claude rewrites the file regardless. Required keyword — the
                # first version omitted it, every rename raised TypeError into
                # the lifecycle's catch, and the header never changed (Codex,
                # PR #140).
                already_delivered=False,
            )
        except Exception:
            # Arm the retry `_ensure_context_injected` runs on the next message:
            # it returns at once while `context_injected` is set and the
            # injector's status says done, so without this the old header
            # would stand for the watcher's lifetime while the log promised a
            # retry (Codex, PR #140). The renamed config is already in place,
            # so that retry builds the new header.
            if self._watcher_state is not None:
                self._watcher_state.context_injected = False
            self._context_injector.reset_session(self._session_id)
            raise
        if to_repeat is not None:
            self._append_system_prompt_file = to_repeat

    async def _ensure_context_injected(self) -> None:
        """Retry durable-context delivery on message processing when appropriate.

        ``InjectedContextBuilder.ensure()`` is called unconditionally once at
        watcher startup (``WatcherLifecycle._start_watcher()``). For backends
        using the default ``ensure_durable_instructions()`` fallback (a
        one-time ``send()`` — OpenCode today), a transient failure there has
        no other recovery path: without this retry, the watcher would stay
        in ``failed_retryable``/``failed_degraded`` for its entire uptime,
        recoverable only via a manual watcher reset or gateway restart. This
        restores the retry-on-message cadence the old ``ContextInjector`` had
        (throttled naturally by actual traffic, same as before), now pointed
        at the ``build()``/``ensure()`` pipeline.

        For backends that return a value to re-supply every turn (Claude),
        ``ensure_durable_instructions()`` has no side effect and essentially
        cannot fail this way in practice — but if a retry here does succeed
        with a fresh non-``None`` value, ``self._append_system_prompt_file``
        is updated so subsequent turns pick it up instead of staying stuck
        with whatever (possibly ``None``) value the failed startup attempt left.

        Raises whatever ``build()``/``ensure()`` raise on a hard failure
        (e.g. a missing context file, or a non-``AgentExecutionError``
        exception from the backend) — callers (``_process()``/
        ``_process_batch()``) catch this and surface a user-facing error,
        aborting that turn rather than silently proceeding without context.
        """
        if (
            self._context_injector is None
            or self._watcher_state is None
            or self._watcher_config is None
        ):
            return
        if self._watcher_state.context_injected:
            return

        status = self._context_injector.status_for(self._session_id)
        if status.state not in {"not_started", "failed_retryable", "pending"}:
            return

        content = await self._context_injector.build(
            self._agent_name,
            self._connector_name,
            self._watcher_config,
            agent_username=self._connector.agent_username,
        )
        to_repeat = await self._context_injector.ensure(
            self._watcher_state,
            self._session_id,
            self._agent,
            self._working_directory,
            self._config.timeout_for(self._agent_name),
            watcher_name=self._watcher_id,
            # Must be the SAME key the watcher start used, or the retry would write a
            # second file. Derived here rather than threaded down from the caller so the
            # two sites cannot drift apart — the derivation is the single source.
            path_key=watcher_prompt_key(self._connector_name, self._room.id),
            content=content,
        )
        if to_repeat is not None:
            self._append_system_prompt_file = to_repeat
