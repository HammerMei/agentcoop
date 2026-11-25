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
from coop.room.model import Reply, Upload  # noqa: E402
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
            assert posts[-1].text == f"@{mm_setup['bot_username']} {marker}"  # delivered as written
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


async def test_a_send_with_a_file_uploads_it_to_mattermost_and_a_read_fetches_it_back(
    mm_setup, mm_test_client: MMClient, mm_bot_client: MMClient, tmp_path
):
    """The simple form of ``POST /files`` (the file as the body, ``Content-Length`` set)
    against the real server, then the streamed ``GET /files/{id}`` through the handle
    a read printed (design §3.3)."""
    config = load(_config(tmp_path, mm_setup, mm_bot_client.token))
    service = Service(config, tmp_path / "state", make_connector)
    await service.start()
    try:
        room = f"lab:{mm_setup['member_channel']}"
        assert room in service.rooms, service.notes
        marker = f"coop e2e file {int(time.time())}"
        payload = (marker.encode() + b"\n") * 20_000  # a few hundred KB, streamed in chunks both ways
        async with HttpDoorbell(f"http://{service.listening[0]}:{service.listening[1]}", "e2e-agent-token") as agent:
            sent = await agent.send(Reply(thread=None, to=[mm_setup["test_user_username"]], text=marker,
                                          files=(Upload.of("notes.txt", payload, "text/plain"),)), room=room)
            [found] = sent.post.attachments
            # the server normalises the type (``text/plain; charset=utf-8`` on 11.7)
            assert (found.name, found.size) == ("notes.txt", len(payload)) and found.content_type.startswith("text/plain")
            assert found.id.startswith("a") and "-" in found.id  # the handle, never the platform's file id
            name, data = await agent.attachment(found.id)
            assert name == "notes.txt" and data == payload
            alone = await agent.send(Reply(thread=sent.post.thread, files=(Upload.of("alone.txt", b"no caption"),)),
                                     room=room)  # a file alone: an empty message with a file, which the server takes
            assert alone.post.text == "" and [a.name for a in alone.post.attachments] == ["alone.txt"]
        posted = mm_test_client.poll_for_message(mm_setup["member_channel_id"], 0,
                                                 lambda p: marker in p.get("message", ""), timeout=20)
        assert posted["user_id"] == mm_setup["bot_user_id"]
        assert [f["name"] for f in posted["metadata"]["files"]] == ["notes.txt"]
    finally:
        await service.stop()


async def test_a_person_who_opens_a_dm_with_the_bot_and_types_at_once_is_answered(
    mm_setup, mm_admin_client: MMClient, mm_bot_client: MMClient, tmp_path
):
    """Runtime DM discovery: the join arrives as ``direct_added`` (no ``user_added``), the
    first post lands while the room opens, and a guest's DM post wakes the agent."""
    stamp = int(time.time())
    person_name = f"e2e_dm_{stamp}"
    person = mm_admin_client.create_user(person_name, "e2e_dm_2024")
    mm_admin_client.add_team_member(mm_setup["team_id"], person["id"])
    person_client = MMClient(mm_setup["mm_url"]).login(person_name, "e2e_dm_2024")
    text = _config(tmp_path, mm_setup, mm_bot_client.token).read_text()
    (tmp_path / "config.yaml").write_text(text.replace(f'"lab:{mm_setup["member_channel"]}":',
                                                       f'"lab:dm:{mm_setup["bot_username"]}:*":'))
    config = load(tmp_path / "config.yaml")
    service = Service(config, tmp_path / "state", make_connector)
    await service.start()
    try:
        handle = f"lab:dm:{mm_setup['bot_username']}:{person_name}"
        assert handle not in service.rooms
        async with HttpDoorbell(f"http://{service.listening[0]}:{service.listening[1]}", "e2e-agent-token") as agent:
            poll = asyncio.create_task(agent.poll(wait=20))
            await asyncio.sleep(0.5)
            dm = person_client.get_dm_channel_id(mm_setup["bot_user_id"])
            first = person_client.post_message(dm, f"hi bot, new dm {stamp}")  # no pause: the open is still running
            wake = await asyncio.wait_for(poll, 30)
            assert wake is not None and wake.room == handle and wake.trigger.post_id == first["id"]
            assert wake.trigger.author == person_name and wake.trigger.author_role == "guest"
            assert await agent.done(wake.wake_id)
        assert service.rooms[handle].core.hosted == [mm_setup["bot_username"]]
    finally:
        await service.stop()
        person_client.close()
