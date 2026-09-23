"""Unit tests for RocketChatAdmin.

Same mocking approach as test_admin_mattermost.py: RocketChatREST is
replaced with a MagicMock exposing only the AsyncMock methods each test
needs. Focus is RocketChatAdmin's own logic — the public/private channel
(channels.* vs groups.*) dispatch, idempotency checks, and read-back
verification — not REST transport (covered by test_rest.py).
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx

from gateway.admin.base import (
    AdminError,
    ChannelAlreadyExistsError,
    ChannelArchivedError,
    ChannelNotFoundError,
    UserAlreadyExistsError,
    UserDeactivatedError,
    UserNotFoundError,
    VerificationError,
)
from gateway.admin.config import AdminConfigError, AdminProfile
from gateway.admin.rocketchat_admin import RocketChatAdmin


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://x")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


def _http_error_with_body(status_code: int, json_body: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://x")
    response = httpx.Response(status_code, request=request, json=json_body)
    return httpx.HTTPStatusError("error", request=request, response=response)


def _profile(**overrides) -> AdminProfile:
    defaults = dict(
        name="rc-lab", type="rocketchat", server_url="https://rc.example",
        username="admin", password="pw",
    )
    defaults.update(overrides)
    return AdminProfile(**defaults)


def _admin_with_mock_rest() -> RocketChatAdmin:
    admin = RocketChatAdmin(_profile())
    admin._rest = MagicMock()
    admin._rest.close = AsyncMock()
    return admin


class TestConstructorValidation(unittest.TestCase):
    def test_missing_username_password_raises(self):
        profile = AdminProfile(
            name="rc-lab", type="rocketchat", server_url="https://rc.example",
            username="admin", password="pw",
        )
        # Simulate a token-only RC profile by clearing username/password
        # after construction (AdminProfile itself requires *some*
        # credential, so this exercises RocketChatAdmin's own stricter check).
        profile.username = None
        profile.password = None
        with self.assertRaises(AdminConfigError):
            RocketChatAdmin(profile)


class TestConnect(unittest.IsolatedAsyncioTestCase):
    async def test_connect_logs_in_with_profile_credentials(self):
        admin = _admin_with_mock_rest()
        admin._rest.login = AsyncMock()

        await admin.connect()

        admin._rest.login.assert_awaited_once_with("admin", "pw")


class TestCreateUser(unittest.IsolatedAsyncioTestCase):
    async def test_creates_and_verifies_new_user(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                _http_error(400),  # pre-check: users.info not found
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},  # create
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": [{"address": "a@x.com"}]}},  # verify
            ]
        )

        user = await admin.create_user("alice", "a@x.com", "pw")

        self.assertEqual(user.id, "u1")
        self.assertEqual(user.email, "a@x.com")
        create_call = admin._rest._request.call_args_list[1]
        payload = create_call.kwargs["json_data"]
        # No `verified` field at all — RC defaults it to false, and this tool
        # deliberately offers no option to change it.
        self.assertNotIn("verified", payload)
        # requirePasswordChange is a separate concern and must stay: it
        # suppresses RC's forced password reset, which a service account
        # logging in unattended must not hit.
        self.assertEqual(payload["requirePasswordChange"], False)

    async def test_create_user_rejects_removed_verified_kwarg(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock()

        with self.assertRaises(TypeError):
            await admin.create_user("alice", "a@x.com", "pw", verified=True)

    async def test_existing_user_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}}
        )

        with self.assertRaises(UserAlreadyExistsError) as ctx:
            await admin.create_user("alice", "a@x.com", "pw")
        # RC's own "no email on file" sentinel ("") fails open to a match —
        # see emails_match's documented trade-off.
        self.assertTrue(ctx.exception.identity_matches)

    async def test_existing_user_with_matching_email_identity_matches(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={
                "success": True,
                "user": {"_id": "u1", "username": "alice", "emails": [{"address": "a@x.com"}]},
            }
        )

        with self.assertRaises(UserAlreadyExistsError) as ctx:
            await admin.create_user("alice", "a@x.com", "pw")
        self.assertTrue(ctx.exception.identity_matches)

    async def test_matches_email_registered_at_a_later_array_index(self):
        # RC's `emails` is an array; the requested address sitting anywhere
        # other than index 0 must still count as the same identity.
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={
                "success": True,
                "user": {
                    "_id": "u1", "username": "alice",
                    "emails": [
                        {"address": "primary@x.com"},
                        {"address": "a@x.com"},
                    ],
                },
            }
        )

        with self.assertRaises(UserAlreadyExistsError) as ctx:
            await admin.create_user("alice", "a@x.com", "pw")
        self.assertTrue(ctx.exception.identity_matches)
        self.assertEqual(ctx.exception.existing.emails, ("primary@x.com", "a@x.com"))

    async def test_all_addresses_are_retained_from_the_lookup(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={
                "success": True,
                "user": {
                    "_id": "u1", "username": "alice",
                    "emails": [{"address": "one@x.com"}, {"address": "two@x.com"}],
                },
            }
        )

        user = await admin._get_user_or_none("alice")

        self.assertEqual(user.emails, ("one@x.com", "two@x.com"))
        # Primary/display address stays the first, as RC presents it.
        self.assertEqual(user.email, "one@x.com")

    async def test_malformed_email_entries_are_dropped_not_turned_into_blanks(self):
        # A stray "" would make matches_email() fail open for the whole
        # account, so entries without a usable address are discarded.
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={
                "success": True,
                "user": {
                    "_id": "u1", "username": "alice",
                    "emails": [{"verified": True}, {"address": ""}, {"address": "real@x.com"}],
                },
            }
        )

        user = await admin._get_user_or_none("alice")

        self.assertEqual(user.emails, ("real@x.com",))
        self.assertFalse(user.matches_email("someone-else@x.com"))

    async def test_existing_user_with_mismatched_email_identity_does_not_match(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={
                "success": True,
                "user": {"_id": "u1", "username": "alice", "emails": [{"address": "someone-else@x.com"}]},
            }
        )

        with self.assertRaises(UserAlreadyExistsError) as ctx:
            await admin.create_user("alice", "a@x.com", "pw")
        self.assertFalse(ctx.exception.identity_matches)

    async def test_create_failure_response_raises_verification_error(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[_http_error(400), {"success": False, "error": "boom"}]
        )

        with self.assertRaises(VerificationError):
            await admin.create_user("alice", "a@x.com", "pw")

    async def test_readback_miss_raises_verification_error(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                _http_error(400),
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},
                _http_error(400),
            ]
        )

        with self.assertRaises(VerificationError):
            await admin.create_user("alice", "a@x.com", "pw")


class TestDeactivatedRocketChatUsers(unittest.IsolatedAsyncioTestCase):
    """RC exposes `active`; discarding it made both the already-exists skip and
    the post-create read-back claim success over an account that cannot log in."""

    async def test_active_state_is_propagated_from_users_info(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={
                "success": True,
                "user": {"_id": "u1", "username": "alice", "emails": [], "active": False},
            }
        )

        user = await admin._get_user_or_none("alice")

        self.assertTrue(user.deactivated)

    async def test_active_true_is_not_reported_as_deactivated(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={
                "success": True,
                "user": {"_id": "u1", "username": "alice", "emails": [], "active": True},
            }
        )

        user = await admin._get_user_or_none("alice")

        self.assertFalse(user.deactivated)

    async def test_missing_active_field_is_not_reported_as_deactivated(self):
        # Only an explicit `active: false` counts — an absent field must not
        # be read as deactivated.
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={
                "success": True,
                "user": {"_id": "u1", "username": "alice", "emails": []},
            }
        )

        user = await admin._get_user_or_none("alice")

        self.assertFalse(user.deactivated)

    async def test_existing_deactivated_user_raises_instead_of_skipping(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={
                "success": True,
                "user": {"_id": "u1", "username": "alice", "emails": [], "active": False},
            }
        )

        with self.assertRaises(UserDeactivatedError) as ctx:
            await admin.create_user("alice", "a@x.com", "pw")
        self.assertTrue(ctx.exception.existing.deactivated)

    async def test_newly_created_but_inactive_account_is_a_verification_error(self):
        """The worse half: a FIRST-run false success.

        With Accounts_ManuallyApproveNewUsers enabled, users.create succeeds but
        the account is created inactive, so the old code printed
        "Created user alice" and exited 0 over an account that cannot log in.
        """
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                _http_error(400),  # pre-check: does not exist
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},
                # read-back: created, but inactive
                {
                    "success": True,
                    "user": {"_id": "u1", "username": "alice", "emails": [], "active": False},
                },
            ]
        )

        with self.assertRaises(VerificationError) as ctx:
            await admin.create_user("alice", "a@x.com", "pw")
        self.assertIn("not active", str(ctx.exception))
        self.assertIn("ManuallyApproveNewUsers", str(ctx.exception))


class TestArchivedRocketChatChannels(unittest.IsolatedAsyncioTestCase):
    async def test_archived_state_is_propagated_for_public_channels(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={
                "success": True,
                "channel": {"_id": "c1", "name": "eng", "archived": True},
            }
        )

        channel = await admin._get_channel_or_none("eng")

        self.assertTrue(channel.archived)
        self.assertFalse(channel.is_private)

    async def test_archived_state_is_propagated_for_private_groups(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                _http_error(400),  # channels.info miss
                {"success": True, "group": {"_id": "g1", "name": "secret", "archived": True}},
            ]
        )

        channel = await admin._get_channel_or_none("secret")

        self.assertTrue(channel.archived)
        self.assertTrue(channel.is_private)

    async def test_absent_archived_field_means_not_archived(self):
        # RC omits `archived` entirely on live rooms.
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={"success": True, "channel": {"_id": "c1", "name": "eng"}}
        )

        channel = await admin._get_channel_or_none("eng")

        self.assertFalse(channel.archived)

    async def test_create_channel_over_an_archived_channel_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={
                "success": True,
                "channel": {"_id": "c1", "name": "eng", "archived": True},
            }
        )

        with self.assertRaises(ChannelArchivedError) as ctx:
            await admin.create_channel("eng")
        self.assertTrue(ctx.exception.existing.archived)

    async def test_live_channel_still_takes_the_already_exists_path(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={"success": True, "channel": {"_id": "c1", "name": "eng"}}
        )

        with self.assertRaises(ChannelAlreadyExistsError):
            await admin.create_channel("eng")


class TestCreateChannel(unittest.IsolatedAsyncioTestCase):
    async def test_public_channel_uses_channels_create(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                _http_error(400),  # channels.info not found
                _http_error(400),  # groups.info not found
                {"success": True},  # channels.create
                {"success": True, "channel": {"_id": "c1", "name": "eng"}},  # verify channels.info
            ]
        )

        channel = await admin.create_channel("eng")

        self.assertFalse(channel.is_private)
        create_call = admin._rest._request.call_args_list[2]
        self.assertEqual(create_call.args[1], "channels.create")

    async def test_private_channel_uses_groups_create(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                _http_error(400),  # channels.info not found
                _http_error(400),  # groups.info not found
                {"success": True},  # groups.create
                _http_error(400),  # verify channels.info not found
                {"success": True, "group": {"_id": "g1", "name": "secret"}},  # verify groups.info
            ]
        )

        channel = await admin.create_channel("secret", is_private=True)

        self.assertTrue(channel.is_private)
        create_call = admin._rest._request.call_args_list[2]
        self.assertEqual(create_call.args[1], "groups.create")

    async def test_existing_channel_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            return_value={"success": True, "channel": {"_id": "c1", "name": "eng"}}
        )

        with self.assertRaises(ChannelAlreadyExistsError):
            await admin.create_channel("eng")

    async def test_create_failure_response_raises_verification_error(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                _http_error(400), _http_error(400),  # pre-check both miss
                {"success": False, "error": "boom"},  # channels.create fails
            ]
        )

        with self.assertRaises(VerificationError):
            await admin.create_channel("eng")

    async def test_readback_miss_raises_verification_error(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                _http_error(400), _http_error(400),  # pre-check
                {"success": True},  # channels.create
                _http_error(400), _http_error(400),  # verify miss
            ]
        )

        with self.assertRaises(VerificationError):
            await admin.create_channel("eng")


class TestAddUserToChannel(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_uses_channels_invite_for_public(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},  # users.info
                {"success": True, "channel": {"_id": "c1", "name": "eng"}},  # channels.info
                {"success": True},  # channels.invite
                {"success": True, "members": [{"username": "alice"}]},  # channels.members verify
            ]
        )

        await admin.add_user_to_channel("alice", "eng")

        invite_call = admin._rest._request.call_args_list[2]
        self.assertEqual(invite_call.args[1], "channels.invite")

    async def test_already_in_room_400_is_treated_as_success(self):
        # Confirmed real RC behavior (see tests/e2e/rc_client.py's
        # invite_to_channel, which already works around this): re-inviting
        # a user who's already a member returns HTTP 400, not success:false
        # in a 200. A re-run seed script must not fail here.
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},
                {"success": True, "channel": {"_id": "c1", "name": "eng"}},
                _http_error_with_body(400, {"success": False, "error": "[User already in room]"}),
                {"success": True, "members": [{"username": "alice"}]},
            ]
        )

        await admin.add_user_to_channel("alice", "eng")  # must not raise

    async def test_membership_is_verified_by_the_resolved_username(self):
        # The invite is keyed on the immutable user.id, so it succeeds even when
        # the caller's string isn't the account's canonical username. Verifying
        # against the raw input would then report a VerificationError for a
        # membership that WAS created.
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                # users.info resolves to a different canonical username
                {"success": True,
                 "user": {"_id": "u1", "username": "canonical.alice", "emails": []}},
                {"success": True, "channel": {"_id": "c1", "name": "eng"}},
                {"success": True},  # channels.invite
                # member list carries the canonical username, not the input
                {"success": True, "members": [{"username": "canonical.alice"}]},
            ]
        )

        await admin.add_user_to_channel("u1", "eng")  # must not raise

    async def test_400_without_already_in_body_still_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},
                {"success": True, "channel": {"_id": "c1", "name": "eng"}},
                _http_error_with_body(400, {"success": False, "error": "some other failure"}),
            ]
        )

        with self.assertRaises(httpx.HTTPStatusError):
            await admin.add_user_to_channel("alice", "eng")

    async def test_500_during_invite_still_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},
                {"success": True, "channel": {"_id": "c1", "name": "eng"}},
                _http_error(500),
            ]
        )

        with self.assertRaises(httpx.HTTPStatusError):
            await admin.add_user_to_channel("alice", "eng")

    async def test_user_not_found(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(side_effect=_http_error(400))

        with self.assertRaises(UserNotFoundError):
            await admin.add_user_to_channel("ghost", "eng")

    async def test_channel_not_found(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},
                _http_error(400),  # channels.info
                _http_error(400),  # groups.info
            ]
        )

        with self.assertRaises(ChannelNotFoundError):
            await admin.add_user_to_channel("alice", "ghost-channel")

    async def test_membership_verify_failure_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},
                {"success": True, "channel": {"_id": "c1", "name": "eng"}},
                {"success": True},
                {"success": True, "members": []},  # alice not in list
            ]
        )

        with self.assertRaises(VerificationError):
            await admin.add_user_to_channel("alice", "eng")

    async def test_invite_failure_response_raises_verification_error(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},
                {"success": True, "channel": {"_id": "c1", "name": "eng"}},
                {"success": False, "error": "boom"},
            ]
        )

        with self.assertRaises(VerificationError):
            await admin.add_user_to_channel("alice", "eng")


class TestDeleteUser(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_hard_deletes_and_verifies(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},  # lookup
                {"success": True},  # users.delete
                _http_error(400),  # verify: now not found
            ]
        )

        await admin.delete_user("alice")

        delete_call = admin._rest._request.call_args_list[1]
        self.assertEqual(delete_call.args[1], "users.delete")

    async def test_not_found_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(side_effect=_http_error(400))

        with self.assertRaises(UserNotFoundError):
            await admin.delete_user("ghost")

    async def test_verification_failure_when_still_present(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},
                {"success": True},
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},  # still there
            ]
        )

        with self.assertRaises(VerificationError):
            await admin.delete_user("alice")

    async def test_delete_failure_response_raises_verification_error(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "user": {"_id": "u1", "username": "alice", "emails": []}},
                {"success": False, "error": "boom"},
            ]
        )

        with self.assertRaises(VerificationError):
            await admin.delete_user("alice")


class TestReactivateUser(unittest.IsolatedAsyncioTestCase):
    """Rocket.Chat has nothing this tool can reactivate — every outcome is an
    error that says which case it is, so a plan that reached for this stops."""

    async def test_not_found_says_deletion_is_permanent_and_points_at_create_user(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(side_effect=_http_error(400))
        with self.assertRaises(UserNotFoundError) as ctx:
            await admin.reactivate_user("alice", "n3w")
        self.assertIn("permanent", str(ctx.exception))
        self.assertIn("create-user", str(ctx.exception))

    async def test_a_deactivated_account_is_reported_as_not_supported(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(return_value={
            "success": True,
            "user": {"_id": "u1", "username": "alice", "emails": [], "active": False}})
        with self.assertRaises(AdminError) as ctx:
            await admin.reactivate_user("alice", "n3w")
        self.assertIn("not supported", str(ctx.exception))
        self.assertEqual(admin._rest._request.await_count, 1, "lookup only, no write")

    async def test_an_active_account_is_nothing_to_reactivate(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(return_value={
            "success": True,
            "user": {"_id": "u1", "username": "alice", "emails": [], "active": True}})
        with self.assertRaises(AdminError) as ctx:
            await admin.reactivate_user("alice", "n3w")
        self.assertIn("nothing to reactivate", str(ctx.exception))


class TestDeleteChannel(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_uses_channels_delete_for_public(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "channel": {"_id": "c1", "name": "eng"}},  # lookup
                {"success": True},  # channels.delete
                _http_error(400),  # verify channels.info gone
                _http_error(400),  # verify groups.info gone
            ]
        )

        await admin.delete_channel("eng")

        delete_call = admin._rest._request.call_args_list[1]
        self.assertEqual(delete_call.args[1], "channels.delete")

    async def test_not_found_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(side_effect=_http_error(400))

        with self.assertRaises(ChannelNotFoundError):
            await admin.delete_channel("ghost")

    async def test_delete_failure_response_raises_verification_error(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "channel": {"_id": "c1", "name": "eng"}},
                {"success": False, "error": "boom"},
            ]
        )

        with self.assertRaises(VerificationError):
            await admin.delete_channel("eng")

    async def test_verification_failure_when_still_present(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"success": True, "channel": {"_id": "c1", "name": "eng"}},
                {"success": True},
                {"success": True, "channel": {"_id": "c1", "name": "eng"}},  # still there
            ]
        )

        with self.assertRaises(VerificationError):
            await admin.delete_channel("eng")


class TestIsChannelMemberPagination(unittest.IsolatedAsyncioTestCase):
    async def test_finds_member_on_second_page(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"members": [{"username": "bob"}], "total": 2},
                {"members": [{"username": "alice"}], "total": 2},
            ]
        )

        found = await admin._is_channel_member("channels.members", "c1", "alice")

        self.assertTrue(found)
        self.assertEqual(admin._rest._request.await_count, 2)

    async def test_not_found_after_exhausting_all_pages(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"members": [{"username": "bob"}], "total": 2},
                {"members": [{"username": "carol"}], "total": 2},
            ]
        )

        found = await admin._is_channel_member("channels.members", "c1", "alice")

        self.assertFalse(found)

    async def test_empty_first_page_returns_false_without_looping_forever(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(return_value={"members": [], "total": 0})

        found = await admin._is_channel_member("channels.members", "c1", "alice")

        self.assertFalse(found)
        admin._rest._request.assert_awaited_once()


class TestLookupPropagatesUnexpectedErrors(unittest.IsolatedAsyncioTestCase):
    async def test_get_user_or_none_reraises_non_400_404(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(side_effect=_http_error(500))

        with self.assertRaises(httpx.HTTPStatusError):
            await admin._get_user_or_none("alice")

    async def test_get_user_or_none_returns_none_on_success_false(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(return_value={"success": False})

        self.assertIsNone(await admin._get_user_or_none("alice"))

    async def test_get_channel_or_none_reraises_non_400_404_from_channels_info(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(side_effect=_http_error(500))

        with self.assertRaises(httpx.HTTPStatusError):
            await admin._get_channel_or_none("eng")

    async def test_get_channel_or_none_reraises_non_400_404_from_groups_info(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(side_effect=[_http_error(400), _http_error(500)])

        with self.assertRaises(httpx.HTTPStatusError):
            await admin._get_channel_or_none("eng")


if __name__ == "__main__":
    unittest.main()
