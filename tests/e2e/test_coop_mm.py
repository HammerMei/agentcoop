"""The coop daemon against the docker Mattermost: a human mentions the bot, a fake
agent reached over the doorbell reads the wake, replies in the trigger's thread,
and the reply lands in Mattermost. The daemon runs in this process, listening on
a loopback port; the Mattermost side is the e2e compose stack (``make e2e-up``).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from mm_client import MMClient  # noqa: E402

from coop.agent.client import HttpDoorbell  # noqa: E402
from coop.config import load  # noqa: E402
from coop.daemon import make_connector  # noqa: E402
from coop.room.model import Reply  # noqa: E402
from coop.service import Service  # noqa: E402

pytestmark = pytest.mark.e2e


def _config(tmp_path: Path, mm: dict, bot_token: str) -> Path:
    (tmp_path / "bot.token").write_text(bot_token + "\n")
    (tmp_path / "agent.token").write_text("e2e-agent-token\n")
    text = f"""
servers:
  lab:
    type: mattermost
    url: {mm["mm_url"]}
    team: {mm["team"]}
    owners: [{mm["test_user_username"]}]
    bots:
      bot: {{username: {mm["bot_username"]}, token_file: bot.token}}
agents:
  bot: {{type: remote, token_file: agent.token}}
gateway:
  listen: 127.0.0.1:0
rooms:
  "lab:{mm["member_channel"]}":
    agents: [bot]
    classifier_enabled: false
    sweep_interval_seconds: 0
    catch_up_idle_seconds: 0
    lease_seconds: 120
"""
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


async def test_a_mention_wakes_the_agent_over_the_doorbell_and_its_reply_threads_under_the_trigger(
    mm_setup, mm_test_client: MMClient, mm_bot_client: MMClient, tmp_path
):
    config = load(_config(tmp_path, mm_setup, mm_bot_client.token))
    service = Service(config, tmp_path / "state", make_connector)
    await service.start()
    try:
        assert f"lab:{mm_setup['member_channel']}" in service.rooms, service.notes
        marker = f"coop e2e {int(time.time())}"
        async with HttpDoorbell(f"http://{service.listening[0]}:{service.listening[1]}", "e2e-agent-token") as agent:
            poll = asyncio.create_task(agent.poll(wait=20))
            await asyncio.sleep(0.5)
            trigger = mm_test_client.post_message(mm_setup["member_channel_id"],
                                                  f"@{mm_setup['bot_username']} {marker}")
            wake = await asyncio.wait_for(poll, 30)
            assert wake is not None and wake.trigger.post_id == trigger["id"]
            assert wake.trigger.author == mm_setup["test_user_username"] and wake.trigger.author_role == "owner"
            assert wake.starting_thread == trigger["id"]  # a top-level post is its own thread
            _, posts = await agent.read(wake.wake_id)
            assert [p.id for p in posts][-1] == trigger["id"]
            assert posts[-1].text == f"@bot {marker}"  # the bot's username became its agent name
            reply = await agent.send(Reply(thread=None, to=[mm_setup["test_user_username"]],
                                           text=f"got it {marker}", wake_id=wake.wake_id))
            assert reply is not None
            assert await agent.done(wake.wake_id)
        found = mm_test_client.poll_for_message(mm_setup["member_channel_id"], trigger["create_at"],
                                                lambda p: f"got it {marker}" in p.get("message", ""), timeout=20)
        assert found["user_id"] == mm_setup["bot_user_id"]
        assert found["root_id"] == trigger["id"], "the wake's reply goes to the trigger's thread"
        await asyncio.sleep(1.0)
        assert service.gateway.poll("bot") is None  # the echo of the bot's own post wakes nobody
        state = tmp_path / "state" / "lab" / f"{mm_setup['member_channel_id']}.json"
        assert state.exists()
    finally:
        await service.stop()
