"""Thin Mattermost REST API (v4) client for auth, posting, and room resolution."""

import asyncio
import datetime
import logging
import mimetypes
import secrets
from pathlib import Path
from typing import Any

import httpx

from ...core.connector import HistoryPage

logger = logging.getLogger("coop.connectors.mattermost.rest")


class RoomNotFoundError(Exception):
    """Raised when a channel/team name cannot be resolved because it does not exist.

    Distinct from transport/auth/API failures, which should propagate as-is
    so callers can distinguish a missing room from a broader infrastructure
    problem.
    """


# Mattermost's four channel types, and what each means to the rest of the gateway.
# `O` open/public, `P` private, `D` 1:1 DM, `G` group DM (§6.4 — Mattermost distinguishes
# group DMs on the wire, which Rocket.Chat does not).
#
# Stated once because the previous mapping was inline and partial: it recognised `P` and
# called everything else a channel, so an id-based lookup would have typed a DM as a
# channel. Every `type == "dm"` gate would then have inverted — the mention requirement
# would start applying to DMs, which §2.7 records as making the agent answer every message
# from anyone in the room.
_ROOM_TYPES = {"O": "channel", "P": "group", "D": "dm", "G": "group_dm"}


def room_type_for(channel_type: str | None) -> str:
    """Map a Mattermost channel type to the gateway's room type.

    An unknown or missing value falls back to `channel`, which is the conservative answer:
    a channel requires a mention where a DM does not, so guessing `channel` cannot turn a
    quiet room into a talkative one.
    """
    return _ROOM_TYPES.get(channel_type or "", "channel")


class MattermostREST:
    """Async REST client for the Mattermost v4 API.

    Auth is dual-mode:
      - token mode: a Personal Access Token / Bot Account access token is used
        directly as a bearer token. No login call, no expiry, no re-login logic.
      - username/password mode: POST /api/v4/users/login exchanges credentials
        for a session token (returned in the ``Token`` response header — a
        Mattermost quirk, unlike Rocket.Chat which returns it in the JSON body).
        Session tokens can expire, so 401s trigger a re-login the same way
        RocketChatREST does.

    Both modes present the resulting token the same way afterwards:
    ``Authorization: Bearer <token>``.
    """

    def __init__(
        self,
        server_url: str,
        token: str = "",
        username: str = "",
        password: str = "",
    ):
        self.server_url = server_url.rstrip("/")
        self._token: str | None = token or None
        self._username: str | None = username or None
        self._password: str | None = password or None
        self.bot_user_id: str | None = None
        self.bot_username: str | None = None
        self.team_id: str | None = None
        self._user_cache: dict[str, str] = {}  # user_id -> username
        self._client = httpx.AsyncClient(timeout=30.0)
        self._download_client = httpx.AsyncClient(timeout=60.0)
        # Serializes concurrent re-login attempts (login mode only). See
        # RocketChatREST._relogin_lock for the race this prevents.
        self._relogin_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return (
            f"MattermostREST(server_url={self.server_url!r}, "
            f"username={self._username!r}, token={'set' if self._token else 'unset'})"
        )

    @property
    def _is_login_mode(self) -> bool:
        """True when credentials support automatic re-login on token expiry.

        Token-mode auth (PAT / bot access token) has no username/password to
        re-login with — a 401 there means the token was revoked or is wrong,
        which is an operational problem to surface, not something to retry.
        """
        return bool(self._username and self._password)

    async def close(self) -> None:
        """Close shared HTTP clients and release connection pool resources."""
        await self._client.aclose()
        await self._download_client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token or ''}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_data: Any = None,
        params: dict | None = None,
    ) -> Any:
        url = f"{self.server_url}/api/v4/{endpoint}"
        sent_token = self._token  # capture before the request
        response = await self._client.request(
            method, url, headers=self._headers(), json=json_data, params=params
        )
        if response.status_code == 401 and self._is_login_mode:
            async with self._relogin_lock:
                # Skip re-login if another coroutine already refreshed the
                # token while we were waiting for the lock (see RocketChatREST
                # for the same pattern and its rationale).
                if self._token == sent_token:
                    logger.warning("Auth token expired, re-logging in...")
                    await self.login(self._username, self._password)  # type: ignore[arg-type]
            response = await self._client.request(
                method, url, headers=self._headers(), json=json_data, params=params
            )
        if not response.is_success:
            logger.error(
                "Mattermost API error %d for %s %s — body: %s",
                response.status_code,
                method,
                endpoint,
                response.text[:500],
            )
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()

    async def authenticate(self) -> None:
        """Establish auth: use the configured token directly, or log in.

        Must be called before any other request. Does not resolve identity —
        call get_me() afterwards to learn the bot's own user id (required for
        the own-message filter regardless of auth mode).
        """
        if self._token:
            logger.info("Mattermost auth: using configured token")
            return
        if not (self._username and self._password):
            raise RuntimeError(
                "MattermostREST: no token or username/password configured"
            )
        await self.login(self._username, self._password)

    async def login(self, username: str, password: str) -> None:
        """Log in via username/password and store the session token.

        Username and password are stored as instance attributes to support
        automatic re-login when the session token expires (see _request's
        401 handling) — same trade-off RocketChatREST makes.
        """
        url = f"{self.server_url}/api/v4/users/login"
        response = await self._client.post(
            url, json={"login_id": username, "password": password}
        )
        response.raise_for_status()
        token = response.headers.get("Token")
        if not token:
            raise RuntimeError(
                "Mattermost login succeeded but no 'Token' response header was returned"
            )
        self._token = token
        self._username = username
        self._password = password
        logger.info("Logged in as %s", username)

    async def get_me(self) -> dict[str, Any]:
        """Fetch the authenticated bot's own user object.

        Called once during connect() regardless of auth mode, and the
        returned id stored as bot_user_id — this is the own-message-filter
        anchor. Token mode has no login response to pull an identity from,
        so this call is mandatory: skipping it means the bot cannot recognize
        its own posts and will reply to itself.
        """
        result = await self._request("GET", "users/me")
        self.bot_user_id = result["id"]
        self.bot_username = result["username"]
        logger.info("Resolved bot identity: %s (id=%s)", self.bot_username, self.bot_user_id)
        return result

    async def resolve_team(self, team: str) -> str:
        """Resolve a team name (or ID) to its team_id and cache it.

        Uses GET /users/me/teams (the teams the bot is a member of) rather
        than GET /teams/name/{name}. Confirmed empirically against a live
        server: a non-system-admin bot account gets 403 from
        /teams/name/{name} regardless of team membership, while
        /users/me/teams works for any authenticated user and needs no
        elevated permissions. The bot must be a member of the target team
        (e.g. via `mmctl team add <team> <username>`) for this to find it.
        """
        my_teams = await self._request("GET", "users/me/teams")
        for t in my_teams:
            if t.get("name") == team or t.get("id") == team:
                self.team_id = t["id"]
                logger.info("Resolved team '%s' -> id=%s", team, self.team_id)
                return self.team_id
        raise RoomNotFoundError(
            f"Team '{team}' not found among the bot's teams — is the bot "
            f"account a member of this team? (e.g. `mmctl team add {team} <username>`)"
        )

    async def get_user_by_username(self, username: str) -> dict[str, Any]:
        return await self._request("GET", f"users/username/{username}")

    async def resolve_username(self, user_id: str) -> str:
        """Resolve a user ID to a username, with an in-memory cache.

        Needed because Mattermost's mention/sender data gives user IDs, not
        usernames (Rocket.Chat gives usernames directly).
        """
        cached = self._user_cache.get(user_id)
        if cached is not None:
            return cached
        result = await self._request("GET", f"users/{user_id}")
        username = result.get("username", user_id)
        self._user_cache[user_id] = username
        return username

    async def get_file_info(self, file_id: str) -> dict[str, Any]:
        """Fetch a file's metadata (name, size, mime_type) without downloading it.

        Mattermost's post.file_ids only carries bare IDs — unlike RC, which
        embeds name/size/type directly in the message doc — so a separate
        lookup is required before download_file() to know what we're saving.
        """
        return await self._request("GET", f"files/{file_id}/info")

    async def post_message(
        self,
        channel_id: str,
        text: str,
        root_id: str | None = None,
        file_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Post a message to a channel by ID.

        Unlike Rocket.Chat's chat.postMessage (two mutually exclusive
        channel/roomId schemas), Mattermost's POST /posts always takes
        channel_id plus optional root_id (thread) and file_ids in one schema.
        """
        payload: dict[str, Any] = {"channel_id": channel_id, "message": text}
        if root_id:
            payload["root_id"] = root_id
        if file_ids:
            payload["file_ids"] = file_ids
        result = await self._request("POST", "posts", json_data=payload)
        logger.info(
            "Posted message to channel %s%s", channel_id, f" (thread {root_id})" if root_id else ""
        )
        return result

    async def download_file(self, file_id: str, dest_path: str) -> None:
        """Download a file attachment (authenticated) to a local path.

        Same atomic tmp-then-rename + accumulate-in-memory-then-write pattern
        as RocketChatREST.download_file — see that docstring for the
        rationale (caller already enforces max_file_size_mb; offloading the
        write to a thread keeps the event loop responsive).
        """
        url = f"{self.server_url}/api/v4/files/{file_id}"
        dest = Path(dest_path)
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        tmp_path = dest.with_name(f"{dest.name}.{secrets.token_hex(8)}.tmp")

        def _stream_download(headers: dict[str, str]):
            return self._download_client.stream("GET", url, headers=headers)

        async def _collect_chunks(stream_ctx) -> bytes:
            chunks: list[bytes] = []
            async with stream_ctx as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
            return b"".join(chunks)

        def _write_and_rename(data: bytes) -> None:
            with open(tmp_path, "wb") as f:
                f.write(data)
            tmp_path.replace(dest)

        headers = {"Authorization": f"Bearer {self._token or ''}"}
        sent_token = self._token
        try:
            need_reauth = False
            data: bytes = b""
            async with _stream_download(headers) as first_response:
                if first_response.status_code == 401 and self._is_login_mode:
                    need_reauth = True
                else:
                    first_response.raise_for_status()
                    chunks: list[bytes] = []
                    async for chunk in first_response.aiter_bytes():
                        chunks.append(chunk)
                    data = b"".join(chunks)

            if need_reauth:
                async with self._relogin_lock:
                    if self._token == sent_token:
                        logger.warning("Auth token expired during download, re-logging in...")
                        await self.login(self._username, self._password)  # type: ignore[arg-type]
                headers = {"Authorization": f"Bearer {self._token or ''}"}
                data = await _collect_chunks(_stream_download(headers))

            await asyncio.to_thread(_write_and_rename, data)
        except Exception:
            await asyncio.to_thread(tmp_path.unlink, missing_ok=True)
            raise
        logger.info("Downloaded attachment to %s", dest_path)

    async def upload_file(self, channel_id: str, file_path: str) -> list[str]:
        """Upload a file to a channel and return its file_id(s).

        The returned file_ids are attached to a post via post_message(...,
        file_ids=[...]) — Mattermost separates upload from posting (unlike
        RC's rooms.upload, which does both in one call).
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Attachment not found: {file_path}")

        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type is None:
            mime_type = "application/octet-stream"

        url = f"{self.server_url}/api/v4/files"
        headers = {"Authorization": f"Bearer {self._token or ''}"}

        async def _do_upload() -> httpx.Response:
            file_bytes = await asyncio.to_thread(path.read_bytes)
            return await self._download_client.post(
                url,
                headers=headers,
                params={"channel_id": channel_id},
                files={"files": (path.name, file_bytes, mime_type)},
            )

        sent_token = self._token
        response = await _do_upload()
        if response.status_code == 401 and self._is_login_mode:
            async with self._relogin_lock:
                if self._token == sent_token:
                    logger.warning("Auth token expired during upload, re-logging in...")
                    await self.login(self._username, self._password)  # type: ignore[arg-type]
            headers = {"Authorization": f"Bearer {self._token or ''}"}
            response = await _do_upload()
        response.raise_for_status()
        result = response.json()
        file_ids = [fi["id"] for fi in result.get("file_infos", [])]
        logger.info("Uploaded file %s to channel %s -> file_ids=%s", path.name, channel_id, file_ids)
        return file_ids

    async def get_room_history_page(
        self,
        channel_id: str,
        count: int = 50,
        before_ts: str | None = None,
        after_ts: str | None = None,
    ) -> HistoryPage:
        """Fetch the last ``count`` messages from a channel via the REST API.

        Returns messages in **chronological order** (oldest first). System
        messages (Mattermost ``type`` field non-empty, e.g.
        "system_join_channel") and messages with empty body are excluded.

        ``before_ts``/``after_ts`` are Mattermost-native epoch-millisecond
        strings (matching ``post["create_at"]``'s own units) — NOT ISO 8601.
        This is a deliberate deviation from RC's REST client, where
        before_ts/after_ts are ISO strings: Mattermost's connector-internal
        dedup watermark (MattermostConnector._ChannelState.last_processed_ts)
        is already an epoch-ms string (mirroring RC's own internal
        watermark representation), and RC's REST API happens to accept that
        same representation directly for its oldest/latest params — but
        Mattermost's REST API does not, so converting here would make the
        internal reconnect-replay caller (which passes the watermark
        untouched) round-trip through a lossy ISO reparse for no reason.
        The public Connector.fetch_room_history ABC contract (ISO-based, for
        CLI use) converts ISO to this format in MattermostConnector before
        calling this method — see MattermostConnector.fetch_room_history.

        Known limitation: Mattermost's /channels/{id}/posts endpoint pages by
        post ID, not timestamp — there is no direct equivalent of RC's
        latest/oldest timestamp params. before_ts/after_ts are therefore
        applied as a best-effort client-side filter over the most recent
        ``count`` posts (via the ``since`` param for after_ts), not exact
        server-side pagination. Per the Connector ABC contract, connectors
        may treat these params as advisory. Deep pagination past the first
        page is out of scope for this connector.
        """
        params: dict[str, Any] = {"page": 0, "per_page": count}
        if after_ts:
            params["since"] = int(after_ts)
        result = await self._request("GET", f"channels/{channel_id}/posts", params=params)
        order = result.get("order", [])
        posts_by_id = result.get("posts", {})
        # `order` is newest-first; reverse for chronological order.
        posts = [posts_by_id[pid] for pid in reversed(order) if pid in posts_by_id]
        raw_count = len(posts)
        posts = [p for p in posts if not p.get("type") and p.get("message")]
        if before_ts:
            before_ms = int(before_ts)
            posts = [p for p in posts if p.get("create_at", 0) < before_ms]
        if after_ts:
            after_ms = int(after_ts)
            posts = [p for p in posts if p.get("create_at", 0) >= after_ms]
        return HistoryPage(messages=posts, raw_count=raw_count, limit=count)

    async def get_room_history(
        self,
        channel_id: str,
        count: int = 50,
        before_ts: str | None = None,
        after_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        """The filtered chronological list, for callers that do not need the page shape.

        The history handoff wants posts; only replay has to tell "an empty window" from
        "a page the server filled with system posts before ACG could filter them".
        """
        page = await self.get_room_history_page(
            channel_id, count=count, before_ts=before_ts, after_ts=after_ts)
        return page.messages

    async def get_member_channel_ids(self) -> set[str]:
        """Every channel id this account is a member of, across all teams.

        `GET /users/{user_id}/channels` streams the caller's full membership
        (verified in `getChannelsForUser`: it pages internally and writes the
        complete array). This is the one membership probe that is unambiguous
        with the bot's own token — the per-channel member lookup answers 403
        for a non-member, indistinguishable from a permissions problem (§6.2).
        Raises on failure; the caller owns the tri-state (a set that could not
        be read is unknown, never empty).
        """
        result = await self._request(
            "GET", f"users/{self.bot_user_id or 'me'}/channels")
        return {
            chan["id"]
            for chan in (result or [])
            if isinstance(chan, dict) and chan.get("id")
        }

    async def get_channel(self, channel_id: str) -> dict[str, Any]:
        """Channel info by id — the fallback for a room the event could not describe.

        A fallback rather than a hot-path call on purpose: name, type and team are all on
        the wire for a channel post (§6.2), and Mattermost holds a connector-wide permit
        for the whole handler, so a REST round trip there stalls delivery for every
        channel. This exists for the paths that genuinely start from an id — a persisted
        record being recreated, an operator naming a room by id.
        """
        result = await self._request("GET", f"channels/{channel_id}")
        return {
            "id": result["id"],
            "name": result.get("name", ""),
            "display_name": result.get("display_name", ""),
            "type": room_type_for(result.get("type")),
            # Kept, not discarded: a channel id is globally unique, so resolving a
            # persisted record by id can reach a channel in a team this connector no
            # longer serves — the bot may belong to both. Without this the caller cannot
            # tell.
            "team_id": result.get("team_id", ""),
        }

    async def channel_member_usernames(self, channel_id: str, exclude: str = "") -> list[str]:
        """Usernames of a channel's members, for describing a room that has no name.

        A direct channel's REST object carries an **empty** `display_name`: the counterpart
        handle Mattermost puts on a WebSocket event is viewer-specific and is not part of
        the channel itself. So a DM resolved by id has nothing to describe it, and this
        supplies it from the membership instead.

        Only ever called on the recreation path, never per message — it is one request plus
        a cached username lookup per member.
        """
        members = await self._request("GET", f"channels/{channel_id}/members")
        names = []
        for member in members if isinstance(members, list) else []:
            user_id = member.get("user_id", "")
            if not user_id or user_id == exclude:
                continue
            names.append(await self.resolve_username(user_id))
        return names

    async def resolve_room(self, room_name: str) -> dict[str, Any]:
        """Resolve a channel name to its info dict, within the configured team.

        Prefix rules:
          - ``@username`` — resolves as a direct message channel with that user.
          - anything else  — looked up as a channel by name within self.team_id.
        """
        if room_name.startswith("@"):
            username = room_name[1:]
            try:
                user = await self.get_user_by_username(username)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise RoomNotFoundError(f"User '{username}' not found") from e
                raise
            try:
                result = await self._request(
                    "POST", "channels/direct", json_data=[self.bot_user_id, user["id"]]
                )
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"Failed to open DM with '{username}': {e}") from e
            logger.info("Resolved DM '@%s' -> id=%s", username, result["id"])
            return {"id": result["id"], "name": room_name, "type": "dm"}

        if not self.team_id:
            raise RuntimeError("resolve_room: team_id not set — call resolve_team() first")
        try:
            result = await self._request(
                "GET", f"teams/{self.team_id}/channels/name/{room_name}"
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise RoomNotFoundError(
                    f"Channel '{room_name}' not found in team (id={self.team_id})"
                ) from e
            raise
        logger.info("Resolved channel '%s' -> id=%s", room_name, result["id"])
        return {
            "id": result["id"],
            "name": result.get("name", room_name),
            "type": room_type_for(result.get("type")),
        }


def iso_to_epoch_ms_str(iso_ts: str) -> str:
    """Convert an ISO 8601 timestamp string to a Mattermost epoch-millis string.

    Used by MattermostConnector.fetch_room_history to bridge the public
    Connector ABC's ISO-based before_ts/after_ts contract onto
    get_room_history's native epoch-ms string params (see that method's
    docstring for why the two layers use different representations).
    """
    dt = datetime.datetime.fromisoformat(iso_ts)
    return str(int(dt.timestamp() * 1000))
