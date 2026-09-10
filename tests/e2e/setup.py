"""E2E Setup Script — idempotent RC account & channel creation.

Creates the RC accounts and channels needed for E2E tests.
Safe to run multiple times (skips already-existing resources).

Usage:
    python tests/e2e/setup.py [--rc-url http://localhost:3100]

Also importable:
    from tests.e2e.setup import setup
    info = setup()

Fixed credentials (test-only, safe to commit):
    Admin:     admin / admin_e2e_2024
    Bot:       acg_bot / acg_bot_e2e_2024
    Test user: test_user / test_user_e2e_2024
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as standalone script OR importing from conftest
sys.path.insert(0, str(Path(__file__).parent))
from rc_client import RCClient

# ── Fixed E2E credentials (test-only, not production secrets) ────────────────

RC_URL = "http://localhost:3100"

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin_e2e_2024"

BOT_USERNAME = "acg_bot"
BOT_PASSWORD = "acg_bot_e2e_2024"
BOT_EMAIL = "acg_bot@e2e.local"
BOT_NAME = "AgentCoop Bot"

TEST_USER_USERNAME = "test_user"
TEST_USER_PASSWORD = "test_user_e2e_2024"
TEST_USER_EMAIL = "test_user@e2e.local"
TEST_USER_NAME = "E2E Test User"

CLAUDE_CHANNEL = "acg-e2e-claude"
PERMISSION_CHANNEL = "acg-e2e-permission"
OPENCODE_PERMISSION_CHANNEL = "acg-e2e-opencode-permission"
# Admin-only on purpose: the RC probe needs a room the probe user is NOT a
# member of (see the setup step that creates it).
OUTSIDE_CHANNEL = "acg-e2e-outside"


def setup(rc_url: str = RC_URL) -> dict:
    """Perform full E2E setup. Returns config dict consumed by pytest fixtures.

    Steps:
    1. Wait for RC to be healthy.
    2. Login as admin.
    3. Create bot user (if absent).
    4. Create test user (if absent).
    5. Create #acg-e2e-claude channel (if absent) + invite bot + test_user.
    6. Create #acg-e2e-permission channel (if absent) + invite bot + test_user.
    """
    print(f"[setup] Waiting for RC at {rc_url} ...", flush=True)
    RCClient.wait_for_rc(rc_url, timeout=300)
    print("[setup] RC is healthy.", flush=True)

    with RCClient(rc_url) as admin:
        admin.login(ADMIN_USERNAME, ADMIN_PASSWORD)

        # ── Disable 2FA (required so bot can login without email verification) ─
        print("[setup] Disabling 2FA ...", flush=True)
        admin.set_setting("Accounts_TwoFactorAuthentication_Enabled", False)

        # ── Disable API rate limiter (avoids 429 during tests) ───────────────
        print("[setup] Disabling API rate limiter ...", flush=True)
        admin.set_setting("API_Enable_Rate_Limiter", False)

        # ── Bot account ───────────────────────────────────────────────────────
        if not admin.user_exists(BOT_USERNAME):
            print(f"[setup] Creating bot user '{BOT_USERNAME}' ...", flush=True)
            admin.create_user(BOT_USERNAME, BOT_PASSWORD, BOT_EMAIL, BOT_NAME)
        else:
            print(f"[setup] Bot '{BOT_USERNAME}' already exists — skipping.", flush=True)

        # ── Test user ─────────────────────────────────────────────────────────
        if not admin.user_exists(TEST_USER_USERNAME):
            print(
                f"[setup] Creating test user '{TEST_USER_USERNAME}' ...", flush=True
            )
            admin.create_user(
                TEST_USER_USERNAME,
                TEST_USER_PASSWORD,
                TEST_USER_EMAIL,
                TEST_USER_NAME,
            )
        else:
            print(
                f"[setup] Test user '{TEST_USER_USERNAME}' already exists — skipping.",
                flush=True,
            )

        bot_info = admin.get_user(BOT_USERNAME)
        test_info = admin.get_user(TEST_USER_USERNAME)
        bot_id = bot_info["_id"] if bot_info else None
        test_id = test_info["_id"] if test_info else None

        # ── Channels ──────────────────────────────────────────────────────────
        for channel_name in [CLAUDE_CHANNEL, PERMISSION_CHANNEL, OPENCODE_PERMISSION_CHANNEL]:
            ch = admin.get_channel(channel_name)
            if ch is None:
                print(
                    f"[setup] Creating channel '#{channel_name}' ...", flush=True
                )
                ch = admin.create_channel(
                    channel_name,
                    members=[BOT_USERNAME, TEST_USER_USERNAME],
                )
            else:
                print(
                    f"[setup] Channel '#{channel_name}' exists — ensuring members.",
                    flush=True,
                )
                # Invite is idempotent — safe to call even if already a member
                if bot_id:
                    admin.invite_to_channel(ch["_id"], bot_id)
                if test_id:
                    admin.invite_to_channel(ch["_id"], test_id)

        # ── Admin-only channel, for scripts/probe_a1_rc.py ──────────────────
        # The probe needs a room the probe user is NOT in, to check that the
        # subscribe-all stream reports `roomParticipant: false` for a room the
        # account can merely read (design §6.1 — the membership gate the
        # router depends on). Every other channel here has test_user invited,
        # so none of them can play that part.
        if admin.get_channel(OUTSIDE_CHANNEL) is None:
            print(f"[setup] Creating channel '#{OUTSIDE_CHANNEL}' (admin only) ...", flush=True)
            admin.create_channel(OUTSIDE_CHANNEL, members=[])

    print("[setup] Done.", flush=True)

    return {
        "rc_url": rc_url,
        "admin_username": ADMIN_USERNAME,
        "admin_password": ADMIN_PASSWORD,
        "bot_username": BOT_USERNAME,
        "bot_password": BOT_PASSWORD,
        "test_user_username": TEST_USER_USERNAME,
        "test_user_password": TEST_USER_PASSWORD,
        "claude_channel": CLAUDE_CHANNEL,
        "permission_channel": PERMISSION_CHANNEL,
        "opencode_permission_channel": OPENCODE_PERMISSION_CHANNEL,
        "outside_channel": OUTSIDE_CHANNEL,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Idempotent E2E setup: create RC accounts and channels."
    )
    parser.add_argument(
        "--rc-url",
        default=RC_URL,
        help=f"Rocket.Chat URL (default: {RC_URL})",
    )
    args = parser.parse_args()

    result = setup(args.rc_url)
    print(json.dumps(result, indent=2))
