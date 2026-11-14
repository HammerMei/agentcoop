"""Minimal ACP client probe: initialize + session/new against `opencode acp` over stdio."""

import asyncio
import json
import os
import sys

CWD = os.path.abspath(sys.argv[1])  # ACP session paths are absolute
PROMPT = sys.argv[2] if len(sys.argv) > 2 else None
EXTRA_ENV = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}


class Probe:
    def __init__(self, proc):
        self.proc = proc
        self.nid = 0
        self.session_id = None

    async def write(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def send(self, method, params):
        self.nid += 1
        await self.write({"jsonrpc": "2.0", "id": self.nid, "method": method, "params": params})

    async def reply(self, id_, result):
        await self.write({"jsonrpc": "2.0", "id": id_, "result": result})

    async def read(self):
        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=60)
        except asyncio.TimeoutError:
            print("<< timeout waiting")
            return None
        if not line:
            print("<< EOF")
            return None
        try:
            return json.loads(line)
        except Exception:
            print("<< raw", line[:200])
            return {}

    async def on_response(self, msg):
        """Drive the sequence from each response. Returns False when the probe is done."""
        if msg.get("id") == 1:
            await self.send("session/new", {"cwd": CWD, "mcpServers": []})
        elif msg.get("id") == 2:
            self.session_id = msg["result"]["sessionId"]
            if not PROMPT:
                return False
            await self.send("session/prompt", {"sessionId": self.session_id, "prompt": [{"type": "text", "text": PROMPT}]})
        elif msg.get("id") == 3:
            return False
        return True

    async def on_request(self, msg):
        if msg["method"] == "session/request_permission":
            opts = msg["params"]["options"]
            print("!! PERMISSION REQUEST options:", [(o["optionId"], o["kind"]) for o in opts])
            # pick first allow_once
            choice = next((o["optionId"] for o in opts if o["kind"] == "allow_once"), opts[0]["optionId"])
            await self.reply(msg["id"], {"outcome": {"outcome": "selected", "optionId": choice}})
        else:
            print("!! unhandled agent request", msg["method"])
            await self.write({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})


async def main():
    env = {**os.environ, **EXTRA_ENV}
    proc = await asyncio.create_subprocess_exec(
        "opencode", "acp", cwd=CWD,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )
    p = Probe(proc)

    async def drain_err():
        async for line in proc.stderr:
            print("[stderr]", line.decode().rstrip()[:300], file=sys.stderr)

    asyncio.create_task(drain_err())
    await p.send("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
    deadline = asyncio.get_event_loop().time() + 120
    while asyncio.get_event_loop().time() < deadline:
        msg = await p.read()
        if msg is None:
            break
        if not msg:
            continue
        print("<<", json.dumps(msg)[:700])
        if "result" in msg:
            if not await p.on_response(msg):
                break
        elif "method" in msg and "id" in msg:
            await p.on_request(msg)
    proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    print("exit", proc.returncode)


asyncio.run(main())
