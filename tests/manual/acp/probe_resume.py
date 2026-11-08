"""ACP probe: initialize -> session/resume|load <id> -> prompt, in a fresh `opencode acp` process."""
import asyncio, json, sys
CWD, SID, PROMPT = sys.argv[1], sys.argv[2], sys.argv[3]
METHOD = sys.argv[4] if len(sys.argv) > 4 else "session/resume"

async def main():
    proc = await asyncio.create_subprocess_exec("opencode", "acp", cwd=CWD,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    nid = 0
    async def send(method, params):
        nonlocal nid; nid += 1
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": nid, "method": method, "params": params}) + "\n").encode()); await proc.stdin.drain()
    async def drain_err():
        async for line in proc.stderr: print("[stderr]", line.decode().rstrip()[:200], file=sys.stderr)
    asyncio.create_task(drain_err())
    await send("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
    replay = 0
    while True:
        try: line = await asyncio.wait_for(proc.stdout.readline(), timeout=90)
        except asyncio.TimeoutError: print("<< timeout"); break
        if not line: print("<< EOF"); break
        msg = json.loads(line)
        if msg.get("method") == "session/update" and "id" not in msg:
            u = msg["params"]["update"]; kind = u.get("sessionUpdate")
            if kind in ("user_message_chunk", "agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update"):
                replay += 1
                if kind == "agent_message_chunk": print("<< chunk:", u["content"].get("text", "")[:120].replace("\n", " "))
                continue
        print("<<", json.dumps(msg)[:600])
        if "result" in msg and msg["id"] == 1:
            await send(METHOD, {"sessionId": SID, "cwd": CWD, "mcpServers": []})
        elif "result" in msg and msg["id"] == 2:
            print(f"!! {METHOD} ok; message/tool updates seen so far: {replay}")
            await send("session/prompt", {"sessionId": SID, "prompt": [{"type": "text", "text": PROMPT}]})
        elif "error" in msg and msg["id"] == 2:
            print("!! attach failed"); break
        elif "result" in msg and msg["id"] == 3:
            break
        elif msg.get("method") == "session/request_permission":
            opts = msg["params"]["options"]
            choice = next((o["optionId"] for o in opts if o["kind"] == "allow_once"), opts[0]["optionId"])
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": {"outcome": "selected", "optionId": choice}}}) + "\n").encode()); await proc.stdin.drain()
    proc.stdin.close()
    try: await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError: proc.kill()
    print("exit", proc.returncode, "message/tool updates seen:", replay)
asyncio.run(main())
