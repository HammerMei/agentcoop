# Agent-to-Agent Communication & Loop Protection

## Overview

What if your AI agents could talk to each other?

Imagine a Rocket.Chat room where a **research agent** gathers information, then asks a **writing agent** to draft a report. Or a **code review agent** examines a PR, then delegates testing to a **test automation agent**. Or a **project manager agent** breaks down a task and routes it to specialists — all without human hand-offs.

Agent-to-agent communication turns Rocket.Chat into a coordination layer for heterogeneous AI systems. Multiple AgentCoop bots with different backends (Claude Code, OpenCode, custom agents) share a room and collaborate via natural language messages. The chat platform becomes the "bus" — transparent, auditable, and always under human observation.

This is fundamentally different from traditional agent frameworks where agents communicate via direct API calls. Here, agents are loosely coupled, can be deployed independently, and humans can observe or intervene in the conversation at any point.

**The challenge:** Without safeguards, two agents responding to each other create infinite loops. Agent A posts → Agent B replies to A → Agent A detects a new message and replies → Agent B replies again → ...

The `agent_chain` feature solves this with three protective layers: **LLM self-termination** (primary), **per-sender turn budgets** (safety net), and **TTL garbage collection** (cleanup).

---

## How It Works

### Layer 1: LLM Self-Termination (Primary Defense)

When AgentCoop forwards an agent message to Claude (or another LLM backend), it appends a special prompt suffix that teaches the agent to detect loops and exit gracefully.

**Example suffix (simplified):**
```
You are in an agent-to-agent conversation. Multiple AI agents are collaborating in this room.

If you detect a loop (agents repeating the same exchange), or if you have nothing meaningful to add, 
please respond with: <end-of-agent-chain>

This tells the gateway to stop the conversation gracefully without posting your response.
```

The agent reads this instruction and decides whether to continue or stop. If it chooses to stop, it includes the token `<end-of-agent-chain>` in its response. AgentCoop detects this token, **silently drops the response** (never posts it), and the chain stops naturally.

**Why this is the primary layer:** It's the most elegant. The agent understands the situation and makes an informed decision. No hard limits, no counter-based cutoffs — just intelligent self-regulation.

---

### Layer 2: Per-Sender Turn Budget (Safety Net)

Even with self-termination, we need a hard ceiling. What if the LLM doesn't recognize the loop, or if loop detection fails?

Each agent sender gets an independent turn counter per room/thread. Once `max_turns` is exceeded, AgentCoop force-drops all further messages from that sender until:
- A **human message** arrives (which resets all counters for that room/thread)
- The **TTL expires** (configurable grace period)

**Example scenario:**
- Agent A and Agent B start a conversation
- Both understand the self-termination signal → conversation ends naturally within 2–3 turns (typical)
- If either agent keeps responding anyway → turn counter increments
- After 5 turns (default), Agent B's message is silently dropped
- Agent A doesn't see a new message, stops waiting, the loop ends

**Counter independence:** Two different bot pairs in the same room each get their own budget. Agent A ↔ Agent B don't affect Agent C ↔ Agent D's quotas.

---

### Layer 3: TTL Garbage Collection (Cleanup)

Turn counters are ephemeral. If a room falls silent for `ttl_seconds` (default 3600 = 1 hour), all counters for that room are purged lazily on the next incoming message. The next conversation starts fresh with a full budget.

This prevents stale counters from "using up" budget on the next task. For example, if an agent used 4 of 5 turns at 9:00 AM and the next message arrives at 10:05 AM (beyond the 1-hour TTL), the counter is wiped and the agent gets all 5 turns again.

---

## Counter Reset Rules

Counters reset under these conditions:

| Condition | Effect |
|-----------|--------|
| **Human message arrives** | `reset_all` — all agent counters in that room/thread are cleared. Fresh budget for everyone. |
| **Self-termination token detected** | Counter stays. The chain dies naturally (no reply posted = no trigger for the other agent). |
| **Force-drop triggered** | Counter stays locked until human message or TTL expiry. |
| **TTL expires (lazy GC)** | Stale entry is removed on next access — sender gets a fresh full budget. |

The "reset_all on human message" rule is crucial: it gives humans a way to restart conversations. If Alice sends a new message after an agent chain went silent, both agents get a fresh turn budget.

### Design note: single TTL vs two-TTL

Force-drop and self-termination have different semantics — like a dropped call vs a natural hang-up:

- **Force-drop** (budget exhausted, loop cut short): the conversation was interrupted mid-thought. The other agent is likely to retry quickly. A *shorter* TTL would unlock budget sooner.
- **Self-termination** (LLM gracefully exits): the task is done and the room should stay quiet. A *longer* TTL gives more cooldown before fresh budget is granted.

Currently a single `ttl_seconds` covers both cases for simplicity. If real-world usage shows the single value is too coarse — agents restarting too aggressively after force-drops, or not getting fresh budget soon enough after clean terminations — consider splitting into `force_drop_ttl` and `self_terminate_ttl` in a future release.

---

## Configuration

Add the `agent_chain` block to your connector config:

```yaml
connectors:
  - name: rc-home
    type: rocketchat
    server:
      url: "https://chat.example.com"
      username: "my-bot"
      password: "your-bot-password"
    allowed_users:
      owners:
        - alice
      guests: []
    
    # ── Agent-to-agent loop protection ───────────────────────────────────
    agent_chain:
      agent_usernames:
        - another-bot                  # RC username of the other AgentCoop bot
        - research-agent               # and any other agent bots in this room
      max_turns: 5                     # turns per agent before force-drop (default: 5)
      ttl_seconds: 3600                # idle timeout in seconds (default: 3600)
```

### Configuration Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `agent_usernames` | List[str] | — | **Required.** Rocket.Chat usernames of other AgentCoop bots that may send messages to this room. Messages from these senders bypass the allow-list check and are tracked against their own turn budget. |
| `max_turns` | Int | 5 | Maximum turns (response count) per agent per room/thread before force-drop. Use higher values (10+) for complex multi-turn tasks; lower values (2–3) for simple exchanges. |
| `ttl_seconds` | Int | 3600 | Idle timeout in seconds. Stale counters older than this are automatically deleted. 3600 = 1 hour. |

**When `agent_chain` is omitted:** Agent-to-agent communication is disabled. Messages from unknown senders are treated as regular user messages. No loop protection is active.

### Addressing and `@all`

In Rocket.Chat rooms, AgentCoop adds a trusted `to:` field to each agent prompt so agents can
tell whether a message is meant for them, another agent, or the room at large:

- `to: me` — this bot was explicitly mentioned, or the message arrived as a DM.
- `to: @other-agent` — another configured agent was mentioned; this bot should usually stay silent.
- `to: me+@other-agent` — this bot and another configured agent were both mentioned.
- `to: @all` — the sender used Rocket.Chat's room-wide `@all` mention; broader agent fan-out is intentional.
- `to: me+@all+@other-agent` — room-wide fan-out plus specific priority responders.
- `to: *` — no explicit agent mention; agents should be conservative and respond only with useful new information.

When `@all` appears together with specific agent mentions, AgentCoop preserves the specific
mentions as **priority responders** rather than letting `@all` erase them. Agents that
do not have useful, non-duplicative input should respond with only
`<end-of-agent-chain>` so the chain terminates cleanly.

---

## Prompt Injection & Turn Awareness

AgentCoop doesn't send the raw agent message to Claude. It wraps it with context:

### Normal Turns
```
[Original agent message]

---
[AGENT CHAIN CONTEXT]
You are in an agent-to-agent conversation. Other AI agents may respond to you.
If you detect a loop or have nothing meaningful to add, respond with: <end-of-agent-chain>
```

### Penultimate Turn (N-1)
```
[Original agent message]

---
[AGENT CHAIN CONTEXT]
⚠️  This is your second-to-last turn in this conversation. Your next response will be the final one.
If you detect a loop or have nothing meaningful to add, respond with: <end-of-agent-chain>
```

### Final Turn (N)
```
[Original agent message]

---
[AGENT CHAIN CONTEXT]
⚠️  This is your final turn. After this response, the conversation will be locked until a human sends a new message.
Consider scheduling a follow-up task if more work is needed (use `coop schedule`).
If you have nothing meaningful to add, respond with: <end-of-agent-chain>
```

The agent sees these markers and can adjust its behavior accordingly. On the final turn, it's encouraged to schedule a continuation rather than struggle against the hard limit.

---

## Real-World Examples

### Example 1: Research → Writing Pipeline

Setup:
- Room: `#project-tasks`
- Agent A: `research-bot` (queries APIs, gathers data)
- Agent B: `write-bot` (drafts documents)

```
alice: Please research the latest AI trends and write a summary.

research-bot: Found 3 major trends: multimodal models, agentic workflows, and cost optimization.
  More details:
  - Multimodal: GPT-4V, Claude 3.5, Gemini 2.0 support image+video input
  - Agentic: LLM agents can now run tools in loops without human approval in some platforms
  - Cost: New quantization and distillation techniques reduce inference costs by 50%
  
  @write-bot: Here's the research. Can you draft a 2-paragraph summary for our newsletter?

write-bot: Done! Here's a draft:
  
  "Recent advances in AI are reshaping the industry across three fronts. Multimodal models
  like GPT-4V and Claude 3.5 now seamlessly process images and videos alongside text,
  enabling richer analysis and more intuitive interfaces. Simultaneously, agentic workflows
  have matured, allowing AI systems to autonomously plan and execute complex tasks.
  
  Equally important are efficiency improvements. New quantization and distillation techniques
  now cut inference costs by 50%, bringing advanced AI capabilities within reach of smaller
  organizations. These trends converge to a future where AI is more capable, autonomous, and
  economical."
  
  Ready to publish!

research-bot: <end-of-agent-chain>  ← self-terminates (nothing to add)

write-bot: <end-of-agent-chain>  ← self-terminates (task complete)

[Conversation ends. Both agents exited gracefully.]

alice: Thanks! Let me review and post this.
```

### Example 2: Code Review → Testing

Setup:
- Room: `#code-review`
- Agent A: `review-bot` (examines diffs, flags issues)
- Agent B: `test-bot` (runs test suites)

```
alice: Please review PR #42 and test it.

review-bot: Reviewed the changes in PR #42:
  ✅ Code style is clean
  ⚠️  Missing test for new `validate_email()` function
  ✅ No security issues detected
  
  @test-bot: Can you run the test suite and report coverage?

test-bot: Running tests on PR #42...
  ✓ 145 tests passed
  ✗ 2 tests failed (email validation edge cases)
  📊 Coverage: 87% (target: 90%)
  
  The failures match review-bot's concern about missing tests.

review-bot: Noted. Recommended action:
  1. Add unit tests for `validate_email()` edge cases
  2. Rerun the test suite
  3. Aim for 90%+ coverage
  
  @test-bot: Once fixes are committed, will you retest?

test-bot: Will do! Let me know when the new commit is pushed.

[Conversation pauses. Further progress requires human action — code changes + new commits.]

alice: I'll fix the validation logic and push. Then test-bot can rerun.
```

---

## Best Practices

### 1. Name Your Agents Clearly

Use descriptive usernames in `agent_usernames`:
```yaml
agent_chain:
  agent_usernames:
    - claude-research-bot
    - claude-write-bot
    - opencode-deploy-bot
```

Not just `bot1`, `bot2`, `bot3`. Clear names help humans understand the conversation flow.

### 2. Tune `max_turns` to Your Use Case

- **Simple exchanges** (ask → answer): `max_turns: 2` or `3`
- **Multi-step tasks** (research → draft → refine): `max_turns: 5` to `7`
- **Complex workflows** (iterative debugging): `max_turns: 10` to `15`

Higher values give more flexibility but risk longer conversation loops if self-termination fails. Start with 5 (the default) and adjust based on experience.

### 3. Let Humans Anchor Conversations

Agent-to-agent chains work best when humans start them:

Good:
```
alice: Research this topic and write a summary.
```

Less ideal:
```
schedule a recurring task that triggers agent-chain every hour
```

Human initiation sets context, allows observation, and makes the `reset_all` rule meaningful.

### 4. Watch for Stale Counters

If a room is quiet for `ttl_seconds` (default 1 hour), all counters are purged. This is usually fine, but if you have a high-volume room with constant human chatter, stale agent counters will clean up automatically. You don't need to do anything.

### 5. Use the Scheduler for Continuations

If an agent reaches the final turn but the task isn't done, encourage it to schedule a follow-up:

```
Prompt:
  "If you need to continue, use: coop schedule create rc:general 
   'Continue the X task' --every 1h --times 1"
```

This lets agents hand off work gracefully and restart with a fresh budget after a delay.

### 6. Observe Conversations in the Room

Don't hide agent conversations in threads. Let humans see what's happening. Open threads reduce surprise and allow for timely intervention.

### 7. Use `@all` Sparingly

Use Rocket.Chat `@all` only when you intentionally want every configured agent in the
room to consider responding. Prefer specific agent mentions such as `@research-bot` for
directed handoffs. If you combine `@all` with a specific agent mention, that specific
agent remains a priority responder while other agents should only add non-duplicative
value.

---

## Troubleshooting

### Agents Keep Looping Despite `agent_chain`

**Symptom:** Agents respond to each other endlessly.

**Solutions:**
1. **Verify `agent_usernames` is set:** Check your config. Are the bot names spelled correctly?
2. **Check loop detection is working:** Look for `<end-of-agent-chain>` tokens in the chat logs. If they don't appear, the LLM isn't recognizing the self-termination instruction.
3. **Lower `max_turns`:** If self-termination isn't reliable, reduce the hard limit (e.g., `max_turns: 2`).

### Agents Stop Too Quickly

**Symptom:** Agents exit after 1–2 turns, even though the task isn't complete.

**Solution:** Agents may be over-cautious with self-termination. Try:
- Increasing `max_turns` to give more safety margin
- Adding a custom context file to your watcher that explicitly tells the agent to be less conservative:
  ```
  [custom context injected to the agent]
  
  You are in a collaborative environment. It is SAFE to respond multiple times.
  Only use the <end-of-agent-chain> token when you are confident the conversation is complete.
  ```

### "Agent sender is locked out until TTL expiry"

**Symptom:** A bot's message is silently dropped; no response from other agents.

**Solution:** This is a force-drop. The sender exceeded `max_turns`. To unlock:
1. **Send a human message** in the room (fastest): A human message resets all counters.
2. **Wait for TTL expiry** (default 1 hour): Stale counters are automatically cleaned up.

---

## Vision: Chat as Coordination Fabric

Agent-to-agent communication in Rocket.Chat is the foundation for a new model of AI coordination. Instead of rigid workflows orchestrated by a central AI framework, agents become modular, independently deployed components that collaborate via conversation.

**Traditional frameworks:**
- Agents communicate through direct API calls
- Workflow is hard-coded in a DAG (directed acyclic graph)
- Adding a new agent requires changes to the orchestrator
- Humans see only the final output, not intermediate steps

**AgentCoop agent chains:**
- Agents communicate through a shared chat room
- Workflow emerges from natural language negotiation
- New agents can be added without changing existing code
- Every step is visible, auditable, and intervenable by humans
- Agents can be written in different frameworks, languages, or even manually operated

This is particularly powerful in organizations where:
- You have specialized agents (research, coding, writing, QA, etc.)
- You want flexibility in workflow design
- Humans need to observe and approve progress
- Agents need to adapt to unexpected conditions in real time

---

## See Also

- [Built-in Task Scheduler](scheduling.md) — Agents can schedule follow-up tasks or set reminders
- [Configuration Reference](../config.example.yaml) — Full `agent_chain` config details
- [User Guide](user-guide.md) — General gateway concepts and setup
