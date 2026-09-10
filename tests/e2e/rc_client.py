"""Synchronous Rocket.Chat REST API client for E2E tests.

Wraps only the endpoints needed by E2E tests. Uses httpx.Client (sync) so
test bodies stay plain functions without async boilerplate.
"""
from __future__ import annotations

import datetime
import io
import re
import time
from pathlib import Path
from typing import Any, Callable

import httpx


class RCClient:
    """Synchronous RC REST client for test setup and assertions."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.auth_token: str | None = None
        self.user_id: str | None = None
        self.username: str | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RCClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Auth ─────────────────────────────────────────────────────────────────

    def login(self, username: str, password: str) -> "RCClient":
        resp = self._client.post(
            "/api/v1/login", json={"user": username, "password": password}
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Login failed for '{username}': {data}")
        self.auth_token = data["data"]["authToken"]
        self.user_id = data["data"]["userId"]
        self.username = username
        # Kept for set_setting()'s 2FA password fallback — see there.
        self._password = password
        self._client.headers.update(
            {"X-Auth-Token": self.auth_token, "X-User-Id": self.user_id}
        )
        return self

    def set_setting(self, setting_id: str, value: object) -> None:
        """Change an admin setting, satisfying Rocket.Chat's 2FA gate.

        From RC 8.x, writing a protected setting requires two-factor
        confirmation even when 2FA is off — verified against 8.5, which
        answers a bare POST with
        ``{"error": "TOTP Required [totp-required]", "details":
        {"method": "password", "availableMethods": []}}``. Since no TOTP or
        email method is available on a fresh workspace, the only usable
        route is the password fallback, and the code must be the **SHA-256
        digest** of the password, not the password itself (plaintext returns
        ``totp-invalid``, which is how this was pinned down).

        This is what broke `make e2e-up` on the 6.12 → 8.5.1 move: the
        settings write is the first thing setup.py does after logging in.
        """
        import hashlib

        response = self._client.post(
            f"/api/v1/settings/{setting_id}",
            json={"value": value},
            headers={
                "x-2fa-code": hashlib.sha256(self._password.encode()).hexdigest(),
                "x-2fa-method": "password",
            },
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("success"):
            raise RuntimeError(f"settings/{setting_id} failed: {body}")

    # ── Users ────────────────────────────────────────────────────────────────

    def create_user(
        self,
        username: str,
        password: str,
        email: str,
        name: str,
        roles: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a user. Returns the user dict from RC."""
        payload: dict[str, Any] = {
            "username": username,
            "password": password,
            "email": email,
            "name": name,
            "verified": True,
            "requirePasswordChange": False,
        }
        if roles:
            payload["roles"] = roles
        resp = self._client.post("/api/v1/users.create", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"users.create failed for '{username}': {data}")
        return data["user"]

    def get_user(self, username: str) -> dict[str, Any] | None:
        """Get user info by username. Returns None if not found."""
        resp = self._client.get("/api/v1/users.info", params={"username": username})
        if resp.status_code in (400, 404):
            return None
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            return None
        return data.get("user")

    def user_exists(self, username: str) -> bool:
        return self.get_user(username) is not None

    # ── Rooms ────────────────────────────────────────────────────────────────

    def create_channel(
        self, name: str, members: list[str] | None = None
    ) -> dict[str, Any]:
        """Create a public channel. Returns channel dict."""
        payload: dict[str, Any] = {"name": name}
        if members:
            payload["members"] = members
        resp = self._client.post("/api/v1/channels.create", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"channels.create failed for '{name}': {data}")
        return data["channel"]

    def get_channel(self, name: str) -> dict[str, Any] | None:
        """Get channel info by name. Returns None if not found."""
        resp = self._client.get("/api/v1/channels.info", params={"roomName": name})
        if resp.status_code in (400, 404):
            return None
        resp.raise_for_status()
        data = resp.json()
        return data.get("channel")

    def invite_to_channel(self, room_id: str, user_id: str) -> None:
        """Add a user to a channel. Ignores 'already in room' errors."""
        resp = self._client.post(
            "/api/v1/channels.invite", json={"roomId": room_id, "userId": user_id}
        )
        if resp.status_code == 400:
            body = resp.json()
            # RC returns error if already a member — safe to ignore
            if "already" in str(body).lower():
                return
        resp.raise_for_status()

    def get_dm_room_id(self, username: str) -> str:
        """Open or retrieve a DM room with a user. Returns the room _id."""
        resp = self._client.post("/api/v1/im.create", json={"username": username})
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"im.create failed for '@{username}': {data}")
        return data["room"]["_id"]

    # ── Messages ─────────────────────────────────────────────────────────────

    def post_message(
        self,
        room_id: str,
        text: str,
        tmid: str | None = None,
    ) -> dict[str, Any]:
        """Post a message. Returns the message dict (includes _id, ts)."""
        payload: dict[str, Any] = {"roomId": room_id, "text": text}
        if tmid:
            payload["tmid"] = tmid
        resp = self._client.post("/api/v1/chat.postMessage", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"chat.postMessage failed: {data}")
        return data["message"]

    def upload_file(
        self,
        room_id: str,
        file_path: str | Path,
        description: str = "",
        tmid: str | None = None,
    ) -> dict[str, Any]:
        """Upload a file to a room. Returns the file message dict.

        Two steps, because the one-step upload endpoint was **removed in
        Rocket.Chat 8.0.0** and the compose stack now pins 8.5.1:
        ``rooms.media/{rid}`` uploads the bytes, then
        ``rooms.mediaConfirm/{rid}/{fileId}`` sends it into the room. Both are
        required — a file left unconfirmed never appears in the room.

        Deliberately NOT version-dispatching the way the product connector
        does (``gateway/connectors/rocketchat/rest.py`` picks a flow from the
        server's detected major version): that code serves whatever server an
        operator points it at, while this client only ever talks to the server
        this repo's compose pins, so the pin is the contract. If that pin ever
        drops below 8.0, the legacy flow has to come back here — which
        tests/unit/test_e2e_platform_pins.py is what notices.
        """
        file_path = Path(file_path)
        content = file_path.read_bytes()
        files = {
            "file": (file_path.name, io.BytesIO(content), "application/octet-stream")
        }
        media = self._client.post(f"/api/v1/rooms.media/{room_id}", files=files)
        media.raise_for_status()
        media_result = media.json()
        if not media_result.get("success"):
            raise RuntimeError(f"rooms.media failed: {media_result}")
        file_id = (media_result.get("file") or {}).get("_id")
        if not file_id:
            raise RuntimeError(f"rooms.media response missing file._id: {media_result}")

        confirm_body: dict[str, str] = {}
        if description:
            confirm_body["description"] = description
        if tmid:
            confirm_body["tmid"] = tmid
        confirm = self._client.post(
            f"/api/v1/rooms.mediaConfirm/{room_id}/{file_id}",
            json=confirm_body,
        )
        confirm.raise_for_status()
        result = confirm.json()
        if not result.get("success"):
            raise RuntimeError(f"rooms.mediaConfirm failed: {result}")
        return result.get("message", result)

    # ── Message retrieval ─────────────────────────────────────────────────────

    def _to_iso(self, ts_ms: int) -> str:
        dt = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc)
        return dt.isoformat()

    def get_messages(
        self,
        room_id: str,
        oldest_ts_ms: int | None = None,
        count: int = 50,
    ) -> list[dict[str, Any]]:
        """Get messages from a public channel using channels.history.

        channels.history supports the 'oldest' date filter.
        Returns list sorted oldest-first.
        """
        params: dict[str, Any] = {"roomId": room_id, "count": count, "inclusive": True}
        if oldest_ts_ms is not None:
            params["oldest"] = self._to_iso(oldest_ts_ms)
        resp = self._client.get("/api/v1/channels.history", params=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"channels.history failed: {data}")
        # RC returns newest-first; reverse for chronological order
        return list(reversed(data.get("messages", [])))

    def get_dm_messages(
        self,
        room_id: str,
        oldest_ts_ms: int | None = None,
        count: int = 50,
    ) -> list[dict[str, Any]]:
        """Get messages from a DM room using im.history.

        im.history supports the 'oldest' date filter.
        Returns list sorted oldest-first.
        """
        params: dict[str, Any] = {"roomId": room_id, "count": count, "inclusive": True}
        if oldest_ts_ms is not None:
            params["oldest"] = self._to_iso(oldest_ts_ms)
        resp = self._client.get("/api/v1/im.history", params=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"im.history failed: {data}")
        return list(reversed(data.get("messages", [])))

    def get_thread_messages(self, tmid: str) -> list[dict[str, Any]]:
        """Get all messages in a thread by root message ID."""
        resp = self._client.get(
            "/api/v1/chat.getThreadMessages", params={"tmid": tmid}
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"chat.getThreadMessages failed: {data}")
        return data.get("messages", [])

    # ── Polling ───────────────────────────────────────────────────────────────

    def poll_for_message(
        self,
        room_id: str,
        after_ts_ms: int,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float = 120.0,
        interval: float = 2.0,
        room_type: str = "channel",
    ) -> dict[str, Any]:
        """Poll for a message satisfying predicate, posted after after_ts_ms.

        Args:
            room_id:     RC room _id.
            after_ts_ms: Unix ms timestamp — only messages after this are checked.
            predicate:   Returns True for the desired message.
            timeout:     Max seconds to wait.
            interval:    Poll interval in seconds.
            room_type:   "dm" uses im.messages; anything else uses channels.messages.

        Returns:
            Matching message dict.

        Raises:
            TimeoutError: No matching message within timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if room_type == "dm":
                msgs = self.get_dm_messages(room_id, oldest_ts_ms=after_ts_ms)
            else:
                msgs = self.get_messages(room_id, oldest_ts_ms=after_ts_ms)
            for msg in msgs:
                if predicate(msg):
                    return msg
            time.sleep(interval)
        raise TimeoutError(
            f"No matching message in room {room_id} within {timeout}s "
            f"(checked from ts={after_ts_ms})"
        )

    def poll_for_attachment(
        self,
        room_id: str,
        after_ts_ms: int,
        *,
        timeout: float = 180.0,
        interval: float = 2.0,
        room_type: str = "channel",
    ) -> dict[str, Any]:
        """Poll for any message containing a file attachment."""

        def has_attachment(msg: dict[str, Any]) -> bool:
            return bool(msg.get("file")) or bool(msg.get("attachments"))

        return self.poll_for_message(
            room_id,
            after_ts_ms,
            has_attachment,
            timeout=timeout,
            interval=interval,
            room_type=room_type,
        )

    def extract_permission_id(self, msg: dict[str, Any]) -> str | None:
        """Extract the 4-char permission request ID from a bot permission message.

        AgentCoop formats permission notices as:
            🔐 **Permission required** `[a3k9]`
            ...
            Reply `approve a3k9` or `deny a3k9`

        Returns the 4-char ID (e.g. 'a3k9'), or None if not a permission message.
        """
        text = msg.get("msg", "")
        m = re.search(r"`\[([a-z0-9]{4})\]`", text)
        return m.group(1) if m else None

    # ── Health check ──────────────────────────────────────────────────────────

    @classmethod
    def wait_for_rc(
        cls,
        base_url: str,
        timeout: float = 300.0,
        interval: float = 3.0,
    ) -> None:
        """Block until RC's /health endpoint returns 200 with status 'healthy'.

        Raises RuntimeError if RC does not become healthy within timeout.
        """
        deadline = time.monotonic() + timeout
        last_error = "(not tried yet)"
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            while time.monotonic() < deadline:
                try:
                    resp = client.get("/health")
                    if resp.status_code == 200:
                        # RC /health returns plain text "ok" (not JSON)
                        text = resp.text.strip()
                        if text == "ok":
                            return
                        # Fallback: some versions may return JSON
                        try:
                            data = resp.json()
                            if data.get("status") in ("healthy", "ok"):
                                return
                            last_error = f"status={data.get('status')!r}"
                        except Exception:
                            last_error = f"unexpected body: {text!r}"
                    else:
                        last_error = f"HTTP {resp.status_code}"
                except Exception as exc:
                    last_error = str(exc)
                time.sleep(interval)
        raise RuntimeError(
            f"RC at {base_url} did not become healthy within {timeout}s. "
            f"Last error: {last_error}"
        )
