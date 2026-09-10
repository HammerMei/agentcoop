"""Agent abstraction layer: abstract base for all agent backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from .errors import AgentExecutionError
from .response import AgentEvent, AgentResponse

if TYPE_CHECKING:
    # Avoid circular imports — only used in type annotations
    PermissionHandler = Callable[[str, dict], Awaitable[bool]]
    from ..core.permission import (
        PermissionBroker,
        PermissionNotifier,
        PermissionRegistry,
    )


@dataclass
class GatewayBrokerConfig:
    """Permission policy settings stored in the backend for gateway broker creation.

    These are agent-level policy decisions (who can run what tools) that belong
    to the agent config, not to the shared gateway runtime state.  The backend
    stores them and forwards them to ``create_gateway_broker()`` so that
    GatewayService never needs to know about per-agent permission details.

    ``owner_allowed_tools`` / ``guest_allowed_tools`` are lists of
    :class:`~gateway.config.ToolRule` parsed from config.  Pass ``None`` to
    use an empty list (all unmatched owner calls require RC approval; guests
    are fully blocked).
    """

    owner_allowed_tools: list = field(default_factory=list)  # list[ToolRule]
    guest_allowed_tools: list = field(default_factory=list)  # list[ToolRule]
    timeout: int = 300
    skip_owner_approval: bool = False  # when True, bypass owner approval for all tool calls


def check_backend_signatures(backends) -> None:
    """Refuse to start if a backend cannot be called the way the gateway calls it.

    `ensure_durable_instructions`'s `watcher_name` parameter became `path_key`, and the
    two are not interchangeable: the value is now scoped to the watcher in a room, so a
    backend keying a file on it as if it were a display name reintroduces the overwrite
    between same-room watchers (see gateway/core/paths.py).

    **Asked as one question — "would the real call succeed?" — via `Signature.bind`.**
    Three earlier versions asked it in pieces and each was reported as incomplete: is
    `path_key` present (a positional-only declaration passes, then fails on a keyword
    call); is its `kind` callable by keyword (a `**kwargs` signature passes even while a
    required legacy `watcher_name` remains unfilled). Every one of those was a hand-rolled
    approximation of what `bind` answers exactly, so the approximations are gone. The
    probe arguments below mirror the caller, which is what makes the answer meaningful.

    Deliberately a preflight rather than a compatibility shim. `AgentBackend` is a
    documented extension point, but registering one requires editing `service.py`'s
    `_build_agent_backend`, so a custom backend is a fork rather than a plugin — and a fork
    rebasing onto this branch already meets a removed config field and a refused state
    format. Accepting both spellings would keep two names for one parameter alive in a
    contract, which is the ambiguity this rename removed.

    What a shim would genuinely have bought is a better failure than a raw `TypeError` at
    the first watcher start. This buys that directly, and earlier: the base method's own
    `NotImplementedError` shows the standard here is a call-time failure carrying an
    actionable message, so the defect in the raw TypeError was the message, not the timing.

    Nothing is called — `bind` only matches arguments against the signature — so a backend
    that never exercises this path is unaffected.
    """
    import inspect

    # The call `InjectedContextBuilder.ensure()` actually makes. Kept beside the check
    # rather than described, because the check is only meaningful if it matches reality.
    probe_args = ("session-id", "/working/dir", 1, "content")
    probe_kwargs = {"path_key": "key", "already_delivered": False}

    for name, backend in sorted(getattr(backends, "items", lambda: [])()):
        impl = type(backend).ensure_durable_instructions
        if impl is AgentBackend.ensure_durable_instructions:
            continue  # not overridden; the base raises with its own message

        try:
            inspect.signature(impl).bind(backend, *probe_args, **probe_kwargs)
        except TypeError as exc:
            params = inspect.signature(impl).parameters
            hint = (
                "It was renamed to 'path_key' and the meaning changed: the value is an "
                "opaque key scoped to the watcher in a room, not a display name. Rename "
                "the parameter and use it verbatim as the file name — do not derive "
                "anything from it, and do not substitute room_path_key, which belongs to "
                "the attachment workspace."
                if "watcher_name" in params
                else "The gateway passes path_key= and already_delivered= by keyword and "
                "passes nothing else, so match the base class signature: (session_id, "
                "working_directory, timeout, content, *, path_key, already_delivered)."
            )
            raise TypeError(
                f"Agent backend '{name}' ({type(backend).__name__}) cannot be called the "
                f"way the gateway calls ensure_durable_instructions(): {exc}. {hint} "
                "See gateway/core/paths.py and docs/architecture.md's 'Adding a New Agent "
                "Backend'."
            ) from exc


class AgentBackend(ABC):
    """Abstract backend that creates sessions and sends messages to an agent."""

    # ── Backend capability flags ─────────────────────────────────────────────

    @property
    def supports_per_message_env(self) -> bool:
        """Whether this backend uses the ``env`` dict passed to :meth:`send`.

        When ``True`` (the default), :class:`~gateway.core.message_processor.MessageProcessor`
        generates per-message role env (``COOP_ROLE``) and passes it via ``send(env=...)``.
        The backend's subprocess uses these vars for role-aware hook enforcement.

        When ``False``, per-message env is a no-op — the backend either ignores
        ``env`` entirely or requires role to be set at process startup (e.g.
        OpenCode HTTP mode sets ``COOP_ROLE=owner`` on ``opencode serve`` at launch).
        In this case, the processor skips env generation to avoid misleading
        no-op computation.  Guest/owner enforcement is handled entirely by the
        permission broker for such backends.
        """
        return True

    # ── Session retention ────────────────────────────────────────────────────

    def typical_session_retention_days(self) -> int | None:
        """How many days this backend itself typically keeps a session alive
        before its own cleanup mechanism (if any) would delete it, independent
        of anything AgentCoop configures.

        Used by the on-the-fly-watcher idle/expire lifecycle
        (docs/design/dynamic-watcher-design.md) to compute an effective
        ``session_expire_days`` of ``min(configured value, this value)`` when
        the agent declares one — there's no point in AgentCoop holding onto a
        session reference the backend has already thrown away.

        Returns ``None`` (the default here) when the backend has no automatic
        expiry of its own — AgentCoop's own ``session_expire_days`` setting is then
        the only limit in effect. Backends with a real, known limit should
        override this rather than let callers assume unbounded retention.
        """
        return None

    # ── Optional lifecycle hooks (default: no-op) ─────────────────────────────

    async def start(self) -> None:
        """Start any required backend services (e.g. ``opencode serve``).

        Called by :class:`~gateway.service.GatewayService` during startup and
        by :class:`~gateway.agents.session.AgentSession` on ``__aenter__``.
        The default implementation is a no-op — backends that do not require a
        companion process (e.g. :class:`~gateway.agents.claude.adapter.ClaudeBackend`)
        need not override this.

        Implementations must be **idempotent**: calling ``start()`` on an
        already-running backend must return immediately without side-effects.

        Raises:
            RuntimeError: If the backend process fails to start or become healthy.
        """

    async def stop(self) -> None:
        """Stop any background services started by :meth:`start`.

        Called by :class:`~gateway.service.GatewayService` during shutdown and
        by :class:`~gateway.agents.session.AgentSession` on ``__aexit__``.
        The default implementation is a no-op.

        Implementations must be **idempotent**: calling ``stop()`` when already
        stopped must return immediately without raising.
        """

    async def reclaim_durable_instructions(self, path_key: str) -> None:
        """Remove whatever ``ensure_durable_instructions`` left on disk for this key.

        Expiry's half of that contract (§2.5, "expiry reclaims everything"): the
        file's location and layout are the backend's own knowledge — the caller
        holds only the opaque ``path_key`` it passed in — so the backend that
        wrote it is the one that can remove it. Best-effort and idempotent:
        a missing file is success, not an error.

        The default implementation is a no-op, for backends whose delivery
        leaves nothing on disk (the send()-based fallback).
        """

    async def delete_session(self, session_id: str) -> bool:
        """Best-effort deletion of a previously created session.

        Used by watcher startup rollback paths to avoid leaking newly created
        sessions when later setup phases fail (context injection, subscribe,
        etc.).  Returns ``True`` when the backend knows the session was cleaned
        up, ``False`` when deletion is unsupported or could not be confirmed.

        The default implementation returns ``False`` so backends that do not
        support explicit session deletion need not override it.
        """
        return False

    # ── Durable instruction delivery ─────────────────────────────────────────

    async def ensure_durable_instructions(
        self,
        session_id: str,
        working_directory: str,
        timeout: int,
        content: str,
        *,
        path_key: str,
        already_delivered: bool,
    ) -> str | None:
        """Make `content` durably visible to the model, by whatever mechanism this
        backend actually has. Called on EVERY watcher start — unconditionally, never
        gated on prior history by the caller (see InjectedContextBuilder.ensure()).

        Returns:
            None — this backend fully handled it via a one-time side-effecting
            action (e.g. sent a message into conversation history); caller does
            nothing further.
            A non-None value — this backend cannot make content durable on its
            own; caller must keep re-supplying the returned value on every turn
            via send()/stream()'s `append_system_prompt_file` parameter.

        `already_delivered`: True if a PRIOR watcher start already got a
        successful result for this session (persisted across gateway restarts).
        Backends whose mechanism has a real side effect should honor it to
        avoid duplicating content into conversation history. Backends with no
        such side effect (returning a value for the caller to re-supply)
        should ignore it and always return fresh content.

        `path_key`: an **opaque** filesystem key for this file. Backends must treat it as
        a name and derive nothing from it — not the room, not the watcher, nothing
        human-facing.

        Callers pass `gateway.core.paths.watcher_prompt_key(connector, room_id)`,
        keyed by the watcher's identity — its room — and not by its display handle,
        which follows the room's name and would move the file on every rename. The
        room-scoped `room_path_key` exists for the attachment workspace; the two are
        digested with different tags so they cannot be confused — do not reach for
        one where the other is meant.

        It replaced a `watcher_name` parameter, and the rename is the point: a display
        name is free to change (a channel rename, a group DM's membership changing, a
        better sanitizer), and using it directly as a path component made every such
        change orphan a file and let two rooms collide (§2.3).

        There is deliberately NO usable default here — every backend must
        make an explicit choice, visible in its own source, about how it
        makes content durable. A one-time send into conversation history
        (see :meth:`_send_once_as_durable_fallback`) is NOT compaction-
        resistant and is not a behavior any backend should end up with by
        silently inheriting it; a backend that wants exactly that fallback
        (OpenCode, today — see :class:`~gateway.agents.opencode.adapter.OpenCodeBackend`)
        must opt in explicitly by calling ``self._send_once_as_durable_fallback(...)``
        from its own override. This is intentionally NOT ``@abstractmethod``:
        many ``AgentBackend`` subclasses across the test suite never exercise
        this path at all and should not be forced to implement it just to be
        instantiable — the cost of a missing implementation is paid only by
        backends that actually get asked to deliver durable content.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement ensure_durable_instructions(). "
            "Backends must explicitly decide how to make durable content reach "
            "the model — either implement a real mechanism (see ClaudeBackend, "
            "which writes a file for --append-system-prompt-file), or opt into "
            "the one-time-send fallback via self._send_once_as_durable_fallback() "
            "(see OpenCodeBackend)."
        )

    async def _send_once_as_durable_fallback(
        self,
        session_id: str,
        working_directory: str,
        timeout: int,
        content: str,
        already_delivered: bool,
    ) -> str | None:
        """Shared fallback: deliver `content` via one-time self.send().

        Exactly matches the pre-#52 ContextInjector behavior — sends once,
        skips re-sending when `already_delivered` is True, and raises
        AgentExecutionError (retryable by the caller) on an error response.
        Always returns None: this fully handles delivery itself, so the
        caller has nothing further to re-supply on subsequent turns.

        Not called automatically — a backend must call this explicitly from
        its own ensure_durable_instructions() override to opt in (see
        OpenCodeBackend). This is what makes that choice a visible, deliberate
        one rather than a silently-inherited default.
        """
        if already_delivered:
            return None
        response = await self.send(
            session_id=session_id,
            prompt=content,
            working_directory=working_directory,
            timeout=timeout,
        )
        if response.is_error:
            raise AgentExecutionError(response.text[:200])
        return None

    # ── Permission broker factory ──────────────────────────────────────────────

    def create_gateway_broker(
        self,
        registry: "PermissionRegistry",
        notifier: "PermissionNotifier",
        session_room_map: "dict[str, str]",
        session_role_map: "dict[str, str]",
        session_permission_thread_map: "dict[str, str | None]",
    ) -> "PermissionBroker | None":
        """Return a gateway permission broker wired to the shared notification channel.

        Called by :class:`~gateway.service.GatewayService` during startup.
        Returns ``None`` if the backend was constructed without a
        :class:`GatewayBrokerConfig` (i.e. permissions are disabled for this agent).

        The default implementation returns ``None``.  Subclasses override this
        to return the appropriate broker for their permission mechanism:

        - :class:`~gateway.agents.claude.adapter.ClaudeBackend` →
          :class:`~gateway.agents.claude.broker.ClaudePermissionBroker`
        - :class:`~gateway.agents.opencode.adapter.OpenCodeBackend` →
          :class:`~gateway.agents.opencode.broker.OpenCodePermissionBroker`

        Args:
            registry: Shared in-process store for all pending permission requests.
            notifier: Delivers permission messages to chat rooms.
            session_room_map: session_id → room_id for RC notification delivery.
            session_role_map: session_id → "owner"|"guest" for policy enforcement.
            session_permission_thread_map: session_id → RC thread ID (or None).
        """
        return None

    def create_callable_broker(
        self,
        handler: "PermissionHandler",
        timeout_seconds: int,
    ) -> object | None:
        """Return a permission broker that calls ``handler`` for each tool call.

        The returned broker must implement ``start()`` / ``stop()`` coroutines.
        Returning ``None`` means the backend does not support callable permission
        brokers and the handler will be silently ignored.

        The default implementation returns ``None``.  Subclasses override this
        to return the appropriate broker for their permission mechanism:

        - :class:`~gateway.agents.claude.adapter.ClaudeBackend` → ``CallablePermissionBroker``
        - :class:`~gateway.agents.opencode.adapter.OpenCodeBackend` → ``OpenCodeCallablePermissionBroker``

        Args:
            handler: Async callable ``(tool_name: str, tool_input: dict) -> bool``.
            timeout_seconds: Seconds to wait for the handler before auto-denying.
        """
        return None

    # ── Callable broker wiring ─────────────────────────────────────────────

    def attach_callable_broker(self, broker: object) -> None:
        """Wire a callable permission broker into this backend's session path.

        Called by :class:`~gateway.agents.session.AgentSession` after the broker
        is started and before ``create_session()`` runs.  Override in subclasses
        that need broker awareness — for example,
        :class:`~gateway.agents.claude.adapter.ClaudeBackend` patches its own
        ``settings_path`` so ``create_session`` picks up the ``--settings`` flag.

        The default implementation is a no-op — backends whose callable brokers
        are fully independent (e.g. OpenCode SSE) need not override this.
        """

    def detach_callable_broker(self) -> None:
        """Undo any wiring performed by :meth:`attach_callable_broker`.

        Called by :class:`~gateway.agents.session.AgentSession` before the
        broker is stopped.  The default implementation is a no-op.
        """

    # ── Core interface ─────────────────────────────────────────────────────────

    @abstractmethod
    async def create_session(
        self,
        working_directory: str,
        extra_args: list[str] | None = None,
        session_title: str | None = None,
    ) -> str:
        """Start a new agent session and return a persistent session_id.

        Args:
            working_directory: The working directory for the agent subprocess.
            extra_args: Optional extra CLI args passed only during session creation.
            session_title: Optional human-readable title/name for the session
                           (used as --name for Claude, --title for OpenCode).
        """
        ...

    @abstractmethod
    async def send(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        timeout: int,
        attachments: list[str] | None = None,
        env: dict[str, str] | None = None,
        append_system_prompt_file: str | None = None,
    ) -> AgentResponse:
        """Send a message to an existing session and return a normalized AgentResponse.

        Args:
            session_id: The persistent session ID returned by create_session.
            prompt: The text prompt to send.
            working_directory: The working directory for the agent subprocess.
            timeout: Seconds before the call times out.
            attachments: Optional list of local file paths to attach (backend support varies).
            env: Optional extra environment variables to inject into the agent subprocess.
                 Merged on top of the inherited process environment. Used to pass role context
                 (e.g. COOP_ROLE) for hook/plugin enforcement.
            append_system_prompt_file: Optional path to a file whose content should be
                 appended to the system prompt on this turn (backend support varies —
                 e.g. Claude's ``--append-system-prompt-file``). Backends without an
                 equivalent mechanism should ignore it.

        Returns:
            AgentResponse with the agent's reply in ``.text`` and best-effort
            metadata (usage, cost, duration, turns) populated from the backend's
            JSON stream.
        """
        ...

    async def stream(
        self,
        session_id: str,
        prompt: str,
        working_directory: str,
        timeout: int,
        attachments: list[str] | None = None,
        env: dict[str, str] | None = None,
        append_system_prompt_file: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Stream agent events for a turn, yielding intermediate events then a final one.

        Yields :class:`~gateway.agents.response.AgentEvent` objects as the agent
        processes the request.  Intermediate events (``kind != "final"``) carry a
        human-readable ``text`` label for live status updates.  The last event
        always has ``kind == "final"`` and its ``response`` field is the complete
        :class:`AgentResponse`.

        **Default implementation**: wraps :meth:`send` — yields only a single
        ``final`` event.  Backends that support native streaming (Claude
        ``stream-json``, OpenCode SSE) override this to emit intermediate events.
        Backends that do not override remain fully functional; callers receive
        exactly one ``final`` event per turn with no intermediate updates.

        Args:
            session_id       : Persistent session ID from :meth:`create_session`.
            prompt           : Text prompt to send.
            working_directory: Working directory for the agent subprocess.
            timeout          : Seconds before the call times out.
            attachments      : Optional local file paths to attach.
            env              : Optional extra environment variables.
            append_system_prompt_file: Optional path whose content should be appended
                                 to the system prompt (backend support varies).

        Yields:
            :class:`AgentEvent` — zero or more intermediate events followed by
            exactly one ``final`` event.
        """
        response = await self.send(
            session_id=session_id,
            prompt=prompt,
            working_directory=working_directory,
            timeout=timeout,
            attachments=attachments,
            env=env,
            append_system_prompt_file=append_system_prompt_file,
        )
        yield AgentEvent(kind="final", response=response)
