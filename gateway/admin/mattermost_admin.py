"""Mattermost implementation of PlatformAdmin.

Composes a MattermostREST instance rather than extending it — REST stays a
thin transport client (auth, 401-retry, JSON in/out); this class owns admin
business logic (idempotency checks, post-write verification) on top of it.
The handful of admin endpoints not already exposed as public REST methods
(user/channel create, channel membership, delete) are called via
MattermostREST._request directly rather than duplicating its auth-header +
401-retry handling here — see MattermostAdmin.__init__.
"""

import logging

import httpx

from gateway.admin._errors import friendly_error_message, readback_after_write
from gateway.admin._logging import quiet_expected_error
from gateway.admin.base import (
    AdminChannel,
    AdminUser,
    ChannelAlreadyExistsError,
    ChannelNotFoundError,
    PlatformAdmin,
    UserAlreadyExistsError,
    UserDeactivatedError,
    UserNotFoundError,
    VerificationError,
)
from gateway.admin.config import AdminProfile
from gateway.connectors.mattermost.rest import MattermostREST, RoomNotFoundError
from gateway.connectors.mattermost.rest import logger as _mm_rest_logger

logger = logging.getLogger("coop.admin.mattermost")


def _fmt(exc: Exception | None) -> str:
    """Render an exception for embedding in a composed error message.

    HTTP errors go through friendly_error_message() so the platform's own text
    ("Unable to find the existing team", ...) is shown rather than httpx's
    generic "Client error '404 Not Found' for url '...' For more information
    check: https://developer.mozilla.org/..." — which is what a composed
    message would otherwise splice in mid-sentence, since wrapping the error
    means cli._run() never reaches its httpx.HTTPStatusError arm.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return friendly_error_message(exc)
    return str(exc)


class MattermostAdmin(PlatformAdmin):
    def __init__(self, profile: AdminProfile):
        self.profile = profile
        # Prefer a PAT (profile.token) over username/password: it sidesteps
        # the session-token relogin path entirely, and the admin account
        # backing this tool is deliberately a *different*, higher-privilege
        # credential than any bot account a connector uses. This matters
        # concretely for _resolve_team()'s admin-only fallback below (GET
        # teams/name/{team}): MattermostREST.resolve_team's docstring
        # documents that a non-admin bot gets 403 from that same endpoint
        # regardless of team membership — a system-admin-caliber credential
        # is exactly what's needed for that fallback to have a chance of
        # working.
        #
        # When a token is selected, username/password are deliberately NOT
        # passed through even if the profile also sets them (AdminProfile
        # permits both). MattermostREST._is_login_mode only checks whether
        # username+password are present — it has no idea a token was meant
        # to be authoritative — so passing all three would make a 401 from a
        # revoked/expired PAT silently trigger login() with the password
        # instead of failing loudly, defeating PAT revocation as a way to
        # cut this admin tool's access.
        if profile.token:
            self._rest = MattermostREST(server_url=profile.server_url, token=profile.token)
        else:
            self._rest = MattermostREST(
                server_url=profile.server_url,
                username=profile.username or "",
                password=profile.password or "",
            )

    async def connect(self) -> None:
        await self._rest.authenticate()
        await self._rest.get_me()
        # profile.team is required for type=mattermost (enforced in
        # AdminProfile.__post_init__), so this is safe unconditionally.
        await self._resolve_team(self.profile.team)  # type: ignore[arg-type]

    async def _resolve_team(self, team: str) -> None:
        """Resolve profile.team to a team_id, preferring the membership-
        scoped lookup MattermostREST already provides, falling back to an
        admin-visible by-name lookup only when the caller isn't a member.

        Order matters: MattermostREST.resolve_team() (GET /users/me/teams)
        is tried FIRST because it's known-safe for every credential
        AdminProfile accepts today — AdminProfile does NOT require a
        system-admin credential, plain username/password is allowed, and
        resolve_team()'s own docstring (+ its regression test) confirm a
        non-admin gets 403 from GET /teams/name/{name} "regardless of team
        membership." Falling back to that endpoint UNCONDITIONALLY would
        break the currently-working non-admin-but-a-team-member case.

        The fallback only runs when the team isn't among the caller's own
        memberships — the scenario this tool's credential is meant to cover
        (see __init__ above): a system-admin/PAT account administering a
        team it hasn't necessarily joined itself, e.g. provisioning a
        brand-new team's users/channels before anyone (including the admin)
        has joined it.

        KNOWN UNVERIFIED GAP: resolving team_id here does not by itself
        guarantee create_channel()/_ensure_team_member()/
        _get_channel_or_none() (all team-scoped) will actually succeed for
        a non-member admin against a live server — that depends on
        Mattermost's system-admin RBAC bypass actually covering those
        endpoints too, which has not been confirmed against a real server.
        If it doesn't, resolving team_id alone doesn't fully unlock the
        "administer a team you haven't joined" use case this targets — only
        that connect() itself no longer fails immediately.
        """
        try:
            await self._rest.resolve_team(team)
            return
        except RoomNotFoundError as membership_error:
            # `except ... as name` unbinds `name` once this block ends
            # (a standard Python 3 gotcha, to avoid reference cycles) —
            # capture the message now so it's still available below.
            membership_error_msg = str(membership_error)
        # Two admin lookups, not one: MattermostREST.resolve_team() matches
        # profile.team against either a team NAME or a team ID
        # (`if t.get("name") == team or t.get("id") == team`), so a profile
        # legitimately configured with an ID resolves fine while the caller
        # is a member — but would then hit a name-only fallback and fail with
        # a message blaming the team name. Mattermost has no "by name or id"
        # endpoint, so try each in turn.
        #
        # Deliberately NOT gated on an is-this-an-ID heuristic: Mattermost's
        # own NewRandomTeamName() derives team names from NewId(), so ID-
        # shaped names genuinely exist and any such guess would misroute them.
        #
        # Neither call is wrapped in quiet_expected_error: unlike the
        # existence checks elsewhere in this file, a 404 here is a genuine
        # misconfiguration (bad team name/ID, or a non-admin credential),
        # not an expected outcome on the happy path. It should stay loud.
        result = None
        by_name_error: Exception | None = None
        by_id_error: Exception | None = None
        try:
            result = await self._rest._request("GET", f"teams/name/{team}")
            lookup = "by-name"
        except httpx.HTTPStatusError as e:
            by_name_error = e
            try:
                result = await self._rest._request("GET", f"teams/{team}")
                lookup = "by-id"
            except httpx.HTTPStatusError as e2:
                by_id_error = e2

        if result is None:
            # Wording note: keeps the exact substring "admin by-name lookup
            # also failed" that tests assert on, while now also reporting the
            # by-id attempt.
            raise RoomNotFoundError(
                f"Team '{team}' not found among the caller's own teams "
                f"({membership_error_msg}), and the admin by-name lookup "
                f"also failed ({_fmt(by_name_error)}), as did the by-id lookup "
                f"({_fmt(by_id_error)}). Either the team name/ID is wrong, or this "
                "credential isn't a system admin — if it should just be a "
                f"member, add it with `mmctl team add {team} <username>`."
            ) from by_id_error
        self._rest.team_id = result["id"]
        logger.info(
            "Resolved team '%s' -> id=%s (admin %s lookup)", team, self._rest.team_id, lookup
        )

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
                # Checked BEFORE the team-membership repair below: repairing
                # a dead account's team membership accomplishes nothing (it
                # still can't log in), and the whole point is that this must
                # not end in the CLI's idempotent "skipping" + exit 0. Note
                # UserDeactivatedError is deliberately NOT a
                # UserAlreadyExistsError subclass, or cli.py's handler for
                # that would swallow this right back into a false success.
                raise UserDeactivatedError(username, existing=existing)
            # Repair path, not just an idempotency short-circuit: a user can
            # exist but still not be a team member if a PRIOR create_user()
            # call created the account and then failed at the team-join step
            # (see the team-join-failure test below) — reporting that as a
            # plain "already exists" no-op on retry would leave the account
            # permanently stuck outside every one of the team's channels,
            # since nothing else in this tool re-attempts the join for an
            # account that already exists.
            #
            # Only repair when the email also matches, though: a username
            # collision alone isn't proof this is "our" account from a
            # failed prior attempt — it could be an unrelated pre-existing
            # account (typo'd username, stale seed data, etc.), and blindly
            # adding an unrelated identity to the team would grant them
            # access to every one of its channels that they were never
            # meant to have. Matching email is a reasonable proxy for "this
            # is genuinely the same create_user() call, retried." Uses the
            # same AdminUser.matches_email() the CLI's identity_matches
            # check consumes, so the two layers can't disagree about the same
            # inputs. Mattermost reports a single `email` string (no array to
            # traverse), but going through the same helper keeps the two
            # platforms' matching semantics identical.
            matches = existing.matches_email(email)
            if matches:
                await self._ensure_team_member(existing.id)
            raise UserAlreadyExistsError(username, existing=existing, identity_matches=matches)

        # No email_verified field is sent at all: Mattermost defaults it to
        # false, which is what an agent account should be (see
        # PlatformAdmin.create_user for why this tool deliberately offers no
        # option to change it). Sending an explicit `false` would be
        # equivalent but invites someone to "just make it configurable" again.
        payload: dict = {
            "username": username,
            "email": email,
            "password": password,
        }
        if full_name:
            # Mattermost has no single "full name" field — nickname is the
            # closest freeform display field, so that's where this goes
            # rather than guessing a first/last split.
            payload["nickname"] = full_name
        result = await self._rest._request("POST", "users", json_data=payload)
        user_id = result.get("id")
        if not user_id:
            raise VerificationError(
                f"Mattermost user creation for '{username}' returned no id: {result}"
            )

        # Read back rather than trust the response body: a server with
        # EnableUserCreation/EnableOpenServer disabled has been observed to
        # respond in ways that don't reliably reflect a created account
        # (mattermost/mattermost#6644). If it's not actually there, fail
        # loudly instead of returning a user that doesn't exist.
        with readback_after_write(f"Mattermost reported user '{username}' created"):
            created = await self._get_user_or_none(username)
        if created is None:
            raise VerificationError(
                f"Mattermost reported user '{username}' created (id={user_id}) "
                "but a read-back lookup could not find it. Check "
                "EnableUserCreation/EnableOpenServer on the server, or use "
                "`mmctl user create` / `mmctl --local` as a fallback."
            )

        # Add to the profile's team at creation time, not only when the user
        # is later added to a channel: a channel lives inside a team, so a
        # user who isn't a team member yet can't be added to ANY of that
        # team's channels — including by a human doing it manually later,
        # outside this tool. Ensuring team membership up front means a freshly
        # created account is immediately usable, not just usable via
        # add_user_to_channel(). (add_user_to_channel() still does this too,
        # as a safety net for accounts that reached this team from outside —
        # e.g. created directly against the server, not via this tool.)
        await self._ensure_team_member(created.id)
        return created

    async def create_channel(self, name: str, *, is_private: bool = False) -> AdminChannel:
        existing = await self._get_channel_or_none(name)
        if existing is not None:
            raise ChannelAlreadyExistsError(name, existing=existing)

        payload = {
            "team_id": self._rest.team_id,
            "name": name,
            "display_name": name,
            "type": "P" if is_private else "O",
        }
        result = await self._rest._request("POST", "channels", json_data=payload)
        channel_id = result.get("id")
        if not channel_id:
            raise VerificationError(
                f"Mattermost channel creation for '{name}' returned no id: {result}"
            )

        with readback_after_write(f"Mattermost reported channel '{name}' created"):
            verified = await self._get_channel_or_none(name)
        if verified is None:
            raise VerificationError(
                f"Mattermost reported channel '{name}' created (id={channel_id}) "
                "but a read-back lookup could not find it."
            )
        return verified

    async def add_user_to_channel(self, username: str, channel_name: str) -> None:
        user = await self._get_user_or_none(username)
        if user is None:
            raise UserNotFoundError(f"Mattermost user '{username}' not found")
        channel = await self._get_channel_or_none(channel_name)
        if channel is None:
            raise ChannelNotFoundError(f"Mattermost channel '{channel_name}' not found")

        # A channel belongs to a team, and Mattermost rejects adding a user
        # to a channel unless they're already a member of that channel's
        # team — a freshly create_user()'d account is in no team at all, so
        # without this, the create_user -> add_user_to_channel sequence
        # (this tool's primary use case) would fail on Mattermost every time.
        await self._ensure_team_member(user.id)

        await self._rest._request(
            "POST",
            f"channels/{channel.id}/members",
            json_data={"user_id": user.id},
        )

        with readback_after_write(
            f"Mattermost reported '{username}' added to channel '{channel_name}'"
        ):
            await self._rest._request("GET", f"channels/{channel.id}/members/{user.id}")

    async def delete_user(self, username: str) -> None:
        """Deactivate a user. Mattermost has no hard user-delete via the
        standard API — DELETE /users/{id} soft-deletes (sets delete_at)."""
        user = await self._get_user_or_none(username)
        if user is None:
            raise UserNotFoundError(f"Mattermost user '{username}' not found")

        await self._rest._request("DELETE", f"users/{user.id}")

        with readback_after_write(f"Mattermost reported user '{username}' deactivated"):
            result = await self._rest._request("GET", f"users/{user.id}")
        if not result.get("delete_at"):
            raise VerificationError(
                f"Deactivated user '{username}' but a read-back check shows "
                "delete_at is still unset."
            )

    async def delete_channel(self, channel_name: str) -> None:
        """Archive a channel. Mattermost has no hard channel-delete via the
        standard API — DELETE /channels/{id} archives (sets delete_at)."""
        channel = await self._get_channel_or_none(channel_name)
        if channel is None:
            raise ChannelNotFoundError(f"Mattermost channel '{channel_name}' not found")

        await self._rest._request("DELETE", f"channels/{channel.id}")

        with readback_after_write(f"Mattermost reported channel '{channel_name}' archived"):
            result = await self._rest._request("GET", f"channels/{channel.id}")
        if not result.get("delete_at"):
            raise VerificationError(
                f"Archived channel '{channel_name}' but a read-back check "
                "shows delete_at is still unset."
            )

    async def add_user_to_team(self, username: str) -> None:
        """Idempotently ensure ``username`` is a member of the profile's team.

        Public and independently usable (not just an internal step of
        create_user()/add_user_to_channel()) — Mattermost requires team
        membership before a user can be added to ANY channel in that team,
        so this is useful on its own for an account that predates this tool
        or otherwise fell out of the team. MM-only: there's no RC equivalent
        concept, so this isn't part of the PlatformAdmin ABC.

        Raises UserNotFoundError if the username does not exist.
        """
        user = await self._get_user_or_none(username)
        if user is None:
            raise UserNotFoundError(f"Mattermost user '{username}' not found")
        await self._ensure_team_member(user.id)

    async def _ensure_team_member(self, user_id: str) -> None:
        """Idempotently add a user (by id) to the profile's team.

        Internal id-based helper behind add_user_to_team() — used directly
        by create_user()/add_user_to_channel() too, since both already have
        the user's id in hand and calling this instead of add_user_to_team()
        avoids a redundant username lookup.

        Checked first rather than POSTed unconditionally: Mattermost is not
        guaranteed to treat re-adding an existing team member as a no-op, so
        this avoids relying on that being true.
        """
        try:
            with quiet_expected_error(_mm_rest_logger):
                await self._rest._request(
                    "GET", f"teams/{self._rest.team_id}/members/{user_id}"
                )
            return  # already a member
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
        await self._rest._request(
            "POST",
            f"teams/{self._rest.team_id}/members",
            json_data={"team_id": self._rest.team_id, "user_id": user_id},
        )

    async def _get_user_or_none(self, username: str) -> AdminUser | None:
        try:
            with quiet_expected_error(_mm_rest_logger):
                result = await self._rest.get_user_by_username(username)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        return AdminUser(
            id=result["id"],
            username=result["username"],
            email=result.get("email", ""),
            # Mattermost soft-deletes: DELETE /users/{id} sets delete_at, and
            # the username lookup still returns the row afterwards. Retained
            # (rather than discarded, as it was) so create_user() can tell an
            # existing-and-usable account from an existing-but-dead one —
            # see UserDeactivatedError.
            deactivated=bool(result.get("delete_at")),
        )

    async def _get_channel_or_none(self, name: str) -> AdminChannel | None:
        try:
            with quiet_expected_error(_mm_rest_logger):
                result = await self._rest._request(
                    "GET", f"teams/{self._rest.team_id}/channels/name/{name}"
                )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        return AdminChannel(
            id=result["id"], name=result["name"], is_private=result.get("type") == "P"
        )
