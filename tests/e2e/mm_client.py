"""Minimal synchronous Mattermost API client for the E2E suite.

Deliberately mirrors `rc_client.RCClient`'s shape — same method names where
the concept is the same (`login`, `create_user`, `create_channel`,
`post_message`, `poll_for_message`) — so a test reads the same against either
platform and the two suites stay comparable.

Where the platforms genuinely differ, the difference is in the signature
rather than hidden:

* **Channels are scoped to a team.** Every channel lookup takes a `team_id`,
  because a channel name is unique only within a team (design §6.3) — which
  is exactly why an AgentCoop connector is scoped to one team.
* **DMs belong to no team** and are created from the two user ids, not from a
  username.
* Auth is a bearer token from the login response's ``Token`` header, not the
  header pair Rocket.Chat uses.

The async equivalents of most of these calls already existed in
`scripts/probe_a2_mm.py`, which is where the API surface was first pinned
down; this is the sync, test-facing version of the same knowledge.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

_API = "/api/v4"


class MMClient:
    """One authenticated Mattermost session."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url + _API, timeout=timeout)
        self.token: str | None = None
        self.user_id: str | None = None
        self.username: str | None = None
        # Space-separated, from the login response body — `mm_setup` asserts
        # `system_admin` is in here rather than trusting the first-user-wins
        # ordering that grants it.
        self.roles: str | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MMClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Auth ─────────────────────────────────────────────────────────────────

    def login(self, username: str, password: str) -> "MMClient":
        resp = self._client.post(
            "/users/login", json={"login_id": username, "password": password}
        )
        resp.raise_for_status()
        # The token arrives as a RESPONSE HEADER, not in the body.
        self.token = resp.headers["Token"]
        body = resp.json()
        self.user_id = body["id"]
        self.roles = body.get("roles", "")
        self.username = username
        self._client.headers.update({"Authorization": f"Bearer {self.token}"})
        return self

    # ── Users ────────────────────────────────────────────────────────────────

    def create_user(self, username: str, password: str, email: str | None = None) -> dict[str, Any]:
        """Create a user. The FIRST user created on a fresh server becomes the
        system admin — which is how the E2E admin account comes into
        existence, since there is no other bootstrap path over the API."""
        resp = self._client.post(
            "/users",
            json={
                "username": username,
                "password": password,
                "email": email or f"{username}@e2e.local",
            },
        )
        resp.raise_for_status()
        return resp.json()

    def get_user(self, username: str) -> dict[str, Any] | None:
        resp = self._client.get(f"/users/username/{username}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def user_exists(self, username: str) -> bool:
        return self.get_user(username) is not None

    # ── Teams ────────────────────────────────────────────────────────────────

    def create_team(self, name: str, display_name: str | None = None) -> dict[str, Any]:
        resp = self._client.post(
            "/teams",
            json={"name": name, "display_name": display_name or name, "type": "O"},
        )
        resp.raise_for_status()
        return resp.json()

    def get_team(self, name: str) -> dict[str, Any] | None:
        resp = self._client.get(f"/teams/name/{name}")
        if resp.status_code in (403, 404):
            return None
        resp.raise_for_status()
        return resp.json()

    def add_team_member(self, team_id: str, user_id: str) -> None:
        """Idempotent — but only for the reason it claims.

        Mattermost answers 201 on success and 400 when the user is already a
        member. It ALSO answers 400 for a malformed or unknown id, so treating
        the status alone as "fine" made a genuinely failed seeding
        indistinguishable from a no-op. The membership is confirmed instead.
        """
        resp = self._client.post(
            f"/teams/{team_id}/members", json={"team_id": team_id, "user_id": user_id}
        )
        if resp.status_code in (200, 201):
            return
        if resp.status_code in (400, 409):
            check = self._client.get(f"/teams/{team_id}/members/{user_id}")
            if check.status_code == 200:
                return  # already a member — the case the tolerance is for
            raise RuntimeError(
                f"adding user {user_id} to team {team_id} failed "
                f"({resp.status_code}) and they are not a member: {resp.text}"
            )
        resp.raise_for_status()

    # ── Channels ─────────────────────────────────────────────────────────────

    def create_channel(
        self, team_id: str, name: str, display_name: str | None = None
    ) -> dict[str, Any]:
        resp = self._client.post(
            "/channels",
            json={
                "team_id": team_id,
                "name": name,
                "display_name": display_name or name,
                "type": "O",
            },
        )
        resp.raise_for_status()
        return resp.json()

    def get_channel(self, team_id: str, name: str) -> dict[str, Any] | None:
        """By NAME WITHIN A TEAM — a channel name is not a global identifier
        on Mattermost (design §6.3)."""
        resp = self._client.get(f"/teams/{team_id}/channels/name/{name}")
        if resp.status_code in (403, 404):
            return None
        resp.raise_for_status()
        return resp.json()

    def add_channel_member(self, channel_id: str, user_id: str) -> None:
        """Idempotent, and verified rather than assumed — same reasoning as
        `add_team_member`.

        This one matters more: a silently failed add here leaves
        `test_mm_ping_pong[channel]` with a 120s timeout reported as "No
        matching post", i.e. a delivery-shaped symptom for a setup fault. The
        membership test asserts its memberships up front and would have caught
        it; the ping-pong has no such precondition.
        """
        resp = self._client.post(f"/channels/{channel_id}/members", json={"user_id": user_id})
        if resp.status_code in (200, 201):
            return
        if resp.status_code in (400, 409):
            check = self._client.get(f"/channels/{channel_id}/members/{user_id}")
            if check.status_code == 200:
                return
            raise RuntimeError(
                f"adding user {user_id} to channel {channel_id} failed "
                f"({resp.status_code}) and they are not a member: {resp.text}"
            )
        resp.raise_for_status()

    def remove_channel_member(self, channel_id: str, user_id: str) -> None:
        resp = self._client.delete(f"/channels/{channel_id}/members/{user_id}")
        if resp.status_code not in (200, 404):
            resp.raise_for_status()

    def is_channel_member(self, channel_id: str, user_id: str) -> bool:
        """True/False only when the server actually answered the question.

        Call this from an ADMIN session. A non-member cannot query even its own
        membership row and gets **403**, which is why design §6.2 makes the
        admin-token lookup (**404** for a genuine non-member) the load-bearing
        one.

        Anything other than 200/404 raises rather than returning False. A
        `status_code == 200` test reads 403 as "not a member", so a caller
        whose token is not really an admin — Mattermost only grants
        `system_admin` to the FIRST user created, and `mm_setup` skips creation
        when the account already exists — would see every membership question
        answered "no". `test_mm_membership_delivery.py` asserts a non-membership
        as a precondition, so failing open there means the test passes without
        establishing the thing it is built on.
        """
        resp = self._client.get(f"/channels/{channel_id}/members/{user_id}")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    def get_dm_channel_id(self, other_user_id: str) -> str:
        """A DM channel belongs to no team and is addressed by the pair of
        user ids (design §6.3)."""
        assert self.user_id, "login() first"
        resp = self._client.post("/channels/direct", json=[self.user_id, other_user_id])
        resp.raise_for_status()
        return resp.json()["id"]

    # ── Messages ─────────────────────────────────────────────────────────────

    def post_message(self, channel_id: str, message: str, root_id: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        resp = self._client.post("/posts", json=body)
        resp.raise_for_status()
        return resp.json()

    def get_posts(self, channel_id: str, since_ms: int | None = None) -> list[dict[str, Any]]:
        """Posts in the channel, oldest first, from `since_ms` INCLUSIVE.

        Mattermost returns `{order: [...ids], posts: {id: post}}` with `order`
        NEWEST first, so this reverses it to match `rc_client.get_messages()`'s
        oldest-first contract.

        **The boundary is shifted by one millisecond on purpose.** Mattermost's
        `since` is exclusive — it selects posts modified *after* the value — and
        `int(time.time() * 1000)` immediately followed by a post routinely lands
        in the same millisecond. Measured against 11.7.0: a post whose
        `create_at` equalled the timestamp was returned for `since=t-5000` and
        NOT for `since=t`, with the raw `order` array empty.

        Callers pass "the moment before I acted" and mean at-or-after. Left
        exclusive, `poll_for_message` merely under-counts, but the leak check in
        test_mm_membership_delivery.py gets worse than that: a bot post landing
        on the boundary millisecond would be invisible and the negative
        assertion would pass while the thing it forbids had happened.
        """
        params: dict[str, Any] = {}
        if since_ms is not None:
            params["since"] = since_ms - 1
        resp = self._client.get(f"/channels/{channel_id}/posts", params=params)
        resp.raise_for_status()
        payload = resp.json()
        posts = payload.get("posts", {})
        return [posts[pid] for pid in reversed(payload.get("order", [])) if pid in posts]

    def poll_for_message(
        self,
        channel_id: str,
        after_ts_ms: int,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = 120.0,
        interval: float = 2.0,
    ) -> dict[str, Any]:
        """Wait for a post created after `after_ts_ms` satisfying `predicate`.

        Like `rc_client.poll_for_message`, it raises on timeout so a failure
        names the wait rather than surfacing as an unpacking error later. Two
        differences from the Rocket.Chat version, neither of them a platform
        difference — they are simply divergences worth knowing before reading a
        traceback: this raises `AssertionError` where RC raises `TimeoutError`,
        and RC additionally takes a `room_type` (it needs different endpoints
        for a DM and a channel; Mattermost does not).
        """
        deadline = time.monotonic() + timeout
        # DISTINCT post ids, not a running tally of examinations: the channel is
        # re-read every interval, so counting each pass reported "examined 60
        # post(s)" for two posts looked at thirty times — a diagnostic that
        # misdirects whoever reads the failure.
        seen: set[str] = set()
        while time.monotonic() < deadline:
            for post in self.get_posts(channel_id, since_ms=after_ts_ms):
                seen.add(post.get("id", ""))
                # A post's own type is non-empty for system messages
                # (system_join_channel and friends) — the same filter the
                # connector applies, so a join notice cannot satisfy a test.
                if post.get("type"):
                    continue
                if predicate(post):
                    return post
            time.sleep(interval)
        raise AssertionError(
            f"No matching post in channel {channel_id} within {timeout}s "
            f"(saw {len(seen)} distinct post(s) after ts={after_ts_ms})"
        )

    # ── Readiness ────────────────────────────────────────────────────────────

    @staticmethod
    def wait_for_mm(base_url: str, timeout: float = 300.0, interval: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(base_url.rstrip("/") + _API + "/system/ping", timeout=5)
                if resp.status_code == 200:
                    return
                last = f"HTTP {resp.status_code}"
            except Exception as exc:  # noqa: BLE001 — any transport error means "not yet"
                last = str(exc)
            time.sleep(interval)
        raise RuntimeError(f"Mattermost at {base_url} not ready within {timeout}s ({last})")
