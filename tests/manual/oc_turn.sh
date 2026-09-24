#!/usr/bin/env bash
# oc_turn.sh <session-id|new> <log-name> <prompt> [timeout]  — one headless OpenCode turn in the keeper dir.
# Env: COOP_LAB_DIR (logs), COOP_LAB_OPENCODE_MODEL, COOP_LAB_KEEPER_DIR, COOP_LAB_OPENCODE_DATA
# (a private XDG_DATA_HOME for opencode; copy ~/.local/share/opencode/auth.json into <it>/opencode/ first).
# opencode 1.18.13 sometimes never gets past its `init` log line; such an attempt is killed after
# STALL_SECS and retried (up to 5 times). A turn that has passed init gets the full timeout.
set -u
L="${COOP_LAB_DIR:-$PWD/lab-runs}"; mkdir -p "$L"
MODEL="${COOP_LAB_OPENCODE_MODEL:-opencode/big-pickle}"
KEEPER="${COOP_LAB_KEEPER_DIR:-$HOME/.agentcoop/agents/builtin/coop-keeper}"
cd "$KEEPER" || exit 1
SID="$1"; NAME="$2"; PROMPT="$3"; TO="${4:-300}"; STALL_SECS=40
if [ "$SID" = new ]; then SESS=(--title "$NAME"); else SESS=(-s "$SID"); fi
RC=1
for attempt in 1 2 3 4 5; do
  : > "$L/$NAME.err"; : > "$L/$NAME.jsonl"
  XDG_DATA_HOME="${COOP_LAB_OPENCODE_DATA:-$L/oc-data}" \
    opencode run --pure --print-logs --log-level INFO -m "$MODEL" "${SESS[@]}" --format json "$PROMPT" \
    > "$L/$NAME.jsonl" 2> "$L/$NAME.err" &
  PID=$!
  started=0
  for ((i=0; i<TO; i++)); do
    if ! kill -0 "$PID" 2>/dev/null; then break; fi
    if [ $started = 0 ] && grep -q "init count=" "$L/$NAME.err"; then started=1; fi
    if [ $started = 0 ] && [ $i -ge $STALL_SECS ]; then
      kill "$PID" 2>/dev/null; sleep 1; kill -9 "$PID" 2>/dev/null
      echo "[stall at opencode startup — retry $attempt]"; break
    fi
    sleep 1
  done
  if kill -0 "$PID" 2>/dev/null; then kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; RC=124; echo "[timeout after ${TO}s]"; break; fi
  wait "$PID" 2>/dev/null; RC=$?
  if [ $started = 1 ] || [ $RC = 0 ]; then break; fi
  sleep 3
done
python3 "$(dirname "$0")/oc_summ.py" < "$L/$NAME.jsonl"
echo "exit=$RC"
grep -oE 'session.id=ses_[A-Za-z0-9]+' "$L/$NAME.err" | head -1
grep -E 'permission=' "$L/$NAME.err" | grep -vE 'action.action=allow' | head -5
