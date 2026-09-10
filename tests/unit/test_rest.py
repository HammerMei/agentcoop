"""Unit tests for RocketChatREST.

All network I/O is mocked via httpx.MockTransport / respx-style patches so
no real server is required.  Each test targets a single method and verifies:
  - Happy path (correct return value / side effects)
  - Error paths (4xx/5xx, auth expiry, not-found, missing fields)
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from gateway.connectors.rocketchat.rest import RocketChatREST, RoomNotFoundError

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_response(
    status_code: int = 200,
    json_body: dict | None = None,
    text_body: str = "",
) -> httpx.Response:
    """Build a minimal httpx.Response without a real transport."""
    body = (
        json.dumps(json_body or {}).encode()
        if json_body is not None
        else text_body.encode()
    )
    return httpx.Response(
        status_code=status_code,
        content=body,
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "http://example.com"),
    )


def _make_rest() -> RocketChatREST:
    return RocketChatREST("http://chat.example.com")


# ── __repr__ ──────────────────────────────────────────────────────────────────


class TestRepr(unittest.TestCase):
    def test_repr_masks_password(self):
        rest = _make_rest()
        rest._username = "bot"
        rest._password = "s3cr3t"
        r = repr(rest)
        self.assertIn("bot", r)
        self.assertNotIn("s3cr3t", r)
        self.assertIn("***", r)

    def test_repr_no_credentials(self):
        rest = _make_rest()
        r = repr(rest)
        self.assertIn("None", r)


# ── close ─────────────────────────────────────────────────────────────────────


class TestClose(unittest.IsolatedAsyncioTestCase):
    async def test_close_calls_aclose_on_both_clients(self):
        rest = _make_rest()
        rest._client.aclose = AsyncMock()
        rest._download_client.aclose = AsyncMock()
        await rest.close()
        rest._client.aclose.assert_called_once()
        rest._download_client.aclose.assert_called_once()


# ── login ─────────────────────────────────────────────────────────────────────


class TestLogin(unittest.IsolatedAsyncioTestCase):
    async def test_login_success_stores_credentials(self):
        rest = _make_rest()
        ok_resp = _make_response(
            200,
            {
                "status": "success",
                "data": {"authToken": "tok123", "userId": "uid456"},
            },
        )
        rest._client.post = AsyncMock(return_value=ok_resp)

        await rest.login("bot", "pass")

        self.assertEqual(rest.auth_token, "tok123")
        self.assertEqual(rest.user_id, "uid456")
        # No `me` in the response → the typed spelling is all there is.
        self.assertEqual(rest.bot_username, "bot")
        self.assertEqual(rest._username, "bot")
        self.assertEqual(rest._password, "pass")

    async def test_login_stores_the_canonical_username_not_the_typed_one(self):
        """#112: login is not spelling-exact (probed on 6.12 — 'probebot9207'
        and the account's email both log into 'ProbeBot9207'), and every
        message frame carries the canonical form. Identity comparisons built
        on the typed spelling silently fail — the bot never recognises its
        own @mention — so the canonical one is what login must keep."""
        rest = _make_rest()
        ok_resp = _make_response(
            200,
            {
                "status": "success",
                "data": {
                    "authToken": "tok123",
                    "userId": "uid456",
                    "me": {"username": "ProbeBot9207"},
                },
            },
        )
        rest._client.post = AsyncMock(return_value=ok_resp)

        await rest.login("probebot9207", "pass")

        self.assertEqual(rest.bot_username, "ProbeBot9207")
        # The typed spelling survives for re-login only.
        self.assertEqual(rest._username, "probebot9207")

    async def test_login_status_not_success_raises(self):
        rest = _make_rest()
        bad_resp = _make_response(200, {"status": "error", "message": "bad creds"})
        rest._client.post = AsyncMock(return_value=bad_resp)

        with self.assertRaises(RuntimeError, msg="Login failed"):
            await rest.login("bot", "wrongpass")

    async def test_login_http_error_raises(self):
        rest = _make_rest()
        err_resp = _make_response(401, {"message": "Unauthorized"})
        rest._client.post = AsyncMock(return_value=err_resp)

        with self.assertRaises(httpx.HTTPStatusError):
            await rest.login("bot", "bad")


# ── _request ──────────────────────────────────────────────────────────────────


class TestRequest(unittest.IsolatedAsyncioTestCase):
    async def test_request_returns_json_on_success(self):
        rest = _make_rest()
        rest.auth_token = "t"
        rest.user_id = "u"
        resp = _make_response(200, {"success": True, "data": "value"})
        rest._client.request = AsyncMock(return_value=resp)

        result = await rest._request("GET", "some.endpoint")
        self.assertEqual(result["success"], True)

    async def test_request_401_triggers_relogin_and_retries(self):
        rest = _make_rest()
        rest._username = "bot"
        rest._password = "pass"
        rest.auth_token = "old_token"
        rest.user_id = "uid"

        unauthorized = _make_response(401, {"message": "Unauthorized"})
        ok_resp = _make_response(200, {"success": True})

        # First call → 401, second call (retry) → 200
        rest._client.request = AsyncMock(side_effect=[unauthorized, ok_resp])

        # login() is called during re-auth; mock it
        login_resp = _make_response(
            200,
            {
                "status": "success",
                "data": {"authToken": "new_tok", "userId": "uid"},
            },
        )
        rest._client.post = AsyncMock(return_value=login_resp)

        result = await rest._request("GET", "some.endpoint")
        self.assertEqual(result["success"], True)
        self.assertEqual(rest.auth_token, "new_tok")

    async def test_request_401_with_no_password_raises_runtime_error(self):
        """Q3: 401 re-login path must raise RuntimeError when _password is None.

        Previously, _password was typed str | None but passed to login() without
        a None guard — this would produce a confusing TypeError deep in httpx.
        Now it raises a clear RuntimeError.
        """
        rest = _make_rest()
        rest._username = "bot"
        rest._password = None  # simulate uninitialized state
        rest.auth_token = "old_token"
        rest.user_id = "uid"

        unauthorized = _make_response(401, {"message": "Unauthorized"})
        rest._client.request = AsyncMock(return_value=unauthorized)

        with self.assertRaises(RuntimeError) as ctx:
            await rest._request("GET", "some.endpoint")
        self.assertIn("Cannot re-login", str(ctx.exception))

    async def test_request_401_with_no_username_skips_relogin(self):
        """Q3 (related): When _username is None, the 401 re-login path is
        skipped entirely (outer guard 'and self._username' is falsy) and the
        request raises HTTPStatusError directly — no confusing TypeError."""
        rest = _make_rest()
        rest._username = None  # outer guard 'and self._username' will be falsy
        rest._password = "pw"
        rest.auth_token = "old_token"
        rest.user_id = "uid"

        unauthorized = _make_response(401, {"message": "Unauthorized"})
        rest._client.request = AsyncMock(return_value=unauthorized)

        # Re-login is skipped; falls through to raise_for_status() → HTTPStatusError
        with self.assertRaises(httpx.HTTPStatusError):
            await rest._request("GET", "some.endpoint")

    async def test_request_non_success_raises_http_status_error(self):
        rest = _make_rest()
        resp = _make_response(500, {"error": "Internal Server Error"})
        rest._client.request = AsyncMock(return_value=resp)

        with self.assertRaises(httpx.HTTPStatusError):
            await rest._request("GET", "some.endpoint")

    async def test_request_passes_params_and_json(self):
        rest = _make_rest()
        resp = _make_response(200, {"ok": True})
        rest._client.request = AsyncMock(return_value=resp)

        await rest._request(
            "POST", "chat.postMessage", json_data={"text": "hi"}, params={"foo": "bar"}
        )
        rest._client.request.assert_called_once()
        _, kwargs = rest._client.request.call_args
        self.assertEqual(kwargs["json"], {"text": "hi"})
        self.assertEqual(kwargs["params"], {"foo": "bar"})


# ── post_message ──────────────────────────────────────────────────────────────


class TestPostMessage(unittest.IsolatedAsyncioTestCase):
    async def _patched_rest(self, response_body: dict) -> RocketChatREST:
        rest = _make_rest()
        rest._request = AsyncMock(return_value=response_body)
        return rest

    async def test_post_message_no_thread(self):
        rest = await self._patched_rest({"success": True})
        await rest.post_message("general", "Hello!")
        rest._request.assert_called_once_with(
            "POST",
            "chat.postMessage",
            json_data={"channel": "general", "text": "Hello!"},
        )

    async def test_post_message_with_thread(self):
        rest = await self._patched_rest({"success": True})
        await rest.post_message("ROOM123", "reply", tmid="THREAD456")
        rest._request.assert_called_once_with(
            "POST",
            "chat.postMessage",
            json_data={"roomId": "ROOM123", "text": "reply", "tmid": "THREAD456"},
        )

    async def test_post_message_failure_raises(self):
        rest = await self._patched_rest({"success": False, "error": "not_in_room"})
        with self.assertRaises(RuntimeError, msg="not_in_room"):
            await rest.post_message("general", "hi")


# ── download_file ─────────────────────────────────────────────────────────────


class TestDownloadFile(unittest.IsolatedAsyncioTestCase):
    async def test_download_writes_bytes_to_disk(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"

        # Build a fake streaming response
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        async def _fake_aiter_bytes():
            yield b"chunk1"
            yield b"chunk2"

        fake_resp.aiter_bytes = _fake_aiter_bytes
        rest._download_client.stream = MagicMock(return_value=fake_resp)

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = str(Path(tmpdir) / "file.bin")
            await rest.download_file("/file-upload/abc/test.bin", dest)
            self.assertEqual(Path(dest).read_bytes(), b"chunk1chunk2")

    async def test_download_http_error_raises(self):
        rest = _make_rest()

        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "403",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(403, request=httpx.Request("GET", "http://x")),
            )
        )
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)
        rest._download_client.stream = MagicMock(return_value=fake_resp)

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(httpx.HTTPStatusError):
                await rest.download_file(
                    "/file-upload/abc/file.bin", str(Path(tmpdir) / "out.bin")
                )

    async def test_download_401_relogs_and_retries(self):
        rest = _make_rest()
        rest._username = "bot"
        rest._password = "pass"
        rest.auth_token = "old"
        rest.user_id = "uid"

        unauthorized = MagicMock()
        unauthorized.status_code = 401
        unauthorized.__aenter__ = AsyncMock(return_value=unauthorized)
        unauthorized.__aexit__ = AsyncMock(return_value=False)
        unauthorized.aiter_bytes = AsyncMock()

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.raise_for_status = MagicMock()
        ok_resp.__aenter__ = AsyncMock(return_value=ok_resp)
        ok_resp.__aexit__ = AsyncMock(return_value=False)

        async def _fake_aiter_bytes():
            yield b"retry-ok"

        ok_resp.aiter_bytes = _fake_aiter_bytes

        rest._download_client.stream = MagicMock(side_effect=[unauthorized, ok_resp])
        login_resp = _make_response(
            200,
            {
                "status": "success",
                "data": {"authToken": "new_tok", "userId": "uid"},
            },
        )
        rest._client.post = AsyncMock(return_value=login_resp)

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = str(Path(tmpdir) / "file.bin")
            await rest.download_file("/file-upload/abc/test.bin", dest)
            self.assertEqual(Path(dest).read_bytes(), b"retry-ok")
            self.assertEqual(rest.auth_token, "new_tok")
            self.assertEqual(rest._download_client.stream.call_count, 2)


# ── upload_file ───────────────────────────────────────────────────────────────


class TestUploadFile(unittest.IsolatedAsyncioTestCase):
    """Legacy (pre-8.0) rooms.upload flow.

    Each test seeds ``_server_major_version = 6`` directly so upload_file()
    dispatches to _upload_file_legacy() without making a real /api/info call
    (this module's tests mock all network I/O — see module docstring).
    """

    async def test_upload_missing_file_raises(self):
        rest = _make_rest()
        with self.assertRaises(FileNotFoundError):
            await rest.upload_file("ROOM1", "/nonexistent/path/file.txt")

    async def test_upload_success(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._server_major_version = 6

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "report.txt"
            fpath.write_bytes(b"hello world")

            ok_resp = _make_response(200, {"success": True})
            rest._download_client.post = AsyncMock(return_value=ok_resp)

            await rest.upload_file("ROOM1", str(fpath), caption="my file")
            rest._download_client.post.assert_called_once()
            _, kwargs = rest._download_client.post.call_args
            self.assertEqual(kwargs["data"], {"msg": "my file"})

    async def test_upload_no_caption_omits_msg(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._server_major_version = 6

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "img.png"
            fpath.write_bytes(b"\x89PNG")

            ok_resp = _make_response(200, {"success": True})
            rest._download_client.post = AsyncMock(return_value=ok_resp)

            await rest.upload_file("ROOM1", str(fpath))
            _, kwargs = rest._download_client.post.call_args
            self.assertEqual(kwargs["data"], {})

    async def test_upload_api_failure_raises(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._server_major_version = 6

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "doc.pdf"
            fpath.write_bytes(b"%PDF")

            fail_resp = _make_response(200, {"success": False, "error": "too_large"})
            rest._download_client.post = AsyncMock(return_value=fail_resp)

            with self.assertRaises(RuntimeError, msg="too_large"):
                await rest.upload_file("ROOM1", str(fpath))

    async def test_upload_401_relogins_and_retries(self):
        rest = _make_rest()
        rest._username = "bot"
        rest._password = "pass"
        rest.auth_token = "old_tok"
        rest.user_id = "uid"
        rest._server_major_version = 6

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "file.txt"
            fpath.write_bytes(b"data")

            unauth = _make_response(401, {"message": "Unauthorized"})
            ok_resp = _make_response(200, {"success": True})
            rest._download_client.post = AsyncMock(side_effect=[unauth, ok_resp])

            login_resp = _make_response(
                200,
                {
                    "status": "success",
                    "data": {"authToken": "new_tok", "userId": "uid"},
                },
            )
            rest._client.post = AsyncMock(return_value=login_resp)

            await rest.upload_file("ROOM1", str(fpath))
            self.assertEqual(rest.auth_token, "new_tok")
            self.assertEqual(rest._download_client.post.call_count, 2)

    async def test_upload_unknown_mime_falls_back_to_octet_stream(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._server_major_version = 6

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "weirdfile.xyz123"
            fpath.write_bytes(b"binary data")

            ok_resp = _make_response(200, {"success": True})
            rest._download_client.post = AsyncMock(return_value=ok_resp)

            await rest.upload_file("ROOM1", str(fpath))
            _, kwargs = rest._download_client.post.call_args
            # mime type is embedded in the files tuple: (name, bytes, mime)
            file_tuple = kwargs["files"]["file"]
            self.assertEqual(file_tuple[2], "application/octet-stream")

    async def test_legacy_404_with_undetected_version_raises_clear_error(self):
        """When version detection genuinely fails (returns None) and
        rooms.upload then 404s, that must not surface as a bare
        HTTPStatusError (issue #56's own acceptance criterion: a clear error
        pointing at the likely version mismatch, not a generic 4xx)."""
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._get_server_major_version = AsyncMock(return_value=None)

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "f.txt"
            fpath.write_bytes(b"data")

            not_found = _make_response(404, {"success": False, "error": "not-found"})
            rest._download_client.post = AsyncMock(return_value=not_found)

            with self.assertRaises(RuntimeError) as ctx:
                await rest.upload_file("ROOM1", str(fpath))
            self.assertIn("8.0+", str(ctx.exception))
            self.assertIn("/api/info", str(ctx.exception))

    async def test_legacy_404_with_confirmed_pre8_version_raises_real_error(self):
        """Regression test for a Codex review finding on PR #75: when the
        version is *positively confirmed* < 8 (not merely undetected), a
        rooms.upload 404 is a genuine, unrelated failure (bad room ID,
        disabled route, ...) and must propagate as the real HTTPStatusError —
        NOT get misreported as a version mismatch that never happened."""
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._server_major_version = 6  # confirmed pre-8.0, not "undetected"

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "f.txt"
            fpath.write_bytes(b"data")

            not_found = _make_response(404, {"success": False, "error": "not-found"})
            rest._download_client.post = AsyncMock(return_value=not_found)

            with self.assertRaises(httpx.HTTPStatusError):
                await rest.upload_file("ROOM1", str(fpath))


# ── _get_server_major_version ───────────────────────────────────────────────


class TestGetServerMajorVersion(unittest.IsolatedAsyncioTestCase):
    async def test_parses_trimmed_unauthenticated_version(self):
        """Unauthenticated /api/info trims to '<major>.<minor>' (no patch)."""
        rest = _make_rest()
        rest._client.get = AsyncMock(return_value=_make_response(200, {"success": True, "version": "8.5"}))
        major = await rest._get_server_major_version()
        self.assertEqual(major, 8)

    async def test_parses_full_authenticated_version(self):
        """Authenticated callers with view-statistics get the full patch version."""
        rest = _make_rest()
        rest._client.get = AsyncMock(return_value=_make_response(200, {"success": True, "version": "8.5.2"}))
        major = await rest._get_server_major_version()
        self.assertEqual(major, 8)

    async def test_caches_result_does_not_refetch(self):
        rest = _make_rest()
        rest._client.get = AsyncMock(return_value=_make_response(200, {"success": True, "version": "8.0"}))
        first = await rest._get_server_major_version()
        second = await rest._get_server_major_version()
        self.assertEqual(first, 8)
        self.assertEqual(second, 8)
        rest._client.get.assert_awaited_once()

    async def test_http_error_returns_none_and_is_not_cached(self):
        rest = _make_rest()
        rest._client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        result = await rest._get_server_major_version()
        self.assertIsNone(result)
        self.assertIsNone(rest._server_major_version)
        # A later successful call is retried, not permanently stuck at None.
        rest._client.get = AsyncMock(return_value=_make_response(200, {"success": True, "version": "8.0"}))
        result2 = await rest._get_server_major_version()
        self.assertEqual(result2, 8)

    async def test_missing_version_field_returns_none(self):
        rest = _make_rest()
        rest._client.get = AsyncMock(return_value=_make_response(200, {"success": True}))
        result = await rest._get_server_major_version()
        self.assertIsNone(result)

    async def test_malformed_version_field_returns_none(self):
        rest = _make_rest()
        rest._client.get = AsyncMock(return_value=_make_response(200, {"success": True, "version": "not-a-version"}))
        result = await rest._get_server_major_version()
        self.assertIsNone(result)

    async def test_pre_8_version_returns_correct_major(self):
        rest = _make_rest()
        rest._client.get = AsyncMock(return_value=_make_response(200, {"success": True, "version": "6.12.0"}))
        result = await rest._get_server_major_version()
        self.assertEqual(result, 6)


# ── upload_file version dispatch ────────────────────────────────────────────


class TestUploadFileVersionDispatch(unittest.IsolatedAsyncioTestCase):
    """upload_file() must route to the legacy or v8+ flow based on detected version."""

    async def test_version_8_or_above_uses_v8_plus_flow(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._get_server_major_version = AsyncMock(return_value=8)
        rest._upload_file_v8_plus = AsyncMock()
        rest._upload_file_legacy = AsyncMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "f.txt"
            fpath.write_bytes(b"data")
            await rest.upload_file("ROOM1", str(fpath))

        rest._upload_file_v8_plus.assert_awaited_once()
        rest._upload_file_legacy.assert_not_called()

    async def test_version_below_8_uses_legacy_flow(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._get_server_major_version = AsyncMock(return_value=6)
        rest._upload_file_v8_plus = AsyncMock()
        rest._upload_file_legacy = AsyncMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "f.txt"
            fpath.write_bytes(b"data")
            await rest.upload_file("ROOM1", str(fpath))

        rest._upload_file_legacy.assert_awaited_once()
        rest._upload_file_v8_plus.assert_not_called()

    async def test_undetectable_version_falls_back_to_legacy_flow(self):
        """Matches pre-#56 behavior when the server's version can't be determined."""
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._get_server_major_version = AsyncMock(return_value=None)
        rest._upload_file_v8_plus = AsyncMock()
        rest._upload_file_legacy = AsyncMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "f.txt"
            fpath.write_bytes(b"data")
            await rest.upload_file("ROOM1", str(fpath))

        rest._upload_file_legacy.assert_awaited_once()
        rest._upload_file_v8_plus.assert_not_called()


# ── upload_file RC 8.0+ (rooms.media + rooms.mediaConfirm) ─────────────────


class TestUploadFileV8Plus(unittest.IsolatedAsyncioTestCase):
    def _media_ok(self, file_id: str = "file123") -> httpx.Response:
        return _make_response(200, {"success": True, "file": {"_id": file_id, "url": f"/file-upload/{file_id}/x"}})

    def _confirm_ok(self) -> httpx.Response:
        return _make_response(200, {"success": True, "message": {"_id": "msg1"}})

    async def test_two_step_upload_success(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._server_major_version = 8

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "report.txt"
            fpath.write_bytes(b"hello world")

            rest._download_client.post = AsyncMock(
                side_effect=[self._media_ok("file123"), self._confirm_ok()]
            )
            await rest.upload_file("ROOM1", str(fpath), caption="my file")

        self.assertEqual(rest._download_client.post.call_count, 2)
        media_call, confirm_call = rest._download_client.post.call_args_list
        self.assertIn("rooms.media/ROOM1", media_call.args[0])
        self.assertIn("files", media_call.kwargs)
        self.assertIn("rooms.mediaConfirm/ROOM1/file123", confirm_call.args[0])
        self.assertEqual(confirm_call.kwargs["json"], {"msg": "my file"})

    async def test_no_caption_omits_msg_in_confirm(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._server_major_version = 8

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "img.png"
            fpath.write_bytes(b"\x89PNG")

            rest._download_client.post = AsyncMock(
                side_effect=[self._media_ok(), self._confirm_ok()]
            )
            await rest.upload_file("ROOM1", str(fpath))

        _, confirm_call = rest._download_client.post.call_args_list
        self.assertEqual(confirm_call.kwargs["json"], {})

    async def test_media_step_failure_raises_and_skips_confirm(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._server_major_version = 8

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "f.txt"
            fpath.write_bytes(b"data")

            fail_resp = _make_response(200, {"success": False, "error": "too_large"})
            rest._download_client.post = AsyncMock(return_value=fail_resp)

            with self.assertRaises(RuntimeError):
                await rest.upload_file("ROOM1", str(fpath))

        rest._download_client.post.assert_called_once()

    async def test_media_step_missing_file_id_raises(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._server_major_version = 8

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "f.txt"
            fpath.write_bytes(b"data")

            malformed_resp = _make_response(200, {"success": True, "file": {}})
            rest._download_client.post = AsyncMock(return_value=malformed_resp)

            with self.assertRaises(RuntimeError):
                await rest.upload_file("ROOM1", str(fpath))

    async def test_media_step_null_file_field_raises_not_attributeerror(self):
        """{"file": null} must raise RuntimeError, not AttributeError from
        calling .get() on None (media_result.get("file") or {}).get("_id")."""
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._server_major_version = 8

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "f.txt"
            fpath.write_bytes(b"data")

            malformed_resp = _make_response(200, {"success": True, "file": None})
            rest._download_client.post = AsyncMock(return_value=malformed_resp)

            with self.assertRaises(RuntimeError):
                await rest.upload_file("ROOM1", str(fpath))

    async def test_confirm_step_failure_raises(self):
        rest = _make_rest()
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._server_major_version = 8

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "f.txt"
            fpath.write_bytes(b"data")

            fail_confirm = _make_response(200, {"success": False, "error": "invalid-file"})
            rest._download_client.post = AsyncMock(
                side_effect=[self._media_ok(), fail_confirm]
            )
            with self.assertRaises(RuntimeError):
                await rest.upload_file("ROOM1", str(fpath))

    async def test_401_during_media_step_relogins_and_retries(self):
        rest = _make_rest()
        rest._username = "bot"
        rest._password = "pass"
        rest.auth_token = "old_tok"
        rest.user_id = "uid"
        rest._server_major_version = 8

        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "f.txt"
            fpath.write_bytes(b"data")

            unauth = _make_response(401, {"message": "Unauthorized"})
            rest._download_client.post = AsyncMock(
                side_effect=[unauth, self._media_ok(), self._confirm_ok()]
            )
            login_resp = _make_response(
                200, {"status": "success", "data": {"authToken": "new_tok", "userId": "uid"}}
            )
            rest._client.post = AsyncMock(return_value=login_resp)

            await rest.upload_file("ROOM1", str(fpath))

        self.assertEqual(rest.auth_token, "new_tok")
        self.assertEqual(rest._download_client.post.call_count, 3)


# ── resolve_room ──────────────────────────────────────────────────────────────


class TestResolveRoom(unittest.IsolatedAsyncioTestCase):
    # ── DM (@username) ────────────────────────────────────────────────────────

    async def test_resolve_dm_success(self):
        rest = _make_rest()
        rest._request = AsyncMock(
            return_value={
                "success": True,
                "room": {"_id": "DM_ROOM_ID"},
            }
        )
        result = await rest.resolve_room("@alice")
        self.assertEqual(result["_id"], "DM_ROOM_ID")
        self.assertEqual(result["type"], "dm")
        self.assertEqual(result["name"], "@alice")

    async def test_resolve_dm_http_error_raises_runtime(self):
        rest = _make_rest()
        err_response = _make_response(404)
        rest._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "404",
                request=err_response.request,
                response=err_response,
            )
        )
        with self.assertRaises(RuntimeError, msg="Failed to open DM"):
            await rest.resolve_room("@ghost")

    async def test_resolve_dm_no_room_in_response_raises_not_found(self):
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"success": False})
        with self.assertRaises(RoomNotFoundError):
            await rest.resolve_room("@nobody")

    # ── Public channel ────────────────────────────────────────────────────────

    async def test_resolve_public_channel_success(self):
        rest = _make_rest()
        rest._request = AsyncMock(
            return_value={
                "success": True,
                "channel": {"_id": "CH_ID", "name": "general"},
            }
        )
        result = await rest.resolve_room("general")
        self.assertEqual(result["_id"], "CH_ID")
        self.assertEqual(result["type"], "channel")
        self.assertEqual(result["name"], "general")

    async def test_resolve_channel_404_falls_through_to_group(self):
        rest = _make_rest()
        err_resp = _make_response(404)

        # channels.info → 404 (channel not found), groups.info → success
        def _side_effect(method, endpoint, **kwargs):
            if endpoint == "channels.info":
                raise httpx.HTTPStatusError(
                    "404", request=err_resp.request, response=err_resp
                )
            return {"success": True, "group": {"_id": "GRP_ID", "name": "secret"}}

        rest._request = AsyncMock(side_effect=_side_effect)
        result = await rest.resolve_room("secret")
        self.assertEqual(result["_id"], "GRP_ID")
        self.assertEqual(result["type"], "group")

    async def test_resolve_channel_400_falls_through_to_group(self):
        rest = _make_rest()
        err_resp = _make_response(400)

        def _side_effect(method, endpoint, **kwargs):
            if endpoint == "channels.info":
                raise httpx.HTTPStatusError(
                    "400", request=err_resp.request, response=err_resp
                )
            return {"success": True, "group": {"_id": "GRP_ID", "name": "priv"}}

        rest._request = AsyncMock(side_effect=_side_effect)
        result = await rest.resolve_room("priv")
        self.assertEqual(result["type"], "group")

    async def test_resolve_channel_500_re_raises(self):
        rest = _make_rest()
        err_resp = _make_response(500)

        rest._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "500", request=err_resp.request, response=err_resp
            )
        )
        with self.assertRaises(httpx.HTTPStatusError):
            await rest.resolve_room("general")

    # ── Private group ─────────────────────────────────────────────────────────

    async def test_resolve_private_group_success(self):
        rest = _make_rest()
        err_resp = _make_response(404)

        def _side_effect(method, endpoint, **kwargs):
            if endpoint == "channels.info":
                raise httpx.HTTPStatusError(
                    "404", request=err_resp.request, response=err_resp
                )
            return {"success": True, "group": {"_id": "GRP99", "name": "private-stuff"}}

        rest._request = AsyncMock(side_effect=_side_effect)
        result = await rest.resolve_room("private-stuff")
        self.assertEqual(result["_id"], "GRP99")
        self.assertEqual(result["type"], "group")
        self.assertEqual(result["name"], "private-stuff")

    async def test_resolve_group_500_re_raises(self):
        rest = _make_rest()
        ch_err = _make_response(404)
        grp_err = _make_response(500)

        def _side_effect(method, endpoint, **kwargs):
            if endpoint == "channels.info":
                raise httpx.HTTPStatusError(
                    "404", request=ch_err.request, response=ch_err
                )
            raise httpx.HTTPStatusError(
                "500", request=grp_err.request, response=grp_err
            )

        rest._request = AsyncMock(side_effect=_side_effect)
        with self.assertRaises(httpx.HTTPStatusError):
            await rest.resolve_room("some-room")

    async def test_resolve_not_found_raises_room_not_found(self):
        rest = _make_rest()
        err_resp = _make_response(404)

        # Both channels.info and groups.info return 404
        rest._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "404", request=err_resp.request, response=err_resp
            )
        )
        with self.assertRaises(RoomNotFoundError):
            await rest.resolve_room("nowhere")

    async def test_resolve_channel_success_field_falls_through(self):
        """success=True but no 'channel' key → falls through to groups lookup."""
        rest = _make_rest()
        err_resp = _make_response(404)

        def _side_effect(method, endpoint, **kwargs):
            if endpoint == "channels.info":
                # RC returns 200 success=True but no channel key (edge case)
                return {"success": True}
            raise httpx.HTTPStatusError(
                "404", request=err_resp.request, response=err_resp
            )

        rest._request = AsyncMock(side_effect=_side_effect)
        with self.assertRaises(RoomNotFoundError):
            await rest.resolve_room("weird-room")

    async def test_resolve_group_success_field_falls_through_to_not_found(self):
        """groups.info returns success=True but no 'group' key → RoomNotFoundError."""
        rest = _make_rest()
        err_resp = _make_response(404)

        def _side_effect(method, endpoint, **kwargs):
            if endpoint == "channels.info":
                raise httpx.HTTPStatusError(
                    "404", request=err_resp.request, response=err_resp
                )
            return {"success": True}  # no 'group' key

        rest._request = AsyncMock(side_effect=_side_effect)
        with self.assertRaises(RoomNotFoundError):
            await rest.resolve_room("weird-group")


if __name__ == "__main__":
    unittest.main()


# ── Appended from test_round7_fixes.py ────────────────────────────────────────


class TestReloginLock(unittest.IsolatedAsyncioTestCase):
    """Concurrent 401 responses must only trigger one login() call."""

    def _make_rest(self):
        r = RocketChatREST("http://example.com")
        r._username = "bot"
        r._password = "secret"
        r.auth_token = "old_tok"
        r.user_id = "uid"
        r._client = MagicMock()
        return r

    async def test_relogin_lock_exists(self):
        """RocketChatREST must initialize _relogin_lock as asyncio.Lock."""
        rest = RocketChatREST("http://x")
        await rest.close()
        self.assertIsInstance(rest._relogin_lock, asyncio.Lock)

    async def test_concurrent_401_calls_login_once(self):
        """Two simultaneous 401 responses must only invoke login() once."""
        rest = self._make_rest()
        login_count = 0

        async def fake_login(username, password):
            nonlocal login_count
            login_count += 1
            rest.auth_token = "new_tok"
            rest.user_id = "uid"
            rest.bot_username = username
            rest._username = username
            rest._password = password

        def _make_401():
            r = MagicMock()
            r.status_code = 401
            r.is_success = False
            r.raise_for_status.side_effect = Exception("401")
            return r

        def _make_200():
            r = MagicMock()
            r.status_code = 200
            r.is_success = True
            r.raise_for_status = MagicMock()
            r.json.return_value = {"success": True}
            return r

        call_count = 0

        async def fake_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return _make_401()
            return _make_200()

        rest._client.request = fake_request
        rest.login = fake_login

        await asyncio.gather(
            rest._request("GET", "endpoint"),
            rest._request("GET", "endpoint"),
            return_exceptions=True,
        )

        self.assertEqual(login_count, 1, f"login() should be called once, got {login_count}")

    async def test_skip_relogin_when_token_already_refreshed(self):
        """If token already refreshed while waiting, skip re-login."""
        rest = self._make_rest()
        rest.auth_token = "old_tok"
        login_count = 0

        async def fake_login(username, password):
            nonlocal login_count
            login_count += 1
            rest.auth_token = "refreshed"

        r_401 = MagicMock()
        r_401.status_code = 401
        r_401.is_success = False
        r_401.raise_for_status.side_effect = Exception("401")

        r_200 = MagicMock()
        r_200.status_code = 200
        r_200.is_success = True
        r_200.raise_for_status = MagicMock()
        r_200.json.return_value = {"ok": True}

        class TokenChangingLock:
            async def __aenter__(self_inner):
                rest.auth_token = "refreshed"
                return self_inner
            async def __aexit__(self_inner, *_):
                pass

        rest._relogin_lock = TokenChangingLock()
        rest._client.request = AsyncMock(side_effect=[r_401, r_200])
        rest.login = fake_login

        await rest._request("GET", "ep")
        self.assertEqual(login_count, 0, "login() must not be called when token already refreshed")


# ── Appended from test_round8_fixes.py ────────────────────────────────────────


class TestDownloadFileReauthNotNested(unittest.IsolatedAsyncioTestCase):
    """Second streaming request must be opened AFTER the first context manager exits."""

    async def test_reauth_request_opened_after_first_context_closed(self):
        """Verify that on 401 the code exits the first context before retrying."""
        rest = RocketChatREST("http://example.com")
        rest._username = "bot"
        rest._password = "pass"
        rest.auth_token = "tok"
        rest.user_id = "uid"

        open_order: list[str] = []
        close_order: list[str] = []

        class FakeStream:
            def __init__(self, name: str, status: int, body: bytes = b""):
                self._name = name
                self._status = status
                self._body = body

            async def __aenter__(self):
                open_order.append(self._name)
                self.status_code = self._status
                return self

            async def __aexit__(self, *_):
                close_order.append(self._name)

            def raise_for_status(self):
                if self._status >= 400:
                    raise Exception(f"HTTP {self._status}")

            async def aiter_bytes(self):
                yield self._body

        call_count = 0

        def fake_stream(method, url, headers=None, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FakeStream("first", 401)
            return FakeStream("second", 200, b"data")

        rest._download_client = MagicMock()
        rest._download_client.stream = fake_stream

        async def fake_login(u, p):
            rest.auth_token = "new_tok"
            rest.user_id = "uid"
            rest.bot_username = u
            rest._username = u
            rest._password = p

        rest.login = fake_login

        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, "out.bin")
            await rest.download_file("/file/path", dest)

        first_close_idx = close_order.index("first")
        second_open_idx = open_order.index("second")
        self.assertLess(
            first_close_idx,
            second_open_idx,
            f"First context must close before second opens. "
            f"open_order={open_order}, close_order={close_order}",
        )

    async def test_successful_download_no_reauth(self):
        """A 200 response must not trigger re-auth."""
        rest = RocketChatREST("http://example.com")
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._username = "bot"
        rest._password = "pass"

        login_called = []

        async def fake_login(u, p):
            login_called.append(True)

        rest.login = fake_login

        class FakeStream:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield b"hello"

        rest._download_client = MagicMock()
        rest._download_client.stream = lambda method, url, **kw: FakeStream()

        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, "out.bin")
            await rest.download_file("/file/path", dest)

        self.assertEqual(login_called, [], "login() must not be called on 200")


class TestUploadFileNonBlocking(unittest.IsolatedAsyncioTestCase):
    """upload_file must use asyncio.to_thread for file reading."""

    async def test_upload_uses_to_thread_for_file_read(self):
        """path.read_bytes must be called via asyncio.to_thread, not directly."""
        rest = RocketChatREST("http://example.com")
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._username = "bot"
        rest._password = "pass"
        rest._server_major_version = 6

        to_thread_fns: list = []
        original_to_thread = asyncio.to_thread

        async def spy_to_thread(fn, *args, **kwargs):
            to_thread_fns.append(fn)
            return await original_to_thread(fn, *args, **kwargs)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"success": True}
        rest._download_client = MagicMock()
        rest._download_client.post = AsyncMock(return_value=mock_response)

        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "test.txt")
            Path(fpath).write_bytes(b"hello world")

            with patch("gateway.connectors.rocketchat.rest.asyncio.to_thread", side_effect=spy_to_thread):
                await rest.upload_file("room1", fpath)

        read_bytes_calls = [fn for fn in to_thread_fns if getattr(fn, "__name__", "") == "read_bytes"]
        self.assertGreaterEqual(
            len(read_bytes_calls),
            1,
            "path.read_bytes must be called via asyncio.to_thread",
        )


# ── Appended from test_round9_fixes.py ────────────────────────────────────────


class TestDownloadFileUniqueTmpPath(unittest.IsolatedAsyncioTestCase):
    """Concurrent downloads for the same destination must use distinct tmp paths."""

    async def test_concurrent_downloads_have_distinct_tmp_paths(self):
        """Each download call must generate a unique tmp_path suffix."""
        rest = RocketChatREST("http://example.com")
        rest.auth_token = "tok"
        rest.user_id = "uid"
        rest._username = "bot"
        rest._password = "pass"

        import secrets as secrets_mod

        generated_tokens: list[str] = []
        original_token_hex = secrets_mod.token_hex

        def capture_token_hex(n):
            token = original_token_hex(n)
            generated_tokens.append(token)
            return token

        class FakeStream:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield b"data"

        rest._download_client = MagicMock()
        rest._download_client.stream = lambda method, url, **kw: FakeStream()

        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, "file.bin")

            with patch("gateway.connectors.rocketchat.rest.secrets.token_hex", side_effect=capture_token_hex):
                await asyncio.gather(
                    rest.download_file("/file/path", dest),
                    rest.download_file("/file/path", dest),
                )

        self.assertGreaterEqual(len(generated_tokens), 2, "Expected at least 2 token_hex calls")
        self.assertEqual(
            len(set(generated_tokens)),
            len(generated_tokens),
            f"tmp_path tokens must be unique across concurrent downloads; got {generated_tokens}",
        )


# ---------------------------------------------------------------------------
# get_room_history
# ---------------------------------------------------------------------------


class TestGetRoomHistory(unittest.IsolatedAsyncioTestCase):
    """Tests for RocketChatREST.get_room_history()."""

    def _make_rc_msg(self, username: str, text: str, ts_date: int, msg_type: str = "") -> dict:
        """Build a minimal RC REST message dict."""
        m: dict = {
            "_id": f"msg-{ts_date}",
            "msg": text,
            "ts": {"$date": ts_date},
            "u": {"_id": "uid", "username": username},
        }
        if msg_type:
            m["t"] = msg_type
        return m

    async def test_channel_uses_channels_history_endpoint(self):
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"messages": [], "success": True})
        await rest.get_room_history("ROOM_ID", "channel", count=10)
        rest._request.assert_called_once_with(
            "GET", "channels.history",
            params={"roomId": "ROOM_ID", "count": 10, "unreads": "false"},
        )

    async def test_group_uses_groups_history_endpoint(self):
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"messages": [], "success": True})
        await rest.get_room_history("ROOM_ID", "group", count=5)
        rest._request.assert_called_once_with(
            "GET", "groups.history",
            params={"roomId": "ROOM_ID", "count": 5, "unreads": "false"},
        )

    async def test_dm_uses_im_history_endpoint(self):
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"messages": [], "success": True})
        await rest.get_room_history("ROOM_ID", "dm", count=20)
        rest._request.assert_called_once_with(
            "GET", "im.history",
            params={"roomId": "ROOM_ID", "count": 20, "unreads": "false"},
        )

    async def test_group_dm_uses_im_history_endpoint(self):
        """One direct endpoint serves both DM kinds — the group/1:1 distinction
        is AgentCoop's, not the server's (§6.4).

        Reached for real: the creation path types a room from its *classified*
        kind, so `"group_dm"` arrives here. It used to fall through to the
        unknown-type default and ask a channel endpoint about a direct room, so
        history handoff and outage replay failed for every group DM, forever.
        """
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"messages": [], "success": True})
        await rest.get_room_history("ROOM_ID", "group_dm", count=20)
        rest._request.assert_called_once_with(
            "GET", "im.history",
            params={"roomId": "ROOM_ID", "count": 20, "unreads": "false"},
        )

    async def test_every_room_type_the_gateway_produces_has_an_endpoint(self):
        """Derived from `RoomKind`, not a hand-written list.

        The group-DM miss was invisible because the map was written when three
        room types existed and the fourth arrived somewhere else entirely. This
        walks the enum that decides a room's type, so the next kind fails here
        rather than in production — and the unknown-type default stays a
        default, not the answer for a kind the gateway itself creates.
        """
        from gateway.core.watcher_rule import RoomKind

        for kind in RoomKind:
            with self.subTest(kind=kind.value):
                rest = _make_rest()
                rest._request = AsyncMock(
                    return_value={"messages": [], "success": True})
                await rest.get_room_history("ROOM_ID", kind.value, count=1)
                endpoint = rest._request.call_args[0][1]
                expected = "im.history" if kind.is_direct else (
                    "groups.history" if kind is RoomKind.GROUP else "channels.history")
                self.assertEqual(
                    endpoint, expected,
                    f"{kind.value} must not fall through to the unknown-type default",
                )

    async def test_unknown_room_type_defaults_to_channels_history(self):
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"messages": [], "success": True})
        await rest.get_room_history("ROOM_ID", "unknown_type", count=10)
        call_args = rest._request.call_args
        self.assertEqual(call_args[0][1], "channels.history")

    async def test_messages_returned_chronological_oldest_first(self):
        """API returns newest-first; method must reverse to oldest-first."""
        rest = _make_rest()
        rest._request = AsyncMock(return_value={
            "messages": [
                self._make_rc_msg("alice", "third", 1000003),
                self._make_rc_msg("alice", "second", 1000002),
                self._make_rc_msg("alice", "first", 1000001),
            ],
            "success": True,
        })
        msgs = await rest.get_room_history("ROOM_ID", "channel", count=3)
        self.assertEqual([m["msg"] for m in msgs], ["first", "second", "third"])

    async def test_system_messages_excluded(self):
        """Messages with 't' field (system events) must be filtered out."""
        rest = _make_rest()
        rest._request = AsyncMock(return_value={
            "messages": [
                self._make_rc_msg("alice", "normal", 1000001),
                self._make_rc_msg("system", "", 1000002, msg_type="uj"),  # user joined
                self._make_rc_msg("bob", "another", 1000003),
            ],
            "success": True,
        })
        msgs = await rest.get_room_history("ROOM_ID", "channel", count=10)
        texts = [m["msg"] for m in msgs]
        self.assertIn("normal", texts)
        self.assertIn("another", texts)
        self.assertEqual(len(msgs), 2)

    async def test_empty_body_messages_excluded(self):
        """Messages with empty 'msg' field must be filtered out."""
        rest = _make_rest()
        rest._request = AsyncMock(return_value={
            "messages": [
                self._make_rc_msg("alice", "has text", 1000001),
                self._make_rc_msg("alice", "", 1000002),  # empty body (file upload)
            ],
            "success": True,
        })
        msgs = await rest.get_room_history("ROOM_ID", "channel", count=10)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["msg"], "has text")

    async def test_empty_channel_returns_empty_list(self):
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"messages": [], "success": True})
        msgs = await rest.get_room_history("ROOM_ID", "channel", count=50)
        self.assertEqual(msgs, [])

    async def test_api_error_response_raises_runtime_error(self):
        """success=False in the JSON body must raise RuntimeError, not silently return []."""
        rest = _make_rest()
        rest._request = AsyncMock(return_value={
            "success": False,
            "error": "not_in_room",
        })
        with self.assertRaises(RuntimeError) as ctx:
            await rest.get_room_history("ROOM_ID", "channel", count=10)
        self.assertIn("not_in_room", str(ctx.exception))

    async def test_before_ts_maps_to_latest_param(self):
        """before_ts must be sent as the RC 'latest' query parameter."""
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"messages": [], "success": True})
        ts = "2026-05-10T10:00:00+08:00"
        await rest.get_room_history("ROOM_ID", "channel", count=10, before_ts=ts)
        _, kwargs = rest._request.call_args
        self.assertEqual(kwargs["params"].get("latest"), ts)
        self.assertNotIn("inclusive", kwargs["params"])

    async def test_after_ts_maps_to_oldest_with_inclusive(self):
        """after_ts must be sent as the RC 'oldest' parameter with inclusive=true.

        RC treats 'oldest' as exclusive by default (ts > oldest). The inclusive=true
        flag makes it inclusive (ts >= oldest) — matching the --after contract.
        """
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"messages": [], "success": True})
        ts = "2026-05-10T19:25:00+08:00"
        await rest.get_room_history("ROOM_ID", "channel", count=10, after_ts=ts)
        _, kwargs = rest._request.call_args
        self.assertEqual(kwargs["params"].get("oldest"), ts)
        self.assertEqual(kwargs["params"].get("inclusive"), "true",
                         "inclusive must be 'true' (string) so RC returns ts >= after_ts")

    async def test_combined_before_and_after_ts(self):
        """Both before_ts and after_ts can be set simultaneously for a time window."""
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"messages": [], "success": True})
        after = "2026-05-10T08:00:00+08:00"
        before = "2026-05-10T20:00:00+08:00"
        await rest.get_room_history("ROOM_ID", "channel", count=50,
                                    before_ts=before, after_ts=after)
        _, kwargs = rest._request.call_args
        self.assertEqual(kwargs["params"].get("latest"), before)
        self.assertEqual(kwargs["params"].get("oldest"), after)
        self.assertEqual(kwargs["params"].get("inclusive"), "true")


class TestIsRoomMember(unittest.IsolatedAsyncioTestCase):
    """Three answers, and the third is the one worth having.

    A lookup that fails has not said the account is a member, and has not said it was
    removed. The caller — outage replay — can afford to do neither and simply ask again.
    """

    async def test_a_subscription_record_is_membership(self):
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"success": True, "subscription": {"rid": "r1"}})
        self.assertIs(await rest.is_room_member("r1"), True)

    async def test_a_hidden_room_is_still_membership(self):
        """`open: false` is a display choice — the record is what membership is read from,
        and leaving a room is what removes it."""
        rest = _make_rest()
        rest._request = AsyncMock(
            return_value={"success": True, "subscription": {"rid": "r1", "open": False}}
        )
        self.assertIs(await rest.is_room_member("r1"), True)

    async def test_no_subscription_record_is_removal(self):
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"success": True, "subscription": None})
        self.assertIs(await rest.is_room_member("r1"), False)

    async def test_an_http_error_is_unknown_rather_than_either_answer(self):
        """Some versions answer 400/403 where others answer 200 with a null body, and
        neither is distinguishable from a server that is merely unwell."""
        rest = _make_rest()
        rest._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "403", request=httpx.Request("GET", "http://x"),
                response=httpx.Response(403, request=httpx.Request("GET", "http://x")),
            )
        )
        with self.assertLogs("coop.connectors.rocketchat", "WARNING"):
            self.assertIsNone(await rest.is_room_member("r1"))

    async def test_any_other_failure_is_unknown_too(self):
        rest = _make_rest()
        rest._request = AsyncMock(side_effect=RuntimeError("connection reset"))
        with self.assertLogs("coop.connectors.rocketchat", "WARNING"):
            self.assertIsNone(await rest.is_room_member("r1"))

    async def test_it_asks_about_the_room_it_was_given(self):
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"success": True, "subscription": {}})
        await rest.is_room_member("room-xyz")
        args, kwargs = rest._request.call_args
        self.assertEqual(args[1], "subscriptions.getOne")
        self.assertEqual(kwargs["params"], {"roomId": "room-xyz"})


class TestTheVerifiedSubscriptionContract(unittest.IsolatedAsyncioTestCase):
    """Pinned to Rocket.Chat's actual handler, not to a guess about it.

    `subscriptions.getOne` is `success({subscription: findOneByRoomIdAndUserId(...)})`, so a
    room the caller is not in is a 200 with a null subscription. Its declared failures are
    400 for a malformed request and 401 for authentication; neither is a membership answer,
    which is why every HTTP error here stays *unknown*.
    """

    async def test_a_null_subscription_on_200_is_the_negative_answer(self):
        rest = _make_rest()
        rest._request = AsyncMock(return_value={"success": True, "subscription": None})
        self.assertIs(await rest.is_room_member("r1"), False)

    async def test_a_malformed_request_is_not_a_membership_answer(self):
        """400 means the request lacked `roomId`, which says nothing about the account.

        Reading it as "not a member" would let a caller bug close the replay window and
        drop the watermark — silent message loss from an unrelated defect.
        """
        rest = _make_rest()
        req = httpx.Request("GET", "http://x")
        rest._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "400", request=req,
                response=httpx.Response(
                    400, request=req,
                    json={"success": False,
                          "error": "must have required property 'roomId'"},
                ),
            )
        )
        with self.assertLogs("coop.connectors.rocketchat", "WARNING"):
            self.assertIsNone(await rest.is_room_member("r1"))

    async def test_an_auth_failure_is_not_a_membership_answer(self):
        rest = _make_rest()
        req = httpx.Request("GET", "http://x")
        rest._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "401", request=req,
                response=httpx.Response(401, request=req,
                                        json={"status": "error", "message": "unauthorized"}),
            )
        )
        with self.assertLogs("coop.connectors.rocketchat", "WARNING"):
            self.assertIsNone(await rest.is_room_member("r1"))


class TestHistoryPageReportsHowFullItWas(unittest.IsolatedAsyncioTestCase):
    """`count` is applied by the server; filtering happens here, after it."""

    async def test_a_page_of_system_events_is_full_but_empty(self):
        from gateway.connectors.rocketchat.rest import HistoryPage  # noqa: F401

        rest = _make_rest()
        rest._request = AsyncMock(return_value={
            "success": True,
            "messages": [{"_id": f"s{i}", "t": "uj", "msg": "someone"} for i in range(5)],
        })

        page = await rest.get_room_history_page("r1", "channel", count=5)

        self.assertEqual(page.messages, [])
        self.assertEqual(page.raw_count, 5)
        self.assertTrue(
            page.was_full,
            "the caller cannot tell this from an empty window without it",
        )

    async def test_a_short_page_of_real_messages_is_not_full(self):
        rest = _make_rest()
        rest._request = AsyncMock(return_value={
            "success": True,
            "messages": [{"_id": "m1", "msg": "hi"}, {"_id": "m2", "msg": "there"}],
        })

        page = await rest.get_room_history_page("r1", "channel", count=200)

        self.assertEqual([m["_id"] for m in page.messages], ["m2", "m1"])
        self.assertFalse(page.was_full)

    async def test_the_plain_list_call_still_filters_and_reverses(self):
        """`get_room_history` keeps its contract — other callers are unchanged."""
        rest = _make_rest()
        rest._request = AsyncMock(return_value={
            "success": True,
            "messages": [
                {"_id": "m2", "msg": "second"},
                {"_id": "sys", "t": "uj", "msg": "joined"},
                {"_id": "m1", "msg": "first"},
            ],
        })

        msgs = await rest.get_room_history("r1", "channel", count=200)

        self.assertEqual([m["_id"] for m in msgs], ["m1", "m2"])


class TestDmMembersExcludesThisAccountById(unittest.IsolatedAsyncioTestCase):
    """Identity is the user id, not the spelling of a configured username.

    A login whose canonical username differs in casing, or which uses an alias, left the
    account in its own participant list — a 1:1 room described by its own bot, and, if the
    API lists it first, every such room deriving the same `dm-<bot>` label instead of
    distinct counterparts.
    """

    def _rest(self, members):
        rest = _make_rest()
        rest.user_id = "BOT_ID"
        rest._request = AsyncMock(return_value={"success": True, "members": members})
        return rest

    async def test_the_account_is_dropped_however_its_name_is_spelled(self):
        rest = self._rest([
            {"_id": "BOT_ID", "username": "Bot"},       # canonical casing differs
            {"_id": "U1", "username": "alice"},
        ])

        self.assertEqual(await rest.dm_members("r1"), ["alice"])

    async def test_a_namesake_that_is_not_this_account_is_kept(self):
        """The near miss: excluding by name would drop a different user who happens to
        share the spelling."""
        rest = self._rest([
            {"_id": "BOT_ID", "username": "bot"},
            {"_id": "U2", "username": "bot"},           # someone else, same name
        ])

        self.assertEqual(await rest.dm_members("r1"), ["bot"])

    async def test_a_group_direct_room_returns_every_counterpart(self):
        rest = self._rest([
            {"_id": "BOT_ID", "username": "bot"},
            {"_id": "U1", "username": "alice"},
            {"_id": "U2", "username": "carol"},
        ])

        self.assertEqual(await rest.dm_members("r1"), ["alice", "carol"])

    async def test_a_failed_request_raises_instead_of_answering_empty(self):
        """A network failure and a memberless answer used to be one return
        value, which made the routing transaction's abort outcome (§2.2)
        unreachable: retryable and final must arrive as different shapes."""
        rest = _make_rest()
        rest.user_id = "BOT_ID"
        rest._request = AsyncMock(side_effect=RuntimeError("api down"))

        with self.assertRaises(RuntimeError):
            await rest.dm_members("r1")

    async def test_a_malformed_members_payload_is_still_an_empty_final_answer(self):
        rest = _make_rest()
        rest.user_id = "BOT_ID"
        rest._request = AsyncMock(return_value={"success": True, "members": "what"})

        self.assertEqual(await rest.dm_members("r1"), [])


class TestHistoryBoundsAreSentInTheFormatTheServerParses(unittest.IsolatedAsyncioTestCase):
    """`oldest`/`latest` reach `new Date(...)` on the server, which rejects epoch digits.

    Verified against Rocket.Chat 6.12 rather than reasoned about: a room with five
    messages, asked for everything at or after the third.

        oldest="1786816166131"            -> HTTP 200 success=True, 5 messages back
        oldest="2026-08-15T17:49:26.131Z" -> HTTP 200 success=True, 3 messages back

    The request *succeeds* either way — the bound is dropped, not refused — so nothing
    upstream can notice. That is why this is pinned here at the wire, where the format is
    decided, and not at a caller.
    """

    def _rest(self):
        rest = RocketChatREST("http://rc.example.com")
        rest._request = AsyncMock(return_value={"success": True, "messages": []})
        return rest

    def _params(self, rest):
        return rest._request.await_args.kwargs["params"]

    async def test_an_epoch_millisecond_watermark_is_converted(self):
        rest = self._rest()
        await rest.get_room_history_page("r1", "channel", count=10, after_ts="1786816166131")

        self.assertEqual(self._params(rest)["oldest"], "2026-08-15T17:49:26.131000Z")

    async def test_an_iso_bound_is_passed_through_untouched(self):
        """The history-handoff caller documents ISO and must not be double-converted."""
        rest = self._rest()
        await rest.get_room_history(
            "r1", "channel", count=10,
            before_ts="2026-08-15T18:00:00Z", after_ts="2026-08-15T17:00:00Z",
        )

        params = self._params(rest)
        self.assertEqual(params["oldest"], "2026-08-15T17:00:00Z")
        self.assertEqual(params["latest"], "2026-08-15T18:00:00Z")

    async def test_the_upper_bound_is_converted_too(self):
        """Same parameter family, same server-side `new Date` — one rule, both bounds."""
        rest = self._rest()
        await rest.get_room_history("r1", "channel", count=10, before_ts="1786816166131")

        self.assertEqual(self._params(rest)["latest"], "2026-08-15T17:49:26.131000Z")

    async def test_no_bound_stays_absent(self):
        rest = self._rest()
        await rest.get_room_history_page("r1", "channel", count=10)

        params = self._params(rest)
        self.assertNotIn("oldest", params)
        self.assertNotIn("latest", params)

    async def test_a_float_watermark_is_converted_too(self):
        """`str()` of whatever JSON put in `$date`. A digit-string test misses this one,
        and missing it fails the same silent way the conversion exists to prevent."""
        rest = self._rest()
        await rest.get_room_history_page("r1", "channel", count=10, after_ts="1786816166131.0")

        self.assertEqual(self._params(rest)["oldest"], "2026-08-15T17:49:26.131000Z")

    async def test_an_unparseable_bound_is_left_alone(self):
        """Better an unusable bound reaching the server than a fabricated one."""
        rest = self._rest()
        await rest.get_room_history_page("r1", "channel", count=10, after_ts="nan")

        self.assertEqual(self._params(rest)["oldest"], "nan")
