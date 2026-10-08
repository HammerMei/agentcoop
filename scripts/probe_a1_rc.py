#!/usr/bin/env python3
"""A1: does RocketChat's `__my_messages__` subscription work, and what does it carry?

Self-driving experiment for assumption A1 in docs/design/dynamic-watcher-design.md.
Opens the subscription as a probe account, then generates the traffic itself
from a second (admin) account, so nothing depends on hand-timed posting.

Cases exercised:
  1. message in a channel the probe user IS a member of      -> baseline
  2. message in a public channel the probe user is NOT in     -> the question
     that decides whether delivery implies membership, and what roomParticipant says
  3. message posted BY the probe user itself                   -> own-message echo
  4. a system message (user added to a channel)                -> t-field filtering

Reports, per frame: fields.eventName, len(args), the shape of args[1]
("allowed"), and whether roomType/roomName are present there — which decides
whether a by-id REST resolver is required on the routing path.

Deliberately does not use gateway.connectors.rocketchat.websocket, so the
frame is seen exactly as sent.

Usage:
    uv run python scripts/probe_a1_rc.py --url https://rc.labpig.com \
        --probe-user probe-bot --probe-password '...' \
        --admin-user glin --admin-password '...' \
        --member-room sandbox --outside-room probe-outside \
        [--added-event false|true]
"""

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime

import httpx
import websockets


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


class RCRest:
    def __init__(self, url: str):
        self.base = url.rstrip("/") + "/api/v1"
        self.c = httpx.AsyncClient(base_url=self.base, timeout=20)
        self.h: dict = {}

    async def login(self, user: str, pw: str) -> str:
        r = await self.c.post("login", json={"user": user, "password": pw})
        r.raise_for_status()
        d = r.json()["data"]
        self.h = {"X-Auth-Token": d["authToken"], "X-User-Id": d["userId"]}
        return d["userId"]

    async def post(self, room: str, text: str) -> None:
        r = await self.c.post("chat.postMessage", headers=self.h,
                              json={"channel": f"#{room}", "text": text})
        if r.status_code >= 400:
            log(f"  !! post to #{room} failed {r.status_code}: {r.text[:200]}")
        else:
            log(f"  -> posted to #{room}: {text!r}")

    async def invite(self, room: str, username: str) -> None:
        """Generates a system message (au / 'user added') in the room."""
        r = await self.c.get("channels.info", headers=self.h, params={"roomName": room})
        if r.status_code >= 400:
            log(f"  !! channels.info {room} failed: {r.text[:150]}")
            return
        rid = r.json()["channel"]["_id"]
        r2 = await self.c.get("users.info", headers=self.h, params={"username": username})
        if r2.status_code >= 400:
            log(f"  !! users.info {username} failed: {r2.text[:150]}")
            return
        uid = r2.json()["user"]["_id"]
        r3 = await self.c.post("channels.invite", headers=self.h,
                               json={"roomId": rid, "userId": uid})
        log(f"  -> invite {username} -> #{room}: {r3.status_code}")

    async def close(self) -> None:
        await self.c.aclose()


async def driver(url: str, admin_user: str, admin_pw: str, probe_user: str,
                 probe_pw: str, member_room: str, outside_room: str,
                 ready: asyncio.Event) -> None:
    """Generate the four cases once the subscription is confirmed ready."""
    await ready.wait()
    await asyncio.sleep(1.5)

    admin = RCRest(url)
    await admin.login(admin_user, admin_pw)
    probe = RCRest(url)
    await probe.login(probe_user, probe_pw)

    log(f"CASE 1: admin posts in #{member_room} (probe IS a member)")
    await admin.post(member_room, "A1 case1 member-room")
    await asyncio.sleep(3)

    log(f"CASE 2: admin posts in #{outside_room} (probe is NOT a member)")
    await admin.post(outside_room, "A1 case2 outside-room")
    await asyncio.sleep(3)

    log(f"CASE 3: probe posts in #{member_room} (own message)")
    await probe.post(member_room, "A1 case3 own-message")
    await asyncio.sleep(3)

    log(f"CASE 4: system message — invite admin into #{outside_room}")
    await admin.invite(outside_room, admin_user)
    await asyncio.sleep(3)

    await admin.close()
    await probe.close()
    log("driver finished")


async def listener(url: str, user: str, password: str, added_event: bool,
                   ready: asyncio.Event, seconds: int) -> int:
    ws_url = url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/websocket"
    log(f"connecting {ws_url}")
    async with websockets.connect(ws_url) as ws:
        async def send(o: dict) -> None:
            await ws.send(json.dumps(o))

        await send({"msg": "connect", "version": "1", "support": ["1"]})
        while True:
            f = json.loads(await ws.recv())
            if f.get("msg") == "connected":
                log(f"DDP connected session={f.get('session')}")
                break
            if f.get("msg") == "failed":
                log(f"handshake failed: {f}")
                return 1

        await send({"msg": "method", "method": "login", "id": "l1",
                    "params": [{"user": {"username": user},
                                "password": {"digest": hashlib.sha256(password.encode()).hexdigest(),
                                             "algorithm": "sha-256"}}]})
        while True:
            f = json.loads(await ws.recv())
            if f.get("msg") == "result" and f.get("id") == "l1":
                if f.get("error"):
                    log(f"LOGIN FAILED: {f['error']}")
                    return 1
                log(f"logged in as {user} (id={(f.get('result') or {}).get('id')})")
                break

        sub = {"msg": "sub", "id": "s1", "name": "stream-room-messages",
               "params": ["__my_messages__", added_event]}
        log(f"SUB -> {json.dumps(sub)}")
        await send(sub)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        n = 0
        summary = []

        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            except websockets.ConnectionClosed as e:
                log(f"closed: {e}")
                break

            f = json.loads(raw)
            m = f.get("msg")
            if m == "ping":
                await send({"msg": "pong"})
                continue
            if m == "nosub":
                log("*** NOSUB — __my_messages__ REFUSED ***")
                print(json.dumps(f, indent=2), flush=True)
                return 2
            if m == "ready":
                log(f"READY subs={f.get('subs')} — subscription ACCEPTED")
                ready.set()
                continue
            if m != "changed":
                continue

            n += 1
            fl = f.get("fields") or {}
            args = fl.get("args") or []
            msg0 = args[0] if args and isinstance(args[0], dict) else {}
            allowed = args[1] if len(args) > 1 else None

            print("=" * 76, flush=True)
            log(f"CHANGED #{n}")
            print(f"  eventName        : {fl.get('eventName')!r}", flush=True)
            print(f"  len(args)        : {len(args)}", flush=True)
            print(f"  msg.rid          : {msg0.get('rid')!r}", flush=True)
            print(f"  msg.t (system)   : {msg0.get('t')!r}", flush=True)
            print(f"  msg.u.username   : {(msg0.get('u') or {}).get('username')!r}", flush=True)
            print(f"  msg.msg          : {str(msg0.get('msg'))[:60]!r}", flush=True)
            if allowed is None:
                print("  allowed          : ABSENT", flush=True)
            else:
                print(f"  allowed          : {json.dumps(allowed)}", flush=True)
                if isinstance(allowed, dict):
                    print(f"  allowed keys     : {sorted(allowed.keys())}", flush=True)
            print("  RAW:", flush=True)
            print(json.dumps(f)[:1500], flush=True)
            print(flush=True)

            summary.append({
                "n": n, "eventName": fl.get("eventName"), "nargs": len(args),
                "rid": msg0.get("rid"), "t": msg0.get("t"),
                "user": (msg0.get("u") or {}).get("username"),
                "text": str(msg0.get("msg"))[:40],
                "allowed_keys": sorted(allowed.keys()) if isinstance(allowed, dict) else None,
                "allowed": allowed if isinstance(allowed, (dict, bool)) else None,
            })

        print("=" * 76, flush=True)
        log(f"captured {n} changed frame(s)")
        print("\nSUMMARY", flush=True)
        for s in summary:
            print(f"  #{s['n']} eventName={s['eventName']!r} nargs={s['nargs']} "
                  f"rid={s['rid']!r} t={s['t']!r} from={s['user']!r} "
                  f"text={s['text']!r} allowed_keys={s['allowed_keys']}", flush=True)
        return 0


async def main_async(a) -> int:
    ready = asyncio.Event()
    task_driver = asyncio.create_task(
        driver(a.url, a.admin_user, a.admin_password, a.probe_user,
               a.probe_password, a.member_room, a.outside_room, ready))
    rc = await listener(a.url, a.probe_user, a.probe_password,
                        a.added_event == "true", ready, a.seconds)
    task_driver.cancel()
    try:
        await task_driver
    except asyncio.CancelledError:
        pass
    return rc


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--probe-user", required=True)
    p.add_argument("--probe-password", required=True)
    p.add_argument("--admin-user", required=True)
    p.add_argument("--admin-password", required=True)
    p.add_argument("--member-room", default="sandbox")
    p.add_argument("--outside-room", default="probe-outside")
    p.add_argument("--added-event", default="false", choices=["false", "true"])
    p.add_argument("--seconds", type=int, default=45)
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
