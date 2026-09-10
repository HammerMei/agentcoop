"""E2E Test: Built-in task scheduler — fire and verify.

Tests two variants:
  test_schedule_fires[claude]    → watcher rc-e2e:acg-e2e-claude → #acg-e2e-claude
  test_schedule_fires[opencode]  → watcher rc-e2e:dm:test_user   → DM with test_user

Flow:
  1. Create a 1-minute one-shot job via ``docker exec acg-e2e coop
     schedule create``.
  2. Wait up to 90 s for the agent to post the distinctive token in the bound RC room.
  3. Assert the token appeared (proves scheduler fired and agent handled it).
  4. Verify the job shows ``completed`` in ``schedule list --all``.
  5. Clean up: delete the job.

These tests take at least 60 s each — the scheduler polls every 60 s.  They are
marked ``@pytest.mark.slow`` so they can be skipped with ``-m "not slow"`` in CI
pipelines that only want fast feedback.
"""
from __future__ import annotations

import re
import subprocess
import time
import uuid
from typing import Any

import pytest
from rc_client import RCClient

BOT_USERNAME = "acg_bot"
# The connector name in tests/e2e/acg-config/config.yaml. Watcher handles are
# `<connector>:<room label>`, so this is half of every watcher name below.
CONNECTOR_NAME = "rc-e2e"
COOP_CONTAINER = "acg-e2e"

# The scheduler polls every 60 s; allow 90 s for the job to fire + agent to reply.
SCHEDULE_FIRE_TIMEOUT = 90


# ── Helpers ───────────────────────────────────────────────────────────────────


def _docker_exec(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command inside the AgentCoop E2E Docker container and return the result."""
    return subprocess.run(
        ["docker", "exec", COOP_CONTAINER, *cmd],
        capture_output=True,
        text=True,
        check=check,
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(
    scope="function",
    params=["claude", "opencode"],
    ids=["claude", "opencode"],
)
def schedule_room(
    request: pytest.FixtureRequest,
    rc_setup: dict[str, Any],
    test_client: RCClient,
) -> dict[str, Any]:
    """Parameterized fixture — returns watcher + RC room info for the schedule tests.

    claude    → watcher ``rc-e2e:acg-e2e-claude`` → #acg-e2e-claude channel
    opencode  → watcher ``rc-e2e:dm:test_user``   → DM with acg_bot

    **These are WATCHER names, not rule names.** Since the dynamic-watcher
    cutover a ``watchers:`` entry is a *rule* that names no room; the watcher
    it creates per room is handled ``<connector>:<room label>``
    (``gateway/core/watcher_manager.watcher_label``), with ``dm:<counterpart>``
    for a 1:1 DM. ``schedule create`` resolves its argument against the
    PERSISTED WATCHER RECORDS (``control.py``'s ``_find_entry_for_watcher``),
    so passing a rule name — which this fixture did until the cutover was
    reconciled — fails with "Watcher ... not found in any connector". The rule
    names here are ``e2e-claude-channel`` and ``e2e-dm``, and they are
    deliberately NOT what goes below. Pinned by
    tests/unit/test_e2e_watcher_names.py, which runs without the lab.

    A watcher exists only once its room has seen a message, which is already
    guaranteed for both: ``conftest``'s session-scoped ``acg`` fixture warms
    up exactly this DM and this channel before any test runs.

    Returned dict keys:
        watcher:    AgentCoop watcher name to target when creating the job
        room_id:    RC room ``_id`` to poll for the bot reply
        room_type:  ``"channel"`` or ``"dm"`` (for ``poll_for_message``)
        agent:      ``"claude"`` or ``"opencode"``
    """
    if request.param == "claude":
        ch = test_client.get_channel(rc_setup["claude_channel"])
        if ch is None:
            pytest.fail(
                f"Channel '#{rc_setup['claude_channel']}' not found. "
                "Run 'make e2e-up' first."
            )
        return {
            # <connector>:<channel name> — see this fixture's docstring.
            "watcher": f"{CONNECTOR_NAME}:{rc_setup['claude_channel']}",
            "room_id": ch["_id"],
            "room_type": "channel",
            "agent": "claude",
        }
    else:
        room_id = test_client.get_dm_room_id(BOT_USERNAME)
        return {
            # <connector>:dm:<counterpart> — the counterpart is the human,
            # never the bot.
            "watcher": f"{CONNECTOR_NAME}:dm:{rc_setup['test_user_username']}",
            "room_id": room_id,
            "room_type": "dm",
            "agent": "opencode",
        }


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.slow
def test_schedule_fires(
    acg: None,
    test_client: RCClient,
    schedule_room: dict[str, Any],
) -> None:
    """Scheduler fires a 1-minute one-shot job and the agent echoes the token.

    The injected message asks the agent to respond with a unique token.
    The test polls the bound RC room for a bot reply containing that token,
    then verifies the job is marked ``completed``.
    """
    # Unique token — avoids false positives from stale replies in the same room.
    token = f"sched_{uuid.uuid4().hex[:8]}"
    message = f"respond with exactly the single word '{token}'"
    watcher = schedule_room["watcher"]
    room_id = schedule_room["room_id"]
    room_type = schedule_room["room_type"]

    # ── 1. Create the one-shot job ─────────────────────────────────────────────
    before_ts = int(time.time() * 1000)

    create_result = _docker_exec(
        "coop",
        "schedule",
        "create",
        watcher,
        message,
        "--every",
        "1m",
        "--times",
        "1",
    )
    assert create_result.returncode == 0, (
        f"'schedule create' failed (rc={create_result.returncode}):\n"
        f"stdout: {create_result.stdout}\n"
        f"stderr: {create_result.stderr}"
    )

    # Extract job ID from first output line:
    # "Scheduled job created: acg-xxxxxxxx"
    job_id: str | None = None
    for line in create_result.stdout.splitlines():
        match = re.match(r"Scheduled job created:\s+(acg-\w+)", line)
        if match:
            job_id = match.group(1)
            break
    assert job_id, (
        f"Could not parse job ID from 'schedule create' output:\n{create_result.stdout}"
    )

    print(
        f"\n[schedule_e2e] Created job {job_id} → watcher={watcher!r} "
        f"agent={schedule_room['agent']} token={token!r}",
        flush=True,
    )

    # ── 2. Wait for the agent to post the token in the RC room ────────────────
    try:
        bot_msg = test_client.poll_for_message(
            room_id,
            before_ts,
            predicate=lambda m: (
                m["u"]["username"] == BOT_USERNAME and token in m["msg"]
            ),
            timeout=SCHEDULE_FIRE_TIMEOUT,
            room_type=room_type,
        )
    except Exception as exc:
        # Fetch the job list for debugging context before failing.
        list_result = _docker_exec(
            "coop", "schedule", "list", "--all", check=False
        )
        pytest.fail(
            f"Timed out waiting for scheduled job to fire (job_id={job_id}).\n"
            f"Token: {token!r}\n"
            f"Schedule list output:\n{list_result.stdout}\n"
            f"Original error: {exc}"
        )
    else:
        assert token in bot_msg["msg"], (
            f"Expected token {token!r} in bot reply, got: {bot_msg['msg']!r}"
        )

    # ── 3. Verify job is marked completed ─────────────────────────────────────
    list_result = _docker_exec("coop", "schedule", "list", "--all")
    assert list_result.returncode == 0, (
        f"'schedule list --all' failed:\n{list_result.stderr}"
    )
    assert job_id in list_result.stdout, (
        f"Job {job_id!r} not found in 'schedule list --all' output:\n"
        f"{list_result.stdout}"
    )
    # Find the row for this specific job and confirm the status column is "completed".
    job_row: str | None = None
    for row in list_result.stdout.splitlines():
        if job_id in row:
            job_row = row
            break
    assert job_row is not None, f"Row for job {job_id!r} missing from list output."
    assert "completed" in job_row, (
        f"Expected status 'completed' in job row, got:\n  {job_row}"
    )

    # ── 4. Clean up ────────────────────────────────────────────────────────────
    _docker_exec("coop", "schedule", "delete", job_id, check=False)
