"""Unit tests for MattermostAdmin.

The underlying MattermostREST is replaced with a MagicMock exposing only
the AsyncMock methods each test needs — REST transport itself is already
covered by test_mattermost_rest.py. These tests target MattermostAdmin's
own logic: team resolution on connect, pre-create existence checks
(idempotency), and post-write read-back verification.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx

from gateway.admin.base import (
    AdminError,
    ChannelAlreadyExistsError,
    ChannelNotFoundError,
    UserAlreadyExistsError,
    UserDeactivatedError,
    UserNotFoundError,
    VerificationError,
)
from gateway.admin.config import AdminProfile
from gateway.admin.mattermost_admin import MattermostAdmin
from gateway.connectors.mattermost.rest import RoomNotFoundError


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://x")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


def _http_error_with_body(status_code: int, json_body: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://x")
    response = httpx.Response(status_code, request=request, json=json_body)
    return httpx.HTTPStatusError("error", request=request, response=response)


def _profile(**overrides) -> AdminProfile:
    defaults = dict(
        name="mm-lab", type="mattermost", server_url="https://mm.example",
        team="labteam", token="tok",
    )
    defaults.update(overrides)
    return AdminProfile(**defaults)


def _admin_with_mock_rest() -> MattermostAdmin:
    admin = MattermostAdmin(_profile())
    admin._rest = MagicMock()
    admin._rest.team_id = "team-1"
    admin._rest.close = AsyncMock()
    return admin


class TestConstructor(unittest.TestCase):
    def test_token_wins_and_password_login_mode_is_disabled_even_if_both_set(self):
        # AdminProfile permits both a token and username/password to be set
        # at once. If both were passed through to MattermostREST, a 401
        # from a revoked/expired PAT would silently trigger a password
        # relogin (MattermostREST._is_login_mode only checks username+
        # password presence) instead of failing loudly — defeating PAT
        # revocation as a way to cut this tool's access.
        profile = _profile(token="tok", username="admin", password="pw")

        admin = MattermostAdmin(profile)

        self.assertEqual(admin._rest._token, "tok")
        self.assertIsNone(admin._rest._username)
        self.assertIsNone(admin._rest._password)
        self.assertFalse(admin._rest._is_login_mode)

    def test_username_password_used_when_no_token(self):
        profile = _profile(token=None, username="admin", password="pw")

        admin = MattermostAdmin(profile)

        self.assertIsNone(admin._rest._token)
        self.assertEqual(admin._rest._username, "admin")
        self.assertTrue(admin._rest._is_login_mode)


class TestConnect(unittest.IsolatedAsyncioTestCase):
    async def test_connect_authenticates_and_resolves_configured_team(self):
        admin = _admin_with_mock_rest()
        admin._rest.authenticate = AsyncMock()
        admin._rest.get_me = AsyncMock()
        admin._rest.resolve_team = AsyncMock()

        await admin.connect()

        admin._rest.authenticate.assert_awaited_once()
        admin._rest.get_me.assert_awaited_once()
        admin._rest.resolve_team.assert_awaited_once_with("labteam")

    async def test_connect_never_falls_back_when_membership_scan_succeeds(self):
        admin = _admin_with_mock_rest()
        admin._rest.authenticate = AsyncMock()
        admin._rest.get_me = AsyncMock()
        admin._rest.resolve_team = AsyncMock()
        admin._rest._request = AsyncMock()

        await admin.connect()

        admin._rest._request.assert_not_awaited()

    async def test_connect_falls_back_to_admin_by_name_lookup_when_not_a_member(self):
        # The scenario this fallback exists for: a system-admin/PAT
        # credential administering a team it hasn't itself joined.
        admin = _admin_with_mock_rest()
        admin._rest.authenticate = AsyncMock()
        admin._rest.get_me = AsyncMock()
        admin._rest.resolve_team = AsyncMock(
            side_effect=RoomNotFoundError("Team 'labteam' not found among the bot's teams")
        )
        admin._rest._request = AsyncMock(return_value={"id": "team-99", "name": "labteam"})

        await admin.connect()

        admin._rest._request.assert_awaited_once_with("GET", "teams/name/labteam")
        self.assertEqual(admin._rest.team_id, "team-99")

    async def test_connect_raises_combined_error_when_both_lookups_fail(self):
        admin = _admin_with_mock_rest()
        admin._rest.authenticate = AsyncMock()
        admin._rest.get_me = AsyncMock()
        admin._rest.resolve_team = AsyncMock(
            side_effect=RoomNotFoundError("Team 'labteam' not found among the bot's teams")
        )
        admin._rest._request = AsyncMock(side_effect=_http_error(404))

        with self.assertRaises(RoomNotFoundError) as ctx:
            await admin.connect()

        # Both failure reasons should be present, not just whichever
        # happened last — this is what a human debugging a typo'd team
        # name or a non-admin credential actually needs to see.
        self.assertIn("not found among the bot's teams", str(ctx.exception))
        self.assertIn("admin by-name lookup also failed", str(ctx.exception))
        self.assertIn("by-id lookup", str(ctx.exception))

    async def test_connect_falls_back_to_by_id_lookup_when_team_is_an_id(self):
        # MattermostREST.resolve_team() matches profile.team against a team
        # NAME *or* a team ID, so an ID-configured profile is legitimate —
        # but Mattermost has no by-name-or-id endpoint, so the name lookup
        # 404s and the fallback must then try the ID endpoint.
        admin = _admin_with_mock_rest()
        admin._rest.authenticate = AsyncMock()
        admin._rest.get_me = AsyncMock()
        admin._rest.resolve_team = AsyncMock(
            side_effect=RoomNotFoundError("Team 'labteam' not found among the bot's teams")
        )
        admin._rest._request = AsyncMock(
            side_effect=[
                _http_error(404),  # GET teams/name/labteam -> not a name
                {"id": "team-77", "name": "actual-name"},  # GET teams/labteam -> found by id
            ]
        )

        await admin.connect()

        admin._rest._request.assert_any_await("GET", "teams/name/labteam")
        admin._rest._request.assert_any_await("GET", "teams/labteam")
        self.assertEqual(admin._rest.team_id, "team-77")


class TestCreateUser(unittest.IsolatedAsyncioTestCase):
    async def test_creates_verifies_and_joins_team(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            side_effect=[
                _http_error(404),  # pre-check: not found
                {"id": "u1", "username": "alice", "email": "a@x.com"},  # read-back
            ]
        )
        admin._rest._request = AsyncMock(
            side_effect=[
                {"id": "u1"},  # POST create
                {"user_id": "u1"},  # GET team membership check: already a member
            ]
        )

        user = await admin.create_user("alice", "a@x.com", "pw")

        self.assertEqual(user.id, "u1")
        self.assertEqual(user.username, "alice")
        admin._rest._request.assert_any_await(
            "POST", "users",
            json_data={"username": "alice", "email": "a@x.com", "password": "pw"},
        )
        admin._rest._request.assert_any_await("GET", "teams/team-1/members/u1")

    async def test_joins_team_when_not_already_a_member(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            side_effect=[_http_error(404), {"id": "u1", "username": "alice", "email": "a@x.com"}]
        )
        admin._rest._request = AsyncMock(
            side_effect=[
                {"id": "u1"},  # POST create
                _http_error(404),  # GET team membership check: not a member
                {},  # POST team members (add)
            ]
        )

        await admin.create_user("alice", "a@x.com", "pw")

        admin._rest._request.assert_any_await(
            "POST", "teams/team-1/members", json_data={"team_id": "team-1", "user_id": "u1"}
        )

    async def test_create_user_rejects_removed_verified_kwarg(self):
        # The email-verified knob was deliberately removed (see
        # PlatformAdmin.create_user): unobservable on Mattermost, and
        # meaningless for agent accounts with no real inbox.
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(side_effect=_http_error(404))
        admin._rest._request = AsyncMock()

        with self.assertRaises(TypeError):
            await admin.create_user("alice", "a@x.com", "pw", verified=True)

    async def test_email_verified_is_never_sent(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            side_effect=[_http_error(404), {"id": "u1", "username": "alice", "email": "a@x.com"}]
        )
        admin._rest._request = AsyncMock(side_effect=[{"id": "u1"}, {"user_id": "u1"}])

        await admin.create_user("alice", "a@x.com", "pw")

        create_call = admin._rest._request.call_args_list[0]
        self.assertNotIn("email_verified", create_call.kwargs["json_data"])

    async def test_full_name_maps_to_nickname(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            side_effect=[_http_error(404), {"id": "u1", "username": "bob", "email": "b@x.com"}]
        )
        admin._rest._request = AsyncMock(
            side_effect=[{"id": "u1"}, {"user_id": "u1"}]
        )

        await admin.create_user("bob", "b@x.com", "pw", full_name="Bob Smith")

        create_call = admin._rest._request.call_args_list[0]
        self.assertEqual(create_call.kwargs["json_data"]["nickname"], "Bob Smith")

    async def test_existing_user_raises_with_existing_payload(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={"id": "u1", "username": "alice", "email": "a@x.com"}
        )
        admin._rest._request = AsyncMock(return_value={"user_id": "u1"})  # already a team member

        with self.assertRaises(UserAlreadyExistsError) as ctx:
            await admin.create_user("alice", "a@x.com", "pw")
        self.assertEqual(ctx.exception.existing.id, "u1")

    async def test_deactivated_existing_user_raises_user_deactivated_error(self):
        # Reachable with this tool's own commands: delete-user soft-
        # deactivates (MM sets delete_at), and the username lookup still
        # returns the row — so a reseed's create-user must NOT report a
        # clean skip over an account that can't log in.
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={
                "id": "u1", "username": "alice", "email": "a@x.com",
                "delete_at": 1750000000000,
            }
        )
        admin._rest._request = AsyncMock()

        with self.assertRaises(UserDeactivatedError) as ctx:
            await admin.create_user("alice", "a@x.com", "pw")

        self.assertTrue(ctx.exception.existing.deactivated)
        # Must not have bothered repairing team membership for a dead account.
        admin._rest._request.assert_not_awaited()

    def test_user_deactivated_error_is_not_a_user_already_exists_error(self):
        # Load-bearing: cli.py catches UserAlreadyExistsError and reports an
        # idempotent skip with exit 0. If UserDeactivatedError were a
        # subclass, that handler would swallow it and reinstate the bug.
        self.assertFalse(issubclass(UserDeactivatedError, UserAlreadyExistsError))
        self.assertTrue(issubclass(UserDeactivatedError, AdminError))

    async def test_active_existing_user_is_not_treated_as_deactivated(self):
        # delete_at: 0 is MM's "active" value — must not trip the check.
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={"id": "u1", "username": "alice", "email": "a@x.com", "delete_at": 0}
        )
        admin._rest._request = AsyncMock(return_value={"user_id": "u1"})

        with self.assertRaises(UserAlreadyExistsError):
            await admin.create_user("alice", "a@x.com", "pw")

    async def test_existing_user_repairs_team_membership_before_raising(self):
        # Retry-recovery path: a prior create_user() call may have created
        # the account but failed at the team-join step (see
        # test_team_join_failure_propagates) — a retry must not just report
        # "already exists" without ALSO fixing the missing team membership.
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={"id": "u1", "username": "alice", "email": "a@x.com"}
        )
        admin._rest._request = AsyncMock(side_effect=[_http_error(404), {}])  # not a member yet, then join

        with self.assertRaises(UserAlreadyExistsError) as ctx:
            await admin.create_user("alice", "a@x.com", "pw")
        self.assertTrue(ctx.exception.identity_matches)

        admin._rest._request.assert_any_await(
            "POST", "teams/team-1/members", json_data={"team_id": "team-1", "user_id": "u1"}
        )

    async def test_case_and_whitespace_insensitive_email_still_repairs(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={"id": "u1", "username": "alice", "email": " Alice@X.COM "}
        )
        admin._rest._request = AsyncMock(return_value={"user_id": "u1"})  # already a team member

        with self.assertRaises(UserAlreadyExistsError) as ctx:
            await admin.create_user("alice", "alice@x.com", "pw")
        self.assertTrue(ctx.exception.identity_matches)
        admin._rest._request.assert_awaited_once()  # the team membership check ran

    async def test_mismatched_email_does_not_repair_team_membership(self):
        # Username collision alone isn't proof of shared identity — this
        # could be an unrelated pre-existing account. Must not blindly
        # grant them team access.
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={"id": "u1", "username": "alice", "email": "someone-else@x.com"}
        )
        admin._rest._request = AsyncMock()

        with self.assertRaises(UserAlreadyExistsError) as ctx:
            await admin.create_user("alice", "a@x.com", "pw")
        self.assertFalse(ctx.exception.identity_matches)

        admin._rest._request.assert_not_awaited()

    async def test_empty_existing_email_fails_open_and_still_repairs(self):
        # Deliberate, documented trade-off (see emails_match) — not a claim
        # this is provably the same identity, just the chosen default.
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={"id": "u1", "username": "alice", "email": ""}
        )
        admin._rest._request = AsyncMock(return_value={"user_id": "u1"})

        with self.assertRaises(UserAlreadyExistsError) as ctx:
            await admin.create_user("alice", "a@x.com", "pw")
        self.assertTrue(ctx.exception.identity_matches)
        admin._rest._request.assert_awaited_once()

    async def test_existing_user_team_repair_failure_propagates(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={"id": "u1", "username": "alice", "email": "a@x.com"}
        )
        admin._rest._request = AsyncMock(side_effect=_http_error(500))

        # A genuine failure repairing team membership must surface loudly,
        # not be swallowed into the idempotent "already exists" no-op.
        with self.assertRaises(httpx.HTTPStatusError):
            await admin.create_user("alice", "a@x.com", "pw")

    async def test_readback_miss_raises_verification_error(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            side_effect=[_http_error(404), _http_error(404)]
        )
        admin._rest._request = AsyncMock(return_value={"id": "u1"})

        with self.assertRaises(VerificationError):
            await admin.create_user("alice", "a@x.com", "pw")

    async def test_create_response_missing_id_raises_verification_error(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(side_effect=[_http_error(404)])
        admin._rest._request = AsyncMock(return_value={})

        with self.assertRaises(VerificationError):
            await admin.create_user("alice", "a@x.com", "pw")

    async def test_team_join_failure_propagates(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            side_effect=[_http_error(404), {"id": "u1", "username": "alice", "email": "a@x.com"}]
        )
        admin._rest._request = AsyncMock(
            side_effect=[
                {"id": "u1"},  # POST create succeeds
                _http_error(500),  # team membership check: genuine server error
            ]
        )

        # The account was created and verified — a downstream team-join
        # failure should still surface loudly rather than being swallowed,
        # since an account with no team can't be used in any channel.
        with self.assertRaises(httpx.HTTPStatusError):
            await admin.create_user("alice", "a@x.com", "pw")


class TestAddUserToTeam(unittest.IsolatedAsyncioTestCase):
    async def test_already_a_member_is_a_noop(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={"id": "u1", "username": "alice", "email": ""}
        )
        admin._rest._request = AsyncMock(return_value={"user_id": "u1"})

        await admin.add_user_to_team("alice")

        admin._rest._request.assert_awaited_once_with("GET", "teams/team-1/members/u1")

    async def test_adds_when_not_already_a_member(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={"id": "u1", "username": "alice", "email": ""}
        )
        admin._rest._request = AsyncMock(side_effect=[_http_error(404), {}])

        await admin.add_user_to_team("alice")

        admin._rest._request.assert_any_await(
            "POST", "teams/team-1/members", json_data={"team_id": "team-1", "user_id": "u1"}
        )

    async def test_user_not_found_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(side_effect=_http_error(404))

        with self.assertRaises(UserNotFoundError):
            await admin.add_user_to_team("ghost")


class TestCreateChannel(unittest.IsolatedAsyncioTestCase):
    async def test_creates_and_verifies_new_channel(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                _http_error(404),  # pre-check GET
                {"id": "c1"},  # POST create
                {"id": "c1", "name": "eng", "type": "O"},  # read-back GET
            ]
        )

        channel = await admin.create_channel("eng")

        self.assertEqual(channel.id, "c1")
        self.assertFalse(channel.is_private)

    async def test_private_channel_sets_type_p(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                _http_error(404),
                {"id": "c1"},
                {"id": "c1", "name": "secret", "type": "P"},
            ]
        )

        channel = await admin.create_channel("secret", is_private=True)

        self.assertTrue(channel.is_private)
        create_call = admin._rest._request.call_args_list[1]
        self.assertEqual(create_call.kwargs["json_data"]["type"], "P")

    async def test_existing_channel_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(return_value={"id": "c1", "name": "eng", "type": "O"})

        with self.assertRaises(ChannelAlreadyExistsError):
            await admin.create_channel("eng")

    async def test_create_response_missing_id_raises_verification_error(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(side_effect=[_http_error(404), {}])

        with self.assertRaises(VerificationError):
            await admin.create_channel("eng")

    async def test_readback_miss_raises_verification_error(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[_http_error(404), {"id": "c1"}, _http_error(404)]
        )

        with self.assertRaises(VerificationError):
            await admin.create_channel("eng")


class TestAddUserToChannel(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_when_already_a_team_member(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(return_value={"id": "u1", "username": "alice", "email": ""})
        admin._rest._request = AsyncMock(
            side_effect=[
                {"id": "c1", "name": "eng", "type": "O"},  # channel lookup
                {"user_id": "u1"},  # team membership check: already a member
                {},  # POST channel members
                {},  # GET membership verify
            ]
        )

        await admin.add_user_to_channel("alice", "eng")

        admin._rest._request.assert_any_await(
            "GET", "teams/team-1/members/u1"
        )
        admin._rest._request.assert_any_await(
            "POST", "channels/c1/members", json_data={"user_id": "u1"}
        )
        # Not a member -> no POST to add them should have happened.
        for call in admin._rest._request.call_args_list:
            self.assertNotEqual(call.args[:2], ("POST", "teams/team-1/members"))

    async def test_adds_user_to_team_when_not_already_a_member(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(return_value={"id": "u1", "username": "alice", "email": ""})
        admin._rest._request = AsyncMock(
            side_effect=[
                {"id": "c1", "name": "eng", "type": "O"},  # channel lookup
                _http_error(404),  # team membership check: not a member
                {},  # POST team members (add)
                {},  # POST channel members
                {},  # GET membership verify
            ]
        )

        await admin.add_user_to_channel("alice", "eng")

        admin._rest._request.assert_any_await(
            "POST", "teams/team-1/members", json_data={"team_id": "team-1", "user_id": "u1"}
        )

    async def test_team_membership_check_reraises_non_404(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(return_value={"id": "u1", "username": "alice", "email": ""})
        admin._rest._request = AsyncMock(
            side_effect=[
                {"id": "c1", "name": "eng", "type": "O"},
                _http_error(500),
            ]
        )

        with self.assertRaises(httpx.HTTPStatusError):
            await admin.add_user_to_channel("alice", "eng")

    async def test_user_not_found(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(side_effect=_http_error(404))

        with self.assertRaises(UserNotFoundError):
            await admin.add_user_to_channel("ghost", "eng")

    async def test_channel_not_found(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(return_value={"id": "u1", "username": "alice", "email": ""})
        admin._rest._request = AsyncMock(side_effect=_http_error(404))

        with self.assertRaises(ChannelNotFoundError):
            await admin.add_user_to_channel("alice", "ghost-channel")

    async def test_membership_verify_failure_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(return_value={"id": "u1", "username": "alice", "email": ""})
        admin._rest._request = AsyncMock(
            side_effect=[
                {"id": "c1", "name": "eng", "type": "O"},
                {"user_id": "u1"},  # already a team member
                {},  # POST channel members
                _http_error(500),  # verify fails
            ]
        )

        with self.assertRaises(VerificationError):
            await admin.add_user_to_channel("alice", "eng")


class TestPostWriteReadbackErrors(unittest.IsolatedAsyncioTestCase):
    """A read-back failure must report the outcome as UNKNOWN, not as applied.

    Two defects were bundled here. The message used to splice in httpx's
    generic "Client error '403 Forbidden' for url '...' For more information
    check: https://developer.mozilla.org/..." instead of the platform's own
    text (wrapping the error means cli._run() never reaches its
    httpx.HTTPStatusError arm, so friendly_error_message() never ran). And
    nothing told the operator the write had already been applied — a bare
    "Error: You do not have the appropriate permissions." after a successful
    account creation reads like "nothing happened", inviting a re-run of a
    command that already did its job.
    """

    def _forbidden(self):
        return _http_error_with_body(
            403, {"id": "api.context.permissions.app_error",
                  "message": "You do not have the appropriate permissions."}
        )

    async def test_create_user_readback_failure_reports_unknown_not_applied(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            side_effect=[_http_error(404), self._forbidden()]
        )
        admin._rest._request = AsyncMock(return_value={"id": "u1"})

        with self.assertRaises(VerificationError) as ctx:
            await admin.create_user("alice", "a@x.com", "pw")

        msg = str(ctx.exception)
        self.assertIn("reported user 'alice' created", msg)
        # Must NOT assert the write definitely landed — the read-back that
        # would have established that is exactly what failed.
        self.assertIn("UNKNOWN", msg)
        self.assertNotIn("already been applied", msg)
        # Platform's own wording, not httpx's generic text.
        self.assertIn("You do not have the appropriate permissions.", msg)
        self.assertNotIn("developer.mozilla.org", msg)

    async def test_delete_user_readback_failure_reports_unknown_not_applied(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={"id": "u1", "username": "alice", "email": "a@x.com"}
        )
        admin._rest._request = AsyncMock(side_effect=[{}, self._forbidden()])

        with self.assertRaises(VerificationError) as ctx:
            await admin.delete_user("alice")

        msg = str(ctx.exception)
        self.assertIn("reported user 'alice' deactivated", msg)
        self.assertIn("UNKNOWN", msg)
        self.assertNotIn("already been applied", msg)
        self.assertIn("You do not have the appropriate permissions.", msg)

    async def test_add_to_channel_readback_failure_reports_unknown_not_applied(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={"id": "u1", "username": "alice", "email": ""}
        )
        admin._rest._request = AsyncMock(
            side_effect=[
                {"id": "c1", "name": "eng", "type": "O"},  # channel lookup
                {"user_id": "u1"},  # already a team member
                {},  # POST channel members
                self._forbidden(),  # read-back
            ]
        )

        with self.assertRaises(VerificationError) as ctx:
            await admin.add_user_to_channel("alice", "eng")

        msg = str(ctx.exception)
        self.assertIn("added to channel 'eng'", msg)
        self.assertIn("UNKNOWN", msg)
        self.assertNotIn("already been applied", msg)
        self.assertIn("You do not have the appropriate permissions.", msg)

    async def test_a_specific_verification_error_is_not_reworded(self):
        # The read-back's own logic (delete_at still unset) already produces a
        # precise message; only HTTP failures get the wrapper treatment.
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(
            return_value={"id": "u1", "username": "alice", "email": "a@x.com"}
        )
        admin._rest._request = AsyncMock(side_effect=[{}, {"delete_at": 0}])

        with self.assertRaises(VerificationError) as ctx:
            await admin.delete_user("alice")

        self.assertIn("delete_at is still unset", str(ctx.exception))
        self.assertNotIn("already been applied", str(ctx.exception))


class TestResolveTeamErrorMessage(unittest.IsolatedAsyncioTestCase):
    async def test_combined_failure_uses_platform_wording_not_httpx_generic(self):
        admin = _admin_with_mock_rest()
        admin._rest.authenticate = AsyncMock()
        admin._rest.get_me = AsyncMock()
        admin._rest.resolve_team = AsyncMock(
            side_effect=RoomNotFoundError("Team 'labteam' not found among the bot's teams")
        )
        admin._rest._request = AsyncMock(
            side_effect=_http_error_with_body(
                404, {"id": "app.team.get_by_name.missing.app_error",
                      "message": "Unable to find the existing team"}
            )
        )

        with self.assertRaises(RoomNotFoundError) as ctx:
            await admin.connect()

        msg = str(ctx.exception)
        self.assertIn("Unable to find the existing team", msg)
        self.assertNotIn("developer.mozilla.org", msg)
        # The actionable remedy must survive.
        self.assertIn("mmctl team add", msg)


class TestDeleteUser(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_deactivates_and_verifies(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(return_value={"id": "u1", "username": "alice", "email": ""})
        admin._rest._request = AsyncMock(
            side_effect=[
                {},  # DELETE
                {"delete_at": 12345},  # GET verify
            ]
        )

        await admin.delete_user("alice")

        admin._rest._request.assert_any_await("DELETE", "users/u1")

    async def test_not_found_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(side_effect=_http_error(404))

        with self.assertRaises(UserNotFoundError):
            await admin.delete_user("ghost")

    async def test_verification_failure_when_delete_at_unset(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(return_value={"id": "u1", "username": "alice", "email": ""})
        admin._rest._request = AsyncMock(side_effect=[{}, {"delete_at": 0}])

        with self.assertRaises(VerificationError):
            await admin.delete_user("alice")


class TestReactivateUser(unittest.IsolatedAsyncioTestCase):
    """The inverse of delete_user (coop-keeper design §3.10): re-enable, set the
    new password as an admin, read back that delete_at is unset."""

    async def test_happy_path_reenables_sets_password_and_verifies(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(return_value={
            "id": "u1", "username": "alice", "email": "a@x.com", "delete_at": 1750000000000})
        admin._rest._request = AsyncMock(side_effect=[
            {},  # PUT active
            {},  # PUT password
            {"id": "u1", "delete_at": 0},  # GET verify
        ])

        user = await admin.reactivate_user("alice", "n3w")

        self.assertEqual((user.id, user.username, user.deactivated), ("u1", "alice", False))
        calls = admin._rest._request.call_args_list
        self.assertEqual(calls[0].args[:2], ("PUT", "users/u1/active"))
        self.assertEqual(calls[0].kwargs["json_data"], {"active": True})
        self.assertEqual(calls[1].args[:2], ("PUT", "users/u1/password"))
        self.assertEqual(calls[1].kwargs["json_data"], {"new_password": "n3w"})
        self.assertEqual(calls[2].args[:2], ("GET", "users/u1"))

    async def test_not_found_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(side_effect=_http_error(404))
        with self.assertRaises(UserNotFoundError):
            await admin.reactivate_user("ghost", "n3w")

    async def test_an_active_account_is_refused_and_nothing_is_written(self):
        # Rotation is out of scope: an active account keeps its password.
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(return_value={
            "id": "u1", "username": "alice", "email": "", "delete_at": 0})
        admin._rest._request = AsyncMock()
        with self.assertRaises(AdminError) as ctx:
            await admin.reactivate_user("alice", "n3w")
        self.assertIn("nothing to reactivate", str(ctx.exception))
        admin._rest._request.assert_not_awaited()

    async def test_verification_failure_when_delete_at_still_set(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(return_value={
            "id": "u1", "username": "alice", "email": "", "delete_at": 5})
        admin._rest._request = AsyncMock(side_effect=[{}, {}, {"id": "u1", "delete_at": 5}])
        with self.assertRaises(VerificationError):
            await admin.reactivate_user("alice", "n3w")


class TestDeleteChannel(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_archives_and_verifies(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"id": "c1", "name": "eng", "type": "O"},  # lookup
                {},  # DELETE
                {"delete_at": 999},  # verify
            ]
        )

        await admin.delete_channel("eng")

        admin._rest._request.assert_any_await("DELETE", "channels/c1")

    async def test_not_found_raises(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(side_effect=_http_error(404))

        with self.assertRaises(ChannelNotFoundError):
            await admin.delete_channel("ghost")

    async def test_verification_failure_when_delete_at_unset(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(
            side_effect=[
                {"id": "c1", "name": "eng", "type": "O"},
                {},
                {"delete_at": 0},
            ]
        )

        with self.assertRaises(VerificationError):
            await admin.delete_channel("eng")


class TestLookupPropagatesUnexpectedErrors(unittest.IsolatedAsyncioTestCase):
    async def test_get_user_or_none_reraises_non_404(self):
        admin = _admin_with_mock_rest()
        admin._rest.get_user_by_username = AsyncMock(side_effect=_http_error(500))

        with self.assertRaises(httpx.HTTPStatusError):
            await admin._get_user_or_none("alice")

    async def test_get_channel_or_none_reraises_non_404(self):
        admin = _admin_with_mock_rest()
        admin._rest._request = AsyncMock(side_effect=_http_error(500))

        with self.assertRaises(httpx.HTTPStatusError):
            await admin._get_channel_or_none("eng")


if __name__ == "__main__":
    unittest.main()
