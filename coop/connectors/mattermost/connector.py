"""The Mattermost connector: one REST client and one websocket per bot account the
gateway holds on the server, behind :class:`~coop.connectors.base.Connector`.

Facts probed on Mattermost 11.x that this implementation rests on: a bot's own REST
post echoes on its own websocket (so the room log's dedup is what keeps one copy);
history carries no mentions and pages by post id with ``before``/``after``, complete
and strictly ordered by ``(create_at, id)``; ``is_bot`` is present only on real bot
accounts, so it is the last hint, after the configuration.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ...config import Server
from ...room.model import Position, Post
from ...room.platform import PlatformError, RefusedPost
from ...room.roster import Roster
from ..base import MemberInfo, Membership, OnEvent, Posted, RoomInfo
from ..normalize import Names
from .normalize import display_name, normalize
from .rest import MattermostREST
from .websocket import MattermostWebSocket

logger = logging.getLogger(__name__)


@dataclass
class _Bot:
    agent: str
    rest: MattermostREST
    ws: MattermostWebSocket | None = None
    user_id: str = ""
    rooms: set[str] = field(default_factory=set)  # channel ids the account is a member of


class MattermostConnector:
    """``transport`` and ``websocket`` are the test seams: an httpx transport for the
    REST calls and a factory in place of :class:`MattermostWebSocket`."""

    def __init__(self, server: Server, on_event: OnEvent, transport: httpx.AsyncBaseTransport | None = None,
                 websocket: Callable[..., MattermostWebSocket] = MattermostWebSocket) -> None:
        self.server = server
        self.name = server.name
        self.on_event = on_event
        self._transport = transport
        self._websocket = websocket
        self.names = Names({agent: bot.username for agent, bot in server.bots.items()})
        self._bots: dict[str, _Bot] = {}
        self._rosters: dict[str, Roster] = {}
        self._members: dict[str, dict[str, MemberInfo]] = {}  # room id -> username -> member
        self._last: dict[str, Position] = {}  # room id -> newest position seen live
        self._team_id: str | None = None

    async def start(self) -> None:
        pass

    def configure(self, server: Server) -> None:
        self.server = server
        self.names = Names({agent: bot.username for agent, bot in server.bots.items()})

    async def stop(self) -> None:
        for agent in list(self._bots):
            await self.remove_bot(agent)

    # ── bots ────────────────────────────────────────────────────────────────

    async def add_bot(self, agent: str, token: str) -> list[RoomInfo]:
        rest = MattermostREST(self.server.url, token, transport=self._transport)
        bot = _Bot(agent, rest)
        try:
            me = await rest.me()
            bot.user_id = me["id"]
            if me.get("username") != self.server.bots[agent].username:
                raise ValueError(f"the token is {me.get('username')!r}'s, not {self.server.bots[agent].username!r}'s")
            team_id = await self._team(rest)
            channels = [c for c in await rest.my_channels(bot.user_id)
                        if c.get("type") in ("D", "G") or c.get("team_id") == team_id]
            rooms = [await self._room_info(rest, c, bot.user_id) for c in channels]
            bot.rooms = {r.id for r in rooms}
            bot.ws = self._websocket(self.server.url, token, self._frame_handler(bot), self._resync_handler(bot))
            await bot.ws.start()
        except Exception:
            await rest.close()
            raise
        self._bots[agent] = bot
        return rooms

    async def remove_bot(self, agent: str) -> None:
        bot = self._bots.pop(agent, None)
        if bot is None:
            return
        if bot.ws is not None:
            await bot.ws.stop()
        await bot.rest.close()

    async def _team(self, rest: MattermostREST) -> str:
        if self._team_id is None:
            for team in await rest.my_teams():
                if team.get("name") == self.server.team or team.get("id") == self.server.team:
                    self._team_id = team["id"]
                    break
            else:
                raise ValueError(f"team {self.server.team!r} is not among the bot's teams")
        return self._team_id

    def _any_bot(self, room_id: str) -> _Bot:
        for bot in self._bots.values():
            if room_id in bot.rooms:
                return bot
        if self._bots:
            return next(iter(self._bots.values()))
        raise RuntimeError(f"{self.name}: no bot connected")

    # ── rooms ───────────────────────────────────────────────────────────────

    def watch(self, room_id: str, roster: Roster) -> None:
        self._rosters[room_id] = roster

    def unwatch(self, room_id: str) -> None:
        self._rosters.pop(room_id, None)
        self._members.pop(room_id, None)
        self._last.pop(room_id, None)

    async def room(self, room_id: str) -> RoomInfo:
        bot = self._any_bot(room_id)
        return await self._room_info(bot.rest, await bot.rest.channel(room_id), bot.user_id)

    async def _room_info(self, rest: MattermostREST, channel: dict, me: str) -> RoomInfo:
        kind = channel.get("type")
        archived = bool(channel.get("delete_at"))
        if kind == "D":
            # a direct channel's name is the two ids; the counterpart's username is the handle
            others = [u for u in channel.get("name", "").split("__") if u and u != me]
            name = (await rest.user(others[0])).get("username", others[0]) if others else channel.get("name", "")
            return RoomInfo(channel["id"], name, is_dm=True, archived=archived)
        if kind == "G":
            # a group message is a room, not a 1:1 DM: a post there addresses nobody by itself
            return RoomInfo(channel["id"], channel.get("display_name") or channel.get("name", ""), archived=archived)
        return RoomInfo(channel["id"], channel.get("name", ""), archived=archived)

    async def members(self, room_id: str) -> list[MemberInfo]:
        bot = self._any_bot(room_id)
        try:
            users = await bot.rest.users(await bot.rest.channel_member_ids(room_id))
        except httpx.HTTPError as exc:
            raise PlatformError(f"{self.name}: members of {room_id}: {exc}") from exc
        found = [MemberInfo(u["username"], display_name(u), u.get("nickname", ""), bool(u.get("is_bot")))
                 for u in users if u.get("username")]
        self._members[room_id] = {m.username: m for m in found}
        return found

    def _known(self, room_id: str) -> set[str]:
        roster = self._rosters.get(room_id)
        names = {self.names.agent_of(u) for u in self._members.get(room_id, {})}
        return names | (set(roster.bots) if roster else set())

    # ── posts ───────────────────────────────────────────────────────────────

    async def history(self, room_id: str, after: Position | None = None, before: Position | None = None,
                      limit: int = 60) -> list[Post]:
        bot = self._any_bot(room_id)
        before_id = None if before is None else before[1]
        after_id = None if after is None else after[1]
        try:
            while True:
                page = await bot.rest.posts(room_id, before=before_id, after=after_id, per_page=limit)
                order = page.get("order", [])
                if not order:
                    return []
                posts = []
                for post_id in reversed(order):  # oldest first
                    raw = page.get("posts", {}).get(post_id)
                    if raw is not None and (post := await self._normalize(bot, room_id, raw)) is not None:
                        posts.append(post)
                if posts:
                    if before_id is None and after_id is None:
                        # the newest page: the mark a resync starts after, until a live post moves it
                        self._last.setdefault(room_id, posts[-1].position)
                    return posts
                # a page of nothing but system posts: the next page, not the end of history
                if after_id is not None and before_id is None:
                    after_id = order[0]
                else:
                    before_id = order[-1]
        except httpx.HTTPError as exc:
            raise PlatformError(f"{self.name}: history of {room_id}: {exc}") from exc

    async def post(self, agent: str, room_id: str, text: str, thread: str | None) -> Post:
        bot = self._bots[agent]
        try:
            raw = await bot.rest.create_post(room_id, self.names.outbound(text), thread)
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                logger.warning("%s: post refused (%s): %s", self.name, exc.response.status_code, exc.response.text[:200])
                raise RefusedPost(thread) from exc
            raise PlatformError(f"{self.name}: post: {exc}") from exc
        except httpx.HTTPError as exc:
            raise PlatformError(f"{self.name}: post: {exc}") from exc
        post = await self._normalize(bot, room_id, raw)
        if post is None:  # cannot happen for a post we made; a defensive default
            raise RefusedPost(thread)
        return post

    async def fetch_file(self, agent: str, file_id: str) -> bytes:
        try:
            return await self._bots[agent].rest.file(file_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 404):
                raise KeyError(file_id) from exc
            raise PlatformError(f"{self.name}: file {file_id}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise PlatformError(f"{self.name}: file {file_id}: {exc}") from exc

    async def typing(self, agent: str, room_id: str, thread: str | None) -> None:
        bot = self._bots.get(agent)
        if bot is None or bot.ws is None:
            return
        try:
            await bot.ws.typing(room_id, thread)
        except Exception as exc:  # noqa: BLE001 - best effort by contract
            logger.debug("%s: typing not sent: %s", self.name, exc)

    async def _normalize(self, bot: _Bot, room_id: str, raw: dict) -> Post | None:
        roster = self._rosters.get(room_id) or Roster(())
        author = await bot.rest.user(raw.get("user_id", "")) if raw.get("user_id") else {}
        return normalize(raw, author, roster, self.names, self._known(room_id))

    def _seen_live(self, room_id: str, post: Post) -> None:
        """The newest position a connection delivered: what a resync starts after. A
        history read does not move it; the connection did not see those."""
        if room_id not in self._last or post.position > self._last[room_id]:
            self._last[room_id] = post.position

    # ── the websocket ───────────────────────────────────────────────────────

    def _frame_handler(self, bot: _Bot):
        async def handle(frame: dict) -> None:
            event, data, broadcast = frame.get("event"), frame.get("data") or {}, frame.get("broadcast") or {}
            if event == "posted":
                raw = json.loads(data["post"])
                room_id = raw.get("channel_id", "")
                if room_id not in self._rosters:
                    return
                post = await self._normalize(bot, room_id, raw)
                if post is not None:
                    self._seen_live(room_id, post)
                    await self.on_event(Posted(room_id, post, seen_by=bot.agent))
            elif event in ("user_added", "user_removed"):
                # the two carry their ids in mirrored places (frozen gateway §2.7)
                room_id = data.get("channel_id") or broadcast.get("channel_id") or ""
                user_id = data.get("user_id") or broadcast.get("user_id") or ""
                if not room_id or not user_id:
                    return
                joined = event == "user_added"
                if user_id == bot.user_id:
                    (bot.rooms.add if joined else bot.rooms.discard)(room_id)
                username = (await bot.rest.user(user_id)).get("username", user_id)
                await self.on_event(Membership(room_id, self.names.agent_of(username), joined))
        return handle

    def _resync_handler(self, bot: _Bot):
        async def resync() -> None:
            """After a reconnect: what the connection missed, re-emitted as posts. The
            room log drops what it already holds."""
            for room_id in list(bot.rooms):
                if room_id not in self._rosters:
                    continue
                try:
                    while True:  # every page the socket missed, not the first one
                        page = await self.history(room_id, after=self._last.get(room_id))
                        for post in page:
                            self._seen_live(room_id, post)
                            await self.on_event(Posted(room_id, post, seen_by=bot.agent))
                        if not page:
                            break
                except Exception:  # noqa: BLE001
                    logger.exception("%s: resync of %s failed", self.name, room_id)
        return resync
