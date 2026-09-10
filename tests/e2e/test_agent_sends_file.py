"""E2E Test: Agent sends a file back to the user.

Runs twice via e2e_room fixture:
  test_agent_sends_file[dm]      → DM room → OpenCode agent
  test_agent_sends_file[channel] → #acg-e2e-claude → Claude Code agent

The agent is instructed to:
  1. Create a text file with known content.
  2. Send it using the coop send CLI command.

Success criterion: a message with a file attachment appears in the room
from the bot account.
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from rc_client import RCClient

BOT_USERNAME = "acg_bot"


@pytest.mark.e2e
def test_agent_sends_file(
    acg: None,
    test_client: RCClient,
    e2e_room: dict[str, Any],
    rc_setup: dict[str, Any],
) -> None:
    """Agent creates a file and sends it to the room.

    Note: OpenCode (DM variant) is skipped — its free API is less reliable
    for multi-step CLI tasks (write file + run gateway send) within CI timeouts.
    Claude Code (channel variant) covers the full functionality.
    """
    if e2e_room["agent"] == "opencode":
        pytest.skip(
            "test_agent_sends_file skipped for OpenCode: "
            "the free API is unreliable for multi-step CLI tasks within CI timeouts. "
            "Claude Code (channel) covers the agent-sends-file functionality."
        )

    before_ts = int(time.time() * 1000)

    # Tell the agent which room to send to
    if e2e_room["type"] == "dm":
        room_ref = f"@{rc_setup['test_user_username']}"
    else:
        room_ref = f"#{e2e_room['name']}"

    prompt = (
        e2e_room["mention_prefix"]
        + f"Please create a text file named 'agent_output.txt' with the content "
        f"'hello from agent' and then send it to the room {room_ref} using the "
        f"coop send command with the --attach flag."
    )
    test_client.post_message(e2e_room["id"], prompt)

    # Wait for a message with an attachment in the room
    # (longer timeout: agent needs to write file + run CLI send)
    msg = test_client.poll_for_attachment(
        e2e_room["id"],
        before_ts,
        timeout=180,
        room_type=e2e_room["type"],
    )

    # The attachment message may come from acg_bot or from the gateway send command
    assert msg.get("file") or msg.get("attachments"), (
        f"Expected a file attachment but got: {msg}"
    )
