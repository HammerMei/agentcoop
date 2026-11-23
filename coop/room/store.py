"""The persisted part of a room's state, as one JSON-safe document (design §3.1):
cursors, unfinished sets, the paused flag, thread settings; and the persisted part of
what the gateway holds per agent, as another: the bio and its version. The in-memory
part (leases, backoff, decision cache, rate window, ``last_seen``) is rebuilt on
restart. Positions are stored as the platform gave them; a tuple position comes
back as a list and is restored to a tuple, so comparisons keep working.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from .core import BotPersisted, RoomCore, ThreadSettings
from .gateway import AgentState, Gateway
from .model import Position

FORMAT = 2  # room documents; 2: thread control targets keyed by the bot's platform username
AGENTS_FORMAT = 1  # the agents document, unchanged by the room's format


def _position_out(position: Position | None) -> Any:
    return list(position) if isinstance(position, tuple) else position


def _position_in(raw: Any) -> Position | None:
    return tuple(raw) if isinstance(raw, list) else raw


def dump_room(core: RoomCore) -> dict:
    """The room's persisted state, in the core's namespace (bots by username); the
    daemon re-keys ``bots`` by agent name before writing (``Service._save_room``)."""
    return {
        "format": FORMAT,
        "room": core.room,
        "bots": {
            bot: {
                "cursor": _position_out(state.cursor),
                "unfinished": sorted(state.unfinished),
                "paused": state.paused,
                "reset_pending": state.reset_pending,
            }
            for bot, state in core.persisted.items()
        },
        "threads": {
            thread: {
                "orchestrator": s.orchestrator,
                "orchestrator_at": _position_out(s.orchestrator_at),
                "muted": {b: _position_out(p) for b, p in s.muted.items()},
                "unmuted_at": {b: _position_out(p) for b, p in s.unmuted_at.items()},
                "limit_posts": s.limit_posts,
                "limit_words": s.limit_words,
                "limit_at": _position_out(s.limit_at),
                "last_activity": s.last_activity,
                "unknown_mutes": list(s.unknown_mutes),
            }
            for thread, s in core.settings.items()
            if not s.is_default  # a thread no control ever touched is not worth a line
        },
    }


def load_room(core: RoomCore, data: dict) -> None:
    """Restore a room's persisted state into ``core`` before it starts. A bot the
    configuration no longer hosts is left out; a hosted bot the document does not
    know starts fresh (its cursor at the room's latest post is the caller's choice)."""
    if data.get("format") != FORMAT:
        raise ValueError(f"room state format {data.get('format')!r} is not {FORMAT}")
    known = {f.name for f in fields(ThreadSettings)}
    for bot, raw in data.get("bots", {}).items():
        if bot not in core.persisted:
            continue
        core.persisted[bot] = BotPersisted(
            cursor=_position_in(raw.get("cursor")),
            unfinished=set(raw.get("unfinished", [])),
            paused=bool(raw.get("paused", False)),
            reset_pending=bool(raw.get("reset_pending", False)),
        )
    for thread, raw in data.get("threads", {}).items():
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"thread {thread!r}: unknown settings {sorted(unknown)}")
        core.settings[thread] = ThreadSettings(
            orchestrator=raw.get("orchestrator"),
            orchestrator_at=_position_in(raw.get("orchestrator_at")),
            muted={b: _position_in(p) for b, p in raw.get("muted", {}).items()},
            unmuted_at={b: _position_in(p) for b, p in raw.get("unmuted_at", {}).items()},
            limit_posts=raw.get("limit_posts"),
            limit_words=raw.get("limit_words"),
            limit_at=_position_in(raw.get("limit_at")),
            last_activity=float(raw.get("last_activity", 0.0)),
            unknown_mutes=list(raw.get("unknown_mutes", [])),
        )


def dump_agents(gateway: Gateway) -> dict:
    """The gateway's persisted agent state: every bio pushed so far, with its version."""
    return {
        "format": AGENTS_FORMAT,
        "agents": {
            agent: {"bio": state.bio, "bio_version": state.bio_version}
            for agent, state in gateway.agents.items()
            if state.bio is not None
        },
    }


def load_agents(gateway: Gateway, data: dict) -> None:
    """Restore the bios into ``gateway``: into every room already bound, and a later
    binding copies the agent's bio into its room's core under the bot's username."""
    if data.get("format") != AGENTS_FORMAT:
        raise ValueError(f"agent state format {data.get('format')!r} is not {AGENTS_FORMAT}")
    for agent, raw in data.get("agents", {}).items():
        state = gateway.agents.setdefault(agent, AgentState())
        state.bio = raw.get("bio")
        state.bio_version = int(raw.get("bio_version", 0))
        if state.bio is not None:
            gateway.set_bio(agent, state.bio)
