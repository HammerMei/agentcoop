"""PlatformAdmin ABC: uniform admin operations over a pluggable RC/MM backend.

Mirrors the Connector ABC pattern (gateway/core/connector.py) deliberately:
a small, generic interface here now pays off later when this logic becomes
the basis for on-demand agent provisioning (create a scoped RC/MM account +
persona, tear it down afterward) — see gateway/admin/__init__.py. Kept
independent of the Connector ABC itself: that one models an already-running
chat session (fetch history, subscribe, post), this one models one-shot
admin operations (create, wire up, delete) — different lifecycles, no
shared behavior worth inheriting.
"""

import contextlib
from abc import ABC, abstractmethod
from dataclasses import dataclass


class AdminError(Exception):
    """Base class for all admin-operation failures raised by this package."""


def emails_match(existing_email: str, requested_email: str) -> bool:
    """Case/whitespace-insensitive comparison used to decide whether an
    "already exists" collision is plausibly the same identity retrying, or
    an unrelated account that happens to share a username.

    Single source of truth for both concrete admins AND the CLI layer,
    specifically so the decision "is this a safe retry" can't drift between
    them — see UserAlreadyExistsError.identity_matches and
    MattermostAdmin.create_user's team-repair gate, both of which must
    agree on the same answer for the same inputs.

    Two deliberate, NOT provably-safe trade-offs, stated honestly rather
    than as guarantees:

    - An empty ``existing_email`` (Rocket.Chat returns "" when an account
      genuinely has no email on file — see RocketChatAdmin._get_user_or_none)
      is treated as a MATCH, not a mismatch. This fails open: a false
      mismatch turns into a hard CLI failure that blocks this tool's own
      primary re-run-a-seed-script workflow, whereas a false match only
      risks the same limited blast radius idempotent retries already carry
      (repairing team membership / reporting a skip). It is NOT proof the
      account is the same identity — an account with no email on record
      could just as easily be genuinely unrelated.
    - Comparison is case/whitespace-normalized because Mattermost is
      understood to normalize stored email server-side — an exact,
      case-sensitive compare would turn a legitimate idempotent retry
      (same account, different input casing) into a false-positive hard
      failure. This has not been empirically verified against a live
      server.
    """
    if not existing_email:
        return True
    return existing_email.strip().lower() == requested_email.strip().lower()


class UserAlreadyExistsError(AdminError):
    """Raised by create_user() when the username is already taken.

    Carries the pre-existing user so callers that want idempotent
    "ensure this user exists" behavior (e.g. re-run seed scripts, or a
    Btrfs restore that lands on a partially-seeded state) can catch this
    specifically and use .existing instead of treating it as a hard failure.

    ``identity_matches`` (computed by each admin via emails_match(), above)
    tells the caller whether the existing account is plausibly the SAME
    identity retrying, or an unrelated account that happens to share a
    username — the CLI uses this to decide whether "already exists" is a
    safe no-op or a real error worth failing on (see gateway/admin/cli.py).
    Defaults to True so constructing this error without specifying it
    doesn't accidentally manufacture a spurious identity-collision failure.
    """

    def __init__(self, username: str, existing: "AdminUser", *, identity_matches: bool = True):
        super().__init__(f"User '{username}' already exists")
        self.username = username
        self.existing = existing
        self.identity_matches = identity_matches


class ChannelAlreadyExistsError(AdminError):
    """Raised by create_channel() when the channel name is already taken.

    Same idempotency rationale as UserAlreadyExistsError.
    """

    def __init__(self, name: str, existing: "AdminChannel"):
        super().__init__(f"Channel '{name}' already exists")
        self.name = name
        self.existing = existing


class UserNotFoundError(AdminError):
    """Raised when an operation references a username that does not exist."""


class UserDeactivatedError(AdminError):
    """Raised by create_user() when the username belongs to an existing but
    DEACTIVATED account, so the requested "an active user exists" state was
    not achieved.

    Deliberately subclasses AdminError **directly, and must never subclass
    UserAlreadyExistsError**, however tempting that reads (it is, literally,
    an already-exists case). gateway/admin/cli.py catches
    UserAlreadyExistsError and prints an idempotent "already exists —
    skipping" with exit 0; if this were a subclass, that handler would swallow
    it and reinstate the exact bug this exists to fix — a CLI reporting
    success for an account that cannot log in.

    Reachable on BOTH platforms, for different reasons:

    - Mattermost, self-inflicted: delete_user() only SOFT-deactivates (DELETE
      /users/{id} sets delete_at rather than removing the row) and the username
      lookup still returns such accounts, so `delete-user bob` then
      `create-user bob ...` while reseeding would otherwise report a clean skip
      over a dead account.
    - Rocket.Chat, out-of-band: delete_user() there hard-deletes, so this state
      only arises from an admin deactivating the account (`active: false`)
      directly. RocketChatAdmin ALSO raises this from its post-create read-back,
      because a server with Accounts_ManuallyApproveNewUsers creates accounts
      inactive — a first-run false success, not merely a re-run concern.

    Does NOT auto-reactivate: silently resurrecting an account someone
    deliberately deactivated is a bigger surprise than failing loudly, and
    reactivation is a distinct intent that deserves a distinct explicit
    action rather than being a side effect of "create".
    """

    def __init__(self, username: str, existing: "AdminUser"):
        super().__init__(
            f"User '{username}' already exists but is deactivated (id={existing.id}) — "
            "the requested active-user state was not achieved. Reactivate it on the "
            "server, or pick a different username."
        )
        self.username = username
        self.existing = existing


class ChannelNotFoundError(AdminError):
    """Raised when an operation references a channel name that does not exist."""


class ChannelArchivedError(AdminError):
    """Raised by create_channel() when the name belongs to an existing but
    ARCHIVED channel, so the requested "a usable channel exists" state was not
    achieved.

    Subclasses AdminError directly and deliberately NOT
    ChannelAlreadyExistsError — for the same reason UserDeactivatedError does
    not subclass UserAlreadyExistsError: gateway/admin/cli.py catches that and
    prints an idempotent "already exists — skipping" with exit 0, so
    subclassing would silently reinstate the bug this exists to report.

    Rocket.Chat only. Archiving is a normal one-click RC admin action, but this
    tool cannot create the state itself (its RC delete_channel hard-deletes),
    so it always originates out-of-band. Mattermost is unaffected: its channel
    lookup hits GET teams/{id}/channels/name/{name}, whose include_deleted
    parameter defaults to false, so an archived MM channel simply 404s and
    create_channel proceeds normally.

    UNCONFIRMED PREMISE, stated plainly: this only ever fires if RC's
    channels.info/groups.info actually RETURN an archived room (carrying
    ``archived: true``) rather than reporting it as not-found. That could not be
    confirmed from RC's public docs, and the behavior was never observed against
    a live server — only against a stubbed transport, which assumes the very
    response shape in question. If RC instead reports archived rooms as
    not-found, this branch is simply unreachable and create_channel proceeds to
    channels.create, which fails loudly on the name collision — an acceptable
    outcome, and the reason shipping this guard is safe either way. Settle it
    with one command against a live server if it ever matters:
    archive a channel, then run `create-channel <that name>`.

    Does NOT auto-unarchive, matching UserDeactivatedError's reasoning:
    silently resurrecting something an admin deliberately archived is a bigger
    surprise than failing loudly.
    """

    def __init__(self, name: str, existing: "AdminChannel"):
        super().__init__(
            f"Channel '{name}' already exists but is archived (id={existing.id}) — "
            "the requested usable-channel state was not achieved. Unarchive it on "
            "the server, or pick a different name."
        )
        self.name = name
        self.existing = existing


class VerificationError(AdminError):
    """Raised when a create/add call reported success but a read-back check
    could not confirm the change actually took effect.

    Exists because Mattermost is known to return a success-shaped response
    from POST /users without creating the account when EnableUserCreation/
    EnableOpenServer are disabled (see MattermostAdmin.create_user) — a
    silent no-op here would be worse than a loud, specific failure, since
    the whole point of this CLI is to be safe to script against (seed
    scripts, onboarding skill).
    """


@dataclass
class AdminUser:
    id: str
    username: str
    # Primary/display address. Used in human-facing messages; identity
    # matching must go through matches_email(), never this field alone.
    email: str
    # Whether the platform reports this account as deactivated/inactive.
    # Defaults False and is keyword-safe for every existing construction site.
    # Populated by BOTH admins: Mattermost from delete_at (its delete_user()
    # soft-deactivates and its lookup still returns such accounts), Rocket.Chat
    # from `active: false`. RC's delete_user() does hard-delete, so a *deleted*
    # RC account stops resolving entirely — but a *deactivated* one still
    # resolves, and RC also creates inactive accounts when the server has
    # Accounts_ManuallyApproveNewUsers. See UserDeactivatedError.
    deactivated: bool = False
    # EVERY address the platform reports for this account, not just the first.
    # Rocket.Chat's users.info returns `emails` as an array and an account can
    # legitimately hold more than one; reading only `emails[0]` meant a
    # requested address registered at any other index was judged a different
    # identity, turning an idempotent skip into a hard failure. Populated by
    # each admin's lookup; __post_init__ guarantees it always contains
    # `email`, so a caller that sets only `email` still matches correctly.
    emails: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.email and self.email not in self.emails:
            self.emails = (self.email, *self.emails)

    def matches_email(self, requested_email: str) -> bool:
        """True if ANY of this account's addresses matches ``requested_email``.

        Traverses the whole collection rather than checking a single address:
        a multi-address account is a normal platform state, not an edge case,
        and picking one index is only correct by luck.

        Fail-open behavior is inherited from emails_match() and unchanged: an
        account with no address on file — or a malformed/empty entry among
        several — counts as a match. See that function for why failing open
        is the deliberate choice here.
        """
        if not self.emails:
            return True
        return any(emails_match(addr, requested_email) for addr in self.emails)


@dataclass
class AdminChannel:
    id: str
    name: str
    is_private: bool
    # Whether the platform reports this channel as archived. Defaults False
    # and is keyword-safe at every existing construction site. Rocket.Chat
    # only — see ChannelArchivedError for why Mattermost cannot reach this
    # state through this tool's lookup.
    archived: bool = False


class PlatformAdmin(ABC):
    """Uniform admin interface, implemented per-platform (RC/MM/...).

    Lifecycle: construct with a profile, ``await connect()`` once, make
    calls, ``await close()`` when done (async context manager support is
    provided via __aenter__/__aexit__ for convenience).
    """

    @abstractmethod
    async def connect(self) -> None:
        """Authenticate and resolve any platform-specific prerequisites
        (e.g. Mattermost's team_id). Must be called before any other method."""

    @abstractmethod
    async def close(self) -> None:
        """Release underlying HTTP client resources."""

    @abstractmethod
    async def create_user(
        self,
        username: str,
        email: str,
        password: str,
        *,
        full_name: str | None = None,
    ) -> AdminUser:
        """Create a user account.

        There is deliberately NO email-verified option. This tool provisions
        *agent* accounts, which have no real inbox behind them, so marking
        their email verified is meaningless — and offering the knob proved to
        be all cost and no benefit: the platforms make it unobservable
        (Mattermost strips ``email_verified`` from any read of another user's
        account, and silently drops it on write unless the caller has
        manage_system), so the CLI could neither guarantee nor honestly
        report the requested state. If a genuine need for a verified mailbox
        ever appears, do it as an explicit, separately-verifiable operation
        rather than a flag on create.

        Raises UserAlreadyExistsError if the username is already taken,
        VerificationError if creation appeared to succeed but a read-back
        check couldn't confirm the account exists.
        """

    @abstractmethod
    async def create_channel(self, name: str, *, is_private: bool = False) -> AdminChannel:
        """Create a channel. Raises ChannelAlreadyExistsError if it exists."""

    @abstractmethod
    async def add_user_to_channel(self, username: str, channel_name: str) -> None:
        """Add an existing user to an existing channel.

        Raises UserNotFoundError / ChannelNotFoundError as appropriate.
        """

    @abstractmethod
    async def delete_user(self, username: str) -> None:
        """Delete (or deactivate, if the platform has no hard delete) a user.

        Raises UserNotFoundError if the username does not exist.
        """

    @abstractmethod
    async def delete_channel(self, channel_name: str) -> None:
        """Delete a channel. Raises ChannelNotFoundError if it does not exist."""

    @abstractmethod
    async def reactivate_user(self, username: str, password: str) -> AdminUser:
        """Re-enable a deactivated account and give it `password` — the inverse
        of `delete_user` on a platform that soft-deletes (coop-keeper design
        §3.10: a bot removed earlier comes back with a new password).

        Raises UserNotFoundError when there is no such account, and AdminError
        when the account is not deactivated — an active account is not
        reactivated and its password is not rotated (rotation is out of
        scope) — or when the platform cannot reactivate at all.
        """

    async def __aenter__(self) -> "PlatformAdmin":
        # If connect() raises, __aenter__ never returns normally, and per
        # the async-context-manager protocol Python will NOT call
        # __aexit__ in that case (it's only invoked once __aenter__ has
        # succeeded). Both concrete admins allocate their httpx.AsyncClient
        # instances in their constructors (before connect() ever runs), so
        # without this, every failed connection attempt through `async
        # with SomeAdmin(profile) as admin:` would leave those clients
        # un-closed.
        #
        # BaseException, not Exception: asyncio.CancelledError and
        # KeyboardInterrupt do not derive from Exception, and a cancelled or
        # Ctrl-C'd connect() is precisely when cleanup is skipped. (Scope,
        # stated honestly: httpcore closes the in-flight socket itself, so
        # no file descriptor actually leaks — what remains is two Python
        # objects never marked closed. This is correctness-of-intent, not a
        # socket leak, and the shipping CLI doesn't use `async with` at all;
        # it matters for the library callers gateway/admin/__init__.py
        # anticipates.)
        #
        # close() is suppressed rather than awaited bare: if it raised while
        # a CancelledError was propagating, that new exception would replace
        # the CancelledError, which breaks cancellation semantics for the
        # caller — asyncio.timeout() would surface the wrong error and
        # task.cancelled() would report False. Cleanup must never outrank the
        # signal that triggered it.
        try:
            await self.connect()
        except BaseException:
            with contextlib.suppress(Exception):
                await self.close()
            raise
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()
