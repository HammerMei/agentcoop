"""The Mattermost v4 REST calls the connector makes, as one bot account. Lifted from
the frozen gateway's client and cut to what the room core needs. Errors are httpx's.

Auth is the credential's. A bearer token (a bot access token or a personal access
token) is used as it is: no login call. A password is exchanged for a session token
by ``POST /users/login`` (the token comes back in the ``Token`` response header, a
Mattermost quirk) before the first call, and again when a call answers 401, since a
session expires; that call is then made once more. A file call (:meth:`upload`,
:meth:`stream_file`) is not retried: it fails once in that window, and the next JSON
call logs in. ``verify=False`` skips the certificate check (``verify_tls`` in the
configuration), for a lab's self-signed certificate.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..base import Credential

logger = logging.getLogger(__name__)


class MattermostREST:
    def __init__(self, url: str, username: str, credential: Credential, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None, verify: bool = True) -> None:
        self.url = url.rstrip("/")
        self.username = username
        self._credential = credential
        self._client = httpx.AsyncClient(base_url=f"{self.url}/api/v4", timeout=timeout, transport=transport,
                                         verify=verify)
        self._token = ""
        if credential.token:
            self._use(credential.token)
        self._login_lock = asyncio.Lock()
        self._users: dict[str, dict] = {}  # by id

    async def close(self) -> None:
        await self._client.aclose()

    # ── auth ────────────────────────────────────────────────────────────────

    @property
    def token(self) -> str:
        """The bearer token in use: the configured one, or the session's after a login.
        The websocket reads it at every (re)connect."""
        return self._token

    def _use(self, token: str) -> None:
        self._token = token
        self._client.headers["Authorization"] = f"Bearer {token}"

    async def login(self) -> None:
        """``POST /users/login`` with the password; the session token in the ``Token``
        header becomes the bearer."""
        r = await self._client.post("/users/login", json={"login_id": self.username,
                                                          "password": self._credential.password})
        r.raise_for_status()
        token = r.headers.get("Token", "")
        if not token:
            raise httpx.DecodingError("POST /users/login: no Token header in the answer", request=r.request)
        self._use(token)

    async def _login_unless_replaced(self, sent: str) -> None:
        """A login, unless another call already replaced the token ``sent`` was made with."""
        async with self._login_lock:
            if self._token == sent:
                if sent:
                    logger.warning("%s: the session token was refused; logging in again", self.username)
                await self.login()

    async def _json(self, method: str, path: str, **kw) -> Any:
        if self._credential.password and not self._token:
            await self._login_unless_replaced("")
        sent = self._token
        r = await self._client.request(method, path, **kw)
        if r.status_code == 401 and self._credential.password:
            await self._login_unless_replaced(sent)
            r = await self._client.request(method, path, **kw)
        r.raise_for_status()
        try:
            return r.json() if r.content else {}
        except ValueError as exc:  # a 200 that is not JSON (a proxy's page): the platform did not answer
            raise httpx.DecodingError(f"{method} {path}: not JSON: {exc}", request=r.request) from exc

    # ── identity ────────────────────────────────────────────────────────────

    async def me(self) -> dict:
        return await self._json("GET", "/users/me")

    async def my_teams(self) -> list[dict]:
        """``/users/me/teams`` rather than ``/teams/name/{name}``: a non-admin account
        gets 403 from the latter whatever its membership."""
        return await self._json("GET", "/users/me/teams")

    async def user(self, user_id: str) -> dict:
        """A user by id, cached: posts and events carry ids, the core wants usernames."""
        found = self._users.get(user_id)
        if found is None:
            found = self._users[user_id] = await self._json("GET", f"/users/{user_id}")
        return found

    async def users(self, user_ids: list[str]) -> list[dict]:
        missing = [u for u in user_ids if u not in self._users]
        if missing:
            for user in await self._json("POST", "/users/ids", json=missing):
                self._users[user["id"]] = user
        return [self._users[u] for u in user_ids if u in self._users]

    # ── rooms ───────────────────────────────────────────────────────────────

    async def my_channels(self, user_id: str) -> list[dict]:
        """Every channel the account is a member of, across teams: the one membership
        probe that is unambiguous with the bot's own token."""
        return await self._json("GET", f"/users/{user_id}/channels")

    async def channel(self, channel_id: str) -> dict:
        return await self._json("GET", f"/channels/{channel_id}")

    async def channel_member_ids(self, channel_id: str) -> list[str]:
        found: list[str] = []
        page = 0
        while True:
            members = await self._json("GET", f"/channels/{channel_id}/members", params={"page": page, "per_page": 200})
            ids = [m["user_id"] for m in members if isinstance(m, dict) and m.get("user_id")]
            if not ids or set(ids) <= set(found):  # a short last page, or a server that ignores ``page``
                return found + [i for i in ids if i not in found]
            found += ids
            if len(members) < 200:
                return found
            page += 1

    # ── posts ───────────────────────────────────────────────────────────────

    async def posts(self, channel_id: str, before: str | None = None, after: str | None = None,
                    per_page: int = 60) -> dict:
        """One page, ``order`` newest first; ``before``/``after`` are post ids, and the
        server pages by them (probed on 11.x: complete and strictly ordered)."""
        params: dict[str, Any] = {"per_page": per_page}
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after
        return await self._json("GET", f"/channels/{channel_id}/posts", params=params)

    async def create_post(self, channel_id: str, message: str, root_id: str | None = None,
                          file_ids: tuple[str, ...] = ()) -> dict:
        payload: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            payload["root_id"] = root_id
        if file_ids:
            payload["file_ids"] = list(file_ids)
        return await self._json("POST", "/posts", json=payload)

    async def upload(self, channel_id: str, name: str, content_type: str, size: int,
                     chunks: AsyncIterator[bytes], timeout: float | None = None) -> dict:
        """One file as the request body, streamed (``channel_id`` and ``filename`` as
        query parameters, the simple form of ``POST /files``); the ``file_infos`` entry
        the server answers with, whose ``id`` a post then carries. ``timeout`` is how
        long to wait for the server between two chunks and for its answer after the
        last; the client's default otherwise."""
        headers = {"Content-Type": content_type or "application/octet-stream", "Content-Length": str(size)}
        r = await self._client.post("/files", params={"channel_id": channel_id, "filename": name},
                                    content=chunks, headers=headers, timeout=self._transfer(timeout))
        r.raise_for_status()
        try:
            infos = r.json().get("file_infos") or []
        except ValueError as exc:  # a 2xx that is not JSON (a proxy's page), as ``_json`` treats it
            raise httpx.DecodingError(f"POST /files: not JSON: {exc}", request=r.request) from exc
        if not infos:
            raise httpx.DecodingError("POST /files: no file_infos in the answer", request=r.request)
        return infos[0]

    def stream_file(self, file_id: str, timeout: float | None = None):
        """``async with`` for the file's response, its body read as a stream; ``timeout``
        as for :meth:`upload`."""
        return self._client.stream("GET", f"/files/{file_id}", timeout=self._transfer(timeout))

    def _transfer(self, timeout: float | None) -> httpx.Timeout | httpx.USE_CLIENT_DEFAULT.__class__:
        """The client's connect and pool timeouts, with the read and write ones a file
        transfer needs."""
        if timeout is None:
            return httpx.USE_CLIENT_DEFAULT
        return httpx.Timeout(self._client.timeout.connect, read=timeout, write=timeout, pool=self._client.timeout.pool)
