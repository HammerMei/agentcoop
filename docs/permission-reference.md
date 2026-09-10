# Permission & RBAC Reference

Comprehensive guide to AgentCoop's permission and RBAC system.

## Overview

The gateway implements a **two-part permission model**:

1. **Role-Based Access Control (RBAC)** — Determines who can interact with agents and what tools they can execute
   - Roles: OWNER, GUEST, ANONYMOUS
   - Tool allowlists per role with pattern matching
   - Fail-closed behavior (unknown → GUEST)

2. **Human-in-the-Loop Approval (Optional)** — For tools not in auto-approve lists
   - Owner receives 🔐 notification in Rocket.Chat
   - Owner replies `approve <id>` or `deny <id>` to decide
   - Tool execution blocked until owner responds or timeout occurs

---

## Roles

### Role Resolution

Roles are determined per session and passed via environment variables:

| Role | Usage | Determined By |
|------|-------|---------------|
| OWNER | Full agent capabilities | `COOP_ROLE=owner` environment variable |
| GUEST | Read-only tools only | `COOP_ROLE=guest` environment variable |
| ANONYMOUS | Not supported | (fallback if unmapped) |

**Fail-closed default:** If a session has no role mapping, it defaults to **GUEST** (least privilege). This ensures that startup-order issues or state gaps never silently grant elevated permissions.

### Prompt Prefix Injection (Server-Trusted)

The gateway injects role information into the agent's prompt via the `COOP_ROLE` environment variable. This is server-controlled and trusted:

- Claude Code and OpenCode both receive this via `gateway/core/message_processor.py`
- The agent cannot override or spoof its role
- Used for role-aware prompting (e.g., "You are a trusted administrator")

---

## Tool Allow-Lists

### Configuration Format

Tool allowlists are defined in `config.yaml` per agent:

```yaml
agents:
  my-agent:
    type: claude
    owner_allowed_tools:           # Tools owner can use (may require approval)
      - tool: "Read"               # Regex on tool name (case-insensitive)
      - tool: "Bash"
        params: "git (log|diff).*" # Regex on primary parameter
      - tool: "WebFetch"
        params: "https?://.*\\.github\\.com/.*"
    guest_allowed_tools:           # Tools guest can use (auto-approved)
      - tool: "Read"
      - tool: "Grep"
      - tool: "Glob"
      - tool: "WebFetch"
        params: "https?://[^/]*\\.github\\.com/.*"
    permissions:
      enabled: true
      timeout: 300                 # seconds before auto-deny
```

### Named Presets

Any list item in `owner_allowed_tools` / `guest_allowed_tools` can be either
an inline rule object (as above) or a **string naming a top-level
`tool_presets` entry** — useful once the same rule set is repeated across
multiple agents:

```yaml
tool_presets:
  readonly:
    - tool: "Read"
    - tool: "Grep"
    - tool: "Glob"

agents:
  my-agent:
    owner_allowed_tools:
      - readonly                     # preset reference — expands to the 3 rules above
      - tool: "Bash"                 # inline rules and preset references mix freely
        params: "git (log|diff).*"
  other-agent:
    owner_allowed_tools:
      - readonly                     # same preset, no duplication
```

Presets are resolved before matching begins — at runtime there is no
difference between a rule that came from a preset and one written inline.
Rules:
- List order is preserved regardless of which items are presets vs. inline.
- A preset's own rule list must be flat inline rules only — a preset cannot
  reference another preset by name.
- All presets are regex-validated at config load time, even ones no agent
  currently references, so a broken preset fails fast instead of only
  surfacing when finally used.
- An unknown preset name in an allow-list is a config load error naming the
  agent, the field, and the list of presets that do exist.

### Matching Rules

Each rule is an object with two regex fields:

| Field | Required | Type | Behavior |
|-------|----------|------|----------|
| `tool` | Yes | Regex string | Case-insensitive fullmatch against tool name |
| `params` | No | Regex string | Case-insensitive fullmatch against primary parameter |

**Case Sensitivity:**
- Claude Code uses PascalCase tool names: `Bash`, `Read`, `WebFetch`
- OpenCode uses lowercase: `bash`, `read`, `webfetch`
- Matching is **case-insensitive**, so one config works for both backends

**Fullmatch Semantics:**
- `re.fullmatch(rule, input, re.IGNORECASE)` — entire string must match
- Use `.*` for prefix/suffix flexibility: `"echo .*"` matches `"echo hello world"`
- Anchors not needed (fullmatch anchors implicitly)

### Primary Parameter Mapping

Each tool type has a "primary parameter" that is extracted and matched:

| Tool (case-insensitive) | Source Field | Example |
|-------------------------|--------------|---------|
| Bash / bash | `tool_input["command"]` | `"git log --oneline"` |
| WebFetch / webfetch | `tool_input["url"]` | `"https://github.com/api/repos"` |
| Read / read | `tool_input["file_path"]` (normalized) | `"/home/user/project/file.txt"` |
| Edit / edit | `tool_input["file_path"]` (normalized) | `"./src/main.py"` |
| Write / write | `tool_input["file_path"]` (normalized) | `"output.json"` |
| MultiEdit / multiedit | `tool_input["file_path"]` (normalized) | `"README.md"` |
| NotebookEdit / notebookedit | `tool_input["notebook_path"]` (normalized) | `"analysis.ipynb"` |
| MCP tools (`mcp__*`) | Full `tool_input` as JSON | `{"query": "..."}` |
| Unknown tools | Full `tool_input` as JSON | (fallback) |

### Tool-Specific Matching Behavior

#### Bash Commands (tree-sitter AST Splitting)

Compound bash commands are split into individual sub-commands via `tree-sitter-bash`. **ALL sub-commands must match** for auto-approval:

```bash
# Config allows:
params: "echo .*"

# Tool call:
command: "echo hello && rm -rf /"

# Result: Deny
# Sub-commands: ["echo hello", "rm -rf /"]
# "rm -rf /" does NOT match "echo .*" → blocked
```

**Opaque nodes (treated as single units):**
- Command substitutions: `echo $(dangerous_cmd)` — the full substitution is treated opaque
- Process substitutions: `<(sort file) >(wc -l)` — treated opaque
- Config authors should be aware: `params: "echo .*"` permits `echo $(anything)`

**Fallback (tree-sitter unavailable):**
- If `tree-sitter` or `tree-sitter-bash` is not installed, the whole command is treated as one string
- Warning logged on first use; compound splitting is disabled but gateway continues operating

#### File Tools (Read, Edit, Write, MultiEdit, NotebookEdit)

File paths are **normalized via `os.path.normpath()`** before matching to prevent path-traversal bypasses:

```python
# Config allows:
params: "/home/user/project/.*"

# Tool call with traversal attempt:
file_path: "/home/user/project/../../../etc/passwd"

# Result: Deny
# After normpath: "/etc/passwd"
# "/etc/passwd" does NOT match "/home/user/project/.*" → blocked
```

**Normalization process:**
- Relative paths are resolved against the session's `working_directory` first
- `os.path.normpath()` (not `realpath()`) is used so checks work for non-existent files (e.g., Write creating a new file)

#### WebFetch (URL Extraction)

The `url` parameter is matched directly. **SSRF Protection Note:**

```yaml
# Config authors should avoid permissive regexes:
guest_allowed_tools:
  - tool: "WebFetch"
    params: ".*"  # ❌ Dangerous — allows internal addresses

  - tool: "WebFetch"
    params: "https?://[^/]*\\.github\\.com/.*"  # ✅ Explicit domain allowlist
```

OpenCode's built-in validation only checks `http://` or `https://` — it does not block loopback, link-local, or AWS metadata addresses. Config authors must use explicit domain patterns for security.

#### Other Tools (Full JSON Match)

Tools without a known primary parameter field (including all MCP tools) match against the full `tool_input` serialized as JSON:

```python
tool_input = {"query": "search term", "limit": 10}
param_string = '{"query": "search term", "limit": 10}'  # JSON serialized
```

This provides maximum flexibility but requires exact knowledge of the tool's input structure. Regex patterns should be carefully crafted to avoid false positives/negatives.

---

## Permission Broker Decision Logic

### The Decision Tree

When a tool call arrives, the broker executes this decision tree:

```
Tool call arrives
  ├─ session_id → role lookup
  │   └─ not found → role = "guest" (fail-closed)
  │
  ├─ role == "guest"?
  │   ├─ YES → Check guest_allowed_tools
  │   │   ├─ matches → ALLOW (auto-approve, silent)
  │   │   └─ no match → DENY (auto-deny, silent, no RC notification)
  │   │
  │   └─ NO (role == "owner")
  │       ├─ skip_owner_approval enabled?
  │       │   └─ YES → ALLOW (auto-approve, no RC notification)
  │       │
  │       ├─ Check owner_allowed_tools
  │       │   └─ matches → ALLOW (auto-approve, no RC notification)
  │       │
  │       ├─ room_id available?
  │       │   ├─ YES → ASK (post 🔐 notification, await owner decision)
  │       │   └─ NO → DENY (cannot route, block as safe default)
```

### Decision Outcomes

| Outcome | Owner RC Notification? | Guest RC Notification? | Behavior |
|---------|------------------------|------------------------|----------|
| ALLOW | No | No | Tool executes immediately |
| BLOCK | No | No | Tool rejected with reason (owner gets error) |
| ASK | Yes (🔐) | Never | Tool blocked; awaits `approve <id>` or `deny <id>` |

### Guest Isolation

Guest sessions:
- Can only execute tools in `guest_allowed_tools`
- Rejections are **silent** (no RC notification, no visual feedback to owner)
- Cannot trigger approval workflow (no room to post notifications)
- Default is least privilege (all tool calls denied unless explicitly allowlisted)

---

## Approval Workflow

### Step-by-Step Execution

#### 1. Tool Call Arrives, Decision = "ASK"

```
Tool execution paused (connection held open)
  ↓ (backend-specific)
Broker generates 4-char request ID: "a3k9"
```

#### 2. Notification Posted to Rocket.Chat

```
Message format:
  🔐 **Permission required** `[a3k9]`
  **Tool:** `Bash`
  **Params:** `command='git log --oneline'`
  Reply `approve a3k9` or `deny a3k9`
```

**Key details:**
- `[a3k9]` — 4-character lowercase alphanumeric ID (cryptographically random via `secrets.choice()`)
- Tool name and parameters included (sanitized for display)
- Posted to the room associated with the session
- Can be posted into a thread (via `thread_id` if provided)

#### 3. Owner Responds

Owner types in Rocket.Chat (no slash prefix):
```
approve a3k9
```

The gateway intercepts this message **before routing to the agent** via `MessageDispatcher.dispatch()`:

```python
if msg.role == UserRole.OWNER and re.match(r"^(approve|deny)\s+([a-z0-9]+)$", msg.text):
    # Handle approval command → do not forward to agent
    registry.resolve(request_id=msg.match.group(2), approved=(msg.match.group(1)=="approve"))
    return  # never queued
```

**Interception location:** At `dispatch()` time in `MessageDispatcher`, BEFORE messages are passed to `MessageProcessor`, to avoid deadlock (see Slash Command Interception below).

#### 4. Approval Broker Response

Broker returns one of:

| Owner Input | Response | Tool Outcome |
|------------|----------|-------------|
| `approve a3k9` (correct) | ✅ Permission `a3k9` approved. | Tool executes |
| `deny a3k9` (correct) | ❌ Permission `a3k9` denied. | Tool rejected |
| `approve xyz` (wrong length) | ⚠️ Invalid ID `xyz` — expected 4 characters. | No change |
| `approve a3k0` (not found) | ⚠️ No pending permission request with ID `a3k0`. | No change |
| `approve` (no ID) | (not matched → forwarded to agent) | No change |

#### 5. Timeout (Auto-Deny)

If owner does not respond within `timeout_seconds` (config, default 300s):

```
asyncio.wait_for(req.future, timeout=300)
  → TimeoutError after 300s
  → Auto-deny
  → ⏱️ Permission request `a3k9` timed out — **auto-denied**.
```

### Approval Command Interception

> **Why intercept in `MessageDispatcher.dispatch()` and not `MessageProcessor._process()`?**
>
> The MessageProcessor has a single consumer task. While `agent.send()` is suspended waiting for the HTTP hook response (which is blocked on `request_permission()`), the consumer cannot process any queued messages. If `approve` enters the queue normally, it will never be reached — deadlock.
>
> By intercepting in `MessageDispatcher.dispatch()`, approval commands are handled immediately on arrival, resolving the asyncio.Future and unblocking the entire chain.

**Regex design:**
- Pattern: `^(approve|deny)\s+([a-z0-9]+)$` (no slash prefix)
- Matches `approve` or `deny` followed by any alphanumeric string
- 4-char length check happens **inside** the handler (not in regex) to enable friendly mistype feedback
- `approve` with no ID does not match → falls through to agent as a normal message

### Message Flow During Approval

```
MSG1 ("delete file X")
  → queue → consumer → agent.send() SUSPENDED (waiting for permission)

MSG2 ("regular message")
  → queue → waits for consumer (agent will NOT respond until approval is resolved)

MSG3 ("approve a3k9")
  → dispatch() → intercepted before queue
  → registry.resolve() → future resolved
  → agent.send() unblocks → Claude continues
  → consumer finishes MSG1, then processes MSG2
```

**Implication:** While a permission is pending, the agent will not respond to regular messages. This is intentional — processing new agent messages while a tool decision is pending would be confusing.

### Edge Cases

| Scenario | Handling |
|----------|----------|
| Duplicate `approve` on resolved ID | `registry.resolve()` checks `future.done()` → returns False → "No pending" reply |
| Guest sends `approve` | Not intercepted (role check fails) → falls through to agent as normal message |
| Two tools pending simultaneously | Each has distinct request ID; owner approves independently in any order |
| Wrong ID in reply | `resolve()` returns False → "No pending request" reply; real request keeps waiting |
| Approval from different room | Approval command only intercepted in the room's own MessageDispatcher; approval must happen in the same room as notification |
| Session stopped while approval pending | MessageProcessor calls `registry.cancel_session(session_id)` for all pending requests of that session |
| Gateway restart | Pending requests lost (in-memory only); user must re-send original message |
| Network error posting notification | `PermissionNotificationError` raised; tool blocked as safe default; user sees "connection error" message |
| OpenCode SSE disconnect | SSE listener reconnects with 3-second backoff |

---

## Backend-Specific Implementation

### Claude Code

**Transport:** HTTP PreToolUse hook (POST to local endpoint)

**Lifecycle:**
1. ClaudePermissionBroker starts asyncio HTTP server on random localhost port
2. Generates `settings.json` file with hook URL and matcher regex
3. Gateway passes `--settings <path>` to every `claude -p` invocation
4. Claude fires PreToolUse event → POSTs to `http://127.0.0.1:<port>/hook`
5. HTTP handler calls `request_permission()`, blocking the connection
6. Owner replies `approve <id>` in RC → HTTP response `{"decision": "allow/deny"}`
7. Claude resumes or skips tool call

**Hook Handler Flow:**

```python
_handle_hook(raw_body):
    1. Parse JSON (PreToolUse event)
    2. Extract tool_name, tool_input, cwd, session_id
    3. Call get_param_strings_for_claude() in thread (tree-sitter is sync C extension)
    4. Resolve role from session_role_map (fail-closed → "guest")
    5. Call _decide(tool_name, param_strings, role, room_id)
       → "allow": return {"decision": "allow"}
       → "block": return {"decision": "block", "reason": "..."}
       → "ask": await request_permission() → return allow/deny
    6. Return JSON response
```

**Matcher field in settings:**
```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
      "type": "http",
      "url": "http://127.0.0.1:<port>/hook",
      "timeout": 310
    }]
  }
}
```

The `matcher` field ensures read-only tools (Read, Grep, Glob, WebFetch) never trigger the HTTP hook — guest sessions using whitelisted read-only tools never reach the approval flow.

**Fail-Closed Behaviors:**
- Unknown session → defaults to role="guest"
- JSON parse error → block tool as safe default
- No room mapping → block tool (cannot post RC notification)
- Tree-sitter unavailable → treat whole bash command as one (no splitting)

### OpenCode

**Transport:** Server-Sent Events (SSE) + REST reply API

**Lifecycle:**
1. OpenCodePermissionBroker starts background SSE listener
2. Connects to `<opencode_base_url>/event` (long-lived stream)
3. opencode emits `permission.asked` SSE event when a tool needs approval
4. Broker processes event, decides action, routes to owner if needed
5. Owner replies `approve <id>` in RC
6. Broker calls `POST /permission/{opencode_req_id}/reply {"reply": "once" or "reject"}`
7. opencode unblocks tool or rejects it

**SSE Event Format:**
```json
{
  "type": "permission.asked",
  "properties": {
    "id": "per_...",
    "sessionID": "ses_...",
    "permission": "bash",
    "patterns": ["echo hello", "rm -rf /"],
    "metadata": {},
    "tool": { "messageID": "msg_...", "callID": "call_..." }
  }
}
```

**Key detail:** All permission fields are under `properties`, not at top level. The tool name is `properties.permission` (not `properties.type`).

**Multi-Pattern Enforcement:**

OpenCode already parses compound bash commands via tree-sitter internally, producing one pattern per AST command node:

```python
# OpenCode patterns for: "echo hello && rm -rf /"
patterns = ["echo hello", "rm -rf /"]

# Gateway must require ALL patterns to match
if not all_params_match_any(rules, tool_name, patterns):
    # At least one pattern doesn't match → deny
```

This prevents bypasses where dangerous sub-commands sneak through (see tool_match.py — `all_params_match_any()`).

**Reply API:**
```bash
POST /permission/{requestID}/reply
Content-Type: application/json

{"reply": "once"}     # approve
{"reply": "reject"}   # deny
```

**Connection Resilience:**
- SSE stream disconnects → automatic reconnect with 3-second backoff
- Reply API timeout → 10-second timeout, error logged
- On broker shutdown: auto-deny all pending requests (POST reply: "reject")

**Fail-Closed Behaviors:**
- Unknown session → defaults to role="guest"
- No room mapping → auto-deny (cannot post RC notification)
- Malformed SSE JSON → warning logged, event ignored
- Unknown event type → ignored

---

## Configuration Reference

### Complete YAML Schema

```yaml
tool_presets:
  a-preset-name:
    - tool: "regex_pattern"
      params: "optional_regex_pattern"

agents:
  my-agent:
    # ... agent config ...

    owner_allowed_tools:
      - a-preset-name                # string = tool_presets reference
      - tool: "regex_pattern"        # object = inline rule; the two mix freely
        params: "optional_regex_pattern"

    guest_allowed_tools:
      - tool: "regex_pattern"
        params: "optional_regex_pattern"

    permissions:
      enabled: true                 # Enable approval workflow
      timeout: 300                  # Seconds before auto-deny (default 300)
      skip_owner_approval: false    # If true, all owner tool calls auto-approved (for sandbox/dev)

```

### Field Reference

| Path | Type | Default | Description |
|------|------|---------|-------------|
| `tool_presets` | map[string, list[ToolRule]] | `{}` | Named, reusable tool-rule lists |
| `agents[*].owner_allowed_tools` | list[ToolRule \| preset name] | `[]` | Tools owner can execute without approval |
| `agents[*].owner_allowed_tools[*].tool` | regex string | (required) | Case-insensitive regex on tool name |
| `agents[*].owner_allowed_tools[*].params` | regex string | (optional) | Case-insensitive regex on primary parameter |
| `agents[*].guest_allowed_tools` | list[ToolRule \| preset name] | `[]` | Tools guest can execute (auto-approved) |
| `agents[*].guest_allowed_tools[*].tool` | regex string | (required) | Case-insensitive regex on tool name |
| `agents[*].guest_allowed_tools[*].params` | regex string | (optional) | Case-insensitive regex on primary parameter |
| `agents[*].permissions.enabled` | bool | `false` | Enable approval workflow for this agent |
| `agents[*].permissions.timeout` | int | 300 | Seconds before auto-deny |
| `agents[*].permissions.skip_owner_approval` | bool | `false` | Auto-approve all owner tool calls (sandbox/dev only) |

### Example: Multi-Backend Config

```yaml
agents:
  assistance:
    type: claude
    command: claude
    owner_allowed_tools:
      - tool: "Read"                       # no params = tool-only check
      - tool: "Bash"
        params: "git (log|diff|status).*"  # require matching git subcommand
      - tool: "Write"
        params: "/workspace/.*"            # restrict to /workspace directory
    guest_allowed_tools:
      - tool: "Read"
      - tool: "Grep"
      - tool: "Glob"
      - tool: "WebFetch"
        params: "https?://[^/]*\\.github\\.com/.*"
    permissions:
      enabled: true
      timeout: 300

  opencode:
    type: opencode
    command: opencode
    owner_allowed_tools:
      - tool: "bash"
        params: "echo .*"
    guest_allowed_tools:
      - tool: "bash"
        params: "ls"
    permissions:
      enabled: true
      timeout: 600
      skip_owner_approval: false  # require approval even for owners
```

---

## Security Considerations

### Fail-Closed Design

- **Unknown role** → defaults to GUEST (least privilege)
- **No room mapping** → tool rejected (cannot post RC notification)
- **Parse error** → tool rejected as safe default
- **Guest auto-deny silent** → no RC feedback (reduces noise, prevents info leakage)

### Bash Parsing Security

- Compound commands split via tree-sitter AST
- All sub-commands must match for auto-approval
- Command substitutions treated as opaque (regex sees full text, not recursive evaluation)
- If tree-sitter unavailable: whole command treated as one string (degrades safely, no bypass)

### Path Normalization

- `os.path.normpath()` applied to all file tool paths before regex matching
- Prevents path-traversal bypasses: `/project/../../../etc/passwd` → `/etc/passwd`
- Uses `normpath()` not `realpath()` so checks work for non-existent files (Write, etc.)

### Guest Isolation

- Guest tool rejections are **silent** (no RC notification)
- Prevents information leakage (attacker learns what tools exist by watching chat)
- Owner only sees guest rejections in logs
- Guest cannot trigger approval workflow (cannot request RC notification)

### URL Validation (WebFetch)

- Regex is the **only SSRF protection** (OpenCode does not block internal addresses)
- Config authors must use explicit domain patterns: `"https?://[^/]*\\.github\\.com/.*"`
- Avoid permissive regexes: `".*"` allows fetching localhost, 169.254.169.254, etc.

### Claude Sandbox Note

- Claude Code has a built-in sandbox (subprocess isolation)
- Gateway permission system adds **additional control layer**
- Denial of service via long timeouts still possible (tool blocks for 300s)
- Assume owner is trusted (can view tool requests in logs)

### ID Collision Prevention

- Request IDs generated via `secrets.choice()` (cryptographically random)
- 4-char IDs from 36-char alphabet: ~1.7 million possible values
- Collision check: if ID exists, regenerate (max 100 retries, then raise RuntimeError)
- IDs cannot be guessed by watching RC chat (secure RNG)

---

## Extending

### Custom PermissionBroker Implementation

To implement a custom broker for a new backend:

**1. Subclass PermissionBroker:**

```python
from gateway.core.permission import PermissionBroker, PermissionRegistry, PermissionNotifier

class MyCustomBroker(PermissionBroker):
    def __init__(
        self,
        registry: PermissionRegistry,
        notifier: PermissionNotifier,
        owner_allowed_tools: list[ToolRule] | None = None,
        guest_allowed_tools: list[ToolRule] | None = None,
        timeout_seconds: int = 300,
        skip_owner_approval: bool = False,
    ) -> None:
        super().__init__(registry, notifier, timeout_seconds)
        self._owner_allowed_tools = owner_allowed_tools or []
        self._guest_allowed_tools = guest_allowed_tools or []
        self._skip_owner_approval = skip_owner_approval

    async def start(self) -> None:
        """Initialize any background resources (servers, listeners, etc.)."""
        pass

    async def stop(self) -> None:
        """Clean up resources on shutdown."""
        pass
```

**2. Implement Tool Interception Logic:**

When your backend notifies you of a tool call:

```python
# Your backend calls this
tool_name = "Bash"
tool_input = {"command": "git log"}
session_id = "sess_..."
room_id = "room_..."

# Extract parameters (use tool_match utilities)
from gateway.core.tool_match import (
    get_param_strings_for_claude,  # or get_param_strings_for_opencode
    all_params_match_any,
)

param_strings = get_param_strings_for_claude(tool_name, tool_input, cwd="")
role = session_role_map.get(session_id, "guest")  # fail-closed

# Make decision (reuse decision tree from ClaudePermissionBroker._decide)
if role == "guest":
    if all_params_match_any(self._guest_allowed_tools, tool_name, param_strings):
        return "allow"
    return "block"

if self._skip_owner_approval:
    return "allow"

if all_params_match_any(self._owner_allowed_tools, tool_name, param_strings):
    return "allow"

if not room_id:
    return "block"

# Request approval (inherited method)
approved = await self.request_permission(
    tool_name=tool_name,
    tool_input=tool_input,
    session_id=session_id,
    room_id=room_id,
)
return "allow" if approved else "block"
```

**3. Wire into GatewayService:**

```python
# gateway/service.py
broker = MyCustomBroker(
    registry=permission_registry,
    notifier=permission_notifier,
    owner_allowed_tools=agent_config.owner_allowed_tools,
    guest_allowed_tools=agent_config.guest_allowed_tools,
    timeout_seconds=core_config.permission_timeout,
    skip_owner_approval=agent_config.permissions.skip_owner_approval if agent_config.permissions else False,
)
await broker.start()
# ... wire session_room_map, session_role_map into broker
# ... store broker for lifecycle management
```

### Custom Tool Matching

To implement custom matching logic for a tool not covered by `tool_match.py`:

```python
from gateway.core.tool_match import matches_rule

# For a new tool type, add to _CLAUDE_PARAM_FIELD mapping:
_CLAUDE_PARAM_FIELD["custom_tool"] = "custom_param_field"

# Or override matches_any() for custom semantics:
def matches_any_custom(rules, tool_name, param_string):
    for rule in rules:
        if rule.tool == "custom_tool":
            # Custom logic
            return special_matching(rule, param_string)
    return matches_any(rules, tool_name, param_string)  # fallback
```

---

## Glossary

| Term | Definition |
|------|-----------|
| **RBAC** | Role-Based Access Control — permission system based on user roles (OWNER, GUEST) |
| **Allow-list** | Explicit list of permitted tools (whitelist) |
| **Auto-approve** | Tool executes immediately without owner intervention (matched allow-list) |
| **Auto-deny** | Tool rejected immediately without owner intervention (not in allow-list or guest-only) |
| **Fail-closed** | Default to least privilege when information is missing (unknown role → GUEST) |
| **Fail-open** | Default to most privilege (dangerous — avoided in this design) |
| **Request ID** | 4-char alphanumeric ID for a pending approval (e.g., "a3k9") |
| **Approval command** | Plain-text command without a `/` prefix (e.g., `approve a3k9` or `deny a3k9`) |
| **Notifier** | Component that posts RC messages on behalf of the broker |
| **Registry** | In-process store of pending permission requests |
| **Matcher** | Regex rule that determines if a tool call matches an allow-list |
| **Normpath** | Path normalization via `os.path.normpath()` (collapses `..` components) |
| **SSE** | Server-Sent Events (OpenCode's event transport) |
| **Opaque node** | Bash subtree that is not recursed into (command substitutions, process substitutions) |

---

*Last updated: 2026-03-29*
