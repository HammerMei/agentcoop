"""Thin Rocket.Chat REST API client for login, post_message, upload_file, and room resolution."""

import asyncio
import datetime
import logging
import math
import mimetypes
import secrets
from pathlib import Path
from typing import Any, Callable

import httpx

from ...core.connector import HistoryPage

logger = logging.getLogger("coop.connectors.rocketchat.rest")


def _to_rc_ts(value: str | None) -> str | None:
    """Normalise a history bound to what Rocket.Chat's `oldest`/`latest` actually parse.

    The server does `new Date(oldest)` (`apps/meteor/server/api/v1/channels.ts`), and
    JavaScript's `new Date("1786816166131")` is **Invalid Date** — a string of digits is
    not one of the formats it accepts. What that produces is worse than an error, because
    the request still succeeds. Probed against Rocket.Chat 6.12 **and 8.5.1**, five
    messages in a room, asking for everything at or after the third — same result on both:

        oldest="1786816166131"                 -> HTTP 200 success=True, 5 messages
        oldest="2026-08-15T17:49:26.131000Z"   -> HTTP 200 success=True, 3 messages

    The second is the string this function emits, six fractional digits and all — the
    probe was re-run on the real output rather than on a hand-written three-digit form,
    because six digits is outside the ECMAScript Date Time String Format and therefore
    lands in implementation-specific parsing. It works; the point is that it was checked
    rather than assumed from a shorter string that also works.

    Not a quirk of an old server, and not something to wait out: `isChannelsHistoryProps`
    on `develop` types `oldest` as `{type: 'string', minLength: 1}` with no `date-time`
    format, so the digits pass validation and reach `new Date` exactly as before.

    So the bound is silently dropped and the server answers with the newest `count`
    messages in the room. The client-side watermark filter still rejects the ones below
    the cursor, which is why this never showed up as duplicate delivery — it shows up as
    a full page fetched on every reconnect, `was_full` reporting "messages could be
    permanently lost" for any room with more history than the page size, and a window of
    system events that cannot be read past.

    Two callers pass this parameter in two formats: the reconnect replay passes the DDP
    watermark, which is epoch milliseconds (`normalize._extract_ts`), and the history
    handoff passes ISO 8601, as its docstring says. Normalising here rather than at either
    caller is the point — the wire format belongs to the client that owns the request, and
    a rule kept at the call sites is a rule the next call site will not know about.
    """
    if not value:
        return value
    # "Does this parse as a number", not "is it all digits". A watermark is `str()` of
    # whatever JSON put in `$date`, and a float there — `"1786816166131.0"` — is not a
    # digit string. It would pass straight through, be Invalid Date on the server, and
    # fail the same silent way this function exists to prevent. ISO 8601 raises here.
    try:
        ms = float(value)
    except ValueError:
        return value
    if not math.isfinite(ms):
        return value
    return datetime.datetime.fromtimestamp(
        ms / 1000, tz=datetime.timezone.utc
    ).isoformat().replace("+00:00", "Z")


class RoomNotFoundError(Exception):
    """Raised when a room name cannot be resolved because it does not exist.

    Distinct from transport/auth/API failures, which should propagate as-is
    so callers can distinguish a missing room from a broader infrastructure
    problem.
    """


# Rocket.Chat's room-type letters. `c` public channel, `p` private group, `d` direct —
# which covers a 1:1 **and** a group DM, because Rocket.Chat reports them identically
# (§6.4). Telling those apart needs a participant lookup and is done by the connector.
#
# Nothing mapped these before: a room's type came from *which REST endpoint answered*,
# which works when resolving by name and not at all when the type arrives on a message.
_ROOM_TYPES = {"c": "channel", "p": "group", "d": "dm"}


def room_type_for(letter: str | None) -> str:
    """Map a Rocket.Chat room-type letter to the gateway's room type.

    An unknown or missing letter falls back to `channel`: a channel requires a mention
    where a DM does not, so guessing `channel` cannot turn a quiet room into one the agent
    answers unprompted.
    """
    return _ROOM_TYPES.get(letter or "", "channel")


class RocketChatREST:
    """Async REST client for Rocket.Chat API."""

    def __init__(self, server_url: str):
        self.server_url = server_url.rstrip("/")
        self.auth_token: str | None = None
        self.user_id: str | None = None
        self.bot_username: str | None = None
        self._username: str | None = None
        self._password: str | None = None
        # Cached result of _get_server_major_version(); None means "not yet
        # fetched" (fetch is retried), NOT "detection failed" (that also
        # returns None but does not populate this cache, so a transient
        # failure doesn't stick for the connector's whole lifetime).
        self._server_major_version: int | None = None
        self._client = httpx.AsyncClient(timeout=30.0)
        self._download_client = httpx.AsyncClient(timeout=60.0)
        # Serializes concurrent re-login attempts.  Without this lock, two
        # concurrent requests that both receive a 401 would both call login()
        # simultaneously, race to overwrite auth_token/user_id, and one caller
        # would then retry with a stale token from the other's login response.
        self._relogin_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return (
            f"RocketChatREST(server_url={self.server_url!r}, "
            f"username={self._username!r}, password=***)"
        )

    async def close(self) -> None:
        """Close shared HTTP clients and release connection pool resources."""
        await self._client.aclose()
        await self._download_client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "X-Auth-Token": self.auth_token or "",
            "X-User-Id": self.user_id or "",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        url = f"{self.server_url}/api/v1/{endpoint}"
        sent_token = self.auth_token  # capture before the request
        response = await self._client.request(
            method, url, headers=self._headers(), json=json_data, params=params
        )
        if response.status_code == 401 and self._username:
            async with self._relogin_lock:
                # Inside the lock, check whether the token has already been
                # refreshed by a concurrent coroutine that raced through the
                # same 401 path.  If so, skip re-login and just retry with
                # the new token — calling login() twice would be wasteful and
                # could invalidate the other caller's fresh session.
                if self.auth_token == sent_token:
                    logger.warning("Auth token expired, re-logging in...")
                    if self._username and self._password:
                        await self.login(self._username, self._password)
                    else:
                        raise RuntimeError(
                            "Cannot re-login: username or password not set. "
                            "Ensure login() was called before making requests."
                        )
            response = await self._client.request(
                method, url, headers=self._headers(), json=json_data, params=params
            )
        if not response.is_success:
            logger.error(
                "RC API error %d for %s %s — body: %s",
                response.status_code,
                method,
                endpoint,
                response.text[:500],
            )
        response.raise_for_status()
        return response.json()

    async def login(self, username: str, password: str) -> None:
        """Login and store auth credentials.

        Note: username and password are stored as instance attributes
        (_username, _password) to support automatic re-login when the
        auth token expires (see _request's 401 handling). This is a
        known trade-off for transparent session recovery.
        """
        url = f"{self.server_url}/api/v1/login"
        response = await self._client.post(
            url, json={"user": username, "password": password}
        )
        response.raise_for_status()
        data = response.json()

        if data.get("status") != "success":
            raise RuntimeError(f"Login failed: {data}")

        self.auth_token = data["data"]["authToken"]
        self.user_id = data["data"]["userId"]
        # The CANONICAL spelling the server knows, not what the operator
        # typed (#112). Rocket.Chat's login is not spelling-exact — probed on
        # 6.12, `probebot9207` and the account's email both log into
        # `ProbeBot9207` — and every message frame carries the canonical
        # form in `u.username`, so identity comparisons against the typed
        # spelling silently fail under an ordinary configuration. The typed
        # value is kept only for re-login.
        self.bot_username = (
            data["data"].get("me", {}).get("username") or username
        )
        self._username = username
        self._password = password
        logger.info(
            "Logged in as %s (canonical username=%s, uid=%s)",
            username, self.bot_username, self.user_id,
        )

    async def _get_server_major_version(self) -> int | None:
        """Fetch and cache the RC server's major version via ``GET /api/info``.

        RC registers this route with ``authRequired: false``, so it can be
        called with no auth headers. For unauthenticated callers RC trims the
        response's ``version`` field down to ``"<major>.<minor>"`` (patch
        removed) — that's still enough to determine the major version, so no
        auth token is sent here.

        Returns:
            The server's major version (e.g. ``8`` for "8.5.2" or "8.5"), or
            ``None`` if it could not be determined (network error, or an
            unexpected/missing ``version`` field). ``None`` is not cached, so
            a transient failure is retried on the next call instead of
            permanently falling back for the connector's whole lifetime.
        """
        if self._server_major_version is not None:
            return self._server_major_version
        try:
            response = await self._client.get(f"{self.server_url}/api/info")
            response.raise_for_status()
            version_str = response.json().get("version", "")
            major = int(version_str.split(".")[0])
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            logger.warning(
                "Could not determine Rocket.Chat server version via /api/info "
                "(%s); assuming pre-8.0 API surface for file uploads.", e,
            )
            return None
        self._server_major_version = major
        return major

    async def post_message(
        self,
        channel: str,
        text: str,
        tmid: str | None = None,
    ) -> None:
        """Post a message to a room by name or ID.

        Args:
            channel : Room ID or name.
            text    : Message body.
            tmid    : Thread root message ID.  When set, the message is posted
                      inside that thread (or starts a new thread if this is the
                      first reply to that message).
        """
        # RC's chat.postMessage has two mutually exclusive schemas:
        #   - without tmid: accepts "channel" (name or ID)
        #   - with tmid:    requires "roomId" (must be room ID, "channel" is rejected)
        if tmid:
            payload: dict = {"roomId": channel, "text": text, "tmid": tmid}
        else:
            payload = {"channel": channel, "text": text}
        result = await self._request("POST", "chat.postMessage", json_data=payload)
        if not result.get("success"):
            raise RuntimeError(f"post_message failed: {result.get('error', result)}")
        logger.info(
            "Posted message to %s%s", channel, f" (thread {tmid})" if tmid else ""
        )

    async def download_file(self, title_link: str, dest_path: str) -> None:
        """Download a file attachment from RC (authenticated) to a local path.

        Accumulates all chunks in memory then writes to a PID-unique temp file
        via asyncio.to_thread, and atomically renames on success.

        Accumulating in memory is safe because the caller (normalize.py) already
        enforces max_file_size_mb before calling this method.  Streaming directly
        to disk with synchronous f.write() inside an async-for loop would block
        the event loop on every write syscall — especially on slow or NFS-mounted
        filesystems.  Offloading the write to a thread keeps the loop responsive.
        """
        url = f"{self.server_url}{title_link}"
        headers = {
            "X-Auth-Token": self.auth_token or "",
            "X-User-Id": self.user_id or "",
        }
        dest = Path(dest_path)
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        # Use a random suffix (not os.getpid()) so that concurrent downloads
        # for the same dest_path each get a unique tmp file and cannot
        # overwrite each other's data before the atomic rename.
        tmp_path = dest.with_name(f"{dest.name}.{secrets.token_hex(8)}.tmp")

        def _stream_download(current_headers: dict[str, str]):
            return self._download_client.stream("GET", url, headers=current_headers)

        async def _collect_chunks(stream_ctx) -> bytes:
            """Collect response chunks into memory."""
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

        sent_token = self.auth_token  # capture before the request
        try:
            # Phase 1: send the initial request and check for 401.
            # Exit the first context manager *completely* before opening the retry
            # request — keeping a second stream open inside the first context manager
            # would nest two concurrent connections on the same _download_client,
            # which can deadlock if the connection pool is constrained (e.g.
            # max_connections=1) and leaves the first response body unconsumed.
            need_reauth = False
            data: bytes = b""
            async with _stream_download(headers) as first_response:
                if first_response.status_code == 401 and self._username:
                    need_reauth = True
                    # Do NOT read the body — just note that we need to re-auth.
                    # The context manager exits cleanly (httpx discards the body).
                else:
                    first_response.raise_for_status()
                    chunks: list[bytes] = []
                    async for chunk in first_response.aiter_bytes():
                        chunks.append(chunk)
                    data = b"".join(chunks)

            # Phase 2: re-authenticate and retry (first context manager is now fully closed).
            if need_reauth:
                async with self._relogin_lock:
                    if self.auth_token == sent_token:
                        logger.warning(
                            "Auth token expired during download, re-logging in..."
                        )
                        if self._username and self._password:
                            await self.login(self._username, self._password)
                        else:
                            raise RuntimeError(
                                "Cannot re-login: username or password not set. "
                                "Ensure login() was called before making requests."
                            )
                headers = {
                    "X-Auth-Token": self.auth_token or "",
                    "X-User-Id": self.user_id or "",
                }
                data = await _collect_chunks(_stream_download(headers))

            await asyncio.to_thread(_write_and_rename, data)
        except Exception:
            await asyncio.to_thread(tmp_path.unlink, missing_ok=True)
            raise
        logger.info("Downloaded attachment to %s", dest_path)

    async def _post_with_reauth_retry(
        self, url: str, *, headers: Callable[[], dict[str, str]], **request_kwargs
    ) -> httpx.Response:
        """POST to ``url``, retrying once after re-login if the first attempt 401s.

        ``headers`` is a zero-arg callable (not a plain dict) so the retry
        rebuilds it *after* login() has refreshed ``self.auth_token`` — the
        same pattern ``upload_file()`` used inline before this was factored
        out to be shared by the legacy and RC-8.0+ upload flows.
        """
        sent_token = self.auth_token  # capture before the request
        response = await self._download_client.post(
            url, headers=headers(), **request_kwargs
        )
        if response.status_code == 401 and self._username:
            async with self._relogin_lock:
                if self.auth_token == sent_token:
                    logger.warning("Auth token expired during upload, re-logging in...")
                    await self.login(self._username, self._password)
            response = await self._download_client.post(
                url, headers=headers(), **request_kwargs
            )
        return response

    def _upload_auth_headers(self) -> dict[str, str]:
        """Auth-only headers for multipart upload POSTs (no Content-Type —
        httpx sets the multipart boundary Content-Type itself from `files=`)."""
        return {
            "X-Auth-Token": self.auth_token or "",
            "X-User-Id": self.user_id or "",
        }

    async def upload_file(
        self, room_id: str, file_path: str, caption: str = ""
    ) -> None:
        """Upload a file attachment to a room (requires room ID).

        Dispatches to the RC-8.0+ two-step ``rooms.media`` + ``rooms.mediaConfirm``
        flow or the legacy one-step ``rooms.upload`` flow based on the server's
        detected major version (see ``_get_server_major_version``; undetectable
        version falls back to legacy, matching AgentCoop's behavior before this
        version-detection capability existed). See issue #56.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Attachment not found: {file_path}")

        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type is None:
            mime_type = "application/octet-stream"

        major_version = await self._get_server_major_version()
        if major_version is not None and major_version >= 8:
            await self._upload_file_v8_plus(room_id, path, mime_type, caption)
        else:
            await self._upload_file_legacy(
                room_id, path, mime_type, caption,
                version_undetected=major_version is None,
            )

    async def _upload_file_legacy(
        self, room_id: str, path: Path, mime_type: str, caption: str,
        *, version_undetected: bool,
    ) -> None:
        """Pre-8.0 one-step upload via ``POST rooms.upload/{rid}``.

        Reached when the server's major version is confirmed < 8, or when it
        could not be determined at all (see _get_server_major_version).

        ``version_undetected`` distinguishes those two cases so a 404 is
        translated into the "likely RC 8.0+" error message *only* when we
        genuinely don't know the server's version. When the version was
        positively confirmed < 8, a 404 is an unrelated, genuine failure (bad
        room ID, disabled route, reverse-proxy misconfiguration, etc.) and
        must propagate as the real HTTPStatusError instead of being misreported
        as a version mismatch that never happened. See issue #56.
        """
        url = f"{self.server_url}/api/v1/rooms.upload/{room_id}"
        data = {"msg": caption} if caption else {}
        file_bytes = await asyncio.to_thread(path.read_bytes)

        response = await self._post_with_reauth_retry(
            url,
            headers=self._upload_auth_headers,
            files={"file": (path.name, file_bytes, mime_type)},
            data=data,
        )
        if response.status_code == 404 and version_undetected:
            raise RuntimeError(
                f"rooms.upload/{room_id} returned 404. This endpoint was removed "
                "in Rocket.Chat 8.0+; the server is likely running RC 8.0+ but "
                "AgentCoop's version detection via GET /api/info could not confirm it "
                "(see warning logged above). If this server is RC 8.0+, check "
                "that /api/info is reachable and returns a 'version' field."
            )
        response.raise_for_status()
        result = response.json()
        if not result.get("success"):
            raise RuntimeError(f"upload_file failed: {result.get('error', result)}")
        logger.info("Uploaded file %s to room %s (legacy rooms.upload)", path.name, room_id)

    async def _upload_file_v8_plus(
        self, room_id: str, path: Path, mime_type: str, caption: str
    ) -> None:
        """RC 8.0+ two-step upload: ``rooms.media/{rid}`` then
        ``rooms.mediaConfirm/{rid}/{fileId}``.

        Step 1 uploads the file bytes and gets back a ``file._id``; step 2
        confirms/sends it into the room with the caption. Both steps are
        required — a file uploaded via rooms.media alone sits unconfirmed and
        never appears in the room (confirmed against RC's own route handlers
        in apps/meteor/server/api/v1/rooms.ts, not just its public API docs).
        """
        file_bytes = await asyncio.to_thread(path.read_bytes)

        media_url = f"{self.server_url}/api/v1/rooms.media/{room_id}"
        media_response = await self._post_with_reauth_retry(
            media_url,
            headers=self._upload_auth_headers,
            files={"file": (path.name, file_bytes, mime_type)},
        )
        media_response.raise_for_status()
        media_result = media_response.json()
        if not media_result.get("success"):
            raise RuntimeError(
                f"rooms.media upload failed: {media_result.get('error', media_result)}"
            )
        file_id = (media_result.get("file") or {}).get("_id")
        if not file_id:
            raise RuntimeError(f"rooms.media response missing file._id: {media_result}")

        confirm_url = f"{self.server_url}/api/v1/rooms.mediaConfirm/{room_id}/{file_id}"
        confirm_body = {"msg": caption} if caption else {}
        confirm_response = await self._post_with_reauth_retry(
            confirm_url, headers=self._headers, json=confirm_body,
        )
        confirm_response.raise_for_status()
        confirm_result = confirm_response.json()
        if not confirm_result.get("success"):
            raise RuntimeError(
                f"rooms.mediaConfirm failed: {confirm_result.get('error', confirm_result)}"
            )
        logger.info(
            "Uploaded file %s to room %s (rooms.media + rooms.mediaConfirm)",
            path.name, room_id,
        )

    async def get_room_history(
        self,
        room_id: str,
        room_type: str,
        count: int = 50,
        before_ts: str | None = None,
        after_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch the last ``count`` messages from a room via the REST API.

        Selects the correct history endpoint based on room type:
          - ``channel``  → ``channels.history``
          - ``group``    → ``groups.history``
          - ``dm``       → ``im.history``
          - ``group_dm`` → ``im.history`` (one direct endpoint serves both DM
            kinds; the distinction is AgentCoop's, not the server's — §6.4)

        Returns messages in **chronological order** (oldest first).
        System messages (RC ``t`` field present) and messages with empty
        body are excluded — only plain user/bot text messages are returned.

        Args:
            room_id  : Opaque RC room ID (``_id`` field).
            room_type: ``"channel"`` | ``"group"`` | ``"dm"``.
            count    : Maximum number of messages to retrieve.
            before_ts: ISO 8601 exclusive upper-bound timestamp.  Maps to
                       RC's ``latest`` parameter — only messages with
                       ``ts < before_ts`` are returned.  Omitted when None.
            after_ts : ISO 8601 inclusive lower-bound timestamp.  Maps to
                       RC's ``oldest`` parameter with ``inclusive=true`` —
                       only messages with ``ts >= after_ts`` are returned.
                       Omitted when None.
        """
        return await self._get_room_history_raw(
            room_id, room_type, count=count, before_ts=before_ts, after_ts=after_ts,
            _filtered=True,
        )

    async def _get_room_history_raw(
        self,
        room_id: str,
        room_type: str,
        count: int,
        before_ts: str | None,
        after_ts: str | None,
        _filtered: bool = False,
    ) -> list[dict]:
        """One history request. Returns the server's messages, newest-first, unfiltered.

        `_filtered` is the compatibility shim for `get_room_history`, whose contract is the
        filtered chronological list; `get_room_history_page` wants the unfiltered page so
        it can say how full it was.
        """
        # Both DM kinds go to `im.history`: Rocket.Chat has one direct-room
        # endpoint and no group-DM equivalent, which is the same asymmetry that
        # makes `roomType: "d"` cover both on the wire (§6.4). The creation path
        # types a room from its *classified* kind, so `"group_dm"` reaches here
        # for real — and defaulting it to `channels.history` asked a channel
        # endpoint about a direct room, so history handoff and outage replay
        # both failed for every group DM, permanently.
        endpoint_map = {
            "channel":  "channels.history",
            "group":    "groups.history",
            "dm":       "im.history",
            "group_dm": "im.history",
        }
        endpoint = endpoint_map.get(room_type, "channels.history")
        params: dict = {"roomId": room_id, "count": count, "unreads": "false"}
        if before_ts:
            params["latest"] = _to_rc_ts(before_ts)
        if after_ts:
            params["oldest"] = _to_rc_ts(after_ts)
            # RC treats 'oldest' as exclusive by default (ts > oldest).
            # Set inclusive=true to get ts >= oldest — matching the documented
            # contract that --after is an inclusive lower bound.
            params["inclusive"] = "true"
        result = await self._request(
            "GET", endpoint,
            params=params,
        )
        if not result.get("success"):
            raise RuntimeError(
                f"get_room_history API error for room {room_id!r}: "
                f"{result.get('error', result)}"
            )
        msgs = result.get("messages", [])
        if not _filtered:
            return msgs
        # Exclude system events (type field ``t`` present) and empty messages.
        text_msgs = [m for m in msgs if not m.get("t") and m.get("msg")]
        # RC REST API returns newest-first; reverse to chronological order.
        return list(reversed(text_msgs))

    async def get_room_history_page(
        self,
        room_id: str,
        room_type: str,
        count: int = 50,
        before_ts: str | None = None,
        after_ts: str | None = None,
    ) -> "HistoryPage":
        """`get_room_history`, plus how full the page was *before* filtering.

        The server applies `count` and only then are system and empty-body events
        dropped, so an empty result does not mean an empty window: a page filled by
        joins and topic changes hides every older user message behind it. The caller
        cannot tell those apart from the filtered list alone, and the difference decides
        whether it may report the outage as read.
        """
        raw = await self._get_room_history_raw(
            room_id, room_type, count=count, before_ts=before_ts, after_ts=after_ts,
        )
        text_msgs = [m for m in raw if not m.get("t") and m.get("msg")]
        return HistoryPage(
            messages=list(reversed(text_msgs)), raw_count=len(raw), limit=count
        )

    async def dm_members(self, room_id: str) -> list[str]:
        """How many people are in a direct room — the only way to tell a 1:1 from a group.

        Rocket.Chat reports both as `roomType: "d"` with no participant information in the
        frame, and this is not a cosmetic distinction: `require_mention` is skipped
        entirely for a room typed `dm`, so a group DM misclassified as a 1:1 makes the
        agent answer **every** message from **anyone** in that group (§6.4).

        Returns **the other participants' usernames** — this account is excluded by id —
        because the caller needs both halves of that: how many there are answers
        1:1-or-group, and the names *are* the room's description, since a direct room has
        no name and its participants are the only thing that identifies it to a human
        (§2.3).

        **A failed request raises; only a readable-but-empty answer returns `[]`.**
        The two used to be one return value, and the routing transaction (§2.2) is why
        they cannot be: a network failure is *retryable* — the classification was never
        made, so the message must stay redeliverable (abort) — while a server that
        answers with no members is a *data condition* the retry cannot change (final).
        Collapsing them made outcome 3 of the dedup transaction unreachable.

        There is still no safe default kind to guess in either case. Reading a group as
        a 1:1 drops the mention gate and the agent answers everyone in it; reading a
        1:1 as a group makes it wait for a mention its user has no reason to type.
        """
        result = await self._request("GET", "im.members", params={"roomId": room_id})
        members = result.get("members") or []
        if not isinstance(members, list):
            return []
        # The caller is excluded here, by **id**, because this is where the ids are. The
        # connector used to drop it by comparing usernames against its configured
        # spelling, and a login whose canonical username differs in casing or is an alias
        # left the account in its own participant list: a 1:1 room described by its own
        # bot, and — if the API happens to list the bot first — every such room deriving
        # the same `dm-<bot>` label instead of distinct counterparts.
        return [
            m.get("username", "")
            for m in members
            if m.get("username") and m.get("_id") != self.user_id
        ]

    async def get_subscription(self, room_id: str) -> dict[str, Any] | None:
        """This account's subscription document for a room, or `None` if absent.

        The by-id counterpart to `resolve_room`: the subscription carries the
        room's type letter (`t`) and its name, which is what classifying a room
        needs, and Rocket.Chat serves it from the room ID alone. `is_room_member`
        below already asks this endpoint and keeps only whether the record
        exists; this returns the record.

        A missing record is a **200 with a null subscription**, not an error —
        see `is_room_member` for the handler that makes that so. Absent therefore
        returns `None`. Membership is the same fact: Rocket.Chat removes the
        subscription when this account is kicked or leaves, so a room this
        account is no longer in also answers `None`, which is the honest answer
        for a caller asking whether it can still serve the room.

        A transport or auth failure RAISES, deliberately unlike `is_room_member`
        (which collapses those into its third answer): the caller here
        distinguishes "not mine" from "could not ask", and an auth blip must not
        read as a deleted room.
        """
        result = await self._request(
            "GET", "subscriptions.getOne", params={"roomId": room_id}
        )
        subscription = result.get("subscription")
        return subscription if isinstance(subscription, dict) else None

    async def is_room_member(self, room_id: str) -> bool | None:
        """Is this account still in the room — `True`, `False`, or **`None` for unknown**.

        Three answers, not two, and the third is the point: a lookup that fails has not said
        the account is a member, and it has not said it was removed. Collapsing it either way
        is a guess, and the caller (replay) can afford to do neither — it can wait for the
        next round.

        Membership is read as "does this account have a subscription record for the room",
        which is what Rocket.Chat removes when someone is kicked or leaves. A *hidden* room
        keeps its record (`open: false`) and is still membership — being hidden is a display
        choice, not a departure.

        Only the live path gets this answer for free: `roomParticipant` is computed
        server-side per delivered message. Replay reconstructs its documents from REST, so it
        has to ask.
        """
        try:
            result = await self._request(
                "GET", "subscriptions.getOne", params={"roomId": room_id}
            )
        except httpx.HTTPStatusError as e:
            # No HTTP error from this endpoint means "not a member", and that is checked
            # rather than assumed. The handler is
            # `API.v1.success({subscription: await Subscriptions.findOneByRoomIdAndUserId(...)})`
            # — a missing record is a **200 with a null subscription**, which is the branch
            # below. Its declared failures are 400 for a malformed request (its own
            # end-to-end test asserts `must have required property 'roomId'`) and 401 for
            # authentication; neither says anything about membership.
            #
            # So every status error here is genuinely unknown, and must stay unknown:
            # answering `False` would let an auth failure or a request bug close the replay
            # window and drop the watermark, which is silent message loss caused by an
            # unrelated defect.
            logger.warning(
                "Could not read the subscription record for room %s (%s) — "
                "membership is unknown",
                room_id, e,
            )
            return None
        except Exception as e:
            logger.warning(
                "Membership lookup for room %s failed: %s", room_id, e
            )
            return None
        return bool(result.get("subscription"))

    async def get_subscription_room_ids(self) -> set[str]:
        """Every room id this account holds a subscription record for.

        `subscriptions.get` without `updatedSince` returns the full set in
        `update` (verified in the handler: `getSubscriptions(userId)` with no
        date returns an array, wrapped as `{update: result, remove: []}`).
        Hidden rooms are included — a hidden room keeps its record and is
        still membership, same rule as `is_room_member`. Raises on failure;
        the caller owns the tri-state (a set that could not be read is
        unknown, never empty).
        """
        result = await self._request("GET", "subscriptions.get")
        return {
            sub["rid"]
            for sub in result.get("update", [])
            if isinstance(sub, dict) and sub.get("rid")
        }

    async def resolve_room(self, room_name: str) -> dict[str, Any]:
        """Resolve a room name to its info dict.

        Prefix rules:
          - ``@username`` — resolves as a direct message (im.create) with that user.
          - anything else  — tries public channel (channels.info) then private
            group (groups.info).
        """
        if room_name.startswith("@"):
            username = room_name[1:]
            try:
                result = await self._request(
                    "POST", "im.create", json_data={"username": username}
                )
                if result.get("success") and "room" in result:
                    room = result["room"]
                    logger.info("Resolved DM '@%s' -> id=%s", username, room["_id"])
                    return {"_id": room["_id"], "name": room_name, "type": "dm"}
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"Failed to open DM with '{username}': {e}") from e
            raise RoomNotFoundError(
                f"DM room for user '{username}' not found (im.create returned unexpected response)"
            )

        # Try public channel
        try:
            result = await self._request(
                "GET", "channels.info", params={"roomName": room_name}
            )
            if result.get("success") and "channel" in result:
                ch = result["channel"]
                logger.info("Resolved channel '%s' -> id=%s", room_name, ch["_id"])
                return {
                    "_id": ch["_id"],
                    "name": ch.get("name", room_name),
                    "type": "channel",
                }
        except httpx.HTTPStatusError as e:
            # RC returns 400 ("Channel_not_found") or 404 when the room does not
            # exist on this endpoint — treat those as "try next endpoint".
            # Any other status (401 auth failure, 500 server error, etc.) is a
            # real infrastructure problem and must NOT be silently swallowed.
            if e.response.status_code not in (400, 404):
                raise

        # Try private group
        try:
            result = await self._request(
                "GET", "groups.info", params={"roomName": room_name}
            )
            if result.get("success") and "group" in result:
                grp = result["group"]
                logger.info("Resolved group '%s' -> id=%s", room_name, grp["_id"])
                return {
                    "_id": grp["_id"],
                    "name": grp.get("name", room_name),
                    "type": "group",
                }
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (400, 404):
                raise

        raise RoomNotFoundError(
            f"Room '{room_name}' not found (tried channels and groups)"
        )
