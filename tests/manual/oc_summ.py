"""Summarise `opencode run --format json` events: TEXT / TOOL / EV lines."""

import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except Exception:
        print("RAW", line[:200])
        continue
    part = ev.get("part") or {}
    kind = part.get("type")
    if kind == "text":
        print("TEXT:", part.get("text", "")[:1200])
    elif kind == "tool":
        state = part.get("state") or {}
        output = (state.get("output") or "")[:240].replace("\n", " ")
        print("TOOL:", part.get("tool"), json.dumps(state.get("input"))[:300], "|", state.get("status"), output)
    elif ev.get("type") not in ("step_start", "step_finish"):
        print("EV:", ev.get("type"), str(ev)[:160])
