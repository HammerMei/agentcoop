"""Minimal ACP client probe: initialize + session/new against `opencode acp` over stdio."""
import asyncio, json, os, sys

CWD = sys.argv[1]
PROMPT = sys.argv[2] if len(sys.argv) > 2 else None
EXTRA_ENV = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}

async def main():
    env = {**os.environ, **EXTRA_ENV}
    proc = await asyncio.create_subprocess_exec(
        "opencode", "acp", cwd=CWD,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )
    nid = 0
    async def send(method, params, notify=False):
        nonlocal nid
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            nid += 1; msg["id"] = nid
        proc.stdin.write((json.dumps(msg) + "\n").encode()); await proc.stdin.drain()
        return nid
    async def reply(id_, result):
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}) + "\n").encode()); await proc.stdin.drain()

    async def drain_err():
        async for line in proc.stderr:
            print("[stderr]", line.decode().rstrip()[:300], file=sys.stderr)
    asyncio.create_task(drain_err())

    await send("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
    session_id = None
    deadline = asyncio.get_event_loop().time() + 120
    while asyncio.get_event_loop().time() < deadline:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
        except asyncio.TimeoutError:
            print("<< timeout waiting"); break
        if not line: print("<< EOF"); break
        try: msg = json.loads(line)
        except Exception: print("<< raw", line[:200]); continue
        s = json.dumps(msg)
        print("<<", s[:700])
        if "result" in msg and msg.get("id") == 1:
            await send("session/new", {"cwd": CWD, "mcpServers": []})
        elif "result" in msg and msg.get("id") == 2:
            session_id = msg["result"]["sessionId"]
            if PROMPT:
                await send("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": PROMPT}]})
            else:
                break
        elif "result" in msg and msg.get("id") == 3:
            break
        elif msg.get("method") == "session/request_permission":
            opts = msg["params"]["options"]
            print("!! PERMISSION REQUEST options:", [(o["optionId"], o["kind"]) for o in opts])
            # pick first allow_once
            choice = next((o["optionId"] for o in opts if o["kind"] == "allow_once"), opts[0]["optionId"])
            await reply(msg["id"], {"outcome": {"outcome": "selected", "optionId": choice}})
        elif "method" in msg and "id" in msg:
            print("!! unhandled agent request", msg["method"])
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}}) + "\n").encode())
    proc.stdin.close()
    try: await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError: proc.kill()
    print("exit", proc.returncode)

asyncio.run(main())
