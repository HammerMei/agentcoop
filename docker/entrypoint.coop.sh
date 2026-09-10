#!/usr/bin/env bash
# =============================================================================
# AgentCoop Docker Entrypoint
#
# ── Volume mounts ─────────────────────────────────────────────────────────────
#
#   ~/.agentcoop/config/     ONLY config.yaml + .env
#                                      Entrypoint symlinks them one level up.
#                                      Safe to bind-mount — does NOT overwrite
#                                      the runtime directory.
#
#   ~/.agentcoop/work/        Agent working directory
#   ~/.agentcoop/contexts/    Custom context files (defaults if empty)
#   ~/.claude/                         Claude Code config & credentials
#   ~/.config/opencode/                OpenCode config & API key
#
# ⚠️  Do NOT bind-mount ~/.agentcoop directly — it contains
#     install_meta.json and contexts required at runtime.
#
# ── Config modes ─────────────────────────────────────────────────────────────
#
# Mode 1: Volume mount (recommended)
#   Mount a local dir to ~/.agentcoop/config/ — no env vars needed:
#
#     docker run \
#       -v ./acg-config:/root/.agentcoop/config \
#       -v ~/.claude:/root/.claude \
#       coop:latest
#
#   acg-config/ must contain:
#     └── config.yaml   full gateway config (secrets in plaintext — chmod 600)
#
#   .env is no longer required: if acg-config/ still has one from before this
#   change, it's picked up on first start, its secret(s) folded into
#   config.yaml as literal values, and then removed automatically (one-time —
#   see `coop config migrate-env` for a manual/dry run).
#
#   See docker/docker-compose.example/config/ for a ready-to-copy template.
#
# Mode 2: Environment variables (quick start / CI)
#   config.yaml is auto-generated from env vars:
#
#     docker run \
#       -e RC_URL=http://rocketchat:3000 \
#       -e RC_USERNAME=mybot \
#       -e RC_PASSWORD=secret \
#       -e COOP_OWNER_USERS=alice \
#       -v ~/.claude:/root/.claude \
#       coop:latest
#
#   Optional env vars (Mode 2 only):
#     AGENT_TYPE        "claude" (default) or "opencode"
#     COOP_WATCHER_ROOM  room to watch (default: "@<first_owner>" DM)
#
# See docker/docker-compose.example/docker-compose.yml for a full example with all mounts.
# =============================================================================
set -euo pipefail

RUNTIME_DIR="$HOME/.agentcoop"

info()    { printf '\033[0;36m[AgentCoop]\033[0m %s\n' "$*"; }
success() { printf '\033[0;32m[AgentCoop]\033[0m %s\n' "$*"; }
warn()    { printf '\033[0;33m[AgentCoop]\033[0m %s\n' "$*"; }
error()   { printf '\033[0;31m[AgentCoop] Error:\033[0m %s\n' "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# Symlink config files from $RUNTIME_DIR/config (Mode 1)
# or generate them from env vars (Mode 2)
#
# $RUNTIME_DIR/config is the safe bind-mount point — it only holds config.yaml + .env,
# so mounting it never overwrites the rest of ~/.agentcoop.
# -----------------------------------------------------------------------------
MOUNTED_CONFIG="$RUNTIME_DIR/config/config.yaml"
MOUNTED_ENV="$RUNTIME_DIR/config/.env"

if [ -f "$MOUNTED_CONFIG" ]; then
    # ── Mode 1: symlink from mounted $RUNTIME_DIR/config ──────────────────────────────
    # Keyed off config.yaml ALONE, not "both files present": the gateway
    # auto-migrates a .env-backed secret into config.yaml on its first start
    # (one-time, docs/design/config-tool.md decision 6 revisited) and
    # removes .env once done. Requiring .env here would make Mode 1
    # misdetect as Mode 2 (and demand -e RC_URL=... again, or hard-fail) on
    # every container restart after that first migration.
    info "Config mode: volume mount ($RUNTIME_DIR/config detected)"

    ln -sf "$MOUNTED_CONFIG" "$RUNTIME_DIR/config.yaml"
    success "Symlinked: $RUNTIME_DIR/config.yaml → $MOUNTED_CONFIG"

    # .env is optional now — only present for a config not yet migrated.
    if [ -f "$MOUNTED_ENV" ]; then
        ln -sf "$MOUNTED_ENV" "$RUNTIME_DIR/.env"
        chmod 600 "$MOUNTED_ENV"
        success "Symlinked: $RUNTIME_DIR/.env → $MOUNTED_ENV"
    fi

else
    # ── Mode 2: generate config from env vars ────────────────────────────────
    info "Config mode: env vars ($RUNTIME_DIR/config not found — generating config)"

    : "${RC_URL:?RC_URL is required (or mount config.yaml to $RUNTIME_DIR/config)}"
    : "${RC_USERNAME:?RC_USERNAME is required}"
    : "${RC_PASSWORD:?RC_PASSWORD is required}"
    : "${COOP_OWNER_USERS:?COOP_OWNER_USERS is required (comma-separated, e.g. alice,bob)}"

    AGENT_TYPE="${AGENT_TYPE:-claude}"
    info "Generating config: agent=$AGENT_TYPE, owners=$COOP_OWNER_USERS"

    # Credentials go straight into config.yaml as plaintext (chmod 600, same
    # as onboard.py's own generator) — no .env, matching the rest of the
    # project post-decision-6-revisited. config.yaml is chmod'd below, right
    # after it's written.

    # Generate config.yaml. Deliberately a QUOTED heredoc ('PYEOF') so bash
    # never text-substitutes RC_URL/USERNAME/PASSWORD into the Python source
    # — a password containing a quote or backslash would otherwise corrupt
    # (or inject into) the script. Read via os.environ instead, same as
    # COOP_OWNER_USERS/AGENT_TYPE below — plain runtime lookup, no
    # interpolation-into-source-text risk.
    "$RUNTIME_DIR/repo/.venv/bin/python3" - << 'PYEOF'
import os, yaml

owner_users = [u.strip() for u in os.environ["COOP_OWNER_USERS"].split(",") if u.strip()]
agent_type  = os.environ.get("AGENT_TYPE", "claude")
runtime_dir = os.path.expanduser("~/.agentcoop")

# COOP_WATCHER_ROOM: a channel name to watch. Unset (the default) means the
# rule claims 1:1 DMs instead — a DM has no room name for a pattern to match,
# so an "@user" value also falls back to the DM opt-in.
watcher_room = os.environ.get("COOP_WATCHER_ROOM", "")
if watcher_room.startswith("@"):
    watcher_room = ""
watcher_rooms = (
    {"include": [watcher_room]} if watcher_room else {"direct": True}
)

config = {
    "connectors": [{
        "name": "rocketchat",
        "type": "rocketchat",
        "server": {
            "url":      os.environ["RC_URL"],
            "username": os.environ["RC_USERNAME"],
            "password": os.environ["RC_PASSWORD"],
        },
        "allowed_users": {
            "owners": owner_users,
            "guests": [],
        },
        "attachments": {
            "max_file_size_mb": 10,
            "download_timeout": 30,
        },
        "reply_in_thread":            False,
        "permission_reply_in_thread": True,
        "context_inject_files": [
            "contexts/rc-gateway-context.md",
        ],
    }],
    "agents": {
        "default-agent": {
            "type":              agent_type,
            "command":           agent_type,
            "working_directory": f"{runtime_dir}/work",
            "session_prefix":    "acg-e2e",
            "context_inject_files": [],
            "owner_allowed_tools": [
                {"tool": "Read"},
                {"tool": "Glob"},
                {"tool": "Grep"},
                {"tool": "WebSearch"},
            ],
            "guest_allowed_tools": [
                {"tool": "Read"},
                {"tool": "Glob"},
                {"tool": "Grep"},
            ],
            "timeout": 120,
            "permissions": {
                "enabled":             True,
                "timeout":             90,
                "skip_owner_approval": True,
            },
        }
    },
    "watcher_rules": [{
        "name":               "e2e-watcher",
        "connector":          "rocketchat",
        "rooms":              watcher_rooms,
        "agent":              "default-agent",
        "context_inject_files": [],
    }],
}

config_path = os.path.join(runtime_dir, "config.yaml")
with open(config_path, "w") as f:
    yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
os.chmod(config_path, 0o600)  # holds a plaintext secret now — same as .env always was

print(f"[AgentCoop] Written: {config_path}")
print(f"[AgentCoop]   agent={agent_type}, rooms={watcher_rooms}, owners={owner_users}")
PYEOF

    success "Config generated."
fi

# -----------------------------------------------------------------------------
# Ensure subdirectories exist inside mounted volumes
# (a bind mount replaces the directory, so pre-created subdirs may be gone)
# -----------------------------------------------------------------------------
mkdir -p "$RUNTIME_DIR/work" "$RUNTIME_DIR/logs" "$RUNTIME_DIR/contexts"

# Restore default context files only if the directory is empty.
# If the user mounted a custom contexts/ directory, leave it untouched.
if [ -z "$(ls -A "$RUNTIME_DIR/contexts" 2>/dev/null)" ]; then
    cp "$RUNTIME_DIR/repo/contexts/"* "$RUNTIME_DIR/contexts/" 2>/dev/null || true
    info "Restored default context files to $RUNTIME_DIR/contexts/"
else
    info "Using existing contexts: $(ls "$RUNTIME_DIR/contexts" | tr '\n' ' ')"
fi

# -----------------------------------------------------------------------------
# Pre-warm opencode
#
# First-time opencode startup in Docker can take longer than AgentCoop's 30-second
# health check timeout, causing the watcher to be skipped.
# Fix: actually start `opencode serve`, wait until it's healthy, then stop it.
# This lets opencode complete any first-time initialization (config creation,
# DB setup, etc.) before AgentCoop tries to start it for real.
# -----------------------------------------------------------------------------
if command -v opencode &>/dev/null; then
    PREWARM_PORT=19999
    mkdir -p "$RUNTIME_DIR/work/.prewarm"

    info "Pre-warming opencode (first-time init may take up to 60s)..."
    cd "$RUNTIME_DIR/work/.prewarm" && \
    opencode serve --port "$PREWARM_PORT" >/tmp/opencode-prewarm.log 2>&1 &
    OC_PREWARM_PID=$!

    OC_READY=false
    for i in $(seq 1 60); do
        if curl -sf "http://localhost:$PREWARM_PORT/session" >/dev/null 2>&1; then
            OC_READY=true
            success "OpenCode ready (took ${i}s)"
            break
        fi
        # Bail early if the process died
        if ! kill -0 "$OC_PREWARM_PID" 2>/dev/null; then
            warn "opencode serve exited unexpectedly during pre-warm"
            break
        fi
        sleep 1
    done

    # Stop the prewarm instance so AgentCoop can start its own
    kill "$OC_PREWARM_PID" 2>/dev/null || true
    wait "$OC_PREWARM_PID" 2>/dev/null || true

    if ! $OC_READY; then
        warn "OpenCode pre-warm timed out — AgentCoop will still try to start it"
        cat /tmp/opencode-prewarm.log >&2 || true
    fi
else
    warn "opencode not found in PATH — skipping pre-warm"
fi

# -----------------------------------------------------------------------------
# Start the gateway daemon
# -----------------------------------------------------------------------------
info "Starting AgentCoop..."
coop start

# Wait briefly and check status — but do NOT exit on failure.
# A misconfigured gateway should keep the container alive so the user can
# inspect logs and fix config without hitting a restart loop.
sleep 2
if coop status; then
    success "Gateway is running."
else
    warn "Gateway failed to start — container will stay alive for inspection."
    warn "Fix your config, then run: docker exec coop coop start"
    warn "Logs: docker logs coop  OR  docker exec coop tail -f $RUNTIME_DIR/gateway.log"
fi

# -----------------------------------------------------------------------------
# Trap SIGTERM/SIGINT — gracefully stop AgentCoop before the container exits
# This ensures the offline notification is sent to Rocket.Chat on docker stop.
# -----------------------------------------------------------------------------
cleanup() {
    info "Shutdown signal received — stopping AgentCoop..."
    coop stop
    info "AgentCoop stopped."
    exit 0
}
trap cleanup SIGTERM SIGINT

# -----------------------------------------------------------------------------
# Keep container alive — tail the log so output is visible via docker logs.
# Run tail in background (not exec) so the shell stays alive to handle signals.
# -----------------------------------------------------------------------------
tail -f "$RUNTIME_DIR/gateway.log" 2>/dev/null &
TAIL_PID=$!
wait $TAIL_PID
