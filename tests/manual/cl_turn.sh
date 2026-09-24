#!/usr/bin/env bash
# cl_turn.sh <session-id|new> <log-name> <prompt> [timeout]  — one headless Claude Code turn in the keeper dir
# Env: COOP_LAB_DIR (logs), COOP_LAB_KEEPER_DIR. Claude Code must trust the keeper dir first
# (open it interactively once, or projects[<dir>].hasTrustDialogAccepted in ~/.claude.json).
set -u
L="${COOP_LAB_DIR:-$PWD/lab-runs}"; mkdir -p "$L"
KEEPER="${COOP_LAB_KEEPER_DIR:-$HOME/.agentcoop/agents/builtin/coop-keeper}"
cd "$KEEPER" || exit 1
SID="$1"; NAME="$2"; PROMPT="$3"; TO="${4:-300}"
if [ "$SID" = new ]; then SESS=(); else SESS=(--resume "$SID"); fi
timeout "$TO" claude -p --output-format stream-json --verbose ${SESS[@]+"${SESS[@]}"} "$PROMPT" 2> "$L/$NAME.err" | tee "$L/$NAME.jsonl" | python3 -c '
import json,sys
sid=None
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    try: ev=json.loads(line)
    except Exception: print("RAW",line[:200]); continue
    sid = sid or ev.get("session_id")
    t=ev.get("type")
    if t=="assistant":
        for c in ev.get("message",{}).get("content",[]):
            if c.get("type")=="text": print("TEXT:", c["text"][:1200])
            elif c.get("type")=="tool_use": print("TOOL:", c.get("name"), json.dumps(c.get("input"))[:300])
    elif t=="user":
        for c in ev.get("message",{}).get("content",[]):
            if isinstance(c,dict) and c.get("type")=="tool_result":
                out=c.get("content"); 
                if isinstance(out,list): out=" ".join(x.get("text","") for x in out if isinstance(x,dict))
                print("  RESULT:", ("ERROR " if c.get("is_error") else "")+str(out)[:240].replace("\n"," "))
    elif t=="result": print("-- result:", ev.get("subtype"), "turns", ev.get("num_turns"), "cost", ev.get("total_cost_usd"))
print("SESSION", sid)
'
echo "exit=${PIPESTATUS[0]}"
