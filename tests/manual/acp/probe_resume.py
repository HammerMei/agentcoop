"""ACP probe: initialize -> session/resume|load <id> -> prompt, in a fresh `opencode acp` process."""

import asyncio
import json
import sys

CWD, SID, PROMPT = sys.argv[1], sys.argv[2], sys.argv[3]
METHOD = sys.argv[4] if len(sys.argv) > 4 else "session/resume"
REPLAYED = ("user_message_chunk", "agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update")


class Probe:
    def __init__(self, proc):
        self.proc = proc
        self.nid = 0
        self.replay = 0

    async def write(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def send(self, method, params):
        self.nid += 1
        await self.write({"jsonrpc": "2.0", "id": self.nid, "method": method, "params": params})

    async def read(self):
        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=90)
        except asyncio.TimeoutError:
            print("<< timeout")
            return None
        if not line:
            print("<< EOF")
            return None
        return json.loads(line)

    def count_replay(self, msg):
        """True when the message is a replayed history update that needs no handling."""
        if msg.get("method") != "session/update" or "id" in msg:
            return False
        u = msg["params"]["update"]
        kind = u.get("sessionUpdate")
        if kind not in REPLAYED:
            return False
        self.replay += 1
        if kind == "agent_message_chunk":
            print("<< chunk:", u["content"].get("text", "")[:120].replace("\n", " "))
        return True

    async def handle(self, msg):
        """Drive the sequence. Returns False when the probe is done."""
        print("<<", json.dumps(msg)[:600])
        if "result" in msg and msg["id"] == 1:
            await self.send(METHOD, {"sessionId": SID, "cwd": CWD, "mcpServers": []})
        elif "result" in msg and msg["id"] == 2:
            print(f"!! {METHOD} ok; message/tool updates seen so far: {self.replay}")
            await self.send("session/prompt", {"sessionId": SID, "prompt": [{"type": "text", "text": PROMPT}]})
        elif "error" in msg and msg["id"] == 2:
            print("!! attach failed")
            return False
        elif "result" in msg and msg["id"] == 3:
            return False
        elif msg.get("method") == "session/request_permission":
            opts = msg["params"]["options"]
            choice = next((o["optionId"] for o in opts if o["kind"] == "allow_once"), opts[0]["optionId"])
            await self.write({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": {"outcome": "selected", "optionId": choice}}})
        return True


async def main():
    proc = await asyncio.create_subprocess_exec(
        "opencode", "acp", cwd=CWD,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    p = Probe(proc)

    async def drain_err():
        async for line in proc.stderr:
            print("[stderr]", line.decode().rstrip()[:200], file=sys.stderr)

    asyncio.create_task(drain_err())
    await p.send("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
    while True:
        msg = await p.read()
        if msg is None:
            break
        if p.count_replay(msg):
            continue
        if not await p.handle(msg):
            break
    proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    print("exit", proc.returncode, "message/tool updates seen:", p.replay)


asyncio.run(main())
