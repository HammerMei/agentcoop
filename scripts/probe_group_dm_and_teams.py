#!/usr/bin/env python3
"""Group DMs on both platforms, and Mattermost cross-team delivery.

Closes the remaining open items in docs/design/dynamic-watcher-design.md §6.4.

Mattermost cases (`mm`):
  1. group DM (channel type G) — what identifies it? Is `channel_name` usable,
     and does `channel_display_name` stay stable as membership changes?
  2. a channel in a SECOND team the bot belongs to — does it reach the same
     socket, and does `data.team_id` distinguish it? Decides whether the team
     gate is genuinely load-bearing or merely theoretical.

Rocket.Chat cases (`rc`):
  1. group DM (>2 participants) — is it still roomType 'd'? Is `roomName`
     present, absent, or something unusable?

Usage:
    uv run python scripts/probe_group_dm_and_teams.py mm \
        --url https://mm.labpig.com --team lab --team2 lab2 \
        --probe-user probe-bot --probe-password '...' \
        --admin-user glin --admin-password '...' --third-user probe-extra

    uv run python scripts/probe_group_dm_and_teams.py rc \
        --url https://rc.labpig.com \
        --probe-user probe-bot --probe-password '...' \
        --admin-user glin --admin-password '...' --third-user probe-extra
"""

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime

import httpx
import websockets


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {m}", flush=True)


# ────────────────────────────── Mattermost ──────────────────────────────
async def mm_driver(a, ready: asyncio.Event) -> None:
    await ready.wait()
    await asyncio.sleep(1.5)
    c = httpx.AsyncClient(base_url=a.url.rstrip("/") + "/api/v4", timeout=25)
    r = await c.post("users/login", json={"login_id": a.admin_user, "password": a.admin_password})
    r.raise_for_status()
    h = {"Authorization": f"Bearer {r.headers['Token']}"}
    admin_id = r.json()["id"]
    pb = (await c.get(f"users/username/{a.probe_user}", headers=h)).json()["id"]
    tu = (await c.get(f"users/username/{a.third_user}", headers=h)).json()["id"]

    log("CASE 1: group DM (3 members) -> channel type G")
    g = await c.post("channels/group", headers=h, json=[admin_id, pb, tu])
    if g.status_code >= 400:
        log(f"  !! group create failed {g.status_code}: {g.text[:200]}")
    else:
        gid = g.json()["id"]
        log(f"  group channel id={gid} name={g.json().get('name')!r} "
            f"display={g.json().get('display_name')!r}")
        log(f"  -> post: {(await c.post('posts', headers=h, json={'channel_id': gid, 'message': 'GDM-test'})).status_code}")
    await asyncio.sleep(4)

    log(f"CASE 2: channel in SECOND team '{a.team2}' (probe belongs to both teams)")
    t2 = (await c.get(f"teams/name/{a.team2}", headers=h)).json()["id"]
    ch2 = (await c.get(f"teams/{t2}/channels/name/sandbox", headers=h)).json()["id"]
    log(f"  team2={t2} sandbox={ch2}")
    log(f"  -> post: {(await c.post('posts', headers=h, json={'channel_id': ch2, 'message': 'team2-crossteam-test'})).status_code}")
    await asyncio.sleep(4)

    await c.aclose()
    log("driver finished")


async def mm_listener(a, ready: asyncio.Event) -> int:
    c = httpx.AsyncClient(base_url=a.url.rstrip("/") + "/api/v4", timeout=25)
    r = await c.post("users/login", json={"login_id": a.probe_user, "password": a.probe_password})
    r.raise_for_status()
    token = r.headers["Token"]
    await c.aclose()

    ws_url = a.url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/api/v4/websocket"
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"seq": 1, "action": "authentication_challenge",
                                  "data": {"token": token}}))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + a.seconds
        authed = False
        n = 0
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - loop.time()))
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            f = json.loads(raw)
            if not authed and (f.get("event") == "hello" or f.get("status") == "OK"):
                authed = True
                log("authenticated")
                ready.set()
                continue
            if f.get("event") != "posted":
                continue
            n += 1
            d = f.get("data") or {}
            post = json.loads(d.get("post", "{}"))
            print("-" * 74, flush=True)
            log(f"POSTED #{n}  msg={post.get('message')!r}")
            print(f"  channel_type        : {d.get('channel_type')!r}", flush=True)
            print(f"  channel_name        : {d.get('channel_name')!r}", flush=True)
            print(f"  channel_display_name: {d.get('channel_display_name')!r}", flush=True)
            print(f"  data.team_id        : {d.get('team_id')!r}", flush=True)
            print(f"  post.channel_id     : {post.get('channel_id')!r}", flush=True)
        log(f"captured {n} event(s)")
        return 0


# ────────────────────────────── Rocket.Chat ─────────────────────────────
async def rc_driver(a, ready: asyncio.Event) -> None:
    await ready.wait()
    await asyncio.sleep(1.5)
    c = httpx.AsyncClient(base_url=a.url.rstrip("/") + "/api/v1", timeout=25)
    d = (await c.post("login", json={"user": a.admin_user, "password": a.admin_password})).json()["data"]
    h = {"X-Auth-Token": d["authToken"], "X-User-Id": d["userId"]}

    log(f"CASE 1: group DM with {a.probe_user} + {a.third_user} (>2 participants)")
    im = await c.post("im.create", headers=h,
                      json={"usernames": f"{a.probe_user},{a.third_user}"})
    if im.status_code >= 400:
        log(f"  !! im.create failed {im.status_code}: {im.text[:250]}")
    else:
        room = im.json()["room"]
        rid = room.get("_id") or room.get("rid")
        log(f"  group DM rid={rid} t={room.get('t')!r} usernames={room.get('usernames')}")
        p = await c.post("chat.postMessage", headers=h,
                         json={"roomId": rid, "text": "RC-GDM-test"})
        log(f"  -> post: {p.status_code}")
    await asyncio.sleep(5)
    await c.aclose()
    log("driver finished")


async def rc_listener(a, ready: asyncio.Event) -> int:
    ws_url = a.url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/websocket"
    async with websockets.connect(ws_url) as ws:
        async def send(o):
            await ws.send(json.dumps(o))
        await send({"msg": "connect", "version": "1", "support": ["1"]})
        while json.loads(await ws.recv()).get("msg") != "connected":
            pass
        await send({"msg": "method", "method": "login", "id": "l1",
                    "params": [{"user": {"username": a.probe_user},
                                "password": {"digest": hashlib.sha256(a.probe_password.encode()).hexdigest(),
                                             "algorithm": "sha-256"}}]})
        while True:
            f = json.loads(await ws.recv())
            if f.get("msg") == "result" and f.get("id") == "l1":
                if f.get("error"):
                    log(f"LOGIN FAILED {f['error']}")
                    return 1
                break
        await send({"msg": "sub", "id": "s1", "name": "stream-room-messages",
                    "params": ["__my_messages__", False]})

        loop = asyncio.get_running_loop()
        deadline = loop.time() + a.seconds
        n = 0
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - loop.time()))
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            f = json.loads(raw)
            m = f.get("msg")
            if m == "ping":
                await send({"msg": "pong"}); continue
            if m == "nosub":
                log(f"NOSUB {f}"); return 2
            if m == "ready":
                log("READY"); ready.set(); continue
            if m != "changed":
                continue
            n += 1
            fl = f.get("fields") or {}
            args = fl.get("args") or []
            m0 = args[0] if args and isinstance(args[0], dict) else {}
            allowed = args[1] if len(args) > 1 else None
            print("-" * 74, flush=True)
            log(f"CHANGED #{n}  msg={str(m0.get('msg'))[:40]!r}")
            print(f"  rid     : {m0.get('rid')!r}", flush=True)
            print(f"  allowed : {json.dumps(allowed)}", flush=True)
            print(f"  allowed keys: {sorted(allowed.keys()) if isinstance(allowed, dict) else None}", flush=True)
        log(f"captured {n} frame(s)")
        return 0


async def main_async(a) -> int:
    ready = asyncio.Event()
    if a.platform == "mm":
        t = asyncio.create_task(mm_driver(a, ready))
        rc = await mm_listener(a, ready)
    else:
        t = asyncio.create_task(rc_driver(a, ready))
        rc = await rc_listener(a, ready)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    return rc


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("platform", choices=["mm", "rc"])
    p.add_argument("--url", required=True)
    p.add_argument("--team", default="lab")
    p.add_argument("--team2", default="lab2")
    p.add_argument("--probe-user", required=True)
    p.add_argument("--probe-password", required=True)
    p.add_argument("--admin-user", required=True)
    p.add_argument("--admin-password", required=True)
    p.add_argument("--third-user", default="probe-extra")
    p.add_argument("--seconds", type=int, default=35)
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
