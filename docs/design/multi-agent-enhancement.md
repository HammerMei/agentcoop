# Multi-agent rooms: let a classifier decide who speaks

## Summary

In a room with several bots, the gateway asks one small classifier a narrow
question for every post:

> Which bots in this room should speak now?

The classifier may add speakers, but it may never silence a bot that the post
addresses. Every bot still eventually sees every post. If a bot should not
speak now, the gateway leaves the post in that bot's backlog and delivers it on
a later turn.

The gateway deliberately does not model collaboration. It keeps no shared
plan, dependency graph, deadline, or record of who owes an answer. It provides
only a reliable delivery path and a few mechanical controls:

- one delivery cursor per bot per room;
- one classifier decision per post per gateway;
- per-thread mute, rate-limit, and cooldown gates;
- a sweeper that revisits held work and catches bots up;
- a structured reply envelope with explicit recipients;
- scheduled follow-ups for work a bot is waiting on.

The platform remains the shared medium. Control commands and bot replies are
ordinary visible posts, so humans and bots on other gateways can observe them.
Appendix B compares this design with the plan-based alternative it replaced.
Appendix C maps the proposal to current code. Appendix D records the other
systems read while positioning it.

---

## 1. Why change the current design?

Today, three layers bound agent-to-agent conversations:

1. A bot may return `<end-of-agent-chain>`, which suppresses its reply.
2. Each bot counts posts from each sender in a room and drops posts beyond
   `max_turns`.
3. Those counters reset after `ttl_seconds` of silence.

`require_mention` applies only to human senders. A post from another bot
bypasses that gate and reaches every bot. The `to:` context contains every
platform mention, even when a mention is only a reference rather than a
request. The rule that bots mention only intended recipients exists only in
the prompt.

Measurements in Appendix A show why this is not enough:

- The counter drops substantive posts, often while a discussion is converging.
- Because each bot drops different posts, bots develop inconsistent views of
  the room.
- Every bot answers every bot, producing about 25 bot posts per human prompt
  in the five-bot test room.
- A quoted mention can trigger a bot that was not asked to respond.
- `@all` starts all bots at once, before any sees the others' answers.

The counter is a rate limiter being used as a convergence mechanism. It cannot
distinguish a loop from a useful conclusion. The room instead needs complete
delivery, explicit turn eligibility, and a better choice of who speaks.

---

## 2. Design goals and invariants

### 2.1 Goals

1. Deliver every post to every bot, in room order, without dropping context.
2. Start turns only for bots that were addressed, selected, or are due to
   catch up.
3. Slow loops without asking the gateway to judge what a bot says.
4. Let humans and a designated orchestrator control a thread through visible
   posts.
5. Work across several gateways on a best-effort basis, without shared gateway
   state.

### 2.2 Invariants

- **A mention is a request to speak.** The classifier cannot remove an
  addressed bot.
- **Delivery and speaking are separate.** A bot may receive a post as context
  without getting a turn for that post, and a delivered thread may be closed
  to replies.
- **Every bot eventually sees everything.** A bot has one cursor per room, not
  per thread.
- **One bot runs at most one turn per room at a time.** No queue of waiting
  turns is persisted; eligibility is reconstructed from backlog and state.
- **The platform is the shared record.** Gateways do not exchange private
  coordination state.
- **The gateway enforces mechanics, not collaboration policy.** Meetings,
  handoffs, deadlines, and brainstorms live in agent skills. Delivery,
  eligibility, authority, and reply validation are deterministic gateway
  rules, with no model involved, so a bot cannot bypass them.

### 2.3 Non-goals

- Guaranteed completion, plan timeouts, per-member reports, or automatic
  notices about who did not answer.
- Hidden information or anonymous voting. A room is the visibility boundary.
- Semantic convergence. The gateway slows loops; it does not decide who is
  correct or when a discussion is finished.
- Fair allocation of a thread's speaking allowance.
- Ordering bots selected by the same post. They start in parallel.

---

## 3. Terms

Each definition below uses only ordinary language or terms defined above it.

| Term | Meaning |
|---|---|
| **post** | One platform message written by a human or bot. |
| **bot** | An agent's presence on one chat server. Within one room, that presence is a watcher. |
| **thread** | A platform thread, or all top-level room posts treated as one thread. The rules below apply per thread. |
| **addressed** | A post mentions a bot. Human posts may use `@bot`, `@all`, or a room-wide mention. Bot posts use only mentions the platform recorded on their post. A bare name is not an address. A 1:1 DM addresses its bot by definition. |
| **scheduled job** | A reminder a bot creates through the existing scheduler. It fires at a chosen time for that bot in one thread, without a new platform post. |
| **classifier** | A small model that chooses which additional bots should speak for a post. It never writes a reply or removes an addressed bot. |
| **rate limit** | A per-thread post and word allowance. Once reached, it delays further bot invocations unless their source is exempt. |
| **cooldown** | The minimum interval between one bot's own posts in a thread. |
| **orchestrator** | A bot designated by an owner for one thread. It is exempt from the rate limit and cooldown and may change that thread's rules. |
| **muted** | Silenced in a thread except when addressed by a human or the orchestrator. |
| **cursor** | The last post delivered to one bot's session in one room. It is distinct from the connector watermark, which tracks what the connector has processed. |
| **backlog** | Posts after a bot's cursor, plus any scheduled-job prompt held while that bot was muted. |
| **open / closed** | Whether a bot may reply in a thread during one backend invocation. Mute, rate, and the invocation source determine this before the invocation starts. |
| **sweeper** | A periodic per-bot pass over backlog and previously closed threads. It starts eligible bot invocations that were held or are due for catch-up. |
| **turn** | One backend invocation, started by an addressed post, classifier choice, sweeper, or scheduled job. |
| **turn source** | Why a turn started: `addressed`, `picked`, `catch-up`, `unfinished`, or `job`. `HINT` states it. |
| **done** | A bot's explicit statement that it has nothing to post in a thread. |
| **unfinished thread** | A thread delivered during a turn while closed to that bot, in which the bot has not yet replied or returned `done`. Persisted with the cursor. |
| **must-reply thread** | A thread for which the turn's envelope must hold an item: the starting thread, every open delivered thread in which a delivered post addressed the bot, and every open unfinished thread. `HINT` lists them. |
| **control word** | `limit:`, `orchestrator:`, `mute:`, or `unmute:` in a visible post. |
| **reply envelope** | Structured turn output containing one or more reply items, each with visible text, control words, or a `done` decision, grouped by thread. An envelope may produce no visible posts. |
| **settings** | A thread's current orchestrator, limits, and mutes, with the source post for each value. |
| **agent list** | All usernames the gateway treats as agents, including agents hosted by other gateways or by no gateway at all. |

### 3.1 Configuration parameters

A count or duration of `0` disables that limit.

| Parameter | Meaning |
|---|---|
| `rate_limit_posts` | Maximum recent agent posts in a thread. May be overridden by `limit: N posts`. |
| `rate_limit_words` | Maximum words in recent agent posts. May be overridden by `limit: N words`. |
| `rate_limit_seconds` | Window in which an agent post remains recent. |
| `cooldown_seconds` | Minimum interval between one bot's posts in a thread. |
| `sweep_interval_seconds` | Interval between periodic sweeps for one bot in one room. |
| `catch_up_post_threshold` | Undelivered posts in a thread that trigger catch-up. |
| `catch_up_age_seconds` | Age of the oldest undelivered post that triggers catch-up. |
| `control_ttl_days` | Thread inactivity after which settings expire. |
| `classifier_posts` | Maximum recent thread posts shown to the classifier. |
| `classifier_timeout_seconds` | Timeout for one classifier call. |
| `classifier_retries` | Retries before classifier fallback. |
| `classifier_fallback` | `addressed-only`, `everyone`, or `addressed-or-everyone`. |
| `classifier_decision_ttl_seconds` | Lifetime of a decision for a turn held by another gate. |
| `follow_up_retry_cap` | Failed scheduled-turn retries before a notice is posted. |
| `failure_backoff_seconds` | First wait after a backend failure; doubles per consecutive failure. |
| `failure_backoff_max_seconds` | Upper bound of that wait. |
| `consecutive_invalid_limit` | Consecutive turns failed on invalid envelopes before a bot is degraded in a room. `0` keeps the count for `STATUS` and degrades nobody. |

Only `rate_limit_posts` and `rate_limit_words` may vary by thread.

---

## 4. End-to-end flow

When a post arrives, the gateway applies the following sequence for every local
bot in the room. The same rules apply to human and bot senders; unlike today's
path, bot-authored posts do not bypass turn eligibility.

1. **Apply controls.** If an authorised sender wrote a control word, update the
   thread settings. Stop if that authorised post contains only controls.
2. **Check addressing.** An addressed bot is eligible according to the turn
   table below.
3. **Check mute.** If the bot was not addressed and is muted, do not start a
   turn. The post remains in its backlog.
4. **Classify.** Ask the classifier once for the post and share that decision
   among all local bots. Selected bots become eligible.
5. **Apply rate and cooldown gates.** Unless exempt, an eligible turn waits
   while the thread is rate-limited or the bot is cooling down.
6. **Serialize per bot.** If that bot already has a turn running in the room,
   let the sweeper reconstruct the held turn later.
7. **Build context.** Deliver all posts after the bot's cursor through the
   trigger post, or through a fixed high-water mark for a sweep or job. Label
   every post by thread, state the turn source, and group the threads as
   must-reply, optional, or closed (`HINT`).
8. **Handle output.** Validate the reply envelope, canonicalise mentions, and
   post each reply to its thread. `done` posts nothing.
9. **Commit delivery.** On a normal turn end, save the cursor and update
   unfinished threads. On turn failure, keep the old cursor so the same input
   is delivered again.

The sweeper and scheduled jobs may initiate step 5 independently of a new post.

### 4.1 Turn eligibility (`TURN_ELIGIBILITY`)

| Turn source | Bot not muted | Bot muted |
|---|---|---|
| Human mention | Start; rate and cooldown exempt | Start; rate and cooldown exempt |
| Orchestrator mention | Start; rate and cooldown exempt | Start; rate and cooldown exempt |
| Another bot's mention | Start under rate limit and cooldown | Do not start |
| Classifier | Start under rate limit and cooldown | Bot is not offered to classifier |
| Catch-up sweep | Start under rate limit and cooldown | Do not start |
| Newly opened unfinished thread | Start under rate limit and cooldown | Do not start |
| Bot's scheduled job | Start; rate and cooldown exempt | Store the prompt in backlog until unmuted |

The orchestrator's own turns are always exempt from rate and cooldown gates. A
human may explicitly mute the orchestrator; after that, only a human mention
wakes it. `mute: @all` means every bot except the current orchestrator.

A bot degraded after repeated invalid envelopes uses the muted column; a bot in
backoff after backend failures waits as under cooldown (`FAILURE_NOTICE`).

### 4.2 Short example

Room `#team` contains bots `a`, `b`, and `c`, with a six-post limit over five
minutes.

1. `H` addresses `a` and `b`: check the nginx log, then restart the backend.
   Both start. The classifier adds nobody. `b` returns `done` because it must
   wait, and schedules a follow-up. `c` keeps the post in backlog.
2. `a` reports to `[H, b]` that the log is clean. The mention starts `b`, which
   restarts the backend and reports to `[H, a]`.
3. `a` returns `done`. There are two recent visible agent posts.
4. Catch-up gives `c` the exchange; `c` returns `done`.
5. `H` asks `@all` whether this matches last week's incident. All three start
   and can see the `@all` in the post itself. `a` answers, `b` adds one line
   after seeing `a` in backlog, and `c` returns `done`.
6. After one more `a`/`b` pair, the six-post limit holds the next turn. If `H`
   writes "enough, thanks", the human post clears the count and requests a
   sweep, which starts the held turn with that post in context; the bot returns
   `done`. Without human input, it waits until enough posts age out of the
   window and the periodic sweep finds it.

---

## 5. Mechanisms

### 5.1 Speaker classifier (`WHO_SPEAKS`)

The classifier receives only:

- the new post, with addressed bots marked;
- up to `classifier_posts` recent posts from the same thread, newest first and
  bounded by a token budget; if older posts are omitted, their count;
- the agent list, including each bot's username, display name, nickname, and a
  free-text profile describing its role and when it should be asked. Display
  names and nicknames let the classifier recognise a bot named in prose
  without `@`.

It does not receive backlog, busy state, cooldown, or rate-limit state. Those
are gateway concerns.

The answer contains two username lists, each entry with a short reason:

- `add`: unaddressed bots that should speak;
- `remove`: addressed bots that appear unnecessary.

Speakers are `addressed + add`. The gateway records `remove` for measurement
but does not act on it. Addressed bots start immediately while classification
runs; added bots start when the decision arrives.

#### One call per post per gateway

The gateway caches `(connector, room, post id) -> in-flight decision | result`.
All local connector copies of the post share this entry. Addressed bots do not
wait for it; unaddressed bots do. A decision lives for
`classifier_decision_ttl_seconds`. The sweeper reads it while the turn it
selects is held by the rate limit, by cooldown, or by the bot's running turn
(`ONE_TURN`), so the TTL must outlast a normal hold. It applies only while its
post remains in that bot's backlog. A late decision cannot start a turn after
another turn has already delivered its post. A mute also holds a selected turn,
but recovery from a mute does not use the decision: the post is delivered as
closed context, the thread becomes unfinished, and `unmute:` requests a sweep
(`MUTE`). A decision that expires while its turn is held is counted
(`STATUS`); the post then waits for ordinary catch-up.

The agent list given to the classifier includes bots hosted by other gateways,
so a decision may name a bot this gateway does not run. A gateway starts turns
only for its own bots and records the rest of the decision for status.

Each gateway classifies independently. Gateways may disagree.

Do not call the classifier for a 1:1 DM, a control-only post, or a thread in
which every bot is muted. The classifier only proposes speakers; it never
writes text, starts a turn directly, removes an addressed bot, or orders the
bots it selects. The gateway asks it to choose the smallest useful set of
speakers rather than every bot that could answer.

#### Failure policy

After `classifier_retries` timeouts or errors, use the configured fallback:

| Fallback | Human post | Bot post |
|---|---|---|
| `addressed-only` | Addressed bots | Addressed bots |
| `everyone` | Every bot | Every bot |
| `addressed-or-everyone` | Addressed bots | Every bot, matching current behaviour |

Mute, rate, and cooldown rules still apply. Every fallback and reason is logged
and visible in room status. Catch-up does not depend on classification, so a
classifier failure cannot permanently hide a post.
Processing of a post waits for the classifier no longer than the configured
attempts: after the final timeout or error, the fallback decision completes
classification.

The classifier judges relevance from text; it cannot know private bot context.
It will sometimes miss expertise or corrections. Catch-up is the safety net,
so its thresholds should initially be more eager than in a design with
explicit per-thread modes and tuned from simulator results.

### 5.2 Mute (`MUTE`)

`mute: @x` silences `x` in one thread except when a human or orchestrator
mentions it. While muted:

- the classifier cannot select it;
- the sweeper skips it;
- another ordinary bot's mention does not wake it;
- its scheduled-job prompt waits in backlog;
- its next turn may still receive the thread as closed context, making the
  thread unfinished for that bot.

`unmute: @x` requests an immediate sweep for `x`; if the thread is unfinished
or has a waiting job, a turn starts as soon as the other gates allow.

Settings store explicit usernames, never the shorthand `all`:

- For `mute: all`, resolve all bots except the orchestrator.
- For `unmute: all`, resolve all bots currently muted.
- A named user resolves only if it is both on the agent list and in the room.

For a bot-authored control, the gateway resolves names before posting so every
gateway reads the same explicit list. An unknown or empty explicit list is
invalid; `all` resolving to nobody is valid and omitted. The difference is
intentional: an explicit empty list is wrong as written, while whether `all`
names anyone depends on room state that the bot does not see. For a
human-authored control, each gateway resolves names on receipt. Unknown names
are ignored and shown in status.

A bot joining after `mute: @all` is not muted. If the meeting requires it, the
orchestrator must mute the newcomer after its first post.
A bot whose gateway was offline when `mute: @all` was posted behaves the same
way: it may speak once after returning, then the orchestrator mutes it.

### 5.3 Rate limit and cooldown (`RATE_LIMIT`)

For each thread, the gateway keeps in memory:

- recent agent posts: timestamp, author, and word count;
- each bot's last post time.

A human post clears the recent-agent list. Agent posts older than
`rate_limit_seconds` expire. A gateway counts its own successful outbound post
immediately because connectors discard messages from their own account. At
watcher startup, replayed posts seed the window; posts older than the replay
window may be missed, making the limit briefly loose after restart. Starting a
turn reads this in-memory state and performs no history fetch; the temporary
looseness is accepted instead of adding a fetch to the hot path.
Gateways with the same agent list should usually compute the same counts, but
API lag and different replay windows can make them diverge temporarily.

A turn waits if any enabled condition holds:

- recent agent posts >= `rate_limit_posts`;
- recent agent words >= `rate_limit_words`;
- the bot's last post is newer than `cooldown_seconds`.

A word is a non-whitespace run, except each CJK character counts as one word.
This definition keeps counts consistent across gateways.
The word limit protects readers: a post count cannot distinguish a one-line
acknowledgement from a thousand-word analysis.

The check applies when a turn starts. A running turn may finish, so the post
limit can overshoot by at most the number of bots already running in the room,
plus one additional race per gateway when several gateways share the room.
Word overshoot is the text those turns produce. `HINT` marks each delivered
thread open or closed at turn start; this decision does not change while the
turn runs.

Post, word, and cooldown gates bind independently. Setting any one to `0`
disables only that gate. A human post clears the recent-agent count and
requests a sweep for the bots the limit held, as `unmute:` does; a limit that
lifts because posts aged out waits for the periodic sweep. With a very large
`rate_limit_seconds`, the limit behaves like a stop that only a human post
clears.

#### Exemptions

| Source | Rate/cooldown | Does its reply count? | Safety bound |
|---|---|---|---|
| Human mention | Exempt | Yes | A human explicitly intervened |
| Orchestrator mention or turn | Exempt | No, for orchestrator posts | Owner can mute or replace it; status reports its activity |
| Scheduled-job firing | Exempt | Yes | Retry cap and backoff |

Humans and non-agent integration accounts do not count. A bot selected by the
classifier for an unaddressed human post is not exempt.

Being rate-limited produces no chat notice. Status exposes the hold, and the
sweeper retries it. The mechanism slows a loop but does not end it; `done`, a
mute, or a human ends the exchange.

Thread overrides use `limit: N posts`, `limit: N words`, or both. `limit: 0
posts 0 words` disables the thread limit.

### 5.4 Authority and settings (`AUTHORITY`, `SETTINGS`)

The gateway applies a control only when written by:

- a human with the owner role; or
- the thread's orchestrator, except that only an owner may set
  `orchestrator:`.

Controls from guests or ordinary bots remain visible text and change nothing.
In a reply envelope, a `control` from a non-orchestrator is invalid. An
orchestrator may be replaced by a later owner post and has no special authority
outside its designated threads. Sender roles come from trusted connector
headers.

When the orchestrator writes a control word inside envelope `text` instead of
the `control` field, the gateway still posts that text. Every gateway then
applies the visible control because the sender is authorised. The structured
field is the required agent convention, not a second authority boundary.

Persist the latest value of each control kind with its source post, alongside
the room's cursors and unfinished state. Apply controls in server post order,
not arrival order. Keep a value even when it returns to its default, so
replaying an older control cannot restore stale state.

Settings expire after `control_ttl_days` without a thread post, or when the
thread is archived or deleted. TTL expiry restores default settings but does
not remove unfinished state: the thread becomes open and the sweeper can start
the owed turn. Archive or deletion removes the thread from every unfinished set
because it can no longer be answered.

Thread silence is measured from the newest post time already observed in
memory. Expiry does not fetch platform history.

Each gateway derives settings from the same platform posts. It does not receive
its own bot's post back, which is acceptable because that post is intended to
control bots on other gateways. Replacing the orchestrator changes which past
posts count toward the current rate limit; this simplification is accepted.

### 5.5 One turn per bot (`ONE_TURN`)

A bot runs one backend invocation at a time in a room. Eligible post turns are
ordered by the platform's server order; sweeps and job firings use a local event
sequence.

There is no persisted waiting-turn object. If the bot is busy or gated, the
gateway retains only existing facts:

- the trigger post remains after the cursor;
- a classifier choice remains in the short-lived decision cache;
- a delivered closed thread remains in the unfinished set.

When a turn ends or the bot becomes free, request a sweep. Becoming free does
not start a held turn directly. The later turn sees everything posted meanwhile
and decides whether an answer is still useful.

### 5.6 Cursor and delivery (`CURSOR`)

Persist one post id per bot per room: the last post delivered to that bot's
session.

- **Post-triggered turn:** deliver every post after the cursor and before the
  trigger, then the trigger exactly once.
- **Sweep or scheduled turn:** fix a high-water mark at turn start and deliver
  every post after the cursor through that mark. Later posts wait.
- **Reconnect or startup:** treat replayed posts like new post triggers.

Each delivered post carries its thread id. The bot may reply in any delivered
thread that `HINT` marks open, and in any open unfinished thread listed by
`HINT` whose posts were delivered by an earlier turn.

Save the cursor only when the turn ends normally: every envelope item was
`done`, posted, or refused by the platform. Do not save it after backend failure
or a second invalid output. This gives at-least-once input after fetch failure,
turn failure, or a crash before save, and may duplicate an already-posted
reply. That is within AgentCoop's best-effort delivery contract.

Ordinary turns have no retry cap. A failed turn is retried under the backoff in
`FAILURE_NOTICE`, so repeated delivery, and therefore a duplicate reply, is
possible until a turn ends normally.

Delivered backlog is context only. The gateway must not re-execute control
words, mentions, or any other eligibility effects found in backlog history.

Backlog size and age per thread are computed from platform history after the
cursor; they are not stored separately. History fetching must support a post-id
bound, thread ids, and pagination.

### 5.7 Sweeper and unfinished threads (`CATCH_UP`)

A sweep runs for one bot in one room: every `sweep_interval_seconds`, after one
of its turns ends, when it becomes free, and when a control or human post
requests one (`MUTE`, `RATE_LIMIT`). Sweeps for different bots or rooms may run
in parallel. It considers, in this order:

1. the oldest backlog post that currently addresses or selects the bot;
2. unfinished threads that have become open;
3. threads due for catch-up.

The sweep chooses its candidate list once, then rechecks each candidate before
starting a turn. Because the first turn delivers the entire room backlog, later
candidates usually no longer have undelivered posts and are skipped.

The empty-backlog check has two exceptions: a scheduled job, which has no
platform post of its own, and an unfinished thread that has become open, whose
posts are already in the bot's session. Either may start a turn without an
undelivered post.

Eligibility is evaluated against current thread settings at the time of the
sweep. For example, a bot mention that arrived before a later `mute:` does not
start a turn after the bot becomes muted. A cached classifier choice is usable
only while it has not expired and its source post remains in the backlog.

A thread is due for catch-up when the bot is not muted, the thread is open, the
bot is out of cooldown, and either:

- undelivered posts >= `catch_up_post_threshold`; or
- the oldest undelivered post is at least `catch_up_age_seconds` old.

Catch-up candidates are ordered by newest post. This is the bot's independent
chance to notice something the classifier missed; `done` should be common.

#### Unfinished threads

A room-level cursor means one turn can deliver posts from threads in which the
bot may not currently speak. On a normal turn end:

- add each delivered closed thread unless the envelope contains `done` for it;
- remove each thread for which the envelope contains text, control, or `done`;
- change nothing after a failed turn.

Every later `HINT` lists the unfinished threads that are now open as must-reply
threads, so whichever turn runs next clears all of them at once. If no other
turn comes, the sweeper starts a turn for them even with an empty backlog; the
relevant posts are already in the persistent agent session.

Cooldown gates the start of this turn. It does not close a thread to a turn
that is already running.

### 5.8 Turn hint (`HINT`)

Every turn receives:

- its turn source: `addressed` (a human, orchestrator, or bot mention),
  `picked` (the classifier), `catch-up`, `unfinished`, or `job`, so the bot
  knows why it was started;
- its starting thread and every other delivered thread, each in one of three
  groups computed by the gateway:
  - **must reply**: the starting thread, every open delivered thread in which
    a delivered post addressed the bot, and every open unfinished thread. An
    unfinished thread may have been delivered by an earlier turn; its posts
    are already in the bot's session and it is a valid reply target. Each
    must-reply thread needs one item: `text`, `control`, or `done`.
  - **optional reply**: every other open delivered thread. A reply here saves
    a later turn; silence is allowed.
  - **closed**: `text` and `control` are invalid there. Silence records the
    thread as unfinished for the bot; `done` says there is nothing to say and
    records nothing.
- remaining post and word allowance for each open thread;
- outbound replies refused by the platform on the previous turn (an API error,
  an archived thread, a permission the account lacks);
- whether the bot's previous turn in this room failed and how (backend failure
  or invalid envelope), and whether the bot is in backoff or degraded in this
  room (`FAILURE_NOTICE`);
- concise instructions to avoid restatement, use `done` when appropriate,
  place controls in `control`, treat `to` as recipients rather than incidental
  mentions, include everyone whose request the backlog reply serves, respond
  in other open threads when needed, use `all` only with a reason, schedule a
  follow-up when waiting, and address whoever a reply unblocks.

The starting thread is always open because its eligibility path admitted the
turn. Another delivered thread is open if the bot is not muted and the thread
is under its limits, or if a human or orchestrator addressed the bot there.
Rate limits never close a thread to its orchestrator.

### 5.9 Reply envelope (`REPLY_ENVELOPE`)

The logical schema is:

```yaml
replies:
  - thread: T1              # defaults to the starting thread
    to: [H, b]
    text: "502 burst at 09:12; the log is clean now. b, go ahead."

  - thread: T2
    control:
      mute: [a, b]          # or: all
      limit: {posts: 20, words: 2000}

  - thread: T1
    to: [a, b, c]
    control: {mute: all}
    text: "One post each. Address only me."

  - thread: T3
    to: [x]                 # optional for done; recorded if present
    done: true
```

The transport encoding (JSON block, whole-response JSON, or backend-native
structured output) is intentionally undecided. The contract is the fields.

Each reply contains exactly one of:

- `text` with required non-empty `to`;
- `control`;
- `text` plus `control`;
- `done: true`, with optional `to`.

`text` and `done` are mutually exclusive because they make contradictory
claims: one says there is something to post, while the other says there is not.

`thread` must be a thread delivered to this turn or an open unfinished thread
listed in `HINT`; it defaults to the starting thread. `to` names room members
or `all`, never the sender. The only bot-issued controls are `mute`, `unmute`,
and `limit`; only an owner human may designate or remove an orchestrator.

The exact output `<end-of-agent-chain>` maps to one `done` reply. If that token
has any surrounding text, the output is invalid.

#### Canonical platform post

Before posting, the gateway:

1. resolves controls and renders them as visible lines before text;
2. prepends missing recipient mentions;
3. removes `@` from mentions not present in `to`;
4. converts `all` to the platform's room-wide mention.

Thus platform mentions exactly match recipients. This matters because other
gateways see only the platform post, not the envelope.

A reply may post only in a thread `HINT` marked open. That decision is fixed at
turn start; a new mute or rate-limit event does not revoke it mid-turn.

#### Validation

On invalid output, return the schema error to the same session once. A second
invalid output fails the turn. Reject an envelope when:

- `replies` is absent, or a must-reply thread listed in `HINT` has no item;
- an item has no action, has both `text` and `done`, or has an unsupported
  combination;
- text lacks recipients, has an empty recipient list, or addresses only the
  sender;
- a recipient is not a room member;
- a name in an explicit mute/unmute list is not both on the agent list and a
  room member;
- a thread was neither delivered to this turn nor listed as an open unfinished
  thread, or is closed for text/control;
- a non-orchestrator issues `control`, or the control has an unsupported key;
- an explicit mute/unmute list is empty.

`all` resolving to nobody is not an error. Omit that control; if nothing else
remains, treat the item as `done`.

The gateway cannot verify that `to` includes everyone whose request the turn
served, because it stores no request-to-reply relation. The hint asks for this,
and validation measures omissions.

A reply to a mention is enforced through the must-reply group: a delivered post
that addressed the bot in an open thread requires an item there. A mention in a
closed thread is not enforced at that turn; the thread becomes unfinished and
is required once it opens.

#### Partial outbound failure

Attempt each reply once, in order. If the platform refuses one reply (an API
error, an archived thread, a permission the account lacks), keep
already-posted replies, report the refusal in the next hint, end the turn
normally, and save the cursor. Do not retry automatically because a retry may
duplicate a post.

### 5.10 Turn failure (`FAILURE_NOTICE`)

A turn fails in one of two ways: the backend fails (process error, timeout,
expired credentials, usage limit), or the second output is still invalid. The
two are tracked apart, because an invalid envelope is the agent misusing the
contract while a backend failure is the environment. On either failure the
gateway:

- leaves the cursor and the unfinished set unchanged, so the next turn
  receives the same posts;
- returns a failed scheduled occurrence to the scheduler (`FOLLOW_UP`);
- tells the bot on its next turn what failed (`HINT`).

#### Backend failures: backoff

For each bot in each room the gateway keeps, in memory, a `retry_after` time.
The first failure sets it `failure_backoff_seconds` ahead; each consecutive
failure doubles the wait, up to `failure_backoff_max_seconds`. While that time
is in the future the bot is treated as under cooldown: no sweep, classifier, or
bot-mention turn starts, and a scheduled job that falls due waits for the
backoff to end without counting as a job retry. A human or orchestrator mention
still starts a turn. One normal turn end clears the backoff. The gateway posts
one failure notice at the first failure of an episode and none during the
backoff. `STATUS` shows the backoff and the next retry time.

#### Invalid envelopes: degraded bot

For each bot in each room the gateway counts, in memory, consecutive turns that
failed on a second invalid envelope. Backend failures neither add to nor reset
the count. At `consecutive_invalid_limit` the bot is degraded in that room: it
is eligible as in the muted column of `TURN_ELIGIBILITY` (the sweeper skips it,
the classifier is not offered it, a bot mention starts nothing, its jobs wait
in backlog), while a human or orchestrator mention still starts a turn, which
is the probe. A normal turn end clears the degraded state and the count. The
gateway posts one failure notice at the first twice-invalid turn of a streak,
one more when the bot becomes degraded, and none between. `STATUS` shows
degraded bots. A restart resets both the count and the state. With
`consecutive_invalid_limit` at `0` the gateway still counts and shows the
streaks but degrades nobody, which turns the mechanism into measurement only;
`failure_backoff_seconds` at `0` does the same for backoff.

Both mechanisms are provisional. Section 8 counts backoff episodes and degraded
trips; if the simulator and the lab show that consecutive failures are rare,
they are removed.

#### The failure notice

The notice is posted from the failing bot's account. It carries a
gateway-generated event kind in platform metadata where the platform allows
it, and connectors recognise that kind before `TURN_ELIGIBILITY`. Text alone is
unsafe because any user could imitate it. The exact carrier (a Mattermost
`props` field, a Rocket.Chat custom field) is a lab question; today's
connectors read no post metadata. The metadata is set by the poster, not
stamped by the server, so a foreign bot tagging its own posts can only silence
itself, which is accepted.

A recognised notice:

- starts no turn by addressing and is not sent to the classifier;
- counts as an undelivered post for catch-up (`catch_up_post_threshold`,
  `catch_up_age_seconds`), so every other bot gets at most one catch-up look at
  it, and a bot watching a peer can act on it there;
- does not count as a recent agent post for the rate limit;
- is not seen by the failing bot itself, because a connector drops its own
  account's posts; that bot learns its state from `HINT`.

### 5.11 Scheduled follow-up (`FOLLOW_UP`)

Follow-up is agent policy built on the existing scheduler. A bot waiting for
work records what it expects, from whom, and when to check. On firing, it
reports progress or asks for status, optionally schedules a later check, and
eventually reports that the expected time passed.

The existing scheduler's contract changes as follows (increment 5):

- Persist connector, room, thread, task text, and retry state.
- Fire in the original thread.
- Clear an occurrence only after its turn ends normally, not when queued.
- Retry failed turns under the backoff in `FAILURE_NOTICE`; a firing due during
  backoff waits and is not a retry.
- After `follow_up_retry_cap`, post a notice and stop.

Saving the cursor and clearing the occurrence are separate writes, so a crash
may fire an occurrence more than once. Skills must tolerate duplicate firings.

If the bot is muted, keep at most one pending firing per job in its thread
backlog; a newer firing replaces an older one. Unmute requests a sweep and the
bot decides whether the delayed task is still useful. Job types with different
time-sensitivity are a planned follow-on (section 10).

The firing itself is not a platform post and counts nothing toward the rate
limit. A visible reply produced by its turn counts normally.

### 5.12 Orchestrator (`ORCHESTRATOR`)

An owner post `orchestrator: @bot` designates one bot for one thread. The bot:

- is exempt from rate and cooldown gates;
- can address other bots without those gates;
- may issue `mute`, `unmute`, and `limit` controls.

`orchestrator: none` from an owner removes the designation and restores the
thread's default behaviour. Muting the orchestrator does not remove it: a muted
orchestrator keeps its role and wakes only on a human mention (`MUTE`).

It receives no special delivery. Bots report by addressing it, and each report
can start an orchestrator turn. Its skill decides when to return `done`, gather
more input, or synthesise.

A structured meeting is simply `mute: @all` followed by orchestrator mentions.
The gateway does not know that a meeting or round exists.

### 5.13 Status (`STATUS`)

`coop room status <room>` should show:

- per thread: orchestrator, muted bots, setting age, recent post/word counts,
  limits, and orchestrator activity;
- per bot: backlog size and age by thread, unfinished threads, and cooldown;
- per bot: backoff with its next retry time, degraded state, and the
  consecutive invalid count;
- recent classifier and fallback decisions with reasons, and decisions that
  expired while their turn was held;
- the turn source and exemption of each recent turn;
- the configured agent list and bot-like room members missing from it.

### 5.14 Agent list (`AGENT_LIST`)

`agent_usernames` must include every agent in the room, even agents hosted by
another gateway or by no gateway at all (section 10). The classifier also needs display name, nickname, and the
profile described in section 9.

Today, the connector rejects a sender that belongs to none of its user lists.
Therefore, a missing agent is rejected rather than misclassified as human.
Where supported, startup validates the list against room membership and reports
missing or unknown agents loudly.

Gateways with different lists will disagree about classification and rate
counts. Status must make this visible; synchronising lists is later work.

---

## 6. Worked examples

### 6.1 Handoff

Human `H` writes:

> `@a run the migration; @b, when a is done, deploy.`

Both bots are addressed and start in parallel. `b` sees that it must wait,
returns `done`, and schedules a 30-minute follow-up. `a` later replies to
`[H, b]` that the migration is complete. That mention starts `b`, which deploys
and replies to `[H, a]`. `a` returns `done`. A third bot eventually receives
the exchange through catch-up and also returns `done`.

If `a` had not mentioned `b`, the classifier could infer the dependency from
the thread. If `a` had said nothing, `b`'s scheduled follow-up would ask for
status.

### 6.2 Orchestrated brainstorm

An owner designates `@chair`, then asks it to run a brainstorm. The chair posts
`mute: @a @b @c @d` and addresses all four with "one post each; address only
me." All four turns start in parallel and are exempt because the orchestrator
addressed them. Their mentions of peers do not start peer turns because those
peers are muted. A bot that starts later may see an earlier bot's reply in its
backlog; these parallel turns are not isolated.

Each response addresses the chair. Its skill returns `done` while collecting
responses, synthesises when ready, asks for objections, and may omit a noisy
bot from the next round. Finally it posts `unmute: @a @b @c @d` with the
summary. The skill remembers rounds; the gateway does not.

### 6.3 One turn, two threads

In thread A, `b` corrects a figure from `x`, but the classifier selects nobody.
Later, a human addresses `x` in thread B. `x`'s turn receives both the
correction and the new question. If A and B are open, one envelope can answer
the human in B and acknowledge `b` in A.

If A is muted or rate-limited, the hint marks it closed. `x` answers only B and
A becomes unfinished. When A opens, a later turn may clear it; otherwise the
sweeper starts a dedicated turn.

### 6.4 Two gateways in one room

Gateway 1 hosts `a`; gateway 2 hosts `b`; both agent lists contain both bots.
For an unaddressed question, each gateway classifies independently. Both may
select their own bot, producing two answers, or each may select the bot on the
other gateway, producing none. This limitation is accepted in this version.

Direct addressing still works across gateways. Both gateways observe both bot
posts and compute approximately the same rate state. A human `mute: @all`
resolves to the same explicit bot set when their agent lists agree.

---

## 7. Supported scenarios

| Scenario | Mechanism | Support and limits |
|---|---|---|
| Several scenarios in one room | Use `orchestrator:` and `mute: @all` only during structured meetings | Supported: casual talk, meeting, then casual talk again. |
| Several scenarios in one thread | Change controls on that thread | Supported: a debugging thread can become a vote and return; there is no gateway-owned activity that must be "finished". |
| Casual conversation | Classifier + catch-up | Supported; catch-up should initially be tuned more eagerly. |
| Brainstorm | Orchestrator skill + `mute: @all` | Supported; costs more orchestrator turns and keeps no gateway round record. |
| Status, decision, or roadmap meeting | Orchestrator addresses reporters or voters | Supported; the orchestrator mutes a bot that will not stop. |
| Debugging | Classifier + larger thread limit | Supported; likely experts speak first and others catch up. |
| Divide and collaborate | Mentions + follow-up skill | Supported without gateway deadlines or a completion report; the room is the report. |
| Support or triage | Answering bot mentions a specialist | Supported. |
| Scheduled or agent-only room | Rate, cooldown, `done`, mute | Supported, but needs self-termination, a cap, or a mute to end a loop. |
| Code review | Reviewer mentions the test bot | Supported. |
| Critic or pair | Profile requests every post from its partner | Supported, dependent on profile quality. |
| Unattended work | Mentions + follow-up | Supported best effort; guarantees belong to the skill. |
| Role-play | Game-master skill + `mute: @all` | Supported except for hidden information. |
| CI or webhook room | Guest integration account | Supported; integration posts do not count toward the agent rate limit. |

Single-bot rooms and 1:1 DMs remain effectively unchanged because the bot is
addressed by definition or by `require_mention`.

---

## 8. Validation strategy

### 8.1 Build a simulator first

Before gateway changes, implement section 5 as a deterministic simulator in
`tools/roomsim/`. It must encode the eligibility table, rate limit, cursor,
sweeper, settings, and envelope directly, not ask a model to trace them.
If the script and this specification disagree, one of them is wrong; resolve
the disagreement instead of treating either output as authoritative by default.

Each scenario should run with real backend turns using the test-room personas,
one persistent session per bot per room. A bot turn is one call through the
gateway's `AgentBackend` interface (`gateway/agents/`), permission broker off;
the simulator imports the backend adapters as a library and reuses nothing else
from the gateway, so the mechanism layer is written from section 5 alone.
Recorded replays run through a fake backend implementing the same interface.
Live runs start with Claude only, to get the simulator and the flow stable;
runs with other models and agents follow once the ACP backend lands, because
the current OpenCode adapter targets opencode 1.x. Conclusions about how well
models follow these rules are provisional until a second model has run.
Scripted human events enter at fixed steps. Output should include an annotated transcript and per-bot totals for
turns, posts, words, tokens, context size, classifier decisions, and sweeps.
People review each transcript for exchanges that read incorrectly, revise the
relevant rule in section 5, and rerun the scenario. This feedback loop is the
purpose of building the simulator first.

Use two kinds of run:

- **Live runs** evaluate whether conversations read well; they need not repeat.
- **Recorded replays** fix inbound events and bot outputs so mechanism traces
  are deterministic. Gateway and simulator must then produce the same trace.

The simulator cannot validate platform-specific mentions, thread APIs, history
pagination, failure-notice metadata, or session compaction. Validate those in
the test-server lab.

Run every scenario with:

1. classifier enabled, including injected failures and retries, recording raw
   failure rate, retry recovery rate, and retries required;
2. each fallback with no classifier;
3. classifier plus each fallback under injected failures.

Measure classifier quality against blind human labels of the exact classifier
input. Keep that separate from operational outcomes, including:

- selected-to-`done` and unselected-to-later-reply rates;
- classifier `remove` suggestions and what addressed bots actually did;
- duplicates, corrections, missed recipients, and closed-thread attempts;
- unfinished-thread duration and silent handoff stalls;
- follow-ups that recovered stalls, and jobs delayed by mutes;
- orchestrator turns, rate holds, exemptions, and fallback decisions;
- classifier decisions that expired while held, backoff episodes, and degraded
  trips per bot;
- must-reply items missing from envelopes, by turn source;
- posts, latency, classifier calls, and tokens per human prompt.

### 8.2 Implementation sequence

| # | Increment | Main mechanisms |
|---|---|---|
| 0 | Simulator and scenarios | All section 5 mechanics, outside the gateway runtime; backend adapters imported |
| 1 | Deliver, do not drop | Cursor, multi-thread envelope, canonical form, failure notice and failure counters, settings persistence |
| 2 | Bot-sender eligibility and mute | Eligibility table, mute, authority |
| 3 | Rate and cooldown | Rate state, exemptions, orchestrator, agent-list checks, hints |
| 4 | Sweeper, serialization, status | Catch-up, unfinished threads, one turn, backoff and degraded-state enforcement, status |
| 5 | Scheduler contract and follow-up | Thread-aware jobs, normal-end clearing, retry/backoff, skill, connector context documents aligned (Appendix C) |
| 6 | Classifier in shadow, then active | `WHO_SPEAKS` |
| 7 | Collaboration skills | Brainstorm orchestrator and project-management follow-up |

Dependencies: 0 first; 1 before all gateway work; 2 and 3 can proceed
independently; 4 and 5 require 1; 6 requires 2–4; 7 requires 5 and 6.

### 8.3 Required mechanism tests

The simulator gets these tests first; the gateway reuses them.

**Delivery and envelope**

- Save the cursor after normal completion, including partial outbound refusal;
  do not save it after failed or twice-invalid turns.
- Verify that a failed turn is delivered again after its backoff with no retry
  cap, including the resulting duplicate-reply risk.
- A backend failure sets backoff and a second doubles it up to the cap; a human
  mention still starts a turn; one normal end clears it; one notice per episode;
  a job due during backoff waits and is not a retry.
- `consecutive_invalid_limit` twice-invalid turns degrade the bot: muted-column
  eligibility, a human or orchestrator mention probes it, a normal end clears
  it; backend failures do not change the count.
- With `consecutive_invalid_limit: 0`, the streak is counted and shown but no
  bot is degraded.
- A failure notice starts no turn, produces no classifier call, counts toward
  catch-up but not the rate limit, and is not delivered to its own bot.
- Exercise failures at every save/post boundary.
- Post one turn's replies into two threads and count each in its own thread.
- Enforce open/closed threads and canonical `to` mentions.
- Validate every invalid combination described in section 5.9.
- An envelope missing an item for any must-reply thread is invalid once and
  fails the turn twice; optional and closed threads need none.
- Accept `control` without `to`; record `done.to` without posting anything.
- Render `control` plus `text` as one post with control lines first.
- Persist settings, expire them, and reject replayed older controls.
- TTL expiry restores defaults but preserves unfinished state, allowing the
  newly open thread to run; archive/delete removes both.
- Resolve bot-authored `all` before post and human-authored `@all` on read.
- A bot added to the agent list after `mute: all` is not retroactively muted.
- A bot whose gateway missed `mute: all` while offline may speak once and is
  not silently treated as having received the control.
- Omit `mute: all` when it resolves to nobody; if nothing else remains, post
  nothing.
- Ignore a human mute target that is not a bot in the room and show it in
  status.
- Apply an orchestrator control written in envelope `text` after it becomes a
  visible post.
- Count a gateway's own successful post.

**Eligibility, mute, and rate**

- With `addressed-only`, an unaddressed bot post starts no turn.
- Under `mute: @all`, ordinary bot mentions start nothing; human and
  orchestrator mentions still start turns.
- Under `mute: @all`, a bot mention of the orchestrator starts the
  orchestrator's turn.
- A muted bot may answer the human who explicitly addressed it.
- After restart, enforce mutes reconstructed from persisted settings.
- `orchestrator: none` from an owner clears the designation; a mute does not.
- Human and orchestrator mentions bypass rate and cooldown.
- A scheduled job starts in its own thread despite the rate limit.
- A reply in a thread marked open at turn start still posts when a mute arrives
  while the turn runs.
- Post, word, and cooldown limits bind independently; setting one to `0`
  disables only that limit.
- A thread below its post limit but above its word limit holds a turn.
- A human post clears the recent-agent count and requests a sweep that releases
  the held turn; a limit lifted by age waits for the periodic sweep.
- Equal agent lists converge on counts across gateways; status exposes unequal
  lists.

**Sweep and unfinished state**

- A closed delivered thread becomes unfinished and starts when it opens.
- `done` clears a thread; a failed turn does not change unfinished state.
- An open unfinished thread appears in another turn's hint and can be cleared
  there.
- Every open unfinished thread is a must-reply thread of the next turn, and one
  turn clears all of them.
- Archive/delete removes unfinished state; restart preserves it.
- Unmute requests an immediate sweep.
- A mention that arrived before a later mute starts nothing while the bot is
  muted.
- A bot becoming free starts nothing until the requested sweep runs.
- A sweep fixes its candidate-thread list once, before starting any turn.
- Held turns are reconstructed in event order with all meanwhile posts.
- A rate-limited bot-to-bot mention starts on a later sweep once eligible, but
  starts nothing if another turn delivered its post first.
- A still-valid cached classifier choice starts its held bot on a later sweep;
  after expiry, only ordinary catch-up can start it, and the expiry is counted
  in status.
- `HINT` states the turn source and the three thread groups that match the
  eligibility path that started the turn.
- One room-wide delivery usually satisfies several qualifying threads.
- A sweep's high-water mark excludes later posts.

**Scheduler and classifier**

- Jobs fire in their thread, retry failed turns, stop at the cap, and coalesce
  repeated firings while muted.
- A job firing itself does not count as a post; its visible reply does.
- A muted job waits and runs on the requested unmute sweep both when the mute
  predates its firing and when the mute arrives during a retry.
- DMs, control-only posts, and threads with every bot muted produce no
  classifier call.
- Three local copies of one post produce one classifier call and one decision.
- Addressed bots still speak when listed in `remove`.
- A decision naming a bot hosted by another gateway starts no local turn.
- Late classifier results do not revive already-delivered posts, even when
  newer unrelated posts remain in backlog.
- Truncated classifier history reports the omitted count.
- Display name and nickname let the classifier select a bot named without `@`.
- Timeout, retries, fallback, and shadow-label agreement are observable.

Finally, reproduce the handoff and brainstorm in section 6 end to end.

---

## 9. Open decisions

Decide these from simulator and lab measurements:

- Default post, word, time-window, cooldown, and settings-TTL values.
- A reader manages roughly 200–250 English words per minute, suggesting a word
  limit in the hundreds for a five-minute window; lab transcripts decide the
  actual value.
- Which limit catches actual bad conversations and at what threshold.
- Classifier model, history size, timeout, retry count, and fallback.
- Whether evidence from `remove` supports ever overriding an explicit mention.
- A connector abstraction for total room event order and fixed history
  high-water marks.
- Whether orchestrator collection turns should bypass classification so one
  turn can collect a full round.
- Catch-up frequency and thresholds.
- Envelope encoding: a fenced JSON block, the entire response as JSON, or
  backend-native structured output where available. Claude supports
  `--json-schema`; the OpenCode HTTP backend does not. Choose the encoding in
  task 1 when implementing the parser.
- Where bot profiles live. Options are per-gateway configuration, which must be
  synchronised manually, or shared platform profile fields. A third candidate
  is to ask each agent once for a self-introduction and use the answer as its
  profile; the agent list itself stays in configuration. The design requires
  a profile but does not choose its source. Decide before task 6.
- Whether `classifier_decision_ttl_seconds` stays. If expired decisions are
  common in the simulator, replace the TTL by "the decision expires when its
  post leaves the backlog" and delete the entry when the cursor passes the post.

---

## 10. Out of scope

- **Plans, deadlines, and reports.** Reconsider only if follow-up fails to
  recover too many unattended handoffs.
- **Agent-list synchronisation.** Lists are configured manually and checked at
  startup. Reconsider when two gateways share a production room.
- **Skipping classification for addressed posts.** Optimise only after data
  shows the classifier never adds value there.
- **Per-bot classification.** Reconsider if one room-level view cannot serve
  distinct bot profiles.
- **Turn ordering within one gateway.** No stagger or collision check. Based on
  Appendix A turn lengths, a 15-second stagger would let a later bot see an
  earlier visible reply about two-thirds of the time; a 5-second stagger would
  mostly reveal only that the first bot had nothing to say. Reconsider if the
  simulator's duplicate rate justifies the latency. True sequencing belongs to
  an orchestrator.
- **Cross-gateway coordination.** Independent classifiers may produce duplicate
  answers or no answer. Direct human mentions remain reliable. Reconsider only
  when a protocol between gateways is available.
- **Job types.** Mutes and backoff delay all jobs equally. Time-sensitive,
  okay-to-delay, and droppable job types are a planned follow-on once the
  follow-up skill has run in the lab.
- **Classifying failure notices.** A notice bypasses the classifier. Reconsider
  if the lab shows a peer should be chosen to handle a bot's failure.
- **Backoff and degraded state as permanent features.** Both are provisional
  (`FAILURE_NOTICE`) and are removed if consecutive failures prove rare.
- **Agents not connected through a coop gateway.** Such an agent must still be
  on the agent list, so its posts count toward the rate limit and go through
  the classifier like any agent post, and the canonical `@mention` the
  classifier produces is an ordinary platform mention that wakes it the way a
  human mention would. Nothing else reaches it: control words, turn
  eligibility, cooldown, `HINT`, envelope validation, the cursor, catch-up, and
  failure notices are gateway mechanics. The gateway can measure such an agent
  (posts per thread, replies in muted threads, in `STATUS`) but cannot enforce
  anything on it. Candidate ways to extend the rules to it later, none
  designed: a coop bot, on a human's instruction, addresses the agent with the
  room's collaboration rules and control words; a published skill or
  system-prompt snippet that the agent's operator injects before it joins the
  platform; an orchestrator skill that hands the rules to such an agent when it
  first appears in an orchestrated thread. Reconsider when such an agent keeps
  replying in a muted thread or after an orchestrator was named.
- **Anonymous votes and hidden information.** The room remains the visibility
  boundary.

---

## Appendix A: Evidence from current rooms

Measurements cover test room `#product-idea` over one afternoon (five bots,
three using Claude and two using OpenCode, with `max_turns: 5`) and production
room `#nest` over one week (three bots and one human, with `max_turns: 2`).

| Measure | Test room | Production room |
|---|---:|---:|
| Bot turns | 207 | 544 |
| Bot self-termination | 127 (61%) | 259 (48%) |
| Counter drop events | 74 events on 21 posts | 2 |
| Recoverable dropped posts with substance | 18, plus 3 unrecoverable | 2 of 2 |
| Bot posts per human debate prompt | 31 / 18 / 23 | Not measured |

Half the test-room drops came from a reset bug fixed in PR #195. The rest were
real overflows, mostly during convergence. Two false corrections traced to
context removed by the counter (issue #190). TTL expiry never reset either
deployment; a human post always did.

In one closing-round rehearsal, five bots received a six-point summary. Four
returned only the stop token, one objected, and that objection started two
mostly silent chains: 13 turns, 2 visible posts.

Across 615 production turns over eleven days, token-only turns had a 3-second
median; 82% finished within 5 seconds and 96% within 15. Visible-reply turns had
a 10-second median; 21% finished within 5 seconds and 66% within 15.

---

## Appendix B: Comparison with the plan-based alternative

The plan-based alternative (the earlier version of this document, in git
history before this one replaced it) used per-thread modes, a plan record per
request, callbacks, and shared room state.

| Area | Plan-based design | Classifier-and-gates design |
|---|---|---|
| Unaddressed speaker | Thread mode | Classifier once per post |
| Dependencies | Persisted plan and member states | Read from visible conversation; waiting bot uses follow-up |
| Rate control | Persisted count, pause notice, extend command | Rolling post/word count plus cooldown; silent hold |
| Loops | Pause at plan boundary | Slow until `done`, mute, or human intervention |
| Bad actor | Pause whole thread | Mute one bot |
| Completion report | Plan-end notices and callbacks | Room is the record |
| Reply scope | One thread per turn | Any delivered open thread |
| Persisted state | Cursor, rate, mode, orchestrator, plans, deduplicated events | Cursor and unfinished threads per bot, thread settings, jobs |
| Multiple gateways | Non-goal | Best effort; duplicates or omissions possible |
| Duplicate answers | Serial turns | Classifier should pick few; residual duplicates are noise |
| Classifier | Optional helper | Required speaker selector; never removes addressed bots |
| Rules a reader must hold | Plan lifecycle, three modes, mode switching, pause and its resets | One classifier question, mute, rate limit, cursor, unfinished threads, and settings |
| Main tradeoff | More guarantees, more machinery | Simpler mechanics; gives up deadlines, reports, orchestrator aggregation, and deterministic cross-gateway choice |

---

## Appendix C: Current code touchpoints

| Current behaviour | Reference |
|---|---|
| Agent-chain loop protection | `docs/agent-chain.md` |
| `require_mention` applies only to humans | `gateway/connectors/rocketchat/normalize.py:197,213`; `gateway/connectors/mattermost/normalize.py:222` |
| `to:` derives from all mentions | `gateway/connectors/rocketchat/connector.py:1841`; `gateway/connectors/mattermost/connector.py:828` |
| Intended-recipient mentions are a prompt rule | `gateway/contexts/rc-gateway-context.md:56` |
| One turn at a time per bot and room | `gateway/core/message_processor.py:358` |
| History is bounded by the triggering post | `Connector.trigger_history_bound`, `gateway/core/connector.py:651` |
| Room history fetch; Mattermost fetches one page | `Connector.fetch_room_history`, `gateway/core/connector.py:672`; `gateway/connectors/mattermost/rest.py:402-409` |
| Replay of posts missed while offline | `Connector.replay_room_since`, `gateway/core/connector.py:629` |
| Own-account posts are dropped on input | `gateway/connectors/rocketchat/normalize.py:156-158` |
| Unknown senders are rejected | `gateway/connectors/rocketchat/normalize.py:168` |
| Agent list | `agent_usernames`, `gateway/connectors/rocketchat/config.py:97` |
| Scheduler injects without a thread today | `gateway/core/scheduler.py`; `SessionManager.inject_message(room_id, text)`, `gateway/core/session_manager.py:1556` |
| A scheduled job must not answer with the termination token | `gateway/contexts/rc-gateway-context.md:44`; `gateway/contexts/mm-gateway-context.md:44`. Increment 5 replaces this: under `FOLLOW_UP`, `done` on a follow-up firing is valid when there is nothing to report yet |
| Shared backend timeout | `agents.timeout`, `gateway/core/config.py:132` |
| Outbound retry and chunking | `gateway/connectors/rocketchat/outbound.py:31`; `gateway/connectors/mattermost/connector.py:173` |

---

## Appendix D: Other systems

Ten projects were read at code level (commit or reading date in parentheses).
Three position agents as teammates, several personas in one place talking to
humans and to each other, which is this gateway's position; they differ on who
owns the agents and whether each agent has a face of its own on the platform.
The rest are built for something else and are listed for the one thing each
contributes.

**Buzz** (`block/buzz` `799cfd7`) is a team chat on a Nostr relay where an
agent is a channel member with its own keypair, bridged to an ACP harness.
It is the closest system to this one and differs on one rule: its mention
gate applies to agents and humans alike, at the relay, so an unmentioned
post never reaches the agent's process (`crates/buzz-acp/src/relay.rs:3366-3372`);
by default an agent also accepts only its owner and the owner's other
agents (`crates/buzz-acp/src/config.rs:93-105`). Agent-to-agent mention is
a designed path (a delegate must mention the delegator when its work is
done) and nothing in code bounds the exchange that follows: loop control
is prompt text ("silence is usually correct", "never publish a bare
acknowledgement … it re-triggers everyone you mention",
`crates/buzz-acp/src/base_prompt.md:71-75`), and a controversial exchange
between two agents runs until an owner cancels it. Context for an
unaddressed agent is the thread's history, fetched at dispatch when the
agent is next mentioned in that thread, with already-delivered events
removed (`crates/buzz-acp/src/pool.rs:4055-4073, 4136-4150`); a top-level
channel post fetches nothing. That fetch is this design's `CURSOR` built the
same way, a watermark and a query with no stored queue, for threads only.
Buzz has no coordinator, no plan, no rate limit and no mode; its failure
notice carries no mention tag and so cannot wake another agent
(`pool.rs:5540-5548`), which is `FAILURE_NOTICE` here too. No test runs two
agent identities in one channel.

**OpenBot** (`nightly-labs/openbot`, openbot.run, read 2026-11-09 at
`4402298`) is a local-first desktop app that runs persistent AI teammates:
Codex, Claude and Grok CLIs wrapped in durable sessions, each with its own
workspace and task queue, all owned by the one person running the app. Its
use case is recurrent, reviewable work with one owner per task: coding,
testing, releases. It has two collaboration models, and they should be kept
apart. In-app *channels* are a task tree: a lead agent routes a post that
names nobody, one agent owns each task, and members collaborate through the
tools `channel_assign`, `channel_transfer`, `channel_reply` and
`channel_result`. Loop control is structural rather than a limit on a
counter: only assignments and awaited results start turns, a request allows
two replies, a root allows eight assignments, a result is posted once, and
only an opening `@mention` addresses a member. Members answering the same
request do not see each other's replies until they have posted their own.
*Messaging connections* (Slack, Discord, Telegram, WhatsApp) present one bot
identity per platform: every post comes from OpenBot and names no agent, a
per-platform orchestrator agent routes each new conversation, and the thread
then belongs to the assigned agent; a human cannot address a particular agent
on the platform. For its own goal that is coherent: one host owns every
member, so a task tree can be enforced in code and the platform never needs
to carry agent identity. It is the task-tree, single-face exemplar. Its
author calls three agents on one ambiguous task an anti-pattern, which is
the case this design is built for: agents owned by different operators,
entering a room humans already use, each with its own account, where the
conversation itself is the result and nobody can pre-assign who reviews
whom. Every OpenBot mechanism assumes one host owns all members; with
several owners only rules that treat the platform as the sole shared medium
survive, which is why this design has them and OpenBot does not need them.

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
`/activation` and `/queue <mode>` (a per-room mode set at runtime, where this
design has no mode and lets the classifier and catch-up decide), a free
pending-history window for unmentioned posts, and a bot-pair rate guard that
drops silently. QM (`2c94475`) is one company bot shared by many humans; it
contributes a small-model `YES` / `NO` / `REACT` pre-check before an
unprompted turn (the shape of `WHO_SPEAKS`, asked per bot rather than once per
post) and a Postgres turn queue with idempotency keys. jinn (`3ae6465`) is an
org chart of "AI employees"; it contributes a lateral-send guard (10 per
sender per 10 min, 12 relay hops) and the rule that a human message resets
the hop chain (`RATE_LIMIT`'s human reset). AutoGen (`027ecf0`) is an
in-process group chat; its `SelectorGroupChat` is one model call per hop over
the whole transcript with a rule that short-circuits it (`WHO_SPEAKS` asks
over one post and a bounded history instead), and every participant buffers
every broadcast until selected (`CURSOR`'s effect). Google ADK, MetaGPT and
crewAI are task pipelines; parallel branches in all three see only the
fan-out point, an isolation the parallel turns in 6.2 do not promise either.

### Debate in the literature

On single-answer benchmarks, multi-agent debate among copies of one model
does not reliably beat self-consistency (Smit et al., ICML 2024,
arXiv:2311.17371; Zhang et al. 2025, arXiv:2502.08788) or majority voting
(Choi et al. 2025, arXiv:2508.17536), and costs more at an equal number of
responses (Huang et al., ICLR 2024, arXiv:2310.01798, Table 7). A persona
instructed to disagree flips correct answers (Smit et al.). Mixing different
models is what produces a gain, and the gain comes from questions one model
gets right and another wrong (Zhang et al., §4). A synthesising aggregator
over independent proposals beats selecting one proposal (Wang et al. 2024,
arXiv:2406.04692). None of these studied a chat room with a human in it. An
orchestrator skill (6.2) takes from them a default flow to validate in the
lab: a parallel independent first round, a synthesis, one bounded revision
round. The design takes a preference for members that differ in model,
information or tools over copies trading opinions.
