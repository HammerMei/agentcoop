"""Bootstrap the Mattermost side of the E2E environment (idempotent).

Mirrors `setup.py`'s job for Rocket.Chat, and is separate for one structural
reason rather than tidiness: **Mattermost has no admin-bootstrap environment
variables.** Rocket.Chat takes `ADMIN_USERNAME`/`ADMIN_PASS` and creates the
account on first boot; Mattermost does not, and instead makes the FIRST user
created over the API the system admin. So the admin account has to be created
here, in order, before anything that needs admin rights.

Steps:
  1. Wait for the server.
  2. Create the system admin (first user wins) — or log in if it exists.
  3. Assert the admin really holds `system_admin`. Separate from step 2 on
     purpose: the role goes to the FIRST user created, and step 2 skips
     creation when the account already exists, so on a warm database this is
     the only thing standing between a plain-member "admin" and a suite whose
     membership preconditions all pass vacuously.
  4. Create the team. Channels are team-scoped (design §6.3), so everything
     below hangs off it.
  5. Create the bot account AgentCoop logs in as, and the human test account.
  6. Create the member channel, with bot + test user in it.
  7. Create the "outside" channel — the human test user joins it, the bot
     does NOT. This one exists for design §6.2: a public channel is READABLE
     by a non-member, and the finding the MM router depends on is that a post
     there produces no websocket event at all. A channel the bot can read but
     has not joined is the only way to test that.

     The test user has to be a member for two reasons, and both matter.
     Mattermost refuses a post from a non-member (so somebody has to be in
     there to post at all), and — the load-bearing one — the poster must be
     an ALLOW-LISTED user. Posting as the admin instead would work at the
     Mattermost level and quietly ruin the test: `mmadmin` is not in
     `allowed_users.owners`, so a missing reply would be explained just as
     well by the sender filter as by the missing event, and the test could
     not tell which. With the test user posting, the only difference between
     this channel and the member channel is the BOT's membership row.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.parse import urlparse

from mm_client import MMClient

MM_URL = "http://localhost:8065"

TEAM_NAME = "acg-e2e"

ADMIN_USERNAME = "mmadmin"
ADMIN_PASSWORD = "mmadmin_e2e_2024"

BOT_USERNAME = "acg_bot"
BOT_PASSWORD = "acg_bot_e2e_2024"

TEST_USER_USERNAME = "test_user"
TEST_USER_PASSWORD = "test_user_e2e_2024"

# Bot + test user are members here.
MEMBER_CHANNEL = "acg-e2e-mm-claude"
# The human test user joins this one; the bot deliberately does not.
# See the module docstring's step 6 — who is in here IS the test's premise.
OUTSIDE_CHANNEL = "acg-e2e-mm-outside"


def setup(mm_url: str = MM_URL) -> dict[str, Any]:
    print(f"[mm-setup] Waiting for Mattermost at {mm_url} ...", flush=True)
    MMClient.wait_for_mm(mm_url)
    print("[mm-setup] Mattermost is up.", flush=True)

    with MMClient(mm_url) as admin:
        # ── System admin: the first user created wins the role ───────────────
        try:
            admin.login(ADMIN_USERNAME, ADMIN_PASSWORD)
            print(f"[mm-setup] Admin '{ADMIN_USERNAME}' already exists — logged in.", flush=True)
        except Exception as exc:
            # Keep the reason. On a warm platform volume where the account
            # exists with a DIFFERENT password, swallowing this 401 sends the
            # run into create_user, which then fails with "username already
            # exists" — an error about account creation for a problem that is
            # about the password.
            print(
                f"[mm-setup] Admin login failed ({type(exc).__name__}: {exc}) — "
                "treating the account as absent and creating it.",
                flush=True,
            )
            admin.create_user(ADMIN_USERNAME, ADMIN_PASSWORD)
            admin.login(ADMIN_USERNAME, ADMIN_PASSWORD)

        # Assert the role rather than trust the ordering that grants it.
        # `system_admin` goes to the FIRST user created on a fresh server, and
        # the branch above SKIPS creation when the account exists — so on a
        # warm database where something else was created first, this account is
        # a plain member and every later step degrades instead of failing:
        # `get_team` maps 403 to None and would try to create the team, and
        # every membership question comes back 403. The membership E2E test
        # asserts a NON-membership as a precondition, so a 403-as-no would let
        # it pass without establishing its premise.
        roles = admin.roles or ""
        if "system_admin" not in roles.split():
            raise RuntimeError(
                f"'{ADMIN_USERNAME}' is not a system admin (roles: {roles!r}). "
                "Mattermost grants that only to the first user created on a "
                "fresh server, so something else was created first. Reset the "
                "platform data — 'make e2e-down' takes -v — and re-run, or "
                "promote the account by hand with mmctl."
            )

        # ── Team ────────────────────────────────────────────────────────────
        team = admin.get_team(TEAM_NAME)
        if team is None:
            print(f"[mm-setup] Creating team '{TEAM_NAME}' ...", flush=True)
            team = admin.create_team(TEAM_NAME)
        else:
            print(f"[mm-setup] Team '{TEAM_NAME}' exists.", flush=True)
        team_id = team["id"]

        # ── Accounts ────────────────────────────────────────────────────────
        users: dict[str, str] = {}
        for username, password in (
            (BOT_USERNAME, BOT_PASSWORD),
            (TEST_USER_USERNAME, TEST_USER_PASSWORD),
        ):
            existing = admin.get_user(username)
            if existing is None:
                print(f"[mm-setup] Creating user '{username}' ...", flush=True)
                existing = admin.create_user(username, password)
            else:
                print(f"[mm-setup] User '{username}' exists.", flush=True)
            users[username] = existing["id"]
            admin.add_team_member(team_id, existing["id"])

        # ── Member channel: bot + test user ─────────────────────────────────
        channel = admin.get_channel(team_id, MEMBER_CHANNEL)
        if channel is None:
            print(f"[mm-setup] Creating channel '{MEMBER_CHANNEL}' ...", flush=True)
            channel = admin.create_channel(team_id, MEMBER_CHANNEL)
        else:
            print(f"[mm-setup] Channel '{MEMBER_CHANNEL}' exists — ensuring members.", flush=True)
        for username in (BOT_USERNAME, TEST_USER_USERNAME):
            admin.add_channel_member(channel["id"], users[username])

        # ── The "outside" channel, for the §6.2 membership-delivery test ────
        # test_user in, bot deliberately out — see step 7 of the module
        # docstring for why the poster must be the allow-listed human and not
        # the admin.
        outside = admin.get_channel(team_id, OUTSIDE_CHANNEL)
        if outside is None:
            print(
                f"[mm-setup] Creating channel '{OUTSIDE_CHANNEL}' "
                "(test_user joins, bot stays out) ...",
                flush=True,
            )
            outside = admin.create_channel(team_id, OUTSIDE_CHANNEL)
        else:
            print(f"[mm-setup] Channel '{OUTSIDE_CHANNEL}' exists.", flush=True)
        admin.add_channel_member(outside["id"], users[TEST_USER_USERNAME])
        # Belt and braces: if a previous run or a stray click put the bot in
        # here, the membership test would fail for a reason that has nothing
        # to do with the code under test. Take it back out.
        admin.remove_channel_member(outside["id"], users[BOT_USERNAME])

    print("[mm-setup] Done.", flush=True)
    return {
        "mm_url": mm_url,
        "team": TEAM_NAME,
        "team_id": team_id,
        "admin_username": ADMIN_USERNAME,
        "admin_password": ADMIN_PASSWORD,
        "bot_username": BOT_USERNAME,
        "bot_password": BOT_PASSWORD,
        "test_user_username": TEST_USER_USERNAME,
        "test_user_password": TEST_USER_PASSWORD,
        "member_channel": MEMBER_CHANNEL,
        "member_channel_id": channel["id"],
        "outside_channel": OUTSIDE_CHANNEL,
        "outside_channel_id": outside["id"],
        "bot_user_id": users[BOT_USERNAME],
        "test_user_id": users[TEST_USER_USERNAME],
    }


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "mattermost", "host.docker.internal")


def _refuse_non_local(mm_url: str, force: bool) -> None:
    """Refuse to seed a server that does not look like the E2E stack.

    This script creates accounts, a team and channels, and it runs `POST /users`
    against whatever URL it is handed — Mattermost allows that unauthenticated
    when open signup is on. The `system_admin` assertion aborts the run early
    on a foreign server, but only AFTER `mmadmin` has been created there, so the
    guard has to come before the first request rather than rely on that.
    """
    if force:
        return
    host = urlparse(mm_url).hostname or ""
    if host in _LOCAL_HOSTS:
        return
    raise SystemExit(
        f"[mm-setup] Refusing to seed {mm_url!r}: host {host!r} is not one of "
        f"{_LOCAL_HOSTS}.\n"
        "This script CREATES accounts, a team and channels with hardcoded "
        "credentials — it is for the disposable E2E stack, not for a server "
        "anyone relies on. If you really mean it, pass --i-know-this-is-not-the-e2e-stack."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mm-url", default=MM_URL)
    parser.add_argument(
        "--i-know-this-is-not-the-e2e-stack",
        action="store_true",
        dest="force",
        help="Allow seeding a non-local Mattermost (creates accounts — see _refuse_non_local)",
    )
    args = parser.parse_args()
    _refuse_non_local(args.mm_url, args.force)
    try:
        print(json.dumps(setup(args.mm_url), indent=2))
    except Exception as exc:  # noqa: BLE001 — surface the reason, not a traceback wall
        print(f"[mm-setup] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
