"""Unit tests for gateway.admin.base: exception payloads and the
PlatformAdmin async context manager contract."""

from __future__ import annotations

import asyncio
import unittest

from gateway.admin.base import (
    AdminChannel,
    AdminError,
    AdminUser,
    ChannelAlreadyExistsError,
    ChannelArchivedError,
    PlatformAdmin,
    UserAlreadyExistsError,
    emails_match,
)


class TestEmailsMatch(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(emails_match("a@x.com", "a@x.com"))

    def test_case_insensitive_match(self):
        self.assertTrue(emails_match("Alice@X.COM", "alice@x.com"))

    def test_whitespace_insensitive_match(self):
        self.assertTrue(emails_match("  a@x.com  ", "a@x.com"))

    def test_genuine_mismatch(self):
        self.assertFalse(emails_match("a@x.com", "b@x.com"))

    def test_empty_existing_email_fails_open_to_match(self):
        # Deliberate, documented trade-off (see emails_match's docstring),
        # not a claim that it's provably the same identity.
        self.assertTrue(emails_match("", "anything@x.com"))

    def test_empty_requested_email_against_real_existing_is_a_mismatch(self):
        # Only an EMPTY existing_email fails open — an empty requested
        # email against a real existing one is a genuine mismatch.
        self.assertFalse(emails_match("a@x.com", ""))


class TestAdminUserMatchesEmail(unittest.TestCase):
    """Owner rule: when pulling one value out of an array, traverse it —
    don't index [0]. RC's `emails` really is an array."""

    def test_matches_an_address_that_is_not_first(self):
        u = AdminUser(
            id="u1", username="alice", email="primary@x.com",
            emails=("primary@x.com", "secondary@x.com", "third@x.com"),
        )
        self.assertTrue(u.matches_email("secondary@x.com"))
        self.assertTrue(u.matches_email("third@x.com"))

    def test_match_is_position_invariant(self):
        a = AdminUser(id="u1", username="alice", email="a@x.com", emails=("a@x.com", "b@x.com"))
        b = AdminUser(id="u1", username="alice", email="b@x.com", emails=("b@x.com", "a@x.com"))
        for target in ("a@x.com", "b@x.com"):
            with self.subTest(target=target):
                self.assertEqual(a.matches_email(target), b.matches_email(target))
                self.assertTrue(a.matches_email(target))

    def test_genuine_mismatch_against_all_addresses(self):
        u = AdminUser(
            id="u1", username="alice", email="a@x.com", emails=("a@x.com", "b@x.com")
        )
        self.assertFalse(u.matches_email("someone-else@x.com"))

    def test_case_and_whitespace_insensitive_on_a_later_address(self):
        u = AdminUser(
            id="u1", username="alice", email="a@x.com", emails=("a@x.com", " Second@X.COM ")
        )
        self.assertTrue(u.matches_email("second@x.com"))

    def test_primary_email_is_always_included_even_if_emails_not_passed(self):
        # Callers that only set `email` must still match — the collection is
        # backfilled in __post_init__.
        u = AdminUser(id="u1", username="alice", email="a@x.com")
        self.assertEqual(u.emails, ("a@x.com",))
        self.assertTrue(u.matches_email("a@x.com"))
        self.assertFalse(u.matches_email("b@x.com"))

    def test_no_addresses_at_all_fails_open(self):
        u = AdminUser(id="u1", username="alice", email="")
        self.assertEqual(u.emails, ())
        self.assertTrue(u.matches_email("anything@x.com"))


class TestExceptionPayloads(unittest.TestCase):
    def test_user_already_exists_carries_existing_user(self):
        existing = AdminUser(id="u1", username="alice", email="a@x.com")
        err = UserAlreadyExistsError("alice", existing=existing)
        self.assertEqual(err.username, "alice")
        self.assertIs(err.existing, existing)
        self.assertIn("alice", str(err))

    def test_user_already_exists_identity_matches_defaults_to_true(self):
        existing = AdminUser(id="u1", username="alice", email="a@x.com")
        err = UserAlreadyExistsError("alice", existing=existing)
        self.assertTrue(err.identity_matches)

    def test_user_already_exists_identity_matches_explicit_false(self):
        existing = AdminUser(id="u1", username="alice", email="someone-else@x.com")
        err = UserAlreadyExistsError("alice", existing=existing, identity_matches=False)
        self.assertFalse(err.identity_matches)

    def test_channel_already_exists_carries_existing_channel(self):
        existing = AdminChannel(id="c1", name="general", is_private=False)
        err = ChannelAlreadyExistsError("general", existing=existing)
        self.assertEqual(err.name, "general")
        self.assertIs(err.existing, existing)
        self.assertIn("general", str(err))


class TestArchivedChannelError(unittest.TestCase):
    def test_is_not_a_channel_already_exists_subclass(self):
        # Load-bearing, same as UserDeactivatedError: cli.py catches
        # ChannelAlreadyExistsError and prints an idempotent "skipping" with
        # exit 0. Subclassing would silently reinstate the bug.
        self.assertFalse(issubclass(ChannelArchivedError, ChannelAlreadyExistsError))
        self.assertTrue(issubclass(ChannelArchivedError, AdminError))

    def test_carries_the_existing_channel_and_says_archived(self):
        existing = AdminChannel(id="c1", name="eng", is_private=False, archived=True)
        err = ChannelArchivedError("eng", existing=existing)
        self.assertEqual(err.name, "eng")
        self.assertIs(err.existing, existing)
        self.assertIn("archived", str(err))

    def test_archived_defaults_false_and_is_keyword_safe(self):
        self.assertFalse(AdminChannel(id="c1", name="eng", is_private=False).archived)


class _DummyAdmin(PlatformAdmin):
    """Minimal concrete PlatformAdmin to exercise __aenter__/__aexit__."""

    def __init__(
        self,
        fail_connect: bool = False,
        connect_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ):
        self.connected = False
        self.closed = False
        self._fail_connect = fail_connect
        self._connect_error = connect_error
        self._close_error = close_error

    async def connect(self):
        if self._connect_error is not None:
            raise self._connect_error
        if self._fail_connect:
            raise RuntimeError("bad credentials")
        self.connected = True

    async def close(self):
        self.closed = True
        if self._close_error is not None:
            raise self._close_error

    async def create_user(self, username, email, password, *, full_name=None):
        raise NotImplementedError

    async def create_channel(self, name, *, is_private=False):
        raise NotImplementedError

    async def add_user_to_channel(self, username, channel_name):
        raise NotImplementedError

    async def delete_user(self, username):
        raise NotImplementedError

    async def delete_channel(self, channel_name):
        raise NotImplementedError

    async def reactivate_user(self, username, password):
        raise NotImplementedError


class TestPlatformAdminContextManager(unittest.IsolatedAsyncioTestCase):
    async def test_context_manager_connects_and_closes(self):
        admin = _DummyAdmin()
        async with admin as ctx:
            self.assertIs(ctx, admin)
            self.assertTrue(admin.connected)
            self.assertFalse(admin.closed)
        self.assertTrue(admin.closed)

    async def test_closes_and_reraises_when_connect_is_cancelled(self):
        # CancelledError/KeyboardInterrupt are BaseException, so an
        # `except Exception` here would skip cleanup on exactly the paths
        # where __aexit__ also never runs.
        admin = _DummyAdmin(connect_error=asyncio.CancelledError())

        with self.assertRaises(asyncio.CancelledError):
            async with admin:
                pass

        self.assertTrue(admin.closed)

    async def test_cancellation_is_not_replaced_by_a_failing_close(self):
        # Cleanup must never outrank the signal that triggered it: if close()
        # raises while a CancelledError propagates, the CancelledError must
        # still be what the caller sees, or asyncio.timeout()/task.cancelled()
        # misreport.
        admin = _DummyAdmin(connect_error=asyncio.CancelledError(), close_error=RuntimeError("boom"))

        with self.assertRaises(asyncio.CancelledError):
            async with admin:
                pass

    async def test_closes_and_reraises_when_connect_fails(self):
        # Python's async-with protocol never calls __aexit__ if __aenter__
        # raises — __aenter__ itself must clean up, or the httpx.AsyncClient
        # instances allocated in the constructor leak.
        admin = _DummyAdmin(fail_connect=True)

        with self.assertRaises(RuntimeError):
            async with admin:
                pass

        self.assertTrue(admin.closed)


if __name__ == "__main__":
    unittest.main()
