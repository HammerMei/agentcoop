"""The running daemon: the configuration wired to connectors, room logs, room cores,
one gateway and the doorbell listener, with the persisted state on disk and a
reload that applies a new configuration in place (design §3.1, §4, §5).

One ``Service`` holds one ``Gateway``. A room is keyed by its handle,
``<server>:<room name>``, the string the ``rooms:`` patterns match. The daemon
hosts a bot in a room when the bot's connection reports the room and the first
matching rule names the agent.

Events arrive on the connectors' tasks and go onto a queue per room; a worker per
room admits each copy to the room log, which delivers a new post to the core. So a
classifier wait in one room never stalls a connection, and the core sees every
post in server order with no copy twice.

Persisted state: ``<state dir>/<server>/<room id>.json`` per room (cursors,
unfinished sets, pause and reset flags, thread settings) and
``<state dir>/agents.json`` (bios). The core and the gateway say when either
changed, and the file is rewritten then, atomically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from .config import Config, ConfigError, RoomRule, Server
from .connectors.base import Connector, Event, Membership, OnEvent, Posted, RoomInfo
from .connectors.normalize import Names
from .connectors.roomlog import RoomLog
from .doorbell.api import build_app
from .doorbell.tls import server_context
from .room.classifier import Classifier
from .room.classifier_cli import SubprocessClassifier
from .room.clock import AsyncioClock, Clock
from .room.core import RoomCore
from .room.gateway import Gateway
from .room.model import Position
from .room.store import FORMAT, dump_agents, dump_room, load_agents, load_room
from .room.trace import Trace

logger = logging.getLogger(__name__)

ConnectorFactory = Callable[[Server, OnEvent], Connector]
ClassifierFactory = Callable[[Config, str], Classifier | None]

# How far back the startup backfill pages before giving up on reaching a saved
# cursor: the posts below are then lost to that bot, loudly.
MAX_BACKFILL_PAGES = 40


def read_token(path: Path) -> str:
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ConfigError(f"{path}: the token file is empty")
    return token


def default_classifier(config: Config, room: str) -> Classifier:
    cwd = config.classifier.working_directory
    return SubprocessClassifier(config.classifier.command, room, None if cwd is None else str(cwd))


@dataclass
class RoomRun:
    """One room the daemon hosts bots in."""

    handle: str
    server: str
    info: RoomInfo
    log: RoomLog
    core: RoomCore
    rule: RoomRule
    saved: dict = field(default_factory=dict)  # the room's document as loaded at open
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker: asyncio.Task | None = None
    started: bool = False


class Service:
    def __init__(
        self,
        config: Config,
        state_dir: Path,
        connectors: ConnectorFactory,
        clock: Clock | None = None,
        classifiers: ClassifierFactory = default_classifier,
        trace: Trace | None = None,
        wake_seed: int | None = None,
        rules_version: int = 1,
    ) -> None:
        self.config = config
        self.state_dir = state_dir
        self._connector_factory = connectors
        self._classifier_factory = classifiers
        self.clock = clock or AsyncioClock()
        self.trace = trace or Trace()
        self.wake_seed = int(time.time() * 1000) if wake_seed is None else wake_seed
        self.gateway = Gateway(self.clock, self.trace, name="coop", rules_version=rules_version)
        self.gateway.on_bio = self._save_agents
        self.tokens: dict[str, str] = {}  # bearer token -> agent, shared with the doorbell app
        self.connectors: dict[str, Connector] = {}
        self.rooms: dict[str, RoomRun] = {}  # by handle
        self._by_id: dict[tuple[str, str], RoomRun] = {}
        self._connected: dict[str, set[str]] = {}  # server -> agents whose bot is connected
        self._rooms_of: dict[tuple[str, str], list[RoomInfo]] = {}  # (server, agent) -> its rooms
        self._runner: web.AppRunner | None = None
        self.listening: tuple[str, int] | None = None
        self.notes: list[str] = []  # startup notices for the operator, degraded parts included

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def start(self, listen: bool = True) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._load_agents()
        self._set_tokens(self.config)
        for server in self.config.servers.values():
            connector = self._connector_factory(server, self._event_handler(server.name))
            self.connectors[server.name] = connector
            self._connected[server.name] = set()
            await connector.start()
            for bot in server.bots.values():
                if bot.hosted:
                    await self._connect_bot(server, bot.agent)
        for run in list(self.rooms.values()):
            await self._ensure_started(run)
        if listen:
            await self._listen()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        for run in list(self.rooms.values()):
            self._close_worker(run)
            self._save_room(run)
        self._save_agents()
        for connector in self.connectors.values():
            await connector.stop()

    async def _listen(self) -> None:
        host, _, port = self.config.gateway.address.rpartition(":")
        tls = self.config.gateway.tls
        self._runner = web.AppRunner(build_app(self.gateway, self.tokens))
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, int(port),
                           ssl_context=None if tls is None else server_context(*tls))
        await site.start()
        bound = self._runner.addresses[0]  # the port the kernel chose when the address said 0
        self.listening = (str(bound[0]), int(bound[1]))
        logger.info("doorbell listening on %s:%s%s", host, port, " (tls)" if tls else "")

    # ── bots and rooms ──────────────────────────────────────────────────────

    def _set_tokens(self, config: Config) -> None:
        fresh = {read_token(agent.token_file): name for name, agent in config.agents.items()}
        self.tokens.clear()
        self.tokens.update(fresh)

    async def _connect_bot(self, server: Server, agent: str) -> bool:
        bot = server.bots[agent]
        try:
            rooms = await self.connectors[server.name].add_bot(agent, read_token(bot.token_file))
        except Exception as exc:  # noqa: BLE001 - one bot's failure degrades, never stops, the daemon
            self._note(f"[ERROR] {server.name}: bot {agent!r} not connected: {exc}")
            logger.exception("%s: bot %r not connected", server.name, agent)
            return False
        self._connected[server.name].add(agent)
        self._rooms_of[(server.name, agent)] = rooms
        for info in rooms:
            await self._host(server.name, info, agent)
        return True

    async def _disconnect_bot(self, server: str, agent: str) -> None:
        for run in list(self.rooms.values()):
            if run.server == server and agent in run.core.hosted:
                await self._unhost(run, agent)
        await self.connectors[server].remove_bot(agent)
        self._connected[server].discard(agent)
        self._rooms_of.pop((server, agent), None)

    async def _host(self, server: str, info: RoomInfo, agent: str) -> None:
        """Host ``agent`` in ``info`` if a rule names it there."""
        handle = f"{server}:{info.name}"
        rule = self.config.rule_for(handle)
        if rule is None or agent not in rule.agents:
            logger.info("%s: no rule hosts %r in %s", server, agent, handle)
            return
        run = self._by_id.get((server, info.id))
        if run is None:
            try:
                run = await self._open_room(server, info, rule)
            except ConfigError as exc:
                self._note(f"[ERROR] {exc}")
                return
        if agent in run.core.hosted:
            return
        if run.started:  # a bot joining a running room: the member list has changed
            run.log.set_members(await self.connectors[server].members(info.id))
        saved = run.saved.get("bots", {}).get(agent)
        run.core.add_hosted(agent, run.log.last_position())
        if saved is not None:
            self._restore(run, {"format": run.saved["format"], "bots": {agent: saved}})
        logger.info("%s: hosting %r", handle, agent)

    async def _unhost(self, run: RoomRun, agent: str) -> None:
        await run.core.remove_hosted(agent)
        run.saved.get("bots", {}).pop(agent, None)  # a return starts at the latest post, not here
        self._save_room(run)
        if not run.core.hosted:
            self._close_room(run)

    async def _open_room(self, server: str, info: RoomInfo, rule: RoomRule) -> RoomRun:
        handle = f"{server}:{info.name}"
        if handle in self.rooms:
            raise ConfigError(f"{handle}: two rooms on {server} share this name; the second is not hosted")
        connector = self.connectors[server]
        roster = self.config.roster_for(server, rule)
        for missing in self.config.missing_bots(server, rule):
            self._note(f"[WARNING] {handle}: {missing!r} is in the rule but has no bot on {server}")
        log = RoomLog(connector, info, roster, [], self._names(server))
        core = RoomCore(handle, log, self.clock, self.trace, [], roster, rule.params,
                        self._classifier_factory(self.config, handle) if rule.params.classifier_enabled else None,
                        name="coop", rules_version=self.gateway.rules_version, wake_seed=self.wake_seed)
        run = RoomRun(handle, server, info, log, core, rule, saved=self._load_room_doc(server, info.id))
        log.hosted = core.hosted  # the one list: a file is fetched through a bot hosted here
        log.on_new = core.on_post
        core.on_change = lambda: self._save_room(run)
        # registered before the first await: a post arriving while the members and the
        # history are fetched waits on the queue, which the worker drains once the room runs
        self.rooms[handle] = run
        self._by_id[(server, info.id)] = run
        connector.watch(info.id, roster)
        try:
            log.set_members(await connector.members(info.id))
            await self._backfill(run)
        except Exception:
            self._close_room(run)
            raise
        if run.saved:
            self._restore(run, {"format": run.saved["format"], "threads": run.saved.get("threads", {})})
        self.gateway.add_room(core)
        return run

    def _restore(self, run: RoomRun, document: dict) -> None:
        """Part of the room's saved state into the core; a malformed part starts the room
        fresh, loudly, as an unreadable file does."""
        try:
            load_room(run.core, document)
        except (ValueError, TypeError, AttributeError) as exc:
            self._note(f"[ERROR] {self._room_file(run.server, run.info.id)}: malformed room state, starting the room "
                       f"fresh: {exc}")
            run.saved = {}
            run.core.settings.clear()

    def _close_room(self, run: RoomRun) -> None:
        self._close_worker(run)
        self.connectors[run.server].unwatch(run.info.id)
        self.gateway.remove_room(run.handle)
        run.started = False
        self.rooms.pop(run.handle, None)
        self._by_id.pop((run.server, run.info.id), None)
        logger.info("%s: closed", run.handle)

    def _close_worker(self, run: RoomRun) -> None:
        if run.worker is not None and not run.worker.done():
            run.worker.cancel()
        run.worker = None

    async def _backfill(self, run: RoomRun) -> None:
        """Page history back until every saved cursor of the rule's bots is in the log,
        or the pages run out. A cursor the pages never reached is moved up to the oldest
        post fetched, and the operator is told what that bot will not see."""
        cursors = [c for c in (self._saved_cursor(run, a) for a in run.rule.agents) if c is not None]
        floor = min(cursors) if cursors else None
        before: Position | None = None
        oldest: Position | None = None
        for _ in range(MAX_BACKFILL_PAGES):
            page = await self.connectors[run.server].history(run.info.id, before=before)
            if not page:
                return
            run.log.backfill(page)
            oldest = page[0].position
            if floor is None or oldest <= floor:
                return
            before = oldest
        if oldest is not None and floor is not None and oldest > floor:
            self._note(f"[ERROR] {run.handle}: history pages ran out above a saved cursor; posts between "
                       f"{floor!r} and {oldest!r} are not delivered")
            for agent, raw in run.saved.get("bots", {}).items():
                if raw.get("cursor") is not None and tuple(raw["cursor"]) < oldest:
                    raw["cursor"] = list(oldest)

    def _saved_cursor(self, run: RoomRun, agent: str) -> Position | None:
        raw = run.saved.get("bots", {}).get(agent, {}).get("cursor")
        return tuple(raw) if isinstance(raw, list) else raw

    async def _ensure_started(self, run: RoomRun) -> None:
        """A room runs once its first bots are hosted. The posts after the oldest hosted
        cursor go through the core's replay path first (spec §5.6): at startup that is
        what happened while the daemon was down; for a room opened later it is nothing,
        since a new bot's cursor is the latest post."""
        if run.started or not run.core.hosted:
            return
        run.started = True
        cursors = [run.core.persisted[b].cursor for b in run.core.hosted]
        await run.core.restart(replay_after=min(cursors) if cursors and None not in cursors else None)
        run.core.start()
        run.worker = asyncio.create_task(self._work(run), name=f"room {run.handle}")

    async def _work(self, run: RoomRun) -> None:
        while True:
            post = await run.queue.get()
            try:
                await run.log.admit(post)
            except Exception:  # noqa: BLE001 - one post must not end the room's worker
                logger.exception("%s: post %s not processed", run.handle, post.id)

    # ── events ──────────────────────────────────────────────────────────────

    def _event_handler(self, server: str) -> OnEvent:
        async def handler(event: Event) -> None:
            await self._on_event(server, event)
        return handler

    async def _on_event(self, server: str, event: Event) -> None:
        if isinstance(event, Posted):
            run = self._by_id.get((server, event.room_id))
            if run is not None:
                await run.queue.put(event.post)
            return
        if isinstance(event, Membership):
            run = self._by_id.get((server, event.room_id))
            hosted_here = event.agent in self.config.servers[server].bots and self._is_hosted(server, event.agent)
            if hosted_here and event.joined:
                info = await self.connectors[server].room(event.room_id)
                self._rooms_of.setdefault((server, event.agent), []).append(info)
                await self._host(server, info, event.agent)
                run = self._by_id.get((server, event.room_id))
                if run is not None:
                    await self._ensure_started(run)
            elif hosted_here and run is not None and event.agent in run.core.hosted:
                self._rooms_of[(server, event.agent)] = [
                    r for r in self._rooms_of.get((server, event.agent), []) if r.id != event.room_id]
                await self._unhost(run, event.agent)
                run = self._by_id.get((server, event.room_id))
            if run is not None:
                run.log.set_members(await self.connectors[server].members(event.room_id))

    def _is_hosted(self, server: str, agent: str) -> bool:
        bot = self.config.servers[server].bots.get(agent)
        return bot is not None and bot.hosted and agent in self._connected.get(server, set())

    # ── reload ──────────────────────────────────────────────────────────────

    async def reload(self, config: Config) -> list[str]:
        """Apply ``config`` in place. The servers themselves must be unchanged: a new or
        altered server needs a restart, and the call refuses before touching anything.
        Returns the notes for the operator."""
        self._refuse_server_changes(config)
        self._set_tokens(config)  # read before the switch: a missing token file refuses the whole reload
        old, self.config = self.config, config
        notes: list[str] = []
        self.notes = notes
        for server in config.servers.values():
            self.connectors[server.name].configure(server)  # the bots and usernames it holds now
        for run in list(self.rooms.values()):
            await self._reload_room(run, notes)  # rosters first: a bot connected below is hosted by them
        for server in config.servers.values():
            await self._reload_bots(server, old.servers[server.name], notes)
        await self._host_newly_ruled(notes)
        for run in self.rooms.values():
            await self._ensure_started(run)
        return notes

    def _refuse_server_changes(self, config: Config) -> None:
        for name, server in config.servers.items():
            before = self.config.servers.get(name)
            if before is None or (before.type, before.url, before.team) != (server.type, server.url, server.team):
                raise ConfigError(f"servers.{name}: a new or changed server needs a restart")
        for name in self.config.servers:
            if name not in config.servers:
                raise ConfigError(f"servers.{name}: removing a server needs a restart")

    async def _reload_bots(self, server: Server, before: Server, notes: list[str]) -> None:
        hosted_now = {a for a, b in server.bots.items() if b.hosted}
        for agent in sorted(self._connected[server.name] - hosted_now):
            await self._disconnect_bot(server.name, agent)
            notes.append(f"{server.name}: bot {agent!r} disconnected")
        for agent in sorted(hosted_now - self._connected[server.name]):
            if await self._connect_bot(server, agent):
                notes.append(f"{server.name}: bot {agent!r} connected")
        for agent in sorted(hosted_now & self._connected[server.name]):
            was, now = before.bots.get(agent), server.bots[agent]
            if was is not None and (was.username, was.token_file) != (now.username, now.token_file):
                # the connection keeps the credentials it was opened with: said, not silently kept
                self._note(f"[WARNING] {server.name}: bot {agent!r} has new credentials in the configuration; "
                           "the connection keeps the old ones until a restart")

    async def _reload_room(self, run: RoomRun, notes: list[str]) -> None:
        """The rule the room matches now, applied in place; no rule closes it."""
        rule = self.config.rule_for(run.handle)
        if rule is None:
            for agent in list(run.core.hosted):
                await self._unhost(run, agent)
            notes.append(f"{run.handle}: no rule matches now; closed")
            return
        roster = self.config.roster_for(run.server, rule)
        run.rule = rule
        classifier = self._classifier_factory(self.config, run.handle) if rule.params.classifier_enabled else None
        run.core.update(roster, rule.params, classifier)
        run.log.roster = roster
        run.log.names = self._names(run.server)
        self.connectors[run.server].watch(run.info.id, roster)
        for agent in list(run.core.hosted):
            if agent not in rule.agents or not self._is_hosted(run.server, agent):
                await self._unhost(run, agent)
                notes.append(f"{run.handle}: {agent!r} no longer hosted")

    async def _host_newly_ruled(self, notes: list[str]) -> None:
        """Rooms a connected bot is in that a rule names it for now."""
        for (server, agent), rooms in list(self._rooms_of.items()):
            for info in rooms:
                run = self._by_id.get((server, info.id))
                if run is not None and agent in run.core.hosted:
                    continue
                await self._host(server, info, agent)
                run = self._by_id.get((server, info.id))
                if run is not None and agent in run.core.hosted:
                    notes.append(f"{run.handle}: {agent!r} hosted")

    # ── operator commands ───────────────────────────────────────────────────

    def status(self) -> dict:
        agents = {}
        for name in self.config.agents:
            state = self.gateway.agents.get(name)
            agents[name] = {
                "online": self.gateway.online(name, 30),
                "lease": self.gateway.holds_lease(name),
                "bio_version": 0 if state is None else state.bio_version,
                "last_seen": None if state is None else state.last_seen,
            }
        return {
            "listening": None if self.listening is None else f"{self.listening[0]}:{self.listening[1]}",
            "servers": {name: sorted(agents_) for name, agents_ in self._connected.items()},
            "agents": agents,
            "rooms": {handle: run.core.status() for handle, run in sorted(self.rooms.items())},
            "notes": list(self.notes),
        }

    def room(self, handle: str) -> RoomRun:
        try:
            return self.rooms[handle]
        except KeyError:
            raise KeyError(f"no hosted room {handle!r}; `list` shows them") from None

    def pause(self, agent: str, handle: str) -> None:
        self._hosted_in(agent, handle).core.pause(agent)

    def resume(self, agent: str, handle: str) -> None:
        self._hosted_in(agent, handle).core.resume(agent)

    def reset(self, agent: str, handle: str) -> None:
        self._hosted_in(agent, handle).core.reset(agent)

    def _hosted_in(self, agent: str, handle: str) -> RoomRun:
        run = self.room(handle)
        if agent not in run.core.hosted:
            raise KeyError(f"{agent!r} is not hosted in {handle!r}")
        return run

    # ── persistence ─────────────────────────────────────────────────────────

    def _room_file(self, server: str, room_id: str) -> Path:
        return self.state_dir / server / f"{room_id}.json"

    def _load_room_doc(self, server: str, room_id: str) -> dict:
        path = self._room_file(server, room_id)
        if not path.exists():
            return {}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._note(f"[ERROR] {path}: unreadable room state, starting the room fresh: {exc}")
            return {}
        if not isinstance(doc, dict) or doc.get("format") != FORMAT:
            self._note(f"[ERROR] {path}: not a {FORMAT!r} room state document, starting the room fresh")
            return {}
        return doc

    def _save_room(self, run: RoomRun) -> None:
        _write_json(self._room_file(run.server, run.info.id), dump_room(run.core))

    def _load_agents(self) -> None:
        path = self.state_dir / "agents.json"
        if path.exists():
            try:
                load_agents(self.gateway, json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                self._note(f"[ERROR] {path}: unreadable agents state, bios start empty: {exc}")

    def _save_agents(self) -> None:
        _write_json(self.state_dir / "agents.json", dump_agents(self.gateway))

    # ── helpers ─────────────────────────────────────────────────────────────

    def _names(self, server: str) -> Names:
        return Names({agent: bot.username for agent, bot in self.config.servers[server].bots.items()})

    def _note(self, text: str) -> None:
        self.notes.append(text)
        (logger.error if text.startswith("[ERROR]") else logger.warning)(text)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


