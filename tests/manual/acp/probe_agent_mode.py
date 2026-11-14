"""ACP probe: per-watcher opencode agent via OPENCODE_CONFIG hot reload + the `mode` config option.

The process runs with OPENCODE_CONFIG=<config> (agents) and OPENCODE_CONFIG_CONTENT (the §4
force-ask ruleset). Steps, each printed with its result:

  1. session/new -> set mode watcher_a -> name prompt (expect WATCHER_A)
  2. shell prompt on watcher_a -> count session/request_permission (the ruleset must still apply)
  3. add watcher_b to the file, set mode watcher_b with NO wait (withReload) -> name prompt (expect WATCHER_B)
  4. edit watcher_b.system in place (+ description hash) -> wait config_option_update -> name prompt (expect WATCHER_B2)
  5. second session on watcher_a; remove watcher_a from the file -> wait update -> name prompt on it (observe)
  6. close the process; fresh process, session/resume of session 1 -> mode currentValue, name prompt (expect WATCHER_B2)

Usage: python3 -I probe_agent_mode.py <cwd> <config path> [provider/model]
"""
import asyncio, hashlib, json, os, sys, time

CWD = sys.argv[1]
CONFIG = sys.argv[2]
MODEL = sys.argv[3] if len(sys.argv) > 3 else "openai/gpt-5.6-luna-fast"
RULESET = json.dumps({"permissions": [
    {"action": "shell", "resource": "*", "effect": "ask"},
    {"action": "edit", "resource": "*", "effect": "ask"},
    {"action": "external_directory", "resource": "*", "effect": "ask"},
]})
NAME_PROMPT = "What is your watcher name? Reply with the name only."
SHELL_PROMPT = "Use the shell tool to run `echo hi`. Then reply with the single word done."

def agent(name, answer):
    system = (f"You are an AI agent running in OpenCode. Your watcher name is {answer}. "
              f"When asked for your watcher name, reply with exactly {answer} and nothing else.")
    return {"mode": "primary", "description": f"probe {hashlib.sha256(system.encode()).hexdigest()[:8]}", "system": system}

def write_config(agents):
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"model": MODEL, "agents": {n: agent(n, a) for n, a in agents.items()}}, f, indent=2)
    os.replace(tmp, CONFIG)
    print(f"-- wrote config agents={list(agents)}")

class Client:
    def __init__(self, proc):
        self.proc, self.nid, self.pending, self.text, self.asks = proc, 0, {}, {}, 0
        self.options = {}          # sessionId -> latest configOptions from config_option_update
        self.option_event = asyncio.Event()
    async def request(self, method, params, timeout=120):
        self.nid += 1; id_ = self.nid
        fut = asyncio.get_event_loop().create_future(); self.pending[id_] = fut
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params}) + "\n").encode())
        await self.proc.stdin.drain()
        return await asyncio.wait_for(fut, timeout)
    async def reply(self, id_, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": id_}
        msg["error" if error else "result"] = error or result
        self.proc.stdin.write((json.dumps(msg) + "\n").encode()); await self.proc.stdin.drain()
    async def pump(self):
        async for line in self.proc.stdout:
            try: msg = json.loads(line)
            except Exception: print("<< raw", line[:200]); continue
            if "id" in msg and "method" not in msg:
                fut = self.pending.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_exception(RuntimeError(json.dumps(msg["error"]))) if "error" in msg else fut.set_result(msg.get("result"))
            elif msg.get("method") == "session/update":
                p = msg["params"]; u = p["update"]; kind = u.get("sessionUpdate")
                if kind == "agent_message_chunk" and u["content"]["type"] == "text":
                    self.text.setdefault(p["sessionId"], []).append(u["content"]["text"])
                elif kind == "config_option_update":
                    cur, avail = mode_option(u["configOptions"])
                    self.options[p["sessionId"]] = u["configOptions"]
                    self.option_event.set()
                    print(f"<< config_option_update session={p['sessionId'][-6:]} mode current={cur} available={avail}")
                elif kind in ("available_commands_update", "usage_update", "agent_thought_chunk"):
                    pass
                else:
                    print("<< update", kind, json.dumps(u)[:160])
            elif msg.get("method") == "session/request_permission":
                opts = msg["params"]["options"]; self.asks += 1
                tc = msg["params"].get("toolCall", {})
                print(f"!! permission ask tool={tc.get('title') or tc.get('name')} options={[(o['optionId'], o['kind']) for o in opts]}")
                choice = next((o["optionId"] for o in opts if o["kind"] == "allow_once"), opts[0]["optionId"])
                await self.reply(msg["id"], {"outcome": {"outcome": "selected", "optionId": choice}})
            elif "id" in msg:
                print("!! unhandled agent request", msg["method"])
                await self.reply(msg["id"], error={"code": -32601, "message": "not supported"})
            else:
                print("<< notify", msg.get("method"), json.dumps(msg.get("params"))[:160])
    async def wait_option_update(self, timeout=10):
        self.option_event.clear()
        t0 = time.time()
        try:
            await asyncio.wait_for(self.option_event.wait(), timeout)
            print(f"   config_option_update arrived {time.time() - t0:.1f}s after write")
            return True
        except asyncio.TimeoutError:
            print(f"   NO config_option_update within {timeout}s")
            return False

def mode_option(config_options):
    opt = next((o for o in (config_options or []) if o.get("id") == "mode"), None)
    if not opt:
        return None, []
    return opt.get("currentValue"), [o["value"] for o in opt.get("options", [])]

async def prompt(c, sid, text, timeout=120):
    c.text[sid] = []
    t0 = time.time()
    try:
        r = await c.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}, timeout=timeout)
    except Exception as e:
        print(f"   prompt -> {type(e).__name__} {e} after {time.time() - t0:.1f}s")
        return f"{type(e).__name__} {e}".strip()
    answer = "".join(c.text[sid]).strip()
    print(f"   prompt -> stopReason={r.get('stopReason')} answer={answer!r}")
    return answer

async def set_mode(c, sid, mode):
    """opencode 2.2.6 exposes the agent as the `mode` session config option. `session/set_config_option`
    returns the refreshed option list. `session/set_mode` is the same selection and returns {}."""
    t0 = time.time()
    try:
        r = await c.request("session/set_config_option", {"sessionId": sid, "configId": "mode", "value": mode})
        cur, avail = mode_option(r.get("configOptions"))
        print(f"   set_config_option mode={mode} -> ok in {time.time() - t0:.2f}s current={cur} available={avail}")
        return True
    except Exception as e:
        print(f"   set_config_option mode={mode} -> ERROR {e}")
        return False

async def spawn():
    env = {**os.environ, "OPENCODE_CONFIG": CONFIG, "OPENCODE_CONFIG_CONTENT": RULESET}
    proc = await asyncio.create_subprocess_exec("opencode", "acp", cwd=CWD, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    c = Client(proc)
    asyncio.create_task(c.pump())
    async def drain_err():
        async for line in proc.stderr: print("[stderr]", line.decode().rstrip()[:300], file=sys.stderr)
    asyncio.create_task(drain_err())
    await c.request("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
    return proc, c

async def close(proc):
    proc.stdin.close()
    try: await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError: proc.kill()
    print("-- process exit", proc.returncode)

async def main():
    results = {}
    write_config({"watcher_a": "WATCHER_A"})
    proc, c = await spawn()
    s1 = await c.request("session/new", {"cwd": CWD, "mcpServers": []})
    sid1 = s1["sessionId"]
    cur, avail = mode_option(s1.get("configOptions"))
    print("-- session1", sid1, "default mode=", cur, "available=", avail)

    print("== 1. watcher_a")
    await set_mode(c, sid1, "watcher_a")
    results["1 name on watcher_a"] = await prompt(c, sid1, NAME_PROMPT)

    print("== 2. shell under the ruleset on watcher_a")
    before = c.asks
    answer = await prompt(c, sid1, SHELL_PROMPT)
    results["2 shell asks on watcher_a"] = f"{c.asks - before} ask(s), answer={answer!r}"

    print("== 3. add watcher_b: set mode immediately, then after config_option_update")
    c.option_event.clear()
    write_config({"watcher_a": "WATCHER_A", "watcher_b": "WATCHER_B"})
    ok = await set_mode(c, sid1, "watcher_b")
    results["3 set_mode immediately after write"] = "ok" if ok else "ERROR (mode not found)"
    if not ok:
        got = await c.wait_option_update()
        ok = await set_mode(c, sid1, "watcher_b")
        results["3 set_mode after notification"] = f"notified={got} set_mode={'ok' if ok else 'ERROR'}"
    results["3 name on watcher_b"] = await prompt(c, sid1, NAME_PROMPT) if ok else "skipped"

    print("== 4. edit watcher_b.system in place")
    write_config({"watcher_a": "WATCHER_A", "watcher_b": "WATCHER_B2"})
    got = await c.wait_option_update()
    results["4 update after in-place edit"] = "notified" if got else "no notification"
    results["4 name after in-place edit"] = await prompt(c, sid1, NAME_PROMPT)

    print("== 5. second session on watcher_a, then remove watcher_a")
    s2 = await c.request("session/new", {"cwd": CWD, "mcpServers": []})
    sid2 = s2["sessionId"]
    await set_mode(c, sid2, "watcher_a")
    results["5 name on s2 watcher_a"] = await prompt(c, sid2, NAME_PROMPT)
    c.option_event.clear()
    write_config({"watcher_b": "WATCHER_B2"})
    got = await c.wait_option_update()
    cur2, _ = mode_option(c.options.get(sid2))
    results["5 s2 mode after removal"] = f"notified={got} current={cur2}"
    results["5 name on s2 after removal (30s cap)"] = await prompt(c, sid2, NAME_PROMPT, timeout=30)
    ok = await set_mode(c, sid2, "watcher_b")
    results["5 s2 recovered by set_mode watcher_b"] = await prompt(c, sid2, NAME_PROMPT) if ok else "set_mode ERROR"

    print("== 6. fresh process, session/resume of session 1 (its entry watcher_b still exists)")
    await close(proc)
    proc, c = await spawn()
    try:
        r = await c.request("session/resume", {"sessionId": sid1, "cwd": CWD, "mcpServers": []})
        cur, avail = mode_option(r.get("configOptions"))
        print(f"   resume -> mode current={cur} available={avail}")
        results["6 resumed mode"] = cur
        results["6 name after resume, no set_mode"] = await prompt(c, sid1, NAME_PROMPT, timeout=30)
        r = await c.request("session/resume", {"sessionId": sid2, "cwd": CWD, "mcpServers": []})
        cur, _ = mode_option(r.get("configOptions"))
        results["6 s2 resumed mode (was on removed watcher_a, then watcher_b)"] = cur
    except Exception as e:
        results["6 resume"] = f"{type(e).__name__} {e}"
    await close(proc)

    print("== RESULT")
    for k, v in results.items():
        print(f"   {k}: {v}")

asyncio.run(main())
