"""Rocket.Chat implementation of PlatformAdmin.

Composes a RocketChatREST instance rather than extending it, for the same
reason as MattermostAdmin — see that module's docstring.

Two RC-specific asymmetries vs. Mattermost worth flagging up front:
  - No PAT/token-only auth path: RocketChatREST only supports username/
    password login today, so RocketChatAdmin requires both in the profile
    (unlike MattermostAdmin, which prefers a token). See connect().
  - Public/private channels are genuinely different resources in RC's API
    (channels.* vs groups.*), not one resource with a type flag like
    Mattermost — every method here dispatches on is_private accordingly.
  - users.delete is a real hard delete (unlike Mattermost's soft-delete
    deactivation) — see delete_user().
"""

import logging

import httpx

from gateway.admin._errors import readback_after_write
from gateway.admin._logging import quiet_expected_error
from gateway.admin.base import (
    AdminChannel,
    AdminUser,
    ChannelAlreadyExistsError,
    ChannelArchivedError,
    ChannelNotFoundError,
    PlatformAdmin,
    UserAlreadyExistsError,
    UserDeactivatedError,
    UserNotFoundError,
    VerificationError,
)
from gateway.admin.config import AdminConfigError, AdminProfile
from gateway.connectors.rocketchat.rest import RocketChatREST
from gateway.connectors.rocketchat.rest import logger as _rc_rest_logger

logger = logging.getLogger("coop.admin.rocketchat")

# RC treats "not found" on info-lookup endpoints as a 400, not a 404 (same
# behavior RocketChatREST.resolve_room already works around).
_NOT_FOUND_STATUSES = (400, 404)


class RocketChatAdmin(PlatformAdmin):
    def __init__(self, profile: AdminProfile):
        if not (profile.username and profile.password):
            raise AdminConfigError(
                f"Profile '{profile.name}': RocketChatAdmin requires 'username' "
                "and 'password' — token-only auth isn't supported yet "
                "(RocketChatREST has no token-mode constructor path)."
            )
        self.profile = profile
        self._rest = RocketChatREST(server_url=profile.server_url)

    async def connect(self) -> None:
        await self._rest.login(self.profile.username, self.profile.password)  # type: ignore[arg-type]

    async def close(self) -> None:
        await self._rest.close()

    async def create_user(
        self,
        username: str,
        email: str,
        password: str,
        *,
        full_name: str | None = None,
    ) -> AdminUser:
        existing = await self._get_user_or_none(username)
        if existing is not None:
            if existing.deactivated:
                # Same rule as Mattermost: an existing-but-deactivated account
                # means the requested "an active user exists" state was NOT
                # achieved, so it must not be reported as an idempotent skip.
                # Unlike MM this state is never self-inflicted (RC's
                # delete_user hard-deletes), so it always comes from an admin
                # having deactivated the account out-of-band.
                raise UserDeactivatedError(username, existing=existing)
            # RC has no team/repair concept, so identity_matches doesn't
            # gate any action here the way it does for Mattermost — it's
            # still computed and passed through so the CLI layer (which
            # dispatches identically for both platforms) can refuse an
            # identity collision uniformly rather than only for Mattermost.
            raise UserAlreadyExistsError(
                username, existing=existing, identity_matches=existing.matches_email(email)
            )

        payload = {
            "name": full_name or username,
            "email": email,
            "password": password,
            "username": username,
            # No `verified` field is sent: RC defaults it to false, which is
            # correct for an agent account with no real inbox (see
            # PlatformAdmin.create_user for why this tool offers no option to
            # change it).
            #
            # requirePasswordChange stays, and is NOT the same concern: it
            # suppresses RC's forced password reset on first login, which a
            # service account must not hit if it is to log in unattended.
            "requirePasswordChange": False,
        }
        result = await self._rest._request("POST", "users.create", json_data=payload)
        if not result.get("success"):
            raise VerificationError(
                f"Rocket.Chat user creation for '{username}' failed: {result.get('error', result)}"
            )

        with readback_after_write(f"Rocket.Chat reported user '{username}' created"):
            created = await self._get_user_or_none(username)
        if created is None:
            raise VerificationError(
                f"Rocket.Chat reported user '{username}' created but a "
                "read-back lookup could not find it."
            )
        if created.deactivated:
            # First-run false success, not just a re-run concern: with
            # Accounts_ManuallyApproveNewUsers enabled, users.create succeeds
            # but the account is created INACTIVE and cannot log in. Reporting
            # "Created user X" + exit 0 there is exactly the silent wrong
            # state VerificationError exists to prevent.
            raise VerificationError(
                f"Rocket.Chat user '{username}' was created but is not active — "
                "the server is likely configured with "
                "Accounts_ManuallyApproveNewUsers, so the account cannot log in "
                "until an admin approves it."
            )
        return created

    async def create_channel(self, name: str, *, is_private: bool = False) -> AdminChannel:
        existing = await self._get_channel_or_none(name)
        if existing is not None:
            if existing.archived:
                # Checked before the already-exists path: an archived channel
                # means the requested usable-channel state was NOT achieved, so
                # it must not be reported as an idempotent skip. See
                # ChannelArchivedError (which is deliberately not a
                # ChannelAlreadyExistsError subclass, or cli.py would swallow
                # it back into exit 0).
                raise ChannelArchivedError(name, existing=existing)
            raise ChannelAlreadyExistsError(name, existing=existing)

        endpoint = "groups.create" if is_private else "channels.create"
        result = await self._rest._request("POST", endpoint, json_data={"name": name})
        if not result.get("success"):
            raise VerificationError(
                f"Rocket.Chat channel creation for '{name}' failed: {result.get('error', result)}"
            )

        with readback_after_write(f"Rocket.Chat reported channel '{name}' created"):
            verified = await self._get_channel_or_none(name)
        if verified is None:
            raise VerificationError(
                f"Rocket.Chat reported channel '{name}' created but a "
                "read-back lookup could not find it."
            )
        return verified

    async def add_user_to_channel(self, username: str, channel_name: str) -> None:
        user = await self._get_user_or_none(username)
        if user is None:
            raise UserNotFoundError(f"Rocket.Chat user '{username}' not found")
        channel = await self._get_channel_or_none(channel_name)
        if channel is None:
            raise ChannelNotFoundError(f"Rocket.Chat channel '{channel_name}' not found")

        endpoint = "groups.invite" if channel.is_private else "channels.invite"
        try:
            result = await self._rest._request(
                "POST", endpoint, json_data={"roomId": channel.id, "userId": user.id}
            )
        except httpx.HTTPStatusError as e:
            # RC returns an HTTP 400 (not success:false in a 200) when the
            # user is already a member — the exact same behavior
            # tests/e2e/rc_client.py.invite_to_channel() already works
            # around ("RC returns error if already a member — safe to
            # ignore"). Without this, re-running a seed script against a
            # user already in the channel would fail here even though the
            # requested end state already holds — the read-back check right
            # below still confirms membership either way.
            if e.response.status_code == 400 and "already" in e.response.text.lower():
                result = {"success": True}
            else:
                raise
        if not result.get("success"):
            raise VerificationError(
                f"Adding '{username}' to channel '{channel_name}' failed: "
                f"{result.get('error', result)}"
            )

        members_endpoint = "groups.members" if channel.is_private else "channels.members"
        with readback_after_write(f"Rocket.Chat reported '{username}' added to channel '{channel_name}'"):
            # user.username, NOT the caller's input: the invite above is keyed
            # on the immutable user.id, so it succeeds even when the string the
            # caller passed isn't the account's canonical username (an account
            # renamed between the lookup and here, or an id passed where a
            # username was expected). Matching the member list against the raw
            # input would then report a VerificationError for a membership that
            # was in fact created. Same principle as matching every address in
            # AdminUser.emails rather than emails[0].
            is_member = await self._is_channel_member(
                members_endpoint, channel.id, user.username
            )
        if not is_member:
            raise VerificationError(
                f"Added '{username}' to channel '{channel_name}' but a "
                "read-back membership check did not find them in the member list."
            )

    async def delete_user(self, username: str) -> None:
        """Hard-delete a user account (unlike Mattermost, which soft-deletes)."""
        user = await self._get_user_or_none(username)
        if user is None:
            raise UserNotFoundError(f"Rocket.Chat user '{username}' not found")

        result = await self._rest._request(
            "POST", "users.delete", json_data={"userId": user.id}
        )
        if not result.get("success"):
            raise VerificationError(
                f"Rocket.Chat user deletion for '{username}' failed: {result.get('error', result)}"
            )

        with readback_after_write(f"Rocket.Chat reported user '{username}' deleted"):
            still_there = await self._get_user_or_none(username)
        if still_there is not None:
            raise VerificationError(
                f"Deleted user '{username}' but a read-back lookup still finds it."
            )

    async def delete_channel(self, channel_name: str) -> None:
        channel = await self._get_channel_or_none(channel_name)
        if channel is None:
            raise ChannelNotFoundError(f"Rocket.Chat channel '{channel_name}' not found")

        endpoint = "groups.delete" if channel.is_private else "channels.delete"
        result = await self._rest._request(
            "POST", endpoint, json_data={"roomId": channel.id}
        )
        if not result.get("success"):
            raise VerificationError(
                f"Rocket.Chat channel deletion for '{channel_name}' failed: "
                f"{result.get('error', result)}"
            )

        with readback_after_write(f"Rocket.Chat reported channel '{channel_name}' deleted"):
            still_there = await self._get_channel_or_none(channel_name)
        if still_there is not None:
            raise VerificationError(
                f"Deleted channel '{channel_name}' but a read-back lookup still finds it."
            )

    async def _get_user_or_none(self, username: str) -> AdminUser | None:
        try:
            with quiet_expected_error(_rc_rest_logger):
                result = await self._rest._request(
                    "GET", "users.info", params={"username": username}
                )
        except httpx.HTTPStatusError as e:
            if e.response.status_code in _NOT_FOUND_STATUSES:
                return None
            raise
        if not result.get("success"):
            return None
        user = result["user"]
        # RC's `emails` is an array and an account can hold several. Keep ALL
        # of them for identity matching (see AdminUser.matches_email) instead
        # of only emails[0] — a requested address sitting at any other index
        # used to be judged a different identity, turning create_user()'s
        # idempotent skip into a hard failure. Entries without an "address"
        # key are dropped rather than becoming ""; a stray "" would make
        # matches_email() fail open for the whole account.
        raw_emails = user.get("emails") or []
        addresses = tuple(
            entry["address"]
            for entry in raw_emails
            if isinstance(entry, dict) and entry.get("address")
        )
        return AdminUser(
            id=user["_id"],
            username=user["username"],
            # RC admins can deactivate an account without deleting it, and
            # `users.create` itself yields an INACTIVE account when the server
            # has Accounts_ManuallyApproveNewUsers on. Discarding this made
            # both the "already exists" skip and the post-create read-back
            # report success over an account that cannot log in.
            deactivated=user.get("active") is False,
            # First address stays the display/primary one, matching what RC
            # itself presents; matching no longer depends on this choice.
            email=addresses[0] if addresses else "",
            emails=addresses,
        )

    async def _get_channel_or_none(self, name: str) -> AdminChannel | None:
        try:
            with quiet_expected_error(_rc_rest_logger):
                result = await self._rest._request(
                    "GET", "channels.info", params={"roomName": name}
                )
            if result.get("success") and "channel" in result:
                ch = result["channel"]
                return AdminChannel(
                    id=ch["_id"], name=ch.get("name", name), is_private=False,
                    # RC omits `archived` entirely on non-archived rooms, so a
                    # plain .get() (not a truthiness check on a default) is the
                    # correct read here.
                    archived=bool(ch.get("archived")),
                )
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in _NOT_FOUND_STATUSES:
                raise

        try:
            with quiet_expected_error(_rc_rest_logger):
                result = await self._rest._request(
                    "GET", "groups.info", params={"roomName": name}
                )
            if result.get("success") and "group" in result:
                grp = result["group"]
                return AdminChannel(
                    id=grp["_id"], name=grp.get("name", name), is_private=True,
                    archived=bool(grp.get("archived")),
                )
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in _NOT_FOUND_STATUSES:
                raise

        return None

    async def _is_channel_member(self, members_endpoint: str, room_id: str, username: str) -> bool:
        """Paginate through channels.members/groups.members looking for
        ``username``, rather than trusting the default single page.

        RC's default ``count`` for these list endpoints is 50 — a channel
        with more members than that would otherwise make this check miss a
        real member and raise a false VerificationError on a successful
        invite. ``count=0`` is documented to mean "all", but only when the
        server's API_Allow_Infinite_Count setting is enabled, which isn't
        guaranteed — explicit pagination works regardless of that setting.
        """
        offset = 0
        page_size = 100
        while True:
            result = await self._rest._request(
                "GET", members_endpoint,
                params={"roomId": room_id, "offset": offset, "count": page_size},
            )
            members = result.get("members", [])
            if any(m.get("username") == username for m in members):
                return True
            if not members:
                return False
            total = result.get("total", offset + len(members))
            offset += len(members)
            if offset >= total:
                return False
