"""The Mattermost connector against a Mattermost in memory: the handful of v4
endpoints the connector calls, served by an httpx transport, and a websocket stand-in
the test feeds frames to. The server's shapes are the ones the docker probe
recorded on 11.x (newest-first pages, ``before``/``after`` by post id, JSON-encoded
``data.post`` on the wire, ``is_bot`` only on bot accounts)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import yaml

from coop.config import parse
from coop.connectors.base import Membership, Posted
from coop.connectors.mattermost.connector import MattermostConnector
from coop.connectors.mattermost.normalize import normalize
from coop.room.attachments import AttachmentRefused
from coop.room.model import Upload
from coop.room.platform import PlatformError, RefusedAttachment, RefusedPost
from coop.room.roster import Roster

SERVER = parse(yaml.safe_load("""
servers:
  lab:
    type: mattermost
    url: https://mm.example
    team: lab
    owners: [glin]
    bots:
      laomei:  {username: laomei_bot, token_file: a.tok}
      wavebro: {username: wavebro_bot, token_file: b.tok}
      muse:    {username: muse}
agents:
  laomei:  {type: remote, token_file: a.agent}
  wavebro: {type: remote, token_file: b.agent}
"""), Path("/srv")).servers["lab"]

ROSTER = Roster(("laomei_bot", "wavebro_bot", "muse"), {"laomei_bot": frozenset({"glin"}), "wavebro_bot": frozenset({"glin"}),
                                                       "muse": frozenset()}, frozenset())


class FakeMattermost:
    """Users, channels, memberships and posts, behind the endpoints the connector calls."""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}  # token -> user id
        self.channels: dict[str, dict] = {}
        self.members: dict[str, list[str]] = {}
        self.posts: dict[str, list[dict]] = {}
        self.files: dict[str, bytes] = {}
        self.uploads: list[dict] = []  # what ``POST /files`` received: the headers and query a real server sees
        self.refuse_posts = False
        self.refuse_files = False
        self.failure: Exception | None = None  # raised by every request while set: the server is unreachable
        self.not_found = False  # every request answers 404
        self.html = False  # every request answers 200 with a page, as a proxy in the way does
        self._ms = 1_700_000_000_000
        self._n = 0
        self.sockets: list[FakeSocket] = []

    def user(self, username: str, is_bot: bool = False, token: str | None = None) -> str:
        uid = f"u{len(self.users) + 1:02d}"
        self.users[uid] = {"id": uid, "username": username, "first_name": username.title(), "nickname": ""}
        if is_bot:
            self.users[uid]["is_bot"] = True
        if token:
            self.tokens[token] = uid
        return uid

    def channel(self, name: str, kind: str = "O", members: list[str] = (), team_id: str = "team1") -> str:
        cid = f"c{len(self.channels) + 1:02d}"
        self.channels[cid] = {"id": cid, "name": name, "display_name": name, "type": kind, "team_id": team_id,
                              "delete_at": 0}
        self.members[cid] = list(members)
        self.posts[cid] = []
        return cid

    def post(self, channel_id: str, user_id: str, message: str, root_id: str = "", files=(), kind: str = "") -> dict:
        self._n += 1
        self._ms += 7
        post = {"id": f"p{self._n:03d}", "channel_id": channel_id, "user_id": user_id, "message": message,
                "root_id": root_id, "create_at": self._ms, "type": kind, "props": {},
                "metadata": {"files": [{"id": f["id"], "name": f["name"], "mime_type": "text/plain", "size": 5}
                                       for f in files]}}
        self.posts[channel_id].append(post)
        return post

    async def deliver(self, post: dict) -> None:
        """What the server broadcasts: every member's socket gets the ``posted`` frame."""
        for sock in self.sockets:
            if sock.user_id in self.members[post["channel_id"]]:
                await sock.feed({"event": "posted", "data": {"post": json.dumps(post), "channel_type": "O"},
                                 "broadcast": {"channel_id": post["channel_id"]}})

    # the transport
    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.failure is not None:
            raise self.failure
        if self.not_found:
            return httpx.Response(404, json={"message": "nothing here"})
        if self.html:
            return httpx.Response(200, text="<html>sign in</html>")
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        me = self.tokens.get(token)
        if me is None:
            return httpx.Response(401, json={"message": "bad token"})
        path = request.url.path.removeprefix("/api/v4")
        parts = path.strip("/").split("/")
        if parts[0] == "users":
            return self._users(path, parts, request, me)
        if parts[0] == "channels":
            return self._channels(parts, dict(request.url.params))
        if path == "/posts" and request.method == "POST":
            return self._create_post(request, me)
        if path == "/files" and request.method == "POST":
            return self._upload(request)
        if parts[0] == "files" and len(parts) == 2:
            return httpx.Response(200, content=self.files[parts[1]])
        return httpx.Response(404, json={"message": f"no route {path}"})

    def _create_post(self, request: httpx.Request, me: str) -> httpx.Response:
        if self.refuse_posts:
            return httpx.Response(403, json={"message": "no"})
        body = json.loads(request.content)
        files = [{"id": fid, "name": self.uploads[int(fid[1:]) - 1]["filename"]} for fid in body.get("file_ids", [])]
        post = self.post(body["channel_id"], me, body["message"], body.get("root_id", ""), files=files)
        asyncio.get_running_loop().create_task(self.deliver(post))
        return httpx.Response(201, json=post)

    def _upload(self, request: httpx.Request) -> httpx.Response:
        """The simple form of ``POST /files``: the file as the body, ``channel_id`` and
        ``filename`` as query parameters, a ``Content-Length`` the server insists on."""
        if self.refuse_files:
            return httpx.Response(413, json={"message": "Unable to upload file. File is too large."})
        params = dict(request.url.params)
        if "Content-Length" not in request.headers or request.headers.get("Transfer-Encoding") == "chunked":
            return httpx.Response(400, json={"message": "content length required"})
        fid = f"f{len(self.uploads) + 1}"
        self.files[fid] = request.content
        self.uploads.append({"filename": params.get("filename"), "channel_id": params.get("channel_id"),
                             "content_type": request.headers.get("Content-Type"),
                             "content_length": request.headers.get("Content-Length")})
        return httpx.Response(201, json={"file_infos": [{"id": fid, "name": params.get("filename"), "size": len(request.content)}]})

    def _users(self, path: str, parts: list[str], request: httpx.Request, me: str) -> httpx.Response:
        if path == "/users/me":
            return httpx.Response(200, json=self.users[me])
        if path == "/users/me/teams":
            return httpx.Response(200, json=[{"id": "team1", "name": "lab"}, {"id": "team2", "name": "other"}])
        if path == "/users/ids":
            return httpx.Response(200, json=[self.users[u] for u in json.loads(request.content) if u in self.users])
        if len(parts) == 3 and parts[2] == "channels":
            return httpx.Response(200, json=[c for cid, c in self.channels.items() if parts[1] in self.members[cid]])
        if len(parts) == 2:
            return httpx.Response(200 if parts[1] in self.users else 404, json=self.users.get(parts[1], {}))
        return httpx.Response(404, json={"message": f"no route {path}"})

    def _channels(self, parts: list[str], params: dict) -> httpx.Response:
        if len(parts) == 2:
            return httpx.Response(200, json=self.channels[parts[1]])
        if parts[2] == "members":
            page, per_page = int(params.get("page", 0)), int(params.get("per_page", 200))
            users = self.members[parts[1]][page * per_page:(page + 1) * per_page]
            return httpx.Response(200, json=[{"user_id": u, "channel_id": parts[1]} for u in users])
        if parts[2] == "posts":
            return self._page(parts[1], params)
        return httpx.Response(404, json={"message": "no route"})

    def _page(self, channel_id: str, params: dict) -> httpx.Response:
        posts = list(self.posts[channel_id])
        ids = [p["id"] for p in posts]
        if "before" in params:
            posts = posts[:ids.index(params["before"])]
        per_page = int(params.get("per_page", 60))
        if "after" in params:
            posts = posts[ids.index(params["after"]) + 1:][:per_page]  # the chunk nearest the anchor
        else:
            posts = posts[-per_page:]
        order = [p["id"] for p in reversed(posts)]
        return httpx.Response(200, json={"order": order, "posts": {p["id"]: p for p in posts},
                                         "prev_post_id": "", "next_post_id": ""})


class FakeSocket:
    """Stands in for :class:`MattermostWebSocket`: frames the test feeds reach the
    connector's handler; a reconnect is simulated by calling ``on_reconnect``."""

    def __init__(self, server: FakeMattermost):
        self._server = server
        self.user_id = ""
        self.typed: list[tuple[str, str | None]] = []
        self.stopped = False

    def __call__(self, url, token, on_frame, on_reconnect=None):
        self.user_id = self._server.tokens[token]
        self.on_frame, self.on_reconnect = on_frame, on_reconnect
        self._server.sockets.append(self)
        return self

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.stopped = True

    async def typing(self, channel_id: str, parent_id: str | None = None) -> None:
        self.typed.append((channel_id, parent_id))

    async def feed(self, frame: dict) -> None:
        await self.on_frame(frame)


@pytest.fixture
def mm():
    server = FakeMattermost()
    glin = server.user("glin")
    laomei = server.user("laomei_bot", is_bot=True, token="tok-laomei")
    wavebro = server.user("wavebro_bot", is_bot=True, token="tok-wavebro")
    muse = server.user("muse")  # a bot on another gateway, as a plain account
    hook = server.user("hook", is_bot=True)
    server.channel("team", members=[glin, laomei, wavebro, muse, hook])
    server.channel("elsewhere", members=[glin, laomei], team_id="team2")  # another team: not served
    server.channel(f"{glin}__{laomei}", kind="D", members=[glin, laomei])
    server.ids = {"glin": glin, "laomei": laomei, "wavebro": wavebro, "muse": muse, "hook": hook}
    return server


@pytest.fixture
async def connected(mm):
    seen: list = []

    async def on_event(event):
        seen.append(event)

    connector = MattermostConnector(SERVER, on_event, transport=httpx.MockTransport(mm.handle),
                                    websocket=FakeSocket(mm))
    rooms = await connector.add_bot("laomei_bot", "tok-laomei")
    connector.watch("c01", ROSTER)
    await connector.members("c01")
    yield connector, mm, seen, rooms
    await connector.stop()


async def test_a_post_with_files_uploads_each_streamed_with_its_length_then_posts_their_ids(connected):
    connector, mm, seen, _ = connected

    async def two_chunks():
        yield b"hel"
        yield b"lo"

    post = await connector.post("laomei_bot", "c01", "the file", None,
                                files=(Upload("a.txt", "text/plain", 5, two_chunks()), Upload.of("b.bin", b"zz")))
    assert [(u["filename"], u["channel_id"], u["content_type"], u["content_length"]) for u in mm.uploads] == [
        ("a.txt", "c01", "text/plain", "5"), ("b.bin", "c01", "application/octet-stream", "2")]
    assert mm.files["f1"] == b"hello" and [a.name for a in post.attachments] == ["a.txt", "b.bin"]
    mm.refuse_files = True
    with pytest.raises(RefusedAttachment, match="refused the file c.txt: 413"):
        await connector.post("laomei_bot", "c01", "again", None, files=(Upload.of("c.txt", b"c"),))
    assert len(mm.posts["c01"]) == 1  # nothing posted for a refused file


async def test_files_uploaded_before_a_later_failure_are_said_to_stay_on_the_server(connected, caplog):
    connector, mm, seen, _ = connected
    first = Upload.of("ok.txt", b"ok")

    async def refused_second():
        yield b"x"
        raise AttachmentRefused("attachment two.bin is over the limit", "two.bin")

    with caplog.at_level("WARNING"), pytest.raises(AttachmentRefused):
        await connector.post("laomei_bot", "c01", "x", None, files=(first, Upload("two.bin", "", 2, refused_second())))
    assert mm.files["f1"] == b"ok" and len(mm.posts["c01"]) == 0
    assert any("files f1 uploaded for a post that was not made stay on the server unattached" in r.message
               for r in caplog.records)


async def test_what_the_gateways_limits_raise_while_a_file_is_read_passes_through_the_upload(connected):
    connector, mm, seen, _ = connected

    async def over():
        yield b"x"
        raise AttachmentRefused("attachment x.bin is over the limit")

    with pytest.raises(AttachmentRefused):
        await connector.post("laomei_bot", "c01", "x", None, files=(Upload("x.bin", "", 2, over()),))
    assert mm.uploads == [] and len(mm.posts["c01"]) == 0


async def test_add_bot_lists_the_teams_rooms_and_dms_with_the_counterparts_name(connected):
    connector, mm, seen, rooms = connected
    assert [(r.id, r.name, r.kind) for r in rooms] == [("c01", "team", "channel"), ("c03", "glin", "dm")]
    with pytest.raises(ValueError, match="not 'wavebro_bot'"):
        await connector.add_bot("wavebro_bot", "tok-laomei")
    with pytest.raises(httpx.HTTPStatusError):
        await connector.add_bot("wavebro_bot", "tok-nope")


async def test_a_posted_frame_becomes_a_normalised_post_delivered_as_written(connected):
    connector, mm, seen, _ = connected
    post = mm.post("c01", mm.ids["glin"], "@laomei_bot and @muse: please look\nmute: @wavebro_bot")
    await mm.deliver(post)
    assert len(seen) == 1 and isinstance(seen[0], Posted) and seen[0].seen_by == "laomei_bot"
    found = seen[0].post
    assert found.author == "glin" and found.kind == "human" and found.author_role == ""
    assert found.text == "@laomei_bot and @muse: please look\nmute: @wavebro_bot"
    assert found.mentions == {"laomei_bot", "muse"} and found.controls.mute == ("wavebro_bot",)
    assert found.thread == found.id and found.position == (post["create_at"], post["id"])
    # a frame for a room nobody watches is dropped, a system post too
    await mm.deliver(mm.post("c02", mm.ids["glin"], "x"))
    await mm.deliver(mm.post("c01", mm.ids["glin"], "joined", kind="system_join_channel"))
    assert len(seen) == 1


async def test_kinds_come_from_the_roster_first_and_the_platform_last(connected):
    connector, mm, seen, _ = connected
    for who in ("muse", "hook", "laomei"):
        await mm.deliver(mm.post("c01", mm.ids[who], "hi"))
    kinds = {e.post.author: e.post.kind for e in seen}
    assert kinds == {"muse": "bot", "hook": "bot", "laomei_bot": "bot"}  # muse by roster, hook by is_bot
    members = {m.username: m for m in await connector.members("c01")}
    assert members["glin"].display_name == "Glin" and members["hook"].is_bot and not members["muse"].is_bot


async def test_history_pages_by_post_id_oldest_first_and_resync_re_emits_what_a_reconnect_missed(connected):
    connector, mm, seen, _ = connected
    posts = [mm.post("c01", mm.ids["glin"], f"n{i}") for i in range(7)]
    page = await connector.history("c01", limit=3)
    assert [p.text for p in page] == ["n4", "n5", "n6"]
    older = await connector.history("c01", before=page[0].position, limit=3)
    assert [p.text for p in older] == ["n1", "n2", "n3"]
    newer = await connector.history("c01", after=(posts[4]["create_at"], posts[4]["id"]))
    assert [p.text for p in newer] == ["n5", "n6"]
    await mm.deliver(posts[2])  # a live copy older than the history already read: the mark stays at n6
    mm.post("c01", mm.ids["glin"], "missed")
    await mm.sockets[0].on_reconnect()
    assert [e.post.text for e in seen] == ["n2", "missed"]


async def test_a_post_goes_out_as_written_and_comes_back_as_the_echo(connected):
    connector, mm, seen, _ = connected
    post = await connector.post("laomei_bot", "c01", "@wavebro_bot @glin done", None)
    assert mm.posts["c01"][-1]["message"] == "@wavebro_bot @glin done"
    assert post.author == "laomei_bot" and post.text == "@wavebro_bot @glin done" and post.thread == post.id
    await asyncio.sleep(0.01)
    assert [e.post.id for e in seen] == [post.id]  # the echo, for the room log to drop
    reply = await connector.post("laomei_bot", "c01", "in thread", post.id)
    assert reply.thread == post.id
    mm.refuse_posts = True
    with pytest.raises(RefusedPost):
        await connector.post("laomei_bot", "c01", "x", None)


async def test_membership_frames_name_the_username_and_track_the_bots_own_rooms(connected):
    connector, mm, seen, _ = connected
    sock = mm.sockets[0]
    await sock.feed({"event": "user_added", "data": {"user_id": mm.ids["wavebro"], "team_id": "team1"},
                     "broadcast": {"channel_id": "c01"}})
    await sock.feed({"event": "user_removed", "data": {"channel_id": "c02"}, "broadcast": {"user_id": mm.ids["laomei"]}})
    assert seen == [Membership("c01", "wavebro_bot", True), Membership("c02", "laomei_bot", False)]
    assert "c02" not in connector._bots["laomei_bot"].rooms
    await connector.typing("laomei_bot", "c01", "p001")
    assert sock.typed == [("c01", "p001")]
    await connector.typing("nobody", "c01", None)  # best effort, never raises


async def test_a_new_direct_channel_is_the_bots_own_join_described_from_that_bots_side(connected):
    """Mattermost announces a new direct channel with ``direct_added`` (frame as probed on
    11.7: ``creator_id``/``teammate_id`` in data, the channel in broadcast, no user id), to
    both members, so the member is this connection's bot. ``room()`` then reads through
    the bot that joined, whose own join is tracked before the event is delivered, so the
    DM is named for the counterpart, not read through the first bot and named for the
    joining bot. The join is ``created``: the room was made around the bot."""
    connector, mm, seen, _ = connected
    await connector.add_bot("wavebro_bot", "tok-wavebro")
    glin, wavebro = mm.ids["glin"], mm.ids["wavebro"]
    dm = mm.channel(f"{wavebro}__{glin}", kind="D", members=[glin, wavebro])  # the joining bot's id first
    await mm.sockets[1].feed({"event": "direct_added", "data": {"creator_id": glin, "teammate_id": wavebro},
                              "broadcast": {"channel_id": dm}})
    assert seen[-1] == Membership(dm, "wavebro_bot", True, created=True) and dm in connector._bots["wavebro_bot"].rooms
    info = await connector.room(dm)
    assert (info.name, info.kind) == ("glin", "dm")
    await mm.sockets[1].feed({"event": "direct_added", "data": {"creator_id": glin, "teammate_id": wavebro},
                              "broadcast": {"channel_id": dm}})  # reopened: the connection knew the room
    assert seen[-1] == Membership(dm, "wavebro_bot", True, created=False)


async def test_a_new_group_message_is_the_members_own_join(connected):
    """``group_added`` goes to each member with the member in ``broadcast`` (probed on 11.7);
    a group's members cannot change afterwards, so this is its only join."""
    connector, mm, seen, _ = connected
    glin, laomei, wavebro = mm.ids["glin"], mm.ids["laomei"], mm.ids["wavebro"]
    group = mm.channel("glin, laomei_bot, wavebro_bot", kind="G", members=[glin, laomei, wavebro])
    await mm.sockets[0].feed({"event": "group_added", "data": {"teammate_ids": json.dumps([glin, wavebro])},
                              "broadcast": {"user_id": laomei, "channel_id": group}})
    assert seen[-1] == Membership(group, "laomei_bot", True, created=True) and group in connector._bots["laomei_bot"].rooms
    assert (await connector.room(group)).kind == "group"


async def test_a_post_in_a_room_the_bot_was_never_told_it_joined_is_a_warning(connected, caplog):
    connector, mm, seen, _ = connected
    glin, laomei = mm.ids["glin"], mm.ids["laomei"]
    unknown = mm.channel("later", members=[glin, laomei])
    with caplog.at_level("WARNING", logger="coop.connectors.mattermost.connector"):
        await mm.deliver(mm.post(unknown, glin, "anyone?"))
        await mm.deliver(mm.post("c03", glin, "unruled"))  # c03 is listed for the bot and hosted by no rule
        await mm.deliver(mm.post("c02", glin, "another team"))  # c02 is the bot's on team2: listed, not a room here
    assert [r.getMessage() for r in caplog.records] == [f"lab: 'laomei_bot' posted in {unknown}, a room it was not told it joined"]
    assert not any(isinstance(e, Posted) for e in seen[-2:])


async def test_room_wraps_a_platform_failure(connected):
    connector, mm, _, _ = connected
    mm.failure = httpx.ConnectError("down")
    with pytest.raises(PlatformError):
        await connector.room("c03")


async def test_files_are_fetched_and_the_dm_room_is_described_by_id(connected):
    connector, mm, seen, _ = connected
    mm.files["f1"] = b"hello"
    assert b"".join([c async for c in connector.fetch_file("laomei_bot", "f1")]) == b"hello"
    info = await connector.room("c03")
    assert info.name == "glin" and info.is_dm
    post = mm.post("c01", mm.ids["glin"], "see", files=({"id": "f1", "name": "a.txt"},))
    await mm.deliver(post)
    assert seen[-1].post.attachments[0].name == "a.txt" and seen[-1].post.attachments[0].id == "f1"


def test_normalize_is_pure_and_marks_webhooks_as_integrations():
    raw = {"id": "p1", "user_id": "u9", "message": "hi @laomei_bot", "create_at": 5000, "root_id": "",
           "type": "", "props": {"from_webhook": "true"}}
    post = normalize(raw, {"username": "ci", "is_bot": True}, ROSTER, {"laomei_bot"})
    assert post.kind == "integration" and post.text == "hi @laomei_bot" and post.mentions == {"laomei_bot"}
    assert post.time == 5.0 and post.position == (5000, "p1") and post.author == "ci"
    assert normalize({**raw, "type": "system_add_to_channel"}, {}, ROSTER, set()) is None


async def test_resync_pages_through_everything_the_socket_missed(connected):
    connector, mm, seen, _ = connected
    await mm.deliver(mm.post("c01", mm.ids["glin"], "last seen"))
    for i in range(130):
        mm.post("c01", mm.ids["glin"], f"missed {i}")
    await mm.sockets[0].on_reconnect()
    assert [e.post.text for e in seen][-1] == "missed 129" and len(seen) == 131


async def test_history_pages_past_a_page_of_system_posts(connected):
    connector, mm, seen, _ = connected
    real = [mm.post("c01", mm.ids["glin"], f"real {i}") for i in range(3)]
    for _ in range(60):
        mm.post("c01", mm.ids["glin"], "joined", kind="system_join_channel")
    page = await connector.history("c01", limit=60)
    assert [p.text for p in page] == ["real 0", "real 1", "real 2"] and page[0].id == real[0]["id"]
    assert await connector.history("c01", before=page[0].position) == []  # and the end is still the end


async def test_transport_failures_are_platform_errors_and_a_missing_file_a_key_error(connected):
    connector, mm, seen, _ = connected
    from coop.room.platform import PlatformError

    mm.failure = httpx.ConnectError("down")
    with pytest.raises(PlatformError):
        await connector.post("laomei_bot", "c01", "x", None)
    mm.failure, mm.html = None, True  # a 200 that is not JSON is the platform not answering, not a ValueError
    with pytest.raises(PlatformError):
        await connector.room("c01")
    mm.html = False
    mm.failure = httpx.ConnectError("down")
    with pytest.raises(PlatformError):
        await anext(connector.fetch_file("laomei_bot", "f1"))
    with pytest.raises(PlatformError):
        await connector.history("c01")
    mm.failure, mm.not_found = None, True
    with pytest.raises(KeyError):
        await anext(connector.fetch_file("laomei_bot", "f1"))


async def test_resync_starts_after_the_startup_history_when_no_live_post_came_yet(connected):
    connector, mm, seen, _ = connected
    mm.post("c01", mm.ids["glin"], "seen at start")
    await connector.history("c01")  # the daemon's backfill at open
    for i in range(70):
        mm.post("c01", mm.ids["glin"], f"during the outage {i}")
    await mm.sockets[0].on_reconnect()
    assert [e.post.text for e in seen] == [f"during the outage {i}" for i in range(70)]


async def test_members_are_paged_past_two_hundred(connected):
    connector, mm, seen, _ = connected
    for i in range(250):
        uid = mm.user(f"member{i}")
        mm.members["c01"].append(uid)
    found = await connector.members("c01")
    assert len(found) == 255 and any(m.username == "member249" for m in found)


async def test_a_group_message_is_a_room_not_a_one_to_one_dm(mm):
    glin, laomei, wavebro = mm.ids["glin"], mm.ids["laomei"], mm.ids["wavebro"]
    mm.channel("glin, laomei_bot, wavebro_bot", kind="G", members=[glin, laomei, wavebro])
    seen = []

    async def on_event(event):
        seen.append(event)
    connector = MattermostConnector(SERVER, on_event, transport=httpx.MockTransport(mm.handle), websocket=FakeSocket(mm))
    rooms = await connector.add_bot("laomei_bot", "tok-laomei")
    group = next(r for r in rooms if r.name == "glin, laomei_bot, wavebro_bot")
    assert group.kind == "group" and not group.is_dm and next(r for r in rooms if r.id == "c03").is_dm
    mm.failure = httpx.ConnectError("down")
    with pytest.raises(PlatformError):
        await connector.members("c01")
    await connector.stop()
