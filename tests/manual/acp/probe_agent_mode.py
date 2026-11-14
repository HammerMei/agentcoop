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

import asyncio
import hashlib
import json
import os
import sys
import time

CWD = os.path.abspath(sys.argv[1])
CONFIG = os.path.abspath(sys.argv[2])  # the child runs with cwd=CWD, so a relative path would diverge
MODEL = sys.argv[3] if len(sys.argv) > 3 else "openai/gpt-5.6-luna-fast"
RULESET = json.dumps({"permissions": [
    {"action": "shell", "resource": "*", "effect": "ask"},
    {"action": "edit", "resource": "*", "effect": "ask"},
    {"action": "external_directory", "resource": "*", "effect": "ask"},
]})
NAME_PROMPT = "What is your watcher name? Reply with the name only."
SHELL_PROMPT = "Use the shell tool to run `echo hi`. Then reply with the single word done."
QUIET_UPDATES = ("available_commands_update", "usage_update", "agent_thought_chunk")


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


def mode_option(config_options):
    opt = next((o for o in (config_options or []) if o.get("id") == "mode"), None)
    if not opt:
        return None, []
    return opt.get("currentValue"), [o["value"] for o in opt.get("options", [])]


class Client:
    def __init__(self, proc):
        self.proc = proc
        self.nid = 0
        self.pending = {}
        self.text = {}
        self.asks = 0
        self.options = {}          # sessionId -> latest configOptions from config_option_update
        self.option_event = asyncio.Event()

    async def write(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method, params, timeout=120):
        self.nid += 1
        id_ = self.nid
        fut = asyncio.get_event_loop().create_future()
        self.pending[id_] = fut
        await self.write({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})
        return await asyncio.wait_for(fut, timeout)

    async def reply(self, id_, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": id_}
        msg["error" if error else "result"] = error or result
        await self.write(msg)

    def on_response(self, msg):
        fut = self.pending.pop(msg["id"], None)
        if fut is None or fut.done():
            return
        if "error" in msg:
            fut.set_exception(RuntimeError(json.dumps(msg["error"])))
        else:
            fut.set_result(msg.get("result"))

    def on_update(self, params):
        sid = params["sessionId"]
        u = params["update"]
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk" and u["content"]["type"] == "text":
            self.text.setdefault(sid, []).append(u["content"]["text"])
        elif kind == "config_option_update":
            cur, avail = mode_option(u["configOptions"])
            self.options[sid] = u["configOptions"]
            self.option_event.set()
            print(f"<< config_option_update session={sid[-6:]} mode current={cur} available={avail}")
        elif kind not in QUIET_UPDATES:
            print("<< update", kind, json.dumps(u)[:160])

    async def on_request(self, msg):
        if msg["method"] == "session/request_permission":
            opts = msg["params"]["options"]
            self.asks += 1
            tc = msg["params"].get("toolCall", {})
            print(f"!! permission ask tool={tc.get('title') or tc.get('name')} options={[(o['optionId'], o['kind']) for o in opts]}")
            choice = next((o["optionId"] for o in opts if o["kind"] == "allow_once"), opts[0]["optionId"])
            await self.reply(msg["id"], {"outcome": {"outcome": "selected", "optionId": choice}})
        else:
            print("!! unhandled agent request", msg["method"])
            await self.reply(msg["id"], error={"code": -32601, "message": "not supported"})

    async def pump(self):
        async for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except Exception:
                print("<< raw", line[:200])
                continue
            if "id" in msg and "method" not in msg:
                self.on_response(msg)
            elif msg.get("method") == "session/update":
                self.on_update(msg["params"])
            elif "id" in msg:
                await self.on_request(msg)
            else:
                print("<< notify", msg.get("method"), json.dumps(msg.get("params"))[:160])

    async def wait_option_update(self, timeout=10):
        """Callers clear `option_event` before the write that should trigger the update,
        so an update that lands before this call is not lost."""
        t0 = time.time()
        try:
            await asyncio.wait_for(self.option_event.wait(), timeout)
            print(f"   config_option_update arrived {time.time() - t0:.1f}s after write")
            return True
        except asyncio.TimeoutError:
            print(f"   NO config_option_update within {timeout}s")
            return False


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
    except Exception as e:
        print(f"   set_config_option mode={mode} -> ERROR {e}")
        return False
    cur, avail = mode_option(r.get("configOptions"))
    print(f"   set_config_option mode={mode} -> ok in {time.time() - t0:.2f}s current={cur} available={avail}")
    return True


async def spawn():
    env = {**os.environ, "OPENCODE_CONFIG": CONFIG, "OPENCODE_CONFIG_CONTENT": RULESET}
    proc = await asyncio.create_subprocess_exec(
        "opencode", "acp", cwd=CWD, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    c = Client(proc)
    asyncio.create_task(c.pump())

    async def drain_err():
        async for line in proc.stderr:
            print("[stderr]", line.decode().rstrip()[:300], file=sys.stderr)

    asyncio.create_task(drain_err())
    await c.request("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
    return proc, c


async def close(proc):
    proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    print("-- process exit", proc.returncode)


async def new_session(c, label):
    s = await c.request("session/new", {"cwd": CWD, "mcpServers": []})
    cur, avail = mode_option(s.get("configOptions"))
    print(f"-- {label}", s["sessionId"], "default mode=", cur, "available=", avail)
    return s["sessionId"]


async def steps_1_to_5(c, results):
    sid1 = await new_session(c, "session1")

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
    c.option_event.clear()
    write_config({"watcher_a": "WATCHER_A", "watcher_b": "WATCHER_B2"})
    got = await c.wait_option_update()
    results["4 update after in-place edit"] = "notified" if got else "no notification"
    results["4 name after in-place edit"] = await prompt(c, sid1, NAME_PROMPT)

    print("== 5. second session on watcher_a, then remove watcher_a")
    sid2 = await new_session(c, "session2")
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
    return sid1, sid2


async def step_6(c, sid1, sid2, results):
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


async def main():
    results = {}
    write_config({"watcher_a": "WATCHER_A"})
    proc, c = await spawn()
    sid1, sid2 = await steps_1_to_5(c, results)

    print("== 6. fresh process, session/resume of session 1 (its entry watcher_b still exists)")
    await close(proc)
    proc, c = await spawn()
    await step_6(c, sid1, sid2, results)
    await close(proc)

    print("== RESULT")
    for k, v in results.items():
        print(f"   {k}: {v}")


asyncio.run(main())
