# Multi-agent rooms

Status: **proposal, not implemented.** This document specifies how a room with
several agents is run: what starts an agent's turn, what every agent sees,
what pauses a run of agent turns, and where the orchestration of a
conversation lives. Mechanisms carry a short name in `CAPITALS` and a
tracking code in parentheses; the name is what the text uses.

---

## 1. Problem

Loop protection today is three layers (`docs/agent-chain.md`): the agent may
answer with `<end-of-agent-chain>` and nothing is posted; each receiving bot
counts posts per sender per room and drops everything past `max_turns`; the
counters clear after `ttl_seconds`. Underneath them, the `require_mention`
gate applies to human senders only — a post from another agent is dispatched
to every bot in the room whether or not it names them
(`gateway/connectors/rocketchat/normalize.py:197, 213`;
`gateway/connectors/mattermost/normalize.py:222`).

Two rooms were measured (Appendix A). Self-termination is disciplined: half
to two thirds of agent turns end in it. The drops are the damage: every
recoverable dropped post carried substance, the drops fell on the
convergence phase of a debate, and two mutual "corrections" between agents
traced to posts the limit had removed from the correcting agent's context
(issue #190). A `@all` prompt produced four near-identical replies in seven
seconds. Each human prompt in the lab produced about 25 agent posts.

The limit is a rate limiter that was being used as a convergence mechanism.
It cannot tell a loop from a conclusion, so it cut where the conversation
was ending; and because it drops rather than delivers, each bot's view of
the room diverged by whatever its own counter had removed.

---

## 2. Goals and non-goals

### Goals

1. **Consistent context.** No post is ever withheld from a bot's context by
   admission policy. A post that did not start a bot's turn reaches that
   bot's session at its next admitted turn, fetched from the platform. Every
   bot's context is therefore a prefix of the same room history, never a
   history with holes. (A skill may give its orchestrator a fresh session
   for one conversation; that is the skill's choice, not a gateway rule.)
2. **A room is not bound to one scenario.** Mode, rate limit and
   orchestrator are runtime state of a coordination thread, settable by a
   human or an orchestrator. One room goes from casual conversation into a
   meeting and back without a restart or a second room.
3. **Rate limiting, convergence and guidance are kept apart.**
   - The **rate limit** pauses agent turns after a set number of agent
     posts, so that a human can absorb what was said and table, rescope or
     continue. It counts what the human has to read, not what the agents
     spend: a silent turn is free. It is not a convergence mechanism —
     muting agents does not end a topic — and not a cost control: pausing
     defers spend, it does not remove it.
   - **Convergence** happens in two ways only: agents converge on their
     own — the evidence says they usually do — or someone escalates: a
     human or an orchestrator stops, overrides or rescopes. The rate limit
     exists so that escalation always gets its turn.
   - **Guidance** tells an agent how much room it has ("the thread pauses
     in N posts; make your point, do not restate"). It is a prompt hint
     derived from the rate limit's state, never a control.
4. **A mention is a request to speak, not a right to a turn.** Any agent
   may address any agent; whether that starts a turn is decided by the same
   gate for humans and agents.
5. **Mechanism is separate from policy.** The gateway provides the
   mechanisms in §5 and knows nothing about meetings or debates. An
   **orchestrator** — a human, or a bot running a skill — runs a
   conversation with control words. A classifier may resolve addressing
   and recommend an order; it never dispatches and never generates text.

### Non-goals

- **Rooms spanning gateways.** All bots coordinated in a room run under one
  gateway. Two gateways in one room share no state and this document does
  not make them converge; such rooms keep today's per-gateway behaviour.
- **Inferred interruption.** The gateway never decides on its own that a
  human post has made a running plan obsolete. Only an explicit `stop:`
  cancels work, and it reports per member what was done.
- **Hidden information.** The room is the unit of visibility. Secrets need
  separate rooms or DMs.
- **Convergence by the gateway.** No `close`, no judgement of whether an
  agent is right.

---

## 3. A short example

Room `#team`: bots `a`, `b`, `c`; human `H`; mode `catch-up`; `post_limit`
6 per thread.

1. `H: a, can you check the nginx log?` — `a` is addressed by username;
   one turn starts for `a`. `b` and `c` are not addressed: no turn; the
   post is past their watermark.
2. `a` replies: "Found a 502 burst at 09:12. b, was the backend restarted
   then?" — `b` is addressed; one turn for `b`. Its session receives its
   backlog first — `H`'s post — then the trigger, `a`'s post.
3. `b` replies "Yes, at 09:11." — nobody addressed; no turn. Two agent
   posts counted on this thread.
4. The sweeper finds `c` with three undelivered posts and the room on
   `catch-up`: one catch-up turn for `c`, which sees all three and answers
   with the token — no post, so still two counted.
5. `H: @all, is this the same as last week's incident?` — a human post
   resets the count to zero; `@all` addresses all three: a three-member
   parallel plan, three turns, three posts, three counted.
6. `a` answers and names `b`; `b` answers and names `a`; `a` answers and
   names `c` — three agent posts, six counted: the thread **pauses** with a
   notice. `c`'s turn does not start. Posts are still delivered to each
   bot's backlog, but no agent turn starts until `H` posts again, `H`
   writes `extend: 4`, or the thread is silent for `ttl_seconds`.

---

## 4. Terms

| Term | Meaning |
|---|---|
| **post** | One message on the platform. |
| **turn** | One run of a bot's backend. It produces zero or one post. A token-only turn produces none. |
| **addressed** | A post names a bot: a platform mention, `@all` or a room-wide mention, the username in the text, or — with the resolver (§5, `ADDRESS_RESOLVER`) — the bot's display name, an alias, or a description of what it does. |
| **coordination thread** | The unit of mode, rate limit and orchestrator. A post inside a platform thread belongs to that thread; every top-level post in a room belongs to the room's own thread. A bot's reply inherits the thread of its trigger. |
| **plan** | The dispatch of one post to the bots it addresses: one turn per member, in parallel (default) or in sequence. A single addressed bot is a one-member plan. |
| **member**, **owner** | The bots a plan addresses; the sender of the post that created it. |
| **hop** | One member's turn inside a sequential plan. |
| **plan boundary** | The moment a plan ends. |
| **orchestrator** | The bot a human has designated for a thread with `orchestrator: @bot`. Its control words are honoured and its turns are not rate-limited. |
| **backlog** | The posts in a room past a bot's watermark: delivered to the platform, not yet delivered to that bot's session. |
| **thread handle** | A short label (`T1`, `T2`) the gateway assigns to each coordination thread present in one turn, so the agent never handles platform thread ids. |
| **reply envelope** | The structured form of a turn's output: one block per thread the agent answers (`REPLY_ENVELOPE`). Plain text is accepted and goes to the turn's default thread. |
| **control word** | `mode:`, `limit:`, `extend:`, `sequential:`, `timeout:`, `orchestrator:`, `stop:`. Recognised only from an authorised sender (`AUTHORITY`); otherwise text. A control word inside a code block or a quotation is text. An unparseable value is ignored with a notice. |

Parameters, per room with per-thread override: `post_limit` (per mode),
`ttl_seconds`, `catch_up_post_threshold`, `catch_up_age_seconds`,
`hop_timeout_seconds`.

---

## 5. Mechanisms

### Order of handling

Every inbound post, for every bot the gateway runs in that room:

1. **Authority** — is the sender allowed to issue control words? (`AUTHORITY`)
2. **Control words** — apply `mode:`, `limit:`, `extend:`, `orchestrator:`, `stop:` to the thread's state.
3. **Addressing** — which bots does the post address? (`MENTION_GATE`, `ADDRESS_RESOLVER`)
4. **Plan** — the addressed bots form this post's plan; each gets one turn in the plan's mode, coalesced with any turn already pending for that bot on this thread. (`PLAN`)
5. **Unaddressed bots** — handled by the thread's mode. (`UNADDRESSED_MODE`)
6. **Rate limit** — a turn starts only if the thread is not paused; a plan is admitted only if the thread is not paused. (`RATE_LIMIT`)
7. **Context** — a bot that starts a turn first receives its backlog, then the trigger; every message carries its thread handle, and the turn is told which threads it may reply in. (`WATERMARK`, `REPLY_ENVELOPE`)
8. **Reply** — the turn's output is parsed as a reply envelope; each block is posted to its thread if that thread is in the turn's allowed set, and counts there. (`REPLY_ENVELOPE`, `RATE_LIMIT`)

A post that addresses nobody and carries no control word takes steps 5–7
only. A human post resets the rate limit before step 3.

### MENTION_GATE (G1)

- *Scope*: every post, every bot, human and agent senders alike.
- *Normal*: an addressed bot gets a turn, as a member of the post's plan.
  An unaddressed bot does not; it goes to `UNADDRESSED_MODE`.
- *Addressing tiers*: platform mention or `@all` / room-wide mention;
  the bot's username in the text (today's regex without the leading `@`);
  with `ADDRESS_RESOLVER` enabled, a display name, alias or description.
- *Exceptions*: a 1:1 DM addresses its bot by definition. A coalesced turn
  (see `PLAN`) is not a second turn.

### UNADDRESSED_MODE (G2)

- *Scope*: per coordination thread. The room's setting is the default for
  every thread without an override and for the room's own top-level
  thread; a thread created later inherits it until overridden. Set at
  runtime with `mode:`; persisted in the room record; default from config.
  Two threads in one room run two modes independently.
- `snooze`: no turn. The post waits in the bot's backlog.
- `listen`: a turn for each unaddressed bot, **serially** in roster order,
  each seeing the previous reply, answering with the token if it has
  nothing to add. While a plan is running on the thread, listen turns wait
  for the plan boundary. `listen: parallel` is not offered in this version.
- `catch-up` (default): no turn now; `SWEEPER` grants one later.
- *Switching*: a mode change applies to the next post. Running plans and
  pending turns finish under the old mode; listen turns resume at the plan
  boundary. The rate-limit count is kept across a mode change — the
  human's control post resets it like any human post — and the new mode's
  default `post_limit` applies from then on unless `limit:` was set.
- *Exceptions*: control-word-only posts and gateway notices trigger nobody
  in any mode.

### RATE_LIMIT (G3)

- *Scope*: per coordination thread. Counts **posts by agents** — what the
  human has to absorb. A turn that posts nothing (token-only, or a failed
  turn) counts zero; the thread's orchestrator's posts do not count. A
  turn only ever starts from a post, a plan or the sweeper, so a run of
  silent turns cannot loop. Cost is not what this limit bounds; pausing
  defers spend, it does not remove it.
- *Normal*: after `post_limit` agent posts since the last reset the thread
  **pauses**: no agent turn starts, posts are still delivered to backlogs,
  one notice is posted naming the thread, the count, and how to resume.
- *Plans*: a plan is admitted while `remaining ≥ 1`. An admitted plan runs
  to its boundary even if that exceeds the limit (overshoot ≤ members − 1).
  The pause lands at the boundary. A plan is refused while paused, with a
  notice to the owner.
- *Resets*: a human post on the thread resets the count to zero; `extend: N`
  adds N to the remaining budget; `ttl_seconds` of silence resets the count.
  A reset never starts a turn by itself.
- *Callback*: `CALLBACK` runs while paused (the orchestrator is exempt and
  is the way out of a pause).
- *Defaults*: `post_limit` has a per-mode default; `listen` rooms need a
  higher one than `catch-up` rooms.

### AUTHORITY (G4)

- Control words are honoured from humans with the owner role and from the
  thread's orchestrator bot. `orchestrator:` and `stop:` are honoured from
  humans only. From any other sender — guest humans, other bots — control
  words are text.
- Sender role is server-injected (`CLAUDE.md`, connector prompt prefix).

### WATERMARK (D1)

- *Scope*: one post id per bot per room — the last post delivered to that
  bot's session.
- *Normal*: when a bot starts a turn, the gateway fetches the room's posts
  after the watermark from the platform, delivers them as the turn's
  context ahead of the trigger, and advances the watermark to the trigger
  once the backend has accepted the turn. The trigger is never in the
  backlog twice.
- *Contract*: nothing is withheld by admission policy; a bot that is not
  woken has not seen its backlog yet, and sees it at its next admitted
  turn. Delivered backlog is context only: control words and mentions in
  it are not re-executed.
- *Threads*: the backlog is per room, so one turn's context may span
  several coordination threads. Every delivered message carries its
  thread handle, and the turn's reply is routed per thread by
  `REPLY_ENVELOPE`; the watermark advances past everything delivered.
- *Interface*: reuses the connector history fetch
  (`Connector.fetch_room_history`, `gateway/core/connector.py:672`),
  extended to return post ids and thread ids and to page past the first
  page (Mattermost fetches one page today,
  `gateway/connectors/mattermost/rest.py:402-409`). A failed fetch
  delivers the trigger alone and leaves the watermark where it was.
- *Restart*: the watermark is persisted with the room record; a gateway
  restart changes nothing. A new session for a bot starts from the
  existing watermark unless the operator resets it.

### SWEEPER (D2)

- *Scope*: a periodic pass over every (bot, coordination thread) pair
  whose thread is on `catch-up`.
- *Qualifying*: a thread qualifies for a bot when that bot's backlog holds
  ≥ `catch_up_post_threshold` posts of that thread, or its oldest
  undelivered post of that thread is older than `catch_up_age_seconds`.
  Posts in `snooze` or `listen` threads never qualify.
- *Normal*: a bot with at least one qualifying thread gets **one**
  catch-up turn. Its input is the whole backlog (all threads, each message
  with its thread handle); its allowed reply set is the qualifying threads
  that are not paused and have no running plan. Each reply block counts
  against the thread it lands in. A catch-up turn has no trigger; the
  watermark advances to the backlog's newest post.
- *Exceptions*: one pending catch-up turn per bot at a time; a thread that
  is paused or has a running plan is context in the turn but not in the
  allowed set; a thread that qualified but was absorbed into a triggered
  turn (`REPLY_ENVELOPE`) needs no sweep.
- Also posts the pause notice and acknowledges mode changes.

### FAILURE_NOTICE (D3)

- A failed turn is announced from the failing bot's account. The notice is
  delivered through `WATERMARK` and never starts a turn in any mode. A
  plan whose member failed marks it `failed` and advances.

### PLAN (P1)

- *Creation*: every post that addresses at least one bot creates a plan;
  members are the addressed bots (minus the sender); the owner is the
  sender; the thread is the post's coordination thread.
- *Mode*: `parallel` by default. `sequential:` from an authorised sender
  orders the members as named. With `ADDRESS_RESOLVER` enabled, a
  multi-member post with no mode word takes the resolver's recommended
  mode and order once that decision has passed shadow measurement (§8).
- *Parallel*: every member's turn starts now; every member receives the
  same backlog, cut at the plan's post, so no member sees another's reply
  from the same plan. Each member is bounded by `timeout:` (default
  `hop_timeout_seconds`) measured from the plan's post; a member still
  running at the timeout is `timed-out` and the plan can end.
- *Sequential*: members run one at a time in order; each hop receives the
  earlier replies in its backlog. A hop waits for its bot if the bot is
  busy, up to `timeout:` (default `hop_timeout_seconds`); on timeout the
  member is `timed-out` and the next hop starts.
- *Coalescing*: a bot has at most one pending turn per room. A new post
  that addresses a bot with a pending turn does not create a second turn;
  the post rides in the pending turn's backlog, and that thread joins the
  turn's allowed reply set. A bot that has already run in this plan and
  is addressed again gets a new turn.
- *Concurrency*: plans on a thread, and across threads, run concurrently;
  only a sequential plan orders its own members.
- *Pending hop*: while a sequential plan has a hop still to come for bot
  X on thread T, every other turn X runs is instructed not to reply in T
  — T is listed as *pending* in the turn's thread block — and a block for
  T is not posted if the instruction is ignored. X answers T in its hop,
  having seen everything; nothing is stored in between. Plans do not know
  about each other: the gateway holds every running plan of the room, and
  when it builds a turn for X it looks up, for each thread, whether a
  sequential plan on that thread still has a hop for X — that lookup is
  the *pending* mark. This is what keeps a bot from speaking out of turn
  when two plans on two threads reach it at once.
- *Restart*: plans live in memory. After a gateway restart no plan is
  running: members that had not run do not run, and the owner is told the
  members' states as after `stop:`. The rate-limit count is rebuilt from
  room history — agent posts on the thread since the last human post —
  and the watermark is on disk, so nothing else needs recovery.
- *Member states*: exactly one per member — `replied`, `self-terminated`,
  `failed`, `timed-out`, `cancelled` (by `stop:` before the member ran).
- *Boundary*: the plan ends when every member has a state. The owner's
  post counts as the trigger; member turns count against `RATE_LIMIT`.

### CALLBACK (P2)

- When a plan ends and its owner is the thread's orchestrator bot, the
  orchestrator gets one turn carrying the members' replies and states
  (through `WATERMARK`). A plan owned by a human or by any other bot ends
  without a callback; its replies reach the owner by addressing or backlog.
- Runs while the thread is paused.

### STOP (P3)

- `stop:` from a human on a thread: every running plan on the thread marks
  members that have not started `cancelled`, lets running turns finish,
  and ends at that boundary. The notice lists each member and its state
  (`a: replied`, `b: replied`, `c: cancelled`) so the human knows what was
  done and what was not. Nothing else interrupts a plan.

### REPLY_ENVELOPE (R1)

- *Input*: every message delivered to a turn carries `thread: <handle>`
  in its header. The turn ends with a block listing the threads present:
  for each, its handle, its newest post, and its status for this turn —
  *allowed* (the turn may reply there), *pending* (a sequential hop is
  coming; do not reply here), or *paused*. The default thread is the one
  the trigger belongs to, or the newest allowed thread for a catch-up
  turn. The allowed set of a triggered turn is the trigger's thread, the
  threads of any posts coalesced into the turn, and any thread that
  qualifies for catch-up for this bot at that moment — so a turn the bot
  is running anyway absorbs the catch-up the sweeper would otherwise
  grant. Most turns have one allowed thread; a single block with no
  handle is the common output. The global agent context describes the format once; the per-turn
  block is the reminder.
- *Output*: the turn's text is parsed as an envelope — one block per
  thread, each with the handle and the text, or a `done` marker for a
  thread the agent has nothing for:

  ```
  [{"thread": "T1", "text": "502 stopped after 09:12."},
   {"thread": "T2", "done": true}]
  ```

  The envelope is the whole reply, or a fenced block inside it. The exact
  encoding is an implementation choice; the contract is the fields.
- *Routing*: a block is posted to its thread if the handle is in the
  turn's allowed set, and counts against that thread's `RATE_LIMIT`. A
  block for a *pending* thread is not posted — the turn was told not to
  reply there, so this is the backstop, and the agent is reminded at its
  hop. A block for a *paused* thread is posted and counted as overshoot.
  A handle the turn did not list is treated as missing. The gateway keeps
  no agent output: a block is posted or it is not.
- *Fallback*: output with no envelope is one block for the default thread
  — today's behaviour. A block with a missing or unknown handle goes to
  the default thread. `<end-of-agent-chain>` anywhere is `done` for the
  whole turn. Nothing is ever posted to a thread the turn did not list.
- *Replaces*: the catch-up anchor rule, where a batch of messages from
  several threads was answered in the thread of the last one
  (`gateway/core/message_processor.py:465-515`,
  `gateway/core/prompt_builder.py:74-110`).

### HINT (H1)

- Injected into every turn: for each thread in the turn, its mode and the
  remaining posts before the pause; and "make your point; do not restate".
  Derived from `UNADDRESSED_MODE` and `RATE_LIMIT` state; the thread list
  itself is `REPLY_ENVELOPE`'s.

### STATUS (O1)

- `coop room status <handle>`: per thread — mode, count / `post_limit`,
  paused, orchestrator, running plans; per bot — backlog size and age.

### ADDRESS_RESOLVER (J1) — classifier, optional

- Given a post and the room's roster (username, display name, aliases,
  description per bot), returns the addressed bots, an order, and a
  recommended mode. Used by `MENTION_GATE` for tier-3 addressing and by
  `PLAN` for the mode of multi-member posts, each only after its shadow
  accuracy is measured (§8). It never starts a turn and never writes text.

### LISTEN_GATE (J2) — classifier, later

- In `listen` rooms, whether a post contains a claim or a question that
  someone should answer; a post that does not is delivered without listen
  turns. Later still: which skill fits a prompt and which bot should chair.

### What the gateway does not have

No topic ids or per-topic budgets, no per-message classification of mode
without the resolver, no stored queues, no agent bypass of the mention
gate, no inferred interruption, and no knowledge of what a meeting is.

---

## 6. Worked examples

### 6.1 A brainstorm run by a bot with a skill

Room: bots `chair`, `a`, `b`, `c`, `d`; human `H`. The meeting runs in a
platform thread, so the skill's `mode: snooze` and the `post_limit` of 5
are scoped to it and the room's top-level thread keeps its own mode;
`chair` is designated on that thread. Columns: the event, new turns (member / exempt),
public posts, the thread's post count, callbacks.

| # | Event | Turns | Posts | Count | Callback |
|---|---|---|---|---|---|
| 1 | `H: orchestrator: @chair` | — | 1 (H) | 0 | — |
| 2 | `H: @chair run a brainstorm: should we charge per seat or per usage?` | 1 exempt (chair) | 1 (H) | 0 | — |
| 3 | `chair: mode: snooze` · `@a @b @c @d — answer independently in one post; do not address other bots. <question>` | — | 1 (chair; control + plan) | 0 | — |
| 4 | Plan P1 (parallel, 4 members) runs; each sees the backlog cut at #3 | 4 | 4 (a, b, c, d) | 4 | — |
| 5 | P1 ends | 1 exempt (chair) | — | 4 | 1st |
| 6 | `chair: <synthesis: positions, agreements, conflicts, open questions>` · `@a @b @c @d — read the synthesis and the others' replies; post only a substantive addition or objection, otherwise the token.` | — | 1 (chair) | 4 | — |
| 7 | Plan P2 runs; `a`, `b`, `c` answer with the token; `d` objects to one point | 4 | 1 (d) | 5 → **pause** | — |
| 8 | Gateway: `thread paused (5/5). extend: N, or a human post, to continue.` | — | 1 (gateway) | 5 | — |
| 9 | P2 ends | 1 exempt (chair; runs while paused) | — | 5 | 2nd |
| 10 | `chair: <close: decision, open items, who said what, d's objection recorded>` | — | 1 (chair) | 5 | — |

Totals: 8 member turns, 3 exempt turns, 5 counted posts, 2 callbacks,
11 posts in all (2 human, 3 chair, 5 members, 1 gateway). Today's lab debate: about 25 agent posts
per prompt. Had `d`'s objection needed an exchange, the chair would post
`extend: 2` then `@b @d sequential: <the point>`; the plan is admitted
only because the extension made `remaining ≥ 1`.

What the gateway did: two plans, one rate limit, two callbacks, one
notice. It did not know there was a meeting.

### 6.2 A stop, and a failure

`H: a, do the cleanup; then b, restart the server; then c, restart the
client` with `sequential:` — a three-hop plan owned by `H`.

- Hop 1: `a` runs, replies "cleanup done". Hop 2: `b` runs.
- `H: stop:` — `c` has not started: `cancelled`. `b` finishes its turn.
  The plan ends. Notice: `plan stopped — a: replied, b: replied, c:
  cancelled`. `H` knows the client was not restarted.
- Without `stop:`, `H: thanks everyone` would reset the rate limit and
  address nobody; the plan would continue to `c`.

Same plan, no stop, but `b`'s backend fails: a failure notice is posted
from `b`'s account and delivered to `a` and `c` through their backlogs;
nobody's turn starts because of it; `b` is `failed`; hop 3 starts for `c`,
whose backlog contains the notice.

---

## 7. Scenario validation

| Scenario | Mode and control | How it runs | Supported |
|---|---|---|---|
| One room, three scenarios in a day | `catch-up` → `mode: snooze` + `orchestrator:` → `mode: catch-up` | Casual talk; a meeting on one thread; casual talk again — no restart, no second room (goal 2). | yes — a test in §8 |
| One thread, three scenarios | `listen` → `mode: snooze` + `orchestrator:` → `mode: listen` | A debugging thread becomes a vote run by a chair, then a debugging thread again; the running plan finishes under the old mode, the count carries over. | yes — a test in §8 |
| Casual conversation (production `#nest`) | `catch-up` | Addressed bots run; others catch up at the sweep; a correction arrives at the next catch-up turn rather than being dropped. | yes |
| Brainstorm (lab, five bots) | chair skill; `snooze` | §6.1. | yes |
| Status / decision / roadmap meeting | chair skill | `sequential:` reports; `parallel:` vote by token; brainstorm skill. | yes |
| Debugging session | `mode: listen`, `limit: 20` | Serial listen turns correct a wrong claim on the next turn; pause every `post_limit` posts. | yes — one turn per bot per post, chosen |
| Divide and collaborate | PM skill or H; `catch-up` | `parallel: timeout: 30m`; results at the boundary; a member addresses a peer for a dependency. | yes |
| 1:1 DM, single-bot room | — | The bot is addressed by definition; `RATE_LIMIT` and `WATERMARK` still apply. | unchanged behaviour |
| Support / triage channel | `catch-up` | The answering bot addresses a specialist. | yes |
| Scheduled or agent-only room | `snooze` / `catch-up` | `RATE_LIMIT` with the TTL reset is the only stop. | yes |
| Code review room | — | Reviewer addresses the test bot. | yes |
| Critic or pairing | `listen` | B always sees and answers A. | yes |
| Unattended overnight task | `catch-up` | Hops, rate limit, TTL; the pause notice waits. | yes |
| Role-play with a game master | GM skill; `snooze`; `sequential:` per scene | Except hidden information (non-goal). | with that limit |
| Monitor room (CI, webhooks) | `listen` | The integration account is allow-listed as a guest: human-class posts that address a bot start it, listeners follow, the rate limit resets. | yes; §9 |

---

## 8. Implementation and validation

Two kinds of validation, kept apart: **mechanism tests** are deterministic
and run in CI; **effect measurements** are lab or production observations
and are reported as numbers, not guaranteed outcomes. Every task ships
behind a flag and is exercised in the lab room (`#product-idea`, five
bots) before production (`#nest`, three bots, one human); Appendix A is
the baseline. Each measurement records turns, posts, tokens and latency —
fewer posts is not fewer tokens.

| # | Task | Builds | Mechanism tests | Effect measurements |
|---|---|---|---|---|
| 1 | **Deliver, do not drop.** Per-bot watermark; history fetch extended with ids, thread ids and paging; Layer 2 overflow and failure notices delivered at the next turn; thread handles on every delivered message; the reply envelope with plain-text fallback, replacing the catch-up anchor. | WATERMARK, FAILURE_NOTICE, REPLY_ENVELOPE | backlog delivered once, in order, trigger not duplicated; watermark survives restart; failed fetch delivers the trigger alone; envelope parsing — one block per thread, unknown handle → default, no envelope → default, `done`; a batch spanning two threads answers each in its own thread. | Lab: dropped posts 21 → 0 over a debate; the two misattributions in Appendix A absent on replay. Production: error-notice turns per failure 4 → 0. |
| 2 | **Mention gate for agents.** Agent senders pass the same gate; username without `@` counts. Unaddressed bots are `snooze` until task 4, so this task stays lab-only until task 4 lands. | MENTION_GATE | an agent post naming nobody starts no turn; naming one bot starts one. | Lab: posts per human prompt; self-termination rate; every agent post with no mention hand-checked for whether a reply was wanted. |
| 3 | **Rate limit per thread.** Replaces per-pair counters: pause, notice, human reset, `extend:`, `limit:`, TTL; hint. Plans do not exist yet, so the boundary rule is "the current turn finishes". | RATE_LIMIT, AUTHORITY (humans), HINT | token-only and failed turns count zero; pause refuses new turns and keeps delivering; each reset path; guest control words are text. | Lab: where pauses land relative to conclusions (observed, not guaranteed). Production: pauses per week against the two drops; notices read by the human. |
| 4 | **Modes and the sweeper.** `mode:` per room and thread at runtime, persisted; `catch-up` default; `snooze`; status. | UNADDRESSED_MODE (snooze, catch-up), SWEEPER, STATUS | mode change applies to the next post; a thread qualifies on threshold and on age, never when paused; one catch-up turn per bot covers several qualifying threads and replies in each; two threads in one room on different modes; the three-scenario day and the three-scenario thread (§7 rows 1–2) as end-to-end tests. | Lab: backlog age at delivery; production `#nest` on `catch-up`: correction latency. |
| 5 | **Plans.** Parallel snapshot, sequential hops, coalescing, admission and overshoot, member states, `timeout:`, `orchestrator:`, callback, `stop:`. The rate-limit boundary becomes the plan boundary. | PLAN, CALLBACK, STOP, AUTHORITY (bot) | parallel members share one cut; a sequential hop sees earlier replies; coalescing yields one turn across threads; a reply to a pending-hop thread is not posted and the hop still runs; after a restart a running plan ends with a member-state notice and the count is rebuilt from history; callback exactly once and only to the orchestrator; a plan refused while paused; overshoot ≤ members − 1; `stop:` states reported. | Lab: sequential vs parallel self-termination on the same prompt; anchoring (a later bot reversing after reading an earlier one). |
| 6 | **Listen mode.** Serial listen turns; wait for a running plan. | UNADDRESSED_MODE (listen) | order follows the roster; listen turns suspended during a plan. | Lab debugging rehearsal: a planted wrong claim corrected on the next turn; turns per post. |
| 7 | **Brainstorm skill.** §6.1 end to end. | — (policy) | the trace in §6.1 reproduced: 8 member turns, 5 counted posts, 2 callbacks, 11 posts. | Lab: Appendix A prompts replayed: turns, posts, tokens, human re-entry time, objections caught. |
| 8 | **Resolver.** Tier-3 addressing and mode recommendation in shadow; then `LISTEN_GATE` in shadow. | ADDRESS_RESOLVER, LISTEN_GATE | — | Agreement with hand labels per decision type before any of them is given effect. |

Dependencies: 1, 2 and 3 are independent and can ship in any order; Layer 1
(self-termination) stays throughout. 4 needs 1. 5 needs 2 and 3, and
upgrades 3's boundary rule. 6 needs 4 and 5. 7 needs 5. 8 needs 5 and 6.

---

## 9. Undecided

Decisions this version needs and does not yet make; each names the task
that settles it.

- **Envelope encoding** (task 1). A fenced JSON block, the whole reply, or
  a backend-native structured output where one exists. The contract is
  the fields; the encoding is chosen when the parser is written.
- **Per-mode `post_limit`, `catch_up_post_threshold`,
  `catch_up_age_seconds`** (task 4). Measured in the lab before defaults
  are fixed.
- **Webhook sender identity** (task 2). Which username a webhook post
  carries on each platform, so that allow-listing a CI account as a guest
  is known to work there.
- **Glossary** (on acceptance). "Orchestrator", "mode", "watermark",
  "sweeper", "control word", "coordination thread", "reply envelope" go
  into `CONTEXT.md` with the synonyms to avoid (moderator, judge, router,
  queue, topic).

## 10. Out of scope for this version

Known, deliberately not built now; each names what would reopen it.

- **`listen: parallel`** — today's behaviour for unaddressed bots. Reopened
  if a room wants immediacy over the redundancy it produces.
- **Inferred interruption** — a classifier or the chair judging that a
  human post makes a running plan obsolete, confirming with the human
  when unsure. Reopened once `stop:` has been used enough to show what
  humans actually interrupt.
- **An integration sender class** — a role between human and agent for
  CI and webhook accounts. Reopened the first time a feed's human
  semantics (rate-limit reset) are observed to be wrong.
- **Envelope extensions** — mentions resolved by name, a reply-to post, a
  reaction, so that `<end-of-agent-chain>` and `@`-text stop being parsed
  out of prose. Reopened after the envelope has run in the lab.
- **Rooms spanning gateways**, **hidden information**, and **convergence
  by the gateway** — non-goals (§2).

---

## Appendix A — Evidence from gateway logs

Compact figures behind §1.2. Lab: `#product-idea`, 2026-10-07, five agents
(three `claude`, two `opencode`), `max_turns: 5`. Production: `#nest`,
2026-10-05 to 2026-10-11, three agents and one human, `max_turns: 2`.

| | Lab | Production |
|---|---|---|
| Agent turns | 207 | 544 |
| Self-terminated (Layer 1) | 127 (61%) | 259 (48%) |
| Posts dropped at the budget (Layer 2) | 74 events on 21 posts | 2 |
| Dropped posts with substance | 18 of 18 recoverable (3 not recoverable) | 2 of 2 |
| Human-reset windows with zero drops | 3 of 8 | — |
| Agent posts per human prompt (debate windows) | 31 / 18 / 23 | — |

Lab drops: 46% were caused by the counter-reset bug fixed in PR #195 (issue
#187) and would not recur; the remainder are genuine budget overflows on
roughly ten posts, all in the convergence phase of the debate.

Two misattributions in the lab — an agent denying a peer had given a figure,
and an agent accusing a peer of giving two inconsistent figures — each trace
to a post the budget had removed from the accuser's context. Both are
recorded on issue #190.

The TTL never acted as the reset in either deployment; a human post always
came first.

**Closing-round rehearsal, 2026-10-12, same lab room (n=1).** The human
posted a six-point summary to `@all` with the rule "agree: reply with only
the token; object: name the point". Parallel, as today. Four of five bots
answered with the token alone; one objected to one point. The objection
started an agent chain: four turns, three token-only, one bot joining the
objection with its own reason (that bot had agreed with every point when
it answered alone). The join started a third chain of four token-only
turns. Thirteen turns, two posts.

---

---

## Appendix B — Other systems

Nine projects were read at code level (commit in parentheses). Two position
agents as teammates — several personas in one place, talking to humans and
to each other — which is this gateway's position. The rest are built for
something else and are listed for the one thing each contributes.

**Buzz** (`block/buzz` `799cfd7`) is a team chat on a Nostr relay where an
agent is a channel member with its own keypair, bridged to an ACP harness.
It is the closest system to this one and differs on one rule: its mention
gate applies to agents and humans alike, at the relay, so an unmentioned
post never reaches the agent's process (`crates/buzz-acp/src/relay.rs:3366-3372`);
by default an agent also accepts only its owner and the owner's other
agents (`crates/buzz-acp/src/config.rs:93-105`). Agent-to-agent mention is
a designed path — a delegate must mention the delegator when its work is
done — and nothing in code bounds the exchange that follows: loop control
is prompt text ("silence is usually correct", "never publish a bare
acknowledgement … it re-triggers everyone you mention",
`crates/buzz-acp/src/base_prompt.md:71-75`), and a controversial exchange
between two agents runs until an owner cancels it. Context for an
unaddressed agent is the thread's history, fetched at dispatch when the
agent is next mentioned in that thread, with already-delivered events
removed (`crates/buzz-acp/src/pool.rs:4055-4073, 4136-4150`); a top-level
channel post fetches nothing. That fetch is `WATERMARK` built the same way —
watermark and query, no stored queue — for threads only. Buzz has no
coordinator, no plan, no rate limit and no mode; its failure notice carries
no mention tag and so cannot wake another agent (`pool.rs:5540-5548`),
which is `FAILURE_NOTICE`. No test runs two agent identities in one channel.

**munder-difflin** (`chaitanyagiri/munder-difflin` `241d1e5`) is a desktop
office of persona agents with file mailboxes. Any agent can mail any agent
(`src/main/hive.ts:1684-1693`), but the design is a hub: a "god" agent
decomposes work into four-part contracts, is the sole writer of the shared
board, and receives everything ambiguous (`hive.ts:1585, 2985-2999`). It is
built to execute work, not to hold a discussion: the message acts
`inform` and `done` are terminal by prompt, `agree` and `refuse` exist in
the schema and no protocol uses them, and there is no round, vote or
synthesis step. Its site describes peer-to-peer encrypted messaging; the
code is a central file router with no transport encryption found. The one
all-hands exchange is the shutdown handshake (`src/main/closingTime.ts`).

**Positioned otherwise.** OpenClaw (`5559404e`) is a single person's
assistant across messengers; it contributes the per-group runtime commands
`/activation` and `/queue <mode>` (`UNADDRESSED_MODE` set at runtime), a free pending-history
window for unmentioned posts, and a bot-pair rate guard that drops silently.
QM (`2c94475`) is one company bot shared by many humans; it contributes a
small-model `YES` / `NO` / `REACT` pre-check before an unprompted turn (`LISTEN_GATE`)
and a Postgres turn queue with idempotency keys. jinn (`3ae6465`) is an org
chart of "AI employees"; it contributes a lateral-send guard (10 per sender
per 10 min, 12 relay hops) and the rule that a human message resets the hop
chain (`RATE_LIMIT`'s human reset). AutoGen (`027ecf0`) is an in-process group chat;
its `SelectorGroupChat` is one model call per hop over the whole transcript
with a rule that short-circuits it (`ADDRESS_RESOLVER`, with the plan issued by rule first),
and every participant buffers every broadcast until selected (`WATERMARK`'s
effect). Google ADK, MetaGPT and crewAI are task pipelines; parallel
branches in all three see only the fan-out point, which is `PLAN`'s `parallel:`.

### Debate in the literature

 On single-answer benchmarks,
multi-agent debate among copies of one model does not reliably beat
self-consistency (Smit et al., ICML 2024, arXiv:2311.17371; Zhang et al.
2025, arXiv:2502.08788) or majority voting (Choi et al. 2025,
arXiv:2508.17536), and costs more at an equal number of responses (Huang
et al., ICLR 2024, arXiv:2310.01798, Table 7). A persona instructed to
disagree flips correct answers (Smit et al.). Mixing different models is
what produces a gain, and the gain comes from questions one model gets
right and another wrong (Zhang et al., §4). A synthesising aggregator over
independent proposals beats selecting one proposal (Wang et al. 2024,
arXiv:2406.04692). None of these studied a chat room with a human in it.
This design takes from them a default flow to validate in the lab — a
parallel blind round, a synthesis, one bounded revision round — and a
preference for members that differ in model, information or tools over
copies trading opinions.

---
