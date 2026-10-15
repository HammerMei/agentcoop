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
`gateway/connectors/mattermost/normalize.py:222`). The addressing a bot
reads (`to:`) is computed from every mention in a post, so a reply that
quotes a peer — "@H, I agree with @b's point" — addresses `b` as well as
`H` (`gateway/connectors/rocketchat/connector.py:1841`,
`gateway/connectors/mattermost/connector.py:828`); the rule that an agent
mentions only its intended recipients exists in the agent prompt alone
(`gateway/contexts/rc-gateway-context.md:56`).

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
   bot's session at its next admitted turn, fetched from the platform. What
   a bot has been delivered is a prefix of the room's history — never a
   history with holes, except for the bounded gap after a failed fetch,
   which the next fetch closes. A bot has one backend session per room, as
   today, and one delivery cursor per room; a turn appends everything the
   bot has not yet seen, from every thread, in platform order, and replies
   in its own thread. Context is per room; replies, modes, rate limits and
   plans are per thread. (A skill may give its orchestrator a fresh
   session for one conversation; that is the skill's choice, not a gateway
   rule.)
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
   post is past their cursor.
2. `a` replies with `to: [H, b]`; the post reads `@H @b Found a 502 burst
   at 09:12 — b, was the backend restarted then?` — `b` is addressed; one
   turn for `b`. Its session receives its backlog first — `H`'s post —
   then the trigger, `a`'s post.
3. `b` replies with `to: [a]`: `@a Yes, at 09:11.` — one turn for `a`,
   which answers `done`: no post. Two agent posts counted on this thread.
4. The sweeper finds `c` with three undelivered posts and the room on
   `catch-up`: one catch-up turn for `c`, which sees all three and answers
   `done` — no post, so still two counted.
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
| **turn** | One run of a bot's backend, started by one trigger or by a sweeper admission, in one coordination thread. It produces zero or one post, in that thread. A turn whose reply is `done` produces none. |
| **addressed** | A post names a bot. In a human post: a platform mention, `@all` or a room-wide mention, the username in the text, or — with the resolver (§5, `ADDRESS_RESOLVER`) — the bot's display name, an alias, or a description of what it does. In an agent post: the platform's mention set or the room-wide mention only; the canonical form (`REPLY_ENVELOPE`) makes an agent post's mentions exactly its envelope's `to`. |
| **coordination thread** | The unit of mode, rate limit and orchestrator. A post inside a platform thread belongs to that thread; every top-level post in a room belongs to the room's own thread. A bot's reply inherits the thread of its trigger. |
| **plan** | The dispatch of one post to the bots it addresses: one turn per member, in parallel (default) or in sequence. A single addressed bot is a one-member plan. A post that carries only control words creates no plan. |
| **member**, **requester** | The bots a plan addresses; the sender of the post that created it. ("Owner" is reserved for the RBAC role.) |
| **hop** | One member's turn inside a sequential plan. |
| **plan boundary** | The moment a plan ends. |
| **orchestrator** | The bot a human has designated for a thread with `orchestrator: @bot`. Its control words are honoured, its posts are not counted by the rate limit, and its turns start while the thread is paused. |
| **backlog** | The posts of a room past a bot's delivery cursor: delivered to the platform, not yet delivered to that bot's session. Grouped by coordination thread where a thread's size or age matters (`SWEEPER`, `STATUS`). |
| **reply envelope** | The structured form of a turn's output: `to` (who the post is for), `text` (the post), or `done` (nothing to say) (`REPLY_ENVELOPE`). A `text` without a valid `to` is not posted. |
| **control word** | `mode:`, `limit:`, `extend:`, `parallel:`, `sequential:`, `timeout:`, `orchestrator:`, `stop:`. Recognised only from an authorised sender (`AUTHORITY`); otherwise text. A control word inside a code block or a quotation is text. An unparseable value is ignored with a notice. |

Parameters, per room with per-thread override: `post_limit` (per mode),
`ttl_seconds`, `catch_up_post_threshold`, `catch_up_age_seconds`,
`hop_timeout_soft_seconds`, `hop_timeout_hard_seconds`; per room:
`thread_state_ttl`.

---

## 5. Mechanisms

### Order of handling

Every inbound post, for every bot the gateway runs in that room:

1. **Authority** — is the sender allowed to issue control words? (`AUTHORITY`)
2. **Control words** — apply `mode:`, `limit:`, `extend:`, `orchestrator:`, `stop:` to the thread's state.
3. **Addressing** — which bots does the post address? A human post by mention, name or resolver; an agent post by its mentions, which the canonical form makes equal to the sender's `to`. (`MENTION_GATE`, `ADDRESS_RESOLVER`)
4. **Plan** — the addressed bots form this post's plan; it runs when the thread's earlier plans have ended; each member gets one turn in the plan's mode, and a turn serves every queued plan on the thread that addresses that bot. (`PLAN`)
5. **Unaddressed bots** — handled by the thread's mode. (`UNADDRESSED_MODE`)
6. **Rate limit** — a turn starts only if the thread is not paused; a plan is admitted when it reaches the head of the thread's queue, and only if the thread is not paused then. (`RATE_LIMIT`)
7. **Context** — a bot that starts a turn first receives everything in the room past its cursor, each post labelled with its thread, then the trigger. (`WATERMARK`)
8. **Reply** — the turn's output is parsed as a reply envelope; a valid `text` is rewritten to canonical form, posted in the turn's thread and counts there; `done` posts nothing; an invalid envelope is re-prompted once, then the turn fails. (`REPLY_ENVELOPE`, `RATE_LIMIT`, `FAILURE_NOTICE`)

A post that addresses nobody and carries no control word takes steps 5–7
only. A human post resets the rate limit before step 3.

### MENTION_GATE (G1)

- *Scope*: every post, every bot, human and agent senders alike.
- *Normal*: an addressed bot gets a turn, as a member of the post's plan.
  An unaddressed bot does not; it goes to `UNADDRESSED_MODE`.
- *Human posts — addressing tiers*: platform mention or `@all` /
  room-wide mention; the bot's username in the text (today's regex
  without the leading `@`); with `ADDRESS_RESOLVER` enabled, a display
  name, alias or description.
- *Agent posts*: the platform's mention set and the room-wide mention
  only. The canonical form
  (`REPLY_ENVELOPE`) makes an agent post's mentions exactly its
  envelope's `to`, so the gate reads them without guessing. A bare name
  in an agent post is text, never addressing — the canonical rewrite
  produces bare names on purpose.
- *Exceptions*: a 1:1 DM addresses its bot by definition. A coalesced turn
  (see `PLAN`) is not a second turn. A member's reply to its plan's
  orchestrator creates no plan for the orchestrator (`CALLBACK`).

### UNADDRESSED_MODE (G2)

- *Scope*: per coordination thread. The room's setting is the default for
  every thread without an override and for the room's own top-level
  thread; a thread created later inherits it until overridden. Set at
  runtime with `mode:`; persisted in the room record; default from config.
  Two threads in one room run two modes independently.
- `snooze`: no turn. The post waits in the bot's backlog.
- `listen`: a turn for each unaddressed bot, **serially** in roster order,
  each seeing the previous reply, answering `done` if it has
  nothing to add. While a plan is running on the thread, listen turns wait
  for the plan boundary. `listen: parallel` is not offered in this version.
- `catch-up` (default): no turn now; `SWEEPER` grants one later.
- *Switching*: a mode change applies to the next post. Running plans and
  pending turns finish under the old mode; listen turns resume at the plan
  boundary. The rate-limit count is kept across a mode change — the
  human's control post resets it like any human post — and the new mode's
  default `post_limit` applies from then on unless `limit:` was set.
- *Exceptions*: control-word-only posts create no plan and trigger nobody
  in any mode; gateway notices likewise, with one exception — the
  plan-end notice addressed to a bot requester (`CALLBACK`).

### RATE_LIMIT (G3)

- *Scope*: per coordination thread. Counts **posts by agents** — what the
  human has to absorb. A turn that posts nothing (token-only, or a failed
  turn) counts zero; the thread's orchestrator's posts do not count. A
  turn only ever starts from an admitted plan, a listen admission, the
  sweeper, the orchestrator's callback or a plan-end notice to a bot
  requester, so a run of silent turns cannot loop. Cost is not what this limit bounds; pausing
  defers spend, it does not remove it.
- *Normal*: after `post_limit` agent posts since the last reset the thread
  **pauses**: no agent turn starts, posts are still delivered to backlogs,
  one notice is posted naming the thread, the count, and how to resume.
- *Plans*: a plan is admitted when it reaches the head of the thread's
  queue, if `remaining ≥ 1` then; until then it is only queued, and the
  budget at the time of its post means nothing. An admitted plan runs to
  its boundary even if that exceeds the limit (overshoot ≤ members − 1).
  The pause lands at the boundary. A plan reaching the head while paused
  is refused — it does not wait for the reset — with a notice to the
  requester naming each member and its state (`a: replied` where an
  earlier turn served it, `b: not started`), so the requester knows what
  to repeat after the reset.
- *Resets*: a human post on the thread resets the count to zero; `extend: N`
  adds N to the remaining budget; `ttl_seconds` of silence resets the count.
  A reset never starts a turn by itself.
- *Orchestrator*: its posts count zero and its turns — callback or a plan
  addressing it — start while paused; it is the way out of a pause. A
  plan it creates while paused is refused like any other until it
  extends.
- *Defaults*: `post_limit` has a per-mode default; `listen` rooms need a
  higher one than `catch-up` rooms.
- *State*: count, remaining budget and the time of the last reset are
  persisted per thread (`THREAD_STATE`), so a restart resumes the thread
  where it was. (They cannot be rebuilt by counting posts: the TTL reset
  leaves no post, and what `extend:` added depends on the count at that
  moment.)

### AUTHORITY (G4)

- Control words are honoured from humans with the owner role and from the
  thread's orchestrator bot. `orchestrator:` and `stop:` are honoured from
  humans only. From any other sender — guest humans, other bots — control
  words are text.
- Sender role is server-injected (`CLAUDE.md`, connector prompt prefix).

### WATERMARK (D1)

- *Scope*: one post id per bot per room — the last post delivered to the
  bot's session. It is created at the bot's first delivery and never
  removed; it is the only per-bot delivery state. Delivery is per room: a
  turn receives everything past the cursor, in platform order, each post
  labelled with its coordination thread. The reply goes to the turn's
  thread only (`REPLY_ENVELOPE`; `HINT` names the thread).
- *Normal*: when a bot starts a turn, the gateway fetches the room's posts
  after the cursor from the platform and delivers them as the turn's
  context ahead of the trigger; once the backend has accepted the turn
  the cursor advances to the last post delivered.
- *Contract*: nothing is withheld by admission policy; a bot that is not
  woken has not seen its backlog yet, and sees it at its next admitted
  turn in any thread. Delivered backlog is context only: control words
  and mentions in it are not re-executed. A turn that completes normally
  is not delivered again. A fetch failure, or a crash between the backend
  accepting a turn and the cursor being saved, causes bounded
  re-delivery: exactly-once across a crash would need a transaction
  spanning the backend and the record, delivery is best effort
  (`docs/requirements.md`), and re-delivered context is harmless.
- *Failure*: if the fetch fails the trigger is delivered alone and the
  cursor does not move; the next successful fetch re-delivers that
  trigger as part of the backlog.
- *Interface*: reuses the connector history fetch
  (`Connector.fetch_room_history`, `gateway/core/connector.py:672`), with
  its boundary changed from a timestamp to a post id, extended to return
  post ids and thread ids, and to page past the first page (Mattermost
  fetches one page today, `gateway/connectors/mattermost/rest.py:402-409`).
- *Offline posts*: posts that arrive while the gateway is down are found
  by the connector's per-room reconnect replay
  (`Connector.replay_room_since`, `gateway/core/connector.py:629`), which
  runs them through the inbound pipeline. The gate of §5 applies to a
  replayed post as to a live one, and the gate decides whether a turn
  starts, never whether a post is delivered: a replayed post addressed to
  nobody still sits past every bot's cursor and is delivered at their
  next turn.
- *Per thread*: the posts past the cursor, grouped by thread, give each
  thread's backlog size and age for `SWEEPER` and `STATUS`; no per-thread
  entry is stored.

### SWEEPER (D2)

- *Scope*: a periodic pass over every bot in the room; for each bot, its
  undelivered posts grouped by coordination thread (`WATERMARK`).
- *Qualifying*: a thread qualifies for a bot when the bot's undelivered
  posts in it number ≥ `catch_up_post_threshold`, or the oldest is older
  than `catch_up_age_seconds`; and the thread is on `catch-up`, not
  paused, and has no plan running (a running plan gives the bot its turn,
  as a member or a later hop). Posts in `snooze` or `listen` threads never
  qualify; they are still delivered as context by whichever turn comes
  next.
- *Normal*: for each qualifying thread the bot gets one catch-up turn in
  that thread, in the order of the threads' newest posts; the qualifying
  set is taken once at the start of the pass. Each turn
  delivers the bot's whole backlog — the first of them delivers
  everything, the later ones nothing new — and its trigger names the
  thread it is answering; the reply is posted there and counts there. A
  thread that qualifies while the bot is already being given a turn in
  it needs no sweep.
- *Exceptions*: one catch-up turn per bot runs at a time; no turn is
  started when no thread qualifies.
- Also posts the pause notice, acknowledges mode changes, and removes
  thread-state entries that have returned to their defaults
  (`THREAD_STATE`).

### FAILURE_NOTICE (D3)

- A failed turn — a backend failure, or a reply the gateway cannot parse
  after one re-prompt (`REPLY_ENVELOPE`) — is announced from the failing
  bot's account. The notice is
  delivered through `WATERMARK` and starts no turn in any mode (the
  plan-end notice to a bot requester is `CALLBACK`'s, not this one). A
  plan whose member failed marks it `failed` and advances.

### THREAD_STATE (S1)

- *Owner*: one room state per (connector, room id), kept by the gateway
  and persisted like a watcher record. A watcher record
  (`WatcherState`, `gateway/core/state.py:123`) belongs to one bot in one
  room; the room state is shared by every bot the gateway runs there and
  belongs to none of them, so a bot joining or leaving changes nothing in
  it. It holds the per-thread entries below and each bot's delivery
  cursor. All updates come from the one gateway process, in order.
- *Principle*: a coordination thread has state only while it is away from
  its defaults. An entry is created when something first departs from the
  default and removed when it returns; a room with hundreds of historical
  threads holds entries for the handful that are active.
- *Entries*:

  | Entry | Created when | Removed when |
  |---|---|---|
  | delivery cursor, per bot (`WATERMARK`) | the bot's first delivery in the room | never — one post id per bot |
  | rate limit, per thread: count, remaining, last reset | the first agent post counted | a reset leaves the thread at its defaults — a human post with no extension outstanding, or `ttl_seconds` of silence |
  | mode override, orchestrator, per thread | set by a control word | `thread_state_ttl` of silence (days, not seconds), or the platform archives or deletes the thread; the room default applies again |
  | plan record, per thread: post id, requester, members, mode, `timeout:` | a post creates a plan | the plan ends — after its end has been reported (member states and pending turns are in memory) |

- *Garbage collection*: the sweeper removes entries that have returned to
  their defaults on each pass. `coop room status` lists only threads with
  entries.
- *Bound*: the active overrides and plan records, plus one cursor per bot.
  Kilobytes.

### PLAN (P1)

- *Creation*: every post that addresses at least one bot creates a plan;
  members are the addressed bots (minus the sender); the requester is the
  sender; the thread is the post's coordination thread. A post carrying
  only control words creates no plan.
- *Mode*: `parallel` by default. `sequential:` from an authorised sender
  orders the members as named; `parallel:` states the default explicitly.
  With `ADDRESS_RESOLVER` enabled, a multi-member post with no mode word
  takes the resolver's recommended mode and order once that decision has
  passed shadow measurement (§8).
- *One plan at a time per thread*: plans on a thread run in the order
  they were posted; a plan starts when the previous one has ended. Plans
  on different threads run concurrently. (A member of the running plan
  addressing another bot creates the next plan in the queue; it does not
  interleave.) The order in which plans start turns holds between human
  and bot requesters too: a human replies to what they have read, so a
  plan that jumped the queue would answer before the posts it depends on.
- *One turn at a time per bot*: a bot runs at most one turn at a time in
  a room — one backend session, one invocation
  (`gateway/core/message_processor.py:358`). Plans on different threads
  progress concurrently only through different bots; a member whose bot
  is busy waits for it.
- *Parallel*: every member's turn starts now, with no ordering between
  them. Parallel is for work that does not depend on order — "a, kick off
  the tests; b, revise the doc". It makes no promise of isolation: a
  member that starts later may see an earlier member's reply in its
  backlog.
- *Sequential*: members run one at a time in order; each hop receives the
  earlier replies in its backlog.
- *Timeout*: a member is bounded by `timeout:` from the moment the
  gateway tries to start its turn — at the plan's start for parallel
  members, when the hop is reached for sequential ones — waiting for a
  busy bot included; time in the plan queue is not. The backend's own
  turn limit (`agents.timeout`, `gateway/core/config.py:132`) is a
  separate floor per deployment. On a timeout the plan ends: the member
  is `timed-out`, members not yet started are `cancelled`, and the
  requester is told (`CALLBACK`). Whether the remaining hops depended on
  the one that timed out is not the gateway's to guess; the requester
  answers by re-posting the rest or not. `timeout:` defaults to
  `hop_timeout_soft_seconds`; a plan an orchestrator posts carries the
  `timeout:` its skill reads out of the request — long for steps that
  depend on each other, short for opinions in turn; with
  `ADDRESS_RESOLVER` enabled, a sequential post without `timeout:` takes
  the resolver's class, `hard` (`hop_timeout_hard_seconds`) or `soft`,
  once that decision has passed shadow measurement (§8).
- *A turn serves every plan that is waiting for it*: when a bot's turn
  starts on a thread, it serves this plan and every queued plan on the
  same thread that addresses the bot and has not yet given it a turn —
  the later posts ride in the turn's backlog. Each served plan marks the
  member from this turn: `replied` when the turn's `to` includes that
  plan's requester (`REPLY_ENVELOPE` asks for it once), `failed` when it
  still does not, `self-terminated` when the turn was `done`. A
  sequential hop that has not been reached is never served early. This
  keeps a run of posts to one bot a single turn, as today's catch-up
  batch does.
- *Member states*: exactly one per member — `replied`, `self-terminated`,
  `failed`, `timed-out`, `cancelled` (by `stop:` or a timeout before the
  member ran), `unknown` (after a restart, for a member whose outcome is
  not visible in the room).
- *Boundary*: the plan ends when every member has a state. A queued plan
  whose members were all served by earlier turns ends without reaching
  the head of the queue, and its callback fires then — the one place a
  plan's end leaves posting order, allowed because the turns that served
  it had already seen everything it depends on. The requester's post is
  the trigger; member posts count against `RATE_LIMIT`. Overshoot past a
  pause is at most the running plan's members minus one.
- *Restart*: a plan's record is on disk (`THREAD_STATE`); member states
  and pending turns are in memory. After a gateway restart no plan is
  running: the records are read before anything is removed; each
  recorded plan ends with a member `replied` only where a post by that
  member, later than the plan's post, in its thread, addresses the
  requester, and `unknown` otherwise; its requester is told, and the
  record is removed. The thread's rate-limit state and the cursors are
  on disk, so nothing else needs recovery.

### CALLBACK (P2)

- When a plan ends and its requester is the thread's orchestrator bot,
  the orchestrator gets one turn carrying the members' replies and states
  (through `WATERMARK`). A member's post addressed to that orchestrator
  creates no plan for the orchestrator: it is the plan's reply and
  arrives in the callback. Other bots the same post names form a plan as
  usual.
- A plan requested by a human or by any other bot ends without a
  callback. Its members' replies reach the requester as any addressed
  post does — a human is notified; a bot gets a turn, one per reply
  unless it is busy and the later replies ride in its backlog.
  Designating an orchestrator is what turns N member replies into one
  turn.
- A plan that ends abnormally — a member `timed-out` or `failed`, members
  `cancelled`, or refused while paused — tells its requester with a
  notice addressed to it, listing each member's state and how to
  continue (`@b @c sequential:`). A human is notified; the orchestrator
  reads it in its callback; any other bot requester gets a turn for it as
  for any addressed post, once the thread is not paused. The notice is
  the question "continue without `a`?"; the requester's next post is the
  answer — a skill decides from the states, a human from the notice.
- The orchestrator's callback runs while the thread is paused.

### STOP (P3)

- `stop:` from a human on a thread: every running and queued plan on the
  thread marks members that have not started `cancelled`, lets running
  turns finish, and ends at that boundary. The notice lists each plan
  with each member and its state (`a: replied`, `b: replied`, `c:
  cancelled`) so the human knows what was done and what was not. Nothing
  else interrupts a plan.

### REPLY_ENVELOPE (R1)

- *Purpose*: an explicit, structured way for a turn to say "here is my
  post, for these recipients" or "nothing to say". It replaces two things
  parsed out of prose: `<end-of-agent-chain>` — agents in production
  have misspelt the token, embedded it in a message, and written "I have
  nothing to say" as a post — and the recipient list, which the gateway
  reads from every mention in the body, so a reply that quotes a peer
  addresses the peer. A required field is harder to get wrong than a
  magic string or a prompt rule.
- *Fields*: `to` — the recipients: usernames of humans or bots in the
  room, or `all`; required with `text`, non-empty, the sender excluded.
  A `text` consisting only of control words needs no `to`.
  `text` — the post. `done` — nothing to post. One of `text` / `done`;
  `done` with `text` is `text`. The encoding — a fenced block, the whole
  reply, or a backend-native structured output — is decided in task 1
  (§9); the contract is the fields.
- *Canonical form*: the gateway rewrites `text` before posting so that
  the post's mentions are exactly `to`: each recipient not mentioned in
  `text` is prepended as `@name`; every `@name` in `text` that is not in
  `to` loses its `@`; `all` becomes the connector's room-wide mention. A
  human reads the recipients at the head of the post; every bot's gate
  reads the platform mentions and reaches the same set — after a restart
  and from a history fetch alike. Every post is parsed one way, from the
  platform, whoever wrote it.
- *Validation*: no envelope, `text` without `to` (unless it consists
  only of control words), an empty `to`, a `to`
  naming only the sender, a name that is neither a room member nor a bot
  the gateway knows, or a `to` that omits the requester of a plan this
  turn serves (`PLAN`), is returned to the same session once as a schema
  error — a continuation of the turn, not a post, not counted. A second
  output that still omits a served requester is posted as it is, and that
  plan's member is `failed`; any other second invalid output ends the
  turn as `failed` (`FAILURE_NOTICE`) with nothing posted. Output that is
  only `<end-of-agent-chain>` is `done`; the token inside `text` is
  removed.
- *Routing*: `text` is posted in the turn's thread and counts against
  that thread's `RATE_LIMIT`. `done` posts nothing and counts nothing.
- *Input*: the global agent context describes the envelope once — the
  fields; that `to` is who the post is for, not who it mentions; that
  `all` needs a reason. Each turn's prompt reminds the agent of the
  thread it is replying in and that `done` is the answer when it has
  nothing to add.
- *Cost*: a reply to a bot is addressed to that bot, so a bot-to-bot
  exchange ends with one more turn for the asker — where the next task,
  or `done`, comes from. `done` counts nothing.
- The gateway keeps no agent output: a turn's `text` is posted or it is
  not.

### HINT (H1)

- Injected into every turn: the thread this turn replies in, its mode, the remaining posts before
  its pause, and "make your point; do not restate; answer `done` if you
  have nothing to add; `to` is who the post is for, not who it mentions;
  `all` only with a reason". Derived from `UNADDRESSED_MODE` and
  `RATE_LIMIT` state.

### STATUS (O1)

- `coop room status <handle>`: per thread — mode, count / `post_limit`,
  paused, orchestrator, running plans; per bot — backlog size and age.

### ADDRESS_RESOLVER (J1) — classifier, optional

- Given a post and the room's roster (username, display name, aliases,
  description per bot), returns the addressed bots, an order, and a
  recommended mode, and for a sequential post a timeout class — `hard`
  when the steps depend on each other, `soft` when they are opinions in
  turn. Used by `MENTION_GATE` for tier-3 addressing of human posts and
  by `PLAN` for the mode and timeout of multi-member posts, each only
  after its shadow accuracy is measured (§8). It never starts a turn and
  never writes text.

### LISTEN_GATE (J2) — classifier, later

- In `listen` rooms, whether a post contains a claim or a question that
  someone should answer; a post that does not is delivered without listen
  turns. Later still: which skill fits a prompt and which bot should chair.

### What the gateway does not have

No topic ids or per-topic budgets, no per-message classification of mode
without the resolver, no persisted copy of the message backlog (history is
fetched from the platform; pending turns and member states exist in
memory), no agent bypass of the mention gate, no addressing inferred from
an agent's text, no inferred interruption,
no isolation between members of a plan, and no knowledge of what a meeting
is.

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
| 4 | Plan P1 (parallel, 4 members) runs; each member's backlog is whatever has arrived when its turn starts — usually just #1–#3; each replies `to: [chair]`, which starts no chair turn (`CALLBACK`) | 4 | 4 (a, b, c, d) | 4 | — |
| 5 | P1 ends | 1 exempt (chair) | — | 4 | 1st |
| 6 | `chair: <synthesis: positions, agreements, conflicts, open questions>` · `@a @b @c @d — read the synthesis and the others' replies; post only a substantive addition or objection, otherwise done.` | — | 1 (chair) | 4 | — |
| 7 | Plan P2 runs; `a`, `b`, `c` answer `done`; `d` objects to one point, `to: [chair]` | 4 | 1 (d) | 5 → **pause** | — |
| 8 | Gateway: `thread paused (5/5). extend: N, or a human post, to continue.` | — | 1 (gateway) | 5 | — |
| 9 | P2 ends | 1 exempt (chair; runs while paused) | — | 5 | 2nd |
| 10 | `chair: @H <close: decision, open items, who said what, d's objection recorded>` | — | 1 (chair) | 5 | — |

Totals: 8 member turns, 3 exempt turns, 5 counted posts, 2 callbacks,
11 posts in all (2 human, 3 chair, 5 members, 1 gateway). Today's lab debate: about 25 agent posts
per prompt. Had `d`'s objection needed an exchange, the chair would post
`extend: 2` then `@b @d sequential: <the point>`; the plan is admitted
only because the extension made `remaining ≥ 1`.

What the gateway did: two plans, one rate limit, two callbacks, one
notice. It did not know there was a meeting.

### 6.2 A stop, and a failure

`H: a, do the cleanup; then b, restart the server; then c, restart the
client` with `sequential:` — a three-hop plan requested by `H`.

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

### 6.3 One bot, two threads

`x` is the third hop of a sequential plan on `T1` (`a → b → x`); `a` has
replied, `b` is running. On `T2`, `H: x, is the staging deploy green?`
creates a one-member plan; `T2` has no running plan, so it starts now.

- `x`'s `T2` turn receives everything past its cursor — `a`'s `T1`
  reply, labelled `T1`, and `H`'s `T2` post — and replies in `T2` only;
  the hint names `T2`. Its `T1` hop is untouched.
- `b` replies on `T1`; `x`'s hop starts: it receives what arrived since —
  `b`'s reply — and replies in `T1`.
- Had `H` posted `x, …` on `T1` instead, that plan would queue behind the
  sequential one and `x` would get its hop first, then a second turn for
  `H`'s post — unless the hop had not yet started, in which case the hop's
  turn serves both.

## 7. Scenario validation

| Scenario | Mode and control | How it runs | Supported |
|---|---|---|---|
| One room, three scenarios in a day | `catch-up` → `mode: snooze` + `orchestrator:` → `mode: catch-up` | Casual talk; a meeting on one thread; casual talk again — no restart, no second room (goal 2). | yes — a test in §8 |
| One thread, three scenarios | `listen` → `mode: snooze` + `orchestrator:` → `mode: listen` | A debugging thread becomes a vote run by a chair, then a debugging thread again; the running plan finishes under the old mode, the count carries over. | yes — a test in §8 |
| Casual conversation (production `#nest`) | `catch-up` | Addressed bots run; others catch up at the sweep; a correction arrives at the next catch-up turn rather than being dropped. | yes |
| Brainstorm (lab, five bots) | chair skill; `snooze` | §6.1. | yes |
| Status / decision / roadmap meeting | chair skill | `sequential:` reports; `parallel:` vote by token, visible to all; brainstorm skill. | yes |
| Anonymous vote | — | Members would have to answer without seeing each other and without the room seeing them; the room is the unit of visibility and `parallel:` promises no isolation. | **not in this version** (§10) |
| Debugging session | `mode: listen`, `limit: 20` | Serial listen turns correct a wrong claim on the next turn; pause every `post_limit` posts. | yes — one turn per bot per post, chosen |
| Divide and collaborate | PM skill or H; `catch-up` | `parallel: timeout: 30m`; results at the boundary; a member addresses a peer for a dependency. | yes |
| 1:1 DM | — | The bot is addressed by definition; `RATE_LIMIT` and `WATERMARK` still apply. | unchanged behaviour |
| Single-bot channel | per `require_mention` | As today: the bot runs when mentioned, or on every post if the room is configured listen-all. | unchanged behaviour |
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

### 8.1 The simulator comes first

Before any gateway code, §5 is implemented as a standalone simulator
(`tools/roomsim/`, outside the gateway) and the scenarios in §7 are run
through it. Its purpose is to see the conversations this design produces
— and the odd corners — before paying for an implementation.

- **Mechanism**: a deterministic script implementing §5 as written —
  thread state, the mention gate (tiers 1 and 2), modes, the rate limit,
  the sweeper, plans in FIFO order, the reply envelope. It is not an LLM
  tracing the rules: a trace by a model would hide whether a wrong step
  was a hole in the spec or an error in the trace. The script is the
  executable form of §5; where it and the text disagree, one of them is
  wrong and the text is fixed or the script is.
- **Members**: each agent turn is one `claude -p` (or OpenCode) call with
  the bot's persona from the lab configuration, in one session per bot
  per room, as the gateway keeps it; each turn appends what the cursor
  delivers — everything undelivered in the room, labelled by thread — and
  nothing else is injected. The orchestrator
  is the same, loaded with its skill; its control words are parsed by the
  script.
- **Humans**: scripted lines in the scenario file, placed at a step; an
  optional model playing the human is allowed and labelled as such.
- **Output**: a transcript with the script's annotations before each
  event — the turn, its thread and backlog size, the count, a plan's
  member states at its end, every pause, every sweep — and a tally of
  turns, posts, counted posts, tokens and context size per bot. The
  transcript is reviewed by people for exchanges that read wrong; what
  reads wrong is fixed in §5 and the scenario is run again. Counted in
  every run: how often one bot is addressed in several threads at once;
  how many requests a coalesced turn leaves unanswered; whether an early
  callback reads out of order; how many plans refused at a pause were
  partly served, and whether an orchestrator skill that waits one to
  three minutes before `extend:` — the plans continue, the human has time
  to read — removes them; how many `done` turns bot-to-bot replies
  produce; how often a turn answers another thread's post in its own.
- **The classifier**: `ADDRESS_RESOLVER` and `LISTEN_GATE` run in the
  simulator in shadow — every decision logged against the rule-based
  one and hand labels — and, as a separate run, with effect, so that the
  model's accuracy and its cost are known before task 8.
- **Two kinds of run**: a run with live models is for behaviour — what
  the agents do with the rules, over several runs, read by people; it is
  not expected to repeat. A run's inbound events and agent outputs can
  be saved as a replay fixture, and replaying it is deterministic.
- **Afterwards**: the mechanism tests in the table below are written
  against the simulator first and carried to the gateway unchanged; on a
  replay fixture, the gateway must produce the same annotation sequence
  as the simulator.

What the simulator cannot show: platform behaviour — threads, the
mention array each platform computes from a canonical post (code blocks,
quotations), history paging, webhook identities — and session
compaction; those belong to the lab tasks.

### 8.2 Tasks

| # | Task | Builds | Mechanism tests | Effect measurements |
|---|---|---|---|---|
| 0 | **Simulator.** §8.1: the script, the scenario files for every §7 row, persona files from the lab, the classifier in shadow and with effect. | — (reference) | the §6 traces reproduced by the script; each mechanism test below, run against the script. | Every §7 scenario's transcript reviewed; turns, posts, tokens per scenario; classifier agreement with hand labels and with the rule. |
| 1 | **Deliver, do not drop.** The room state record; one delivery cursor per bot; history fetch bounded by post id, with thread ids and paging; Layer 2 overflow and failure notices delivered at the next turn; the reply envelope (`to` / `text` / `done`), the canonical rewrite and the one-retry validation; the catch-up batch replies in one thread. | WATERMARK, FAILURE_NOTICE, REPLY_ENVELOPE, THREAD_STATE | a normally completed turn's posts are delivered once, in order, labelled by thread; the cursor survives restart; a failed fetch delivers the trigger alone and the next fetch re-delivers it; a crash between accept and save re-delivers, never loses; replayed offline posts reach every bot's backlog whether or not they address it; envelope parsing — `text` with `to`, `done`, token-only → `done`, token inside `text` removed; the posted text's mentions equal `to` (recipient prepended, non-recipient demoted, `all` mapped); an invalid envelope is re-prompted once, then `failed` with nothing posted; messages from two threads arriving while the bot is busy yield one turn per thread, the first delivering both, each replying in its own. | Lab: dropped posts 21 → 0 over a debate; the two misattributions in Appendix A absent on replay. Production: error-notice turns per failure 4 → 0. |
| 2 | **Mention gate for agents.** Agent senders pass the same gate, read from their canonical mentions; username without `@` counts in human posts only. Unaddressed bots are `snooze` until task 4, so this task stays lab-only until task 4 lands. | MENTION_GATE | an agent post addressed only to humans starts no turn; naming one bot starts one. | Lab: posts per human prompt; self-termination rate; every agent post addressed only to humans hand-checked for whether a bot's reply was wanted. |
| 3 | **Rate limit per thread.** Replaces per-pair counters: pause, notice, human reset, `extend:`, `limit:`, TTL; hint. Plans do not exist yet, so the boundary rule is "the current turn finishes". | RATE_LIMIT, AUTHORITY (humans), HINT | token-only and failed turns count zero; pause refuses new turns and keeps delivering; each reset path; guest control words are text. | Lab: where pauses land relative to conclusions (observed, not guaranteed). Production: pauses per week against the two drops; notices read by the human. |
| 4 | **Modes and the sweeper.** `mode:` per room and thread at runtime, persisted; `catch-up` default; `snooze`; status; thread-state GC. | UNADDRESSED_MODE (snooze, catch-up), SWEEPER, STATUS, THREAD_STATE | mode change applies to the next post; a thread qualifies on threshold and on age, never when paused or under a running plan; one catch-up turn per qualifying thread, in newest-post order, each replying in its own thread; no turn when nothing qualifies; two threads in one room on different modes; rate-limit and override entries vanish at their reset or `thread_state_ttl`. | Lab: backlog age at delivery; production `#nest` on `catch-up`: correction latency. |
| 5 | **Plans.** One plan at a time per thread, parallel and sequential members, a turn serving every waiting plan, admission and overshoot, member states, `timeout:`, `orchestrator:`, callback, `stop:`, restart notice. The rate-limit boundary becomes the plan boundary. | PLAN, CALLBACK, STOP, AUTHORITY (bot) | plans on a thread run in posting order, one at a time; a sequential hop sees earlier replies; a run of posts to one bot is one turn; an unreached hop is never served early; after a restart the requester is told `replied` / `unknown` per member; callback exactly once and only to the orchestrator; a plan refused while paused; overshoot ≤ members − 1; `stop:` states reported for running and queued plans; a queued plan fully served by earlier turns ends before reaching the head; a bot runs one turn at a time per room; `timeout:` runs from the attempt to start the member, waiting included, never from the post; a timeout ends the plan with the rest `cancelled` and a notice to the requester; a bot requester gets a turn on an abnormal end; a coalesced turn whose `to` omits a served requester is re-prompted once, then that member is `failed`; after a restart `replied` only from a later post by the member addressing the requester; plan records survive restart and are removed after the end is reported. | Lab: sequential vs parallel self-termination on the same prompt; anchoring (a later bot reversing after reading an earlier one). |
| 6 | **Listen mode.** Serial listen turns; wait for a running plan. | UNADDRESSED_MODE (listen) | order follows the roster; listen turns suspended during a plan; the three-scenario day and the three-scenario thread (§7 rows 1–2) as end-to-end tests. | Lab debugging rehearsal: a planted wrong claim corrected on the next turn; turns per post. |
| 7 | **Brainstorm skill.** §6.1 end to end. | — (policy) | the trace in §6.1 reproduced: 8 member turns, 5 counted posts, 2 callbacks, 11 posts. | Lab: Appendix A prompts replayed: turns, posts, tokens, human re-entry time, objections caught. |
| 8 | **Resolver.** Tier-3 addressing, mode and timeout-class recommendation in shadow; then `LISTEN_GATE` in shadow. | ADDRESS_RESOLVER, LISTEN_GATE | — | Agreement with hand labels per decision type before any of them is given effect. |

Dependencies: 0 precedes everything and changes §5 before code does. 1
and 2 are independent; 3 needs 1 for the room state its counters persist
in; Layer 1 (self-termination) stays throughout, and task 1's `done`
flag is its structured form. 4 needs 1. 5 needs 1, 2 and 3, and upgrades
3's boundary rule. 6 needs 4 and 5. 7 needs 5. 8 needs 5 and 6.

---

## 9. Undecided

Decisions this version needs and does not yet make; each names the task
that settles it.

- **Envelope encoding** (task 1). A fenced JSON block, the whole reply, or
  a backend-native structured output where one exists. The contract is
  the three fields; the encoding is chosen when the parser is written.
- **Backend-native schema validation** (task 1). Whether `claude
  --json-schema` returns a schema error to the model and composes with
  the adapter's streaming output is unverified; the OpenCode HTTP backend
  has no equivalent. The gateway's re-prompt is the path that holds for
  every backend; a native one may replace it where it exists.
- **Per-mode `post_limit`, `catch_up_post_threshold`,
  `catch_up_age_seconds`, `thread_state_ttl`** (task 4) **and
  `hop_timeout_soft_seconds` / `hop_timeout_hard_seconds`** (task 5).
  Measured in the lab before defaults are fixed.
- **Webhook sender identity** (task 2). Which username a webhook post
  carries on each platform, so that allow-listing a CI account as a guest
  is known to work there.
- **Glossary** (on acceptance). "Orchestrator", "mode", "delivery cursor",
  "sweeper", "control word", "coordination thread", "reply envelope",
  "thread state" go
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
- **Concurrent plans on one thread** — a member's request to another bot
  waits for the running plan to end, which can read oddly when the pause
  or a slow hop sits between them. Reopened when the lab shows how often
  it is noticed.
- **Isolation between members, and anonymous votes** — a round where no
  member sees another's answer and the room does not see them; it needs
  a side channel (a DM per member, aggregated by the orchestrator), not a
  plan mode. Reopened if a skill needs it.
- **Replies to several threads from one turn** — a turn is delivered the
  whole room but replies in its own thread only; a bot with backlog in
  several threads gets a turn per thread. Reopened if the lab shows
  multi-thread catch-up turns worth merging.
- **Envelope extensions** — mentions resolved by name, a reply-to post, a
  reaction. Reopened after the envelope has run in the lab.
- **Addressing chosen for the agent** — a default `to` for an envelope
  without one (the trigger's sender), or a classifier judging at the
  rewrite whether a reply needs to address the asker at all, to save the
  asker's `done` turn. Both are guesses about intent. Reopened when the
  lab shows what share of turns are those `done` turns.
- **Rooms spanning gateways**, **hidden information**, and **convergence
  by the gateway** — non-goals (§2).

---

## Appendix A — Evidence from gateway logs

Compact figures behind §1. Lab: `#product-idea`, 2026-10-07, five agents
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
branches in all three see only the fan-out point — an isolation `PLAN`'s
`parallel:` does not promise.

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
parallel independent first round, a synthesis, one bounded revision
round — and a
preference for members that differ in model, information or tools over
copies trading opinions.

---
