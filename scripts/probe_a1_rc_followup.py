#!/usr/bin/env python3
"""A1 follow-up: the three cases the first run left open.

  5. a REAL system message in a room the probe user IS in (the first run's
     attempt was void — the inviter was already a member, so no system
     message was generated). Decides whether a `t`-field filter is needed
     on the live path under subscribe-all.
  6. a DM sent to the probe user — does __my_messages__ deliver it, and what
     do roomType/roomName look like for a room that has no name?
  7. the same subscription with the second `sub` param set to true (RC's own
     Go SDK sends true; AgentCoop sends false) — does anything observable change?

Usage:
    uv run python scripts/probe_a1_rc_followup.py --url https://rc.labpig.com \
        --probe-user probe-bot --probe-password '...' \
        --admin-user glin --admin-password '...' \
        --member-room sandbox --extra-user probe-extra --added-event true
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


async def driver(url, admin_user, admin_pw, member_room, extra_user, probe_user, ready):
    await ready.wait()
    await asyncio.sleep(1.5)
    c = httpx.AsyncClient(base_url=url.rstrip("/") + "/api/v1", timeout=20)
    d = (await c.post("login", json={"user": admin_user, "password": admin_pw})).json()["data"]
    h = {"X-Auth-Token": d["authToken"], "X-User-Id": d["userId"]}

    # ── case 5: real system message in the probe user's room ─────────────
    log(f"CASE 5: add {extra_user} to #{member_room} (system message in a room probe IS in)")
    gi = await c.get("groups.info", headers=h, params={"roomName": member_room})
    if gi.status_code >= 400:
        gi = await c.get("channels.info", headers=h, params={"roomName": member_room})
        rid = gi.json()["channel"]["_id"]; invite_ep = "channels.invite"; kick_ep = "channels.kick"
    else:
        rid = gi.json()["group"]["_id"]; invite_ep = "groups.invite"; kick_ep = "groups.kick"
    ui = await c.get("users.info", headers=h, params={"username": extra_user})
    uid = ui.json()["user"]["_id"]
    # Remove first so the add definitely generates a fresh system message.
    await c.post(kick_ep, headers=h, json={"roomId": rid, "userId": uid})
    await asyncio.sleep(2)
    r = await c.post(invite_ep, headers=h, json={"roomId": rid, "userId": uid})
    log(f"  -> {invite_ep}: {r.status_code}")
    await asyncio.sleep(4)

    # ── case 6: DM to the probe user ─────────────────────────────────────
    log(f"CASE 6: DM {probe_user} from {admin_user}")
    r = await c.post("chat.postMessage", headers=h,
                     json={"channel": f"@{probe_user}", "text": "A1 case6 direct-message"})
    log(f"  -> DM post: {r.status_code} {r.text[:120] if r.status_code >= 400 else ''}")
    await asyncio.sleep(4)

    await c.aclose()
    log("driver finished")


async def listener(url, user, password, added_event, ready, seconds):
    ws_url = url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/websocket"
    async with websockets.connect(ws_url) as ws:
        async def send(o):
            await ws.send(json.dumps(o))
        await send({"msg": "connect", "version": "1", "support": ["1"]})
        while json.loads(await ws.recv()).get("msg") != "connected":
            pass
        await send({"msg": "method", "method": "login", "id": "l1",
                    "params": [{"user": {"username": user},
                                "password": {"digest": hashlib.sha256(password.encode()).hexdigest(),
                                             "algorithm": "sha-256"}}]})
        while True:
            f = json.loads(await ws.recv())
            if f.get("msg") == "result" and f.get("id") == "l1":
                if f.get("error"):
                    log(f"LOGIN FAILED {f['error']}"); return 1
                break
        sub = {"msg": "sub", "id": "s1", "name": "stream-room-messages",
               "params": ["__my_messages__", added_event]}
        log(f"SUB (added_event={added_event}) -> {json.dumps(sub)}")
        await send(sub)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
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
                log(f"NOSUB: {f}"); return 2
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
            log(f"CHANGED #{n}")
            print(f"  eventName : {fl.get('eventName')!r}", flush=True)
            print(f"  nargs     : {len(args)}", flush=True)
            print(f"  rid       : {m0.get('rid')!r}", flush=True)
            print(f"  t (system): {m0.get('t')!r}  <-- non-None means SYSTEM MESSAGE", flush=True)
            print(f"  from      : {(m0.get('u') or {}).get('username')!r}", flush=True)
            print(f"  msg       : {str(m0.get('msg'))[:60]!r}", flush=True)
            print(f"  allowed   : {json.dumps(allowed)}", flush=True)
            print(f"  RAW: {json.dumps(f)[:900]}", flush=True)
        log(f"captured {n} frame(s)")
        return 0


async def main_async(a):
    ready = asyncio.Event()
    t = asyncio.create_task(driver(a.url, a.admin_user, a.admin_password,
                                   a.member_room, a.extra_user, a.probe_user, ready))
    rc = await listener(a.url, a.probe_user, a.probe_password,
                        a.added_event == "true", ready, a.seconds)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    return rc


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--probe-user", required=True)
    p.add_argument("--probe-password", required=True)
    p.add_argument("--admin-user", required=True)
    p.add_argument("--admin-password", required=True)
    p.add_argument("--member-room", default="sandbox")
    p.add_argument("--extra-user", default="probe-extra")
    p.add_argument("--added-event", default="true", choices=["false", "true"])
    p.add_argument("--seconds", type=int, default=40)
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
