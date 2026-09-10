"""Unit tests for gateway.admin._errors: friendly_error_message and
log_error_response."""

from __future__ import annotations

import json
import logging
import unittest
from unittest.mock import MagicMock

import httpx

from gateway.admin._errors import (
    friendly_error_message,
    log_error_response,
    readback_after_write,
)
from gateway.admin.base import VerificationError


def _http_status_error(status_code: int, json_body=None, text: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://mm.example/api/v4/users")
    if json_body is not None:
        response = httpx.Response(status_code, request=request, json=json_body)
    else:
        response = httpx.Response(status_code, request=request, text=text or "")
    return httpx.HTTPStatusError("error", request=request, response=response)


class TestFriendlyErrorMessage(unittest.TestCase):
    def test_mattermost_message_field_is_used(self):
        exc = _http_status_error(
            400,
            json_body={
                "id": "app.user.save.email_exists.app_error",
                "message": "An account with that email already exists.",
                "detailed_error": "",
                "request_id": "yms88wqc8jr7bj4gdz6od3xf4w",
                "status_code": 400,
            },
        )

        self.assertEqual(
            friendly_error_message(exc), "An account with that email already exists."
        )

    def test_rocketchat_error_field_is_used(self):
        exc = _http_status_error(400, json_body={"success": False, "error": "User not found."})

        self.assertEqual(friendly_error_message(exc), "User not found.")

    def test_fallback_when_no_message_or_error_field(self):
        exc = _http_status_error(
            400,
            json_body={
                "id": "app.user.save.email_exists.app_error",
                "request_id": "yms88wqc8jr7bj4gdz6od3xf4w",
                "status_code": 400,
            },
        )

        result = friendly_error_message(exc)

        self.assertEqual(
            result,
            "Unknown error: request_id: yms88wqc8jr7bj4gdz6od3xf4w, "
            "error_id: app.user.save.email_exists.app_error, error code: 400",
        )

    def test_fallback_with_no_identifying_fields_at_all(self):
        exc = _http_status_error(500, json_body={})

        self.assertEqual(
            friendly_error_message(exc),
            "Unknown error: request_id: N/A, error_id: N/A, error code: 500",
        )

    def test_non_json_body_falls_back_to_status_code_only(self):
        exc = _http_status_error(502, text="<html>Bad Gateway</html>")

        self.assertEqual(friendly_error_message(exc), "Unknown error: error code: 502")

    def test_empty_message_field_falls_through_to_error_field(self):
        exc = _http_status_error(400, json_body={"message": "", "error": "actual error"})

        self.assertEqual(friendly_error_message(exc), "actual error")


class TestReadbackAfterWrite(unittest.TestCase):
    """The wrapper must preserve the full response body in the log file.

    Wrapping HTTPStatusError in VerificationError makes cli._run() take its
    generic `except Exception` arm, so it never calls log_error_response()
    itself — and read-backs routed through _get_user_or_none()/
    _get_channel_or_none() also have the REST client's own error line suppressed
    by quiet_expected_error(). Measured before this was fixed: the log file was
    left at 0 bytes, losing `detailed_error` and `request_id`, the two fields
    friendly_error_message() drops.
    """

    def _forbidden(self):
        return _http_status_error(
            403,
            {
                "id": "api.context.permissions.app_error",
                "message": "You do not have the appropriate permissions.",
                "detailed_error": "detail-marker",
                "request_id": "reqid-marker",
                "status_code": 403,
            },
        )

    def test_logs_the_full_body_before_wrapping(self):
        records = []
        logger = logging.getLogger("coop.admin.errors")

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        with self.assertRaises(VerificationError):
            with readback_after_write("X reported created"):
                raise self._forbidden()

        blob = "\n".join(records)
        # The fields friendly_error_message() drops must survive somewhere.
        self.assertIn("detail-marker", blob)
        self.assertIn("reqid-marker", blob)

    def test_message_carries_the_platform_text_and_admits_uncertainty(self):
        with self.assertRaises(VerificationError) as ctx:
            with readback_after_write("X reported created"):
                raise self._forbidden()

        msg = str(ctx.exception)
        self.assertIn("X reported created", msg)
        self.assertIn("You do not have the appropriate permissions.", msg)
        self.assertIn("UNKNOWN", msg)
        self.assertIn("--log-file", msg)
        # Must not claim the write definitely landed.
        self.assertNotIn("already been applied", msg)

    def test_non_http_errors_pass_through_untouched(self):
        # A VerificationError from the read-back's own logic is already
        # specific and must not be reworded or re-wrapped.
        original = VerificationError("delete_at is still unset")
        with self.assertRaises(VerificationError) as ctx:
            with readback_after_write("X reported deleted"):
                raise original
        self.assertIs(ctx.exception, original)

    def test_success_path_is_transparent(self):
        with readback_after_write("X reported created"):
            pass  # must not raise


class TestLogErrorResponse(unittest.TestCase):
    def test_logs_full_json_body(self):
        exc = _http_status_error(
            400, json_body={"id": "app.user.save.email_exists.app_error", "message": "boom"}
        )
        logger = MagicMock(spec=logging.Logger)

        log_error_response(logger, exc)

        logger.error.assert_called_once()
        args = logger.error.call_args.args
        self.assertEqual(args[0], "%s %s -> HTTP %s\n%s")
        self.assertEqual(args[1], "POST")
        self.assertIn("users", str(args[2]))
        self.assertEqual(args[3], 400)
        logged_body = json.loads(args[4])
        self.assertEqual(logged_body["message"], "boom")

    def test_logs_raw_text_when_body_is_not_json(self):
        exc = _http_status_error(502, text="<html>Bad Gateway</html>")
        logger = MagicMock(spec=logging.Logger)

        log_error_response(logger, exc)

        args = logger.error.call_args.args
        self.assertEqual(args[4], "<html>Bad Gateway</html>")


if __name__ == "__main__":
    unittest.main()
