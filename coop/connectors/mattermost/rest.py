"""The Mattermost v4 REST calls the connector makes, as one bot account: a bearer
token (a bot access token or a personal access token), no login call. Lifted from the
frozen gateway's client and cut to what the room core needs. Errors are httpx's.
"""

from __future__ import annotations

from typing import Any

import httpx


class MattermostREST:
    def __init__(self, url: str, token: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=f"{self.url}/api/v4", timeout=timeout,
                                         headers={"Authorization": f"Bearer {token}"}, transport=transport)
        self._users: dict[str, dict] = {}  # by id

    async def close(self) -> None:
        await self._client.aclose()

    async def _json(self, method: str, path: str, **kw) -> Any:
        r = await self._client.request(method, path, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

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

    async def create_post(self, channel_id: str, message: str, root_id: str | None = None) -> dict:
        payload: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            payload["root_id"] = root_id
        return await self._json("POST", "/posts", json=payload)

    async def file(self, file_id: str) -> bytes:
        r = await self._client.get(f"/files/{file_id}", timeout=120.0)
        r.raise_for_status()
        return r.content
