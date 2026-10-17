# Rooms with several agents — the classifier gate

A second design for the same problem as `multi-agent-enhancement.md`. It
keeps that document's problem statement, evidence (its Appendix A) and
reply envelope, and replaces its coordination machinery — modes, plans,
callbacks, a shared room state — with one question asked of a small
classifier once per post: *who in this room should speak now?* Everything
a gateway needs to answer the question is in the room's history, so the
gateway persists two small things per room — one delivery cursor per bot
and the current control words per thread — and shares nothing between
bots.

---

## 1. Problem

Unchanged from `multi-agent-enhancement.md` §1: agent senders bypass the
mention gate, so every agent post is dispatched to every bot; a per-pair
counter drops posts past a budget, and the drops fall on the convergence
phase of a debate and split each bot's view of the room. The two
observations this design builds on are from the same measurements: agents
converge on their own in half to two thirds of turns, and the damage comes
from dropped posts, not from the exchanges themselves.

---

## 2. Principles

1. **Who speaks is read from the room, the way a person reads it.** For a
   post that names nobody, a classifier reads the thread and the roster
   and names the bots that should speak now — none, one or several. There
   is no plan, no mode beyond a silence switch, and no coordinator in the
   ordinary case. Sequencing — "a does X, then b does Y" — is read out of
   the conversation when a reports, not held in a gateway record.
2. **A mention is a request to speak and is always honoured.** The
   classifier is not consulted for a bot the post names; the bot itself
   may answer `done`.
3. **Every bot eventually sees everything.** One delivery cursor per bot
   per room; a turn is given everything after the cursor, from every
   thread, and may reply in any thread it was given. Nothing is dropped
   by admission.
4. **The platform is the only shared medium.** Rate limiting is computed
   from the room's history; control words are posts in the room; the
   orchestrator is whoever a post designated. No two bots share gateway
   state, so bots run by different gateways coexist in one room.
5. **Mechanism is separate from policy.** The gateway provides the
   mechanisms in §5; meetings, follow-ups and deadlines are skills.

### Non-goals

- **Guarantees about work.** No plan timeouts, no per-member reports, no
  "who did what" notice. The room is the report; a bot that is waiting on
  another bot tracks that itself (`FOLLOW_UP`).
- **Hidden information.** The room is the unit of visibility.
- **Convergence by the gateway.** No judgement of whether an agent is
  right; a loop is slowed, not ended.
- **Fairness between bots.** A bot that talks a lot uses the thread's
  allowance; others wait, as in any room — until someone mutes it.
- **Scheduling by the gateway.** Bots the classifier names start in
  parallel; the gateway orders nothing. Duplicate answers are the
  classifier's to avoid by naming fewer bots, and are noise when it does
  not.

---

## 3. A short example

Room `#team`: bots `a`, `b`, `c`; human `H`; `window_posts` 6 per
`window_seconds` 300 per thread.

1. `H: a, can you check the nginx log? When it's done, b, restart the
   backend.` — `a` and `b` are named: a turn each. `b` reads "when it's
   done", answers `done` — "waiting for a" — and sets itself a 20-minute
   reminder (`FOLLOW_UP`). `c` is not named; the classifier reads the
   post and names nobody else. No turn; the post is past `c`'s cursor.
2. `a` replies `to: [H, b]`: `@H @b 502 burst at 09:12, log is clean now
   — b, go ahead.` — `b` is named: a turn; it receives `H`'s post and
   `a`'s. `b` restarts the backend and replies `to: [H, a]`.
3. `b`'s reply names `a`: a turn for `a`. The hint says a reply that only
   acknowledges is `done`, and `a` answers `done`. Two agent posts in the
   window.
4. The sweeper finds `c` with three undelivered posts: one catch-up turn
   for `c`, which reads them and answers `done`. Still two posts.
5. `H: @all, same as last week's incident?` — `@all` names everyone:
   three turns in parallel, each told the others were asked too. `a`
   answers; `b` starts a little later, sees `a`'s reply in its backlog
   and adds one line; `c` answers `done`. Four posts in the window.
6. `a` and `b` go back and forth twice more: six posts — the window is
   full. `b`'s next turn does not start; its post waits in the backlog.
   `H: enough, thanks` names nobody and is a human post: it does not
   count, and the classifier names nobody. Five minutes later the window
   has drained and the sweeper gives `b` a catch-up turn; `b` reads the
   room and answers `done`.

---

## 4. Terms

| Term | Meaning |
|---|---|
| **post** | One message on the platform. |
| **turn** | One run of a bot's backend, started by a post that names the bot, by the classifier, by the sweeper, or by a scheduled wake the bot set for itself (`FOLLOW_UP`). It is told which thread triggered it and may reply in any thread it was delivered. |
| **addressed** | A post names a bot. In a human post: a platform mention, `@all` or a room-wide mention, or the username in the text. In an agent post: the platform's mention set or the room-wide mention; the canonical form (`REPLY_ENVELOPE`) makes an agent post's mentions exactly its envelope's `to`. |
| **coordination thread** | The unit of the control words and the window. A post inside a platform thread belongs to that thread; every top-level post belongs to the room's own thread. |
| **classifier** | A small model asked once per unaddressed post which bots in the room should speak now (`WHO_SPEAKS`). It never writes text and never overrides a mention. |
| **window** | The thread's recent agent posts: those within `window_seconds`. The gate compares their count with `window_posts` (`THROTTLE`). |
| **orchestrator** | The bot a human has designated for a thread with `orchestrator: @bot`. Nothing about the window applies to it or to the bots it names, and its control words are honoured. A convenience for meetings, not part of the ordinary path. |
| **muted** | A bot named by `mute: @bot` on a thread: it gets a turn there only from a human's mention. |
| **backlog** | The posts of a room past a bot's cursor: delivered to the platform, not yet delivered to that bot's session. Grouped by thread where a thread's size or age matters. |
| **reply envelope** | The structured form of a turn's output: one or more replies, each `thread`, `to`, `text`; or `done` (`REPLY_ENVELOPE`). |
| **control word** | `mode:`, `limit:`, `orchestrator:`, `mute:`, `unmute:`. Recognised only from an authorised sender (`AUTHORITY`); otherwise text. |
| **control snapshot** | The current control words of a thread, persisted while any is away from its default (`CONTROL_STATE`). |
| **agent roster** | The usernames the gateway treats as agents, from configuration (`AGENT_ROSTER`). |

Parameters, per room with per-thread override: `window_posts`,
`window_seconds`, `cooldown_seconds`, `catch_up_post_threshold`,
`catch_up_age_seconds`; per room: `control_ttl_days`, `classifier_posts`,
`classifier_timeout_seconds`.

---

## 5. Mechanisms

### Order of handling

Every inbound post, for every bot the gateway runs in that room:

1. **Control words** — from an authorised sender, apply `mode:`, `limit:`,
   `orchestrator:`, `mute:`, `unmute:` to the thread. (`AUTHORITY`,
   `CONTROL_STATE`)
2. **Named?** — if the post names the bot, the bot speaks — unless it is
   muted and the sender is not a human. (`GATE`)
3. **Silenced?** — if not named and the thread is on `snooze`, or the bot
   is muted there, it does not speak; the post waits in its backlog.
   (`SILENCE`)
4. **Who speaks?** — in an open thread the classifier is asked once per
   post about the bots the post does not name; a bot it names speaks.
   Others wait. A post naming every bot asks nothing. (`WHO_SPEAKS`)
5. **Allowed now?** — a turn does not start while the thread's window is
   full or the bot's cooldown is running, unless the sender is a human
   or the orchestrator, or the turn is the orchestrator's or a scheduled
   wake. The post waits. (`THROTTLE`)
6. **One at a time** — one bot runs one turn at a time in a room; a
   classifier-chosen turn that waited is dropped if another agent has
   answered the post meanwhile. (`ONE_TURN`)
7. **Context** — the turn receives everything after the bot's cursor and
   before the trigger, labelled by thread, then the trigger. (`CURSOR`)
8. **Reply** — the envelope is parsed; each reply is posted in canonical
   form in its thread if that thread is open to the bot and has room;
   `done` posts nothing. (`REPLY_ENVELOPE`)

The sweeper (`CATCH_UP`) and scheduled wakes (`FOLLOW_UP`) start turns
apart from this, on time.

### GATE (G1)

- *Scope*: every post, every bot, human and agent senders alike.
- *Named*: a human post names a bot by platform mention, `@all` or
  room-wide mention, or the bare username; an agent post names a bot by
  its canonical mentions only. A named bot gets a turn, subject to
  `THROTTLE` when the sender is a bot other than the orchestrator, and to
  `mute:` when the sender is not a human. A 1:1 DM names its bot by
  definition.
- *Not named*: `SILENCE`, then `WHO_SPEAKS`, decide.
- *Agent senders* pass the same gate as humans; no bypass.

### WHO_SPEAKS (J1) — the room classifier

- *Question*: given the room's roster — each bot's username, persona,
  description and **attention policy** (free text: "answers every post
  from `a`", "only when asked", "anything about the build") — the
  thread's recent posts, and the new post: which bots should speak now?
  Answer: a set of usernames, possibly empty, with one line of reason
  each.
- *Scope*: one call per post in an open thread, per gateway, about the
  bots the post does not name; a post that names every bot, or a DM,
  asks nothing. Every bot's connector receives its own copy of the post;
  the gateway
  keeps an ephemeral set of post ids it has already asked about, so the
  question is asked once and the answer applied to each of its bots.
  This is the only coordination between bots, it is not persisted, and
  it concerns only the bots this gateway runs.
- *Input is bounded*: the last `classifier_posts` posts of the thread,
  newest first, within a token budget; the roster; and, per bot, the
  count of its undelivered posts in other threads — a number, not the
  posts. What does not fit is omitted with a count, and the classifier
  is told so.
- *Effect*: a named bot speaks, through `THROTTLE` and `ONE_TURN`, in
  parallel with the others named. A bot not named waits: the post stays
  in its backlog, is delivered at its next turn, and `CATCH_UP` gives the
  bot its own look later. The classifier is asked to name few: one bot
  that can answer, not everyone who could.
- *Failure*: the call has `classifier_timeout_seconds`; a timeout, an
  unavailable model or unparseable output is treated as *nobody speaks
  now*, recorded as a degraded decision with its reason, and shown in
  `STATUS`. Processing of the post never waits on the classifier beyond
  the timeout; `CATCH_UP` does not depend on it.
- *Limits*: the classifier judges relevance and timing from the text. It
  cannot know what a bot knows — that a figure is wrong, that the bot has
  the file — so it under-names bots for corrections and expertise. That
  is why `CATCH_UP` exists and is tuned higher here than a mode-based
  design needs.
- *Never*: writes text, starts a turn by itself, overrides a mention,
  orders the bots it names, or reads anything but the room.
- *Logged*: every decision, reason and degraded result, for `STATUS` and
  for the measurements in §8.

### SILENCE (G2)

- `mode: snooze` on a thread: only a mention opens a turn; the classifier
  is not consulted and the sweeper gives no catch-up turn there; posts
  are delivered as context by whichever turn comes next. `mode: open`
  (the default) restores the classifier.
- `mute: @x` on a thread: `x` gets a turn there only from a **human's**
  mention; the classifier does not name it, the sweeper skips it, other
  bots' mentions of it wait. `unmute: @x` lifts it. This is how an
  orchestrator or a human stops one bot that is flooding a thread without
  silencing the rest.
- Both are posts, so every gateway in the room reads them, and both live
  in the thread's control snapshot (`CONTROL_STATE`). This is all the
  silence a meeting, a role-play or a human who wants quiet needs.

### THROTTLE (G3)

- *Computed, not stored*: from the thread's history, the gateway counts
  agent posts within `window_seconds` and reads the time of the bot's
  own last post. Every gateway in the room computes from the same history
  with the same roster; the result converges, best effort — paging
  limits, API lag, edits and fetch failures can make two gateways differ
  for a while. `STATUS` shows the current count, the oldest post fetched
  and fetch failures.
- *Rule*: a turn does not start while the count is ≥ `window_posts`, or
  the bot posted within `cooldown_seconds`. The post stays in the
  backlog; the sweeper returns to it when the window has drained.
- *Bypass*: a turn started by a **human's** mention always starts — the
  human is the one who intervenes and cannot be shut out by noisy bots.
  The **orchestrator** is outside the window entirely: its turns start,
  its posts do not count, and the bots it names start as if a human had
  named them. A **scheduled wake** (`FOLLOW_UP`) starts regardless of
  window and cooldown; its post counts. Human-class posts (humans,
  allow-listed integration accounts) never count.
- *Bypass bounds*: each bypass has its own limit, because a faulty skill
  or chair could otherwise talk without end: a follow-up has a retry cap,
  an expiry and a minimum backoff (`FOLLOW_UP`); the orchestrator's posts
  per window are counted and shown to the owner in `STATUS`, and a human
  can `mute:` or re-designate it at any time; a human's own controls are
  never throttled. `STATUS` names which bypass started each recent turn.
- *No notice*: being throttled is silence, as it is for a person who
  cannot get a word in. `STATUS` shows it; `HINT` tells a bot that is
  allowed to speak how much room is left.
- *Loops*: two bots trading replies are slowed to `window_posts` per
  `window_seconds` and `cooldown_seconds` apart, not stopped. They stop
  when one answers `done`, when someone mutes one of them or snoozes the
  thread, or when a human tells them to. The measured self-termination
  rate (§1) is what this relies on.
- `limit: N` sets `window_posts` for the thread.
- *The one stall accepted*: a bot that names another bot in a `snooze`
  thread while the window is full leaves a post that nothing retries —
  the classifier and the sweeper are off there, and only a human's or
  the orchestrator's mention bypasses the window. That is the noise a
  meeting is snoozed to suppress; the chair or a human names the bot
  when it is wanted.

### AUTHORITY (G4)

- Control words are honoured from humans with the owner role and from the
  thread's orchestrator. `orchestrator:` is honoured from humans only.
  From anyone else they are text. A thread has one orchestrator;
  `orchestrator:` again replaces it. The same bot may be the orchestrator
  of several threads and is an ordinary bot elsewhere.
- Roles are server-injected (`CLAUDE.md`, connector prompt prefix).

### CONTROL_STATE (S1)

- A thread's current control words — mode, orchestrator, `limit:`, the
  muted bots — are the last control word of each kind posted in it. The
  gateway keeps them as a **control snapshot** per thread, persisted
  with the cursors, written when a control word arrives. Only threads
  with a value away from its default have a snapshot.
- *Expiry*: a snapshot is removed after `control_ttl_days` of silence in
  its thread (days, not seconds), or when the platform archives or
  deletes the thread; the defaults apply again. A thread snoozed and
  then abandoned reopens on its own; a mute lapses with it. `STATUS`
  lists the threads that hold a snapshot and their age.
- Two gateways in one room each keep their own snapshot from the same
  posts; they agree because the posts are the same, not because they
  talk to each other.

### ONE_TURN (D3)

- A bot runs one turn at a time in a room — one backend session, one
  invocation (`gateway/core/message_processor.py:358`). Turns waiting
  for one bot start in the order of the post id of the event that made
  each eligible, ties by thread id.
- *Stale turns*: a classifier-chosen turn that waited — for the bot, for
  the window — is dropped before it starts if another agent has posted
  in the thread since the post that chose it; the post stays in the
  backlog for `CATCH_UP`. A turn from a mention or a scheduled wake is
  never dropped.

### CURSOR (D1)

- *Scope*: one post id per bot per room — the last post delivered to the
  bot's session. Created at the first delivery, never removed.
- *Delivery*: a turn receives the room's posts after the cursor and
  before the trigger, both exclusive (today's `trigger_history_bound`,
  `gateway/core/connector.py:651`), each labelled with its thread, then
  the trigger once; a sweep or a wake fetches to the newest post. Once
  the backend has accepted the turn the cursor advances to the last post
  delivered. The turn may reply in any thread it was delivered
  (`REPLY_ENVELOPE`); `HINT` names the triggering thread and the others
  present.
- *Contract*: nothing is withheld by admission. A turn that completes
  normally is not delivered again; a fetch failure or a crash between
  acceptance and saving the cursor re-delivers, bounded, under the
  project's best-effort delivery (`docs/requirements.md`). Delivered
  backlog is context: control words and mentions in it are not
  re-executed.
- *Interface*: the connector history fetch (`Connector.fetch_room_history`,
  `gateway/core/connector.py:672`) bounded by post id, returning post and
  thread ids, paging past the first page (Mattermost fetches one today,
  `gateway/connectors/mattermost/rest.py:402-409`).
- *Offline posts*: found at reconnect and startup by the connector's
  per-room replay (`Connector.replay_room_since`,
  `gateway/core/connector.py:629`) and run through the gate like live
  posts; the gate decides whether a turn starts, never whether a post is
  delivered.
- *Per thread*: backlog size and age per thread are computed from the
  posts after the cursor, for `CATCH_UP` and `STATUS`; nothing per
  thread is stored for it.

### CATCH_UP (D2)

- *Scope*: a periodic pass per bot over its undelivered posts grouped by
  thread; the qualifying set is taken once per pass.
- *Qualifying*: a thread on `open`, where the bot is not muted, with ≥
  `catch_up_post_threshold` undelivered posts for the bot or an oldest
  undelivered post older than `catch_up_age_seconds`, and a window with
  room.
- *Turn*: one per qualifying thread, in newest-post order; the first
  delivers the whole backlog, later ones nothing new; the trigger names
  the thread; the bot reads and decides — `done` is the common answer.
  This is the bot's own look at what the classifier passed over.
- *What it covers*: posts not yet delivered. A post another thread's turn
  has already delivered is not revisited — that turn could reply in its
  thread (`REPLY_ENVELOPE`), and the bot's session remembers it. "Not
  now" therefore means: the bot sees the post at its next turn, whichever
  thread starts it, and may answer there and then.

### REPLY_ENVELOPE (R1)

- *Fields*: `replies` — a list, each with `thread` (one of the threads
  delivered to this turn; the triggering thread when omitted), `to`
  (required, non-empty: humans or bots in the room, or `all`; the sender
  excluded; not needed for a control-only `text`) and `text`. A single
  reply may be written as top-level `to` / `text`. `done` — nothing to
  post. One of `replies` / `done`.
- *Canonical form*: each reply is posted in its thread with recipients
  prepended, non-recipient `@name` demoted and `all` mapped, so the
  post's mentions are exactly its `to` and every gateway reads the same
  addressing (`multi-agent-enhancement.md` `REPLY_ENVELOPE`).
- *Posting*: a reply is posted when its thread is open to the bot — not
  `snooze` unless the bot was named there, not muted — and the thread's
  window has room (the orchestrator is outside the window here too); it
  counts in that thread. A reply that fails the
  check is **not posted and not held**: the next turn's `HINT` says which
  reply was not posted and why, and the bot decides whether to say it
  again. The triggering thread's reply always posts: the turn was
  admitted there.
- *Validation*: an envelope with no `replies` and no `done`, a reply
  without `to`, an empty `to`, a `to` naming only the sender, a name
  that is neither a room member nor a bot the gateway knows, or a
  `thread` the turn was not delivered, is returned to the session once
  with the schema error; a second invalid output fails the turn
  (`FAILURE_NOTICE`). Token-only output is `done`.
- *What is not enforced*: there is no record of "requests this turn
  serves", so the gateway cannot check that `to` covers everyone who
  asked. `HINT` asks for it; the simulator counts the omissions (§8).

### FAILURE_NOTICE (D4)

- A failed turn — backend failure, or an unparseable reply after one
  re-prompt — is announced from the failing bot's account, delivered as
  context, starting no turn. The cursor does not move, so the posts the
  turn was given are delivered again at the next turn. A failed turn
  from a scheduled wake also returns the wake to the scheduler
  (`FOLLOW_UP`).

### HINT (H1)

- Injected into every turn: the triggering thread and the other threads
  delivered; each thread's mode and whether the bot is muted there; how
  many posts each window has left; replies from the last turn that were
  not posted, and why; and: "make your point; do not restate; answer
  `done` if you have nothing to add; `to` is who the post is for, not
  who it mentions — include everyone who asked you something in this
  backlog; reply in another delivered thread when something there needs
  you; `all` only with a reason; if you are waiting on someone, say so
  and set a reminder; if your reply unblocks someone, address them; in a
  snoozed thread, post only when named or when a commitment has
  expired."

### FOLLOW_UP (S2) — skill on the scheduler

- *Skill*: a bot that takes on or waits for work records it — what, for
  whom, expected by when, judged from the task — and schedules its own
  wake for the follow-up time. At the wake it reports progress, or asks
  the other party for status, and schedules the next check with a longer
  gap; past the expiry it says so and stops. Estimates are the bot's own;
  a wrong one costs a check too early or too late. The party best placed
  to estimate a task's duration is the one doing or awaiting it, which is
  why there is no plan timeout.
- *Scheduler contract* (changes to the existing scheduler): a job
  persists the connector, room, **coordination thread**, the commitment
  text, its expiry, and its retry state; today a job carries a room and a
  message and is injected without a thread
  (`SessionManager.inject_message(room_id, text)`,
  `gateway/core/session_manager.py:1556`). A wake inherits the thread. An
  occurrence is discharged when its turn completes normally — not when
  the message is queued, which is today's rule; a failed turn keeps the
  occurrence, retries with bounded exponential backoff, keeps the
  original expiry, and past `follow_up_retry_cap` posts a notice and
  stops.
- *Under the gate*: a wake starts in any mode, regardless of window and
  cooldown; its post counts in its thread. In a snoozed thread `HINT`
  tells the bot to post only if the thread named it or the commitment
  has expired, and to reschedule otherwise; opening the thread releases
  the next check, not every missed one.

### ORCHESTRATOR (O1) — for meetings

- `orchestrator: @bot` on a thread: the bot is outside the window — its
  turns start, its posts do not count, the bots it names start as if a
  human had named them — and its control words are honoured, `mute:`
  among them. It gets no special delivery: members reply to it by
  addressing it, and each reply is a turn for it; its skill answers
  `done` until it has what it needs. A meeting is `mode: snooze` plus the
  orchestrator naming members, all at once or one at a time as the skill
  decides, muting a member that will not stop.

### STATUS (O2)

- `coop room status <handle>`: per thread — mode, orchestrator, muted
  bots, snapshot age, agent posts in the window / `window_posts`, the
  orchestrator's posts in the window; per bot — backlog size and age per
  thread, computed on request, cooldown remaining; the last classifier
  decisions with reasons and any degraded results; which bypass started
  each recent turn; roster entries, and bot-like room members not on the
  roster.

### AGENT_ROSTER (C1)

- The gateway knows which usernames are agents from configuration
  (`agent_usernames`, `gateway/connectors/rocketchat/config.py:97`),
  which must list every agent in the room, including those run by other
  gateways. A sender on no list at all is rejected by the connector
  today (`gateway/connectors/rocketchat/normalize.py:168`), so an agent
  missing from the roster is not "treated as a human" — it is not heard.
  At startup the gateway checks the roster against the room's members
  where the platform allows, logs a missing or unknown agent loudly, and
  shows both in `STATUS`. Two gateways whose rosters differ for one room
  do not converge on the window or the classifier, and the status says
  so. Synchronising rosters between gateways is later work (§10).

### What the gateway does not have

No plans, no member states, no callbacks, no `stop:`, no timeouts, no
persisted shared state between bots — only an ephemeral set of post ids
the classifier has been asked about, and per bot an in-memory note of
replies its last turn could not post, both lost on restart — no
persisted copy of the backlog,
no per-thread records beyond the control snapshot, no ordering of the
bots the classifier names, no agent bypass of the mention gate, no
knowledge of what a meeting is.

---

## 6. Worked examples

### 6.1 A handoff read from the room

`H: @a run the migration; b, when a is done, deploy.` Thread `T`,
`window_posts` 6.

| # | Event | `a` | `b` | `c` |
|---|---|---|---|---|
| 1 | `H`'s post | named → turn | named → turn: reads "when a is done", answers `done`, schedules a 30-min check | classifier names nobody else → wait |
| 2 | `a: @H @b migration done, 14 tables.` | — | named → turn: deploys, replies `to: [H, a]` | classifier: nobody → wait |
| 3 | `b: @H @a deployed, green.` | named → turn → `done` | — | classifier: nobody → wait |
| 4 | sweeper: `c` has 3 undelivered posts | — | — | catch-up turn → `done` |

Three agent posts, four `done` turns, one classifier call per post about
the bots it did not name. Had `a` posted "migration done" to `H` alone, the classifier would
have read "b, when a is done" in the thread and named `b`; had `a` said
nothing for 30 minutes, `b`'s reminder would have fired and `b` would
have asked `a` for status.

### 6.2 A brainstorm run by a chair

`H: orchestrator: @chair` then `H: @chair run a brainstorm: seat or
usage pricing?` on a thread.

| # | Event | Turns | Posts |
|---|---|---|---|
| 1 | `chair: mode: snooze` · `@a @b @c @d — one post each, independently; address only me.` | — | 1 (chair, not counted) |
| 2 | four members, each named by the orchestrator → four turns in parallel, none throttled; a member that starts later may see an earlier reply in its backlog — no isolation | 4 | 4 |
| 3 | each reply names `chair` → a chair turn per reply; the skill answers `done` three times and synthesises on the fourth | 4 (3 `done`) | 1 (chair) |
| 4 | `chair: @a @b @c @d — objections only, otherwise done.` | 4 | 1 (`d`) |
| 5 | `a` and `d` start trading replies about the point — each names the other, so the turns open under `snooze`, throttled: `chair: mute: @d` | 2 | 2 + 1 (chair) |
| 6 | `chair: @H <close>` · `mode: open` · `unmute: @d` | — | 1 |

Ten member turns, five chair turns after the chair's first post (three
`done`), seven counted posts. The plan-based design does the same meeting
with two callbacks after the chair's first post instead of five turns;
this one keeps no record of the round.

### 6.3 One turn, two threads

Thread `A` is an incident thread; thread `B` is unrelated. `b` posts in
`A`, naming nobody: "the 09:12 burst was 404s, not 502s" — a correction
of a figure `x` gave. The classifier names nobody; `x`'s cursor stays
before it. Later `H` posts in `B`: `@x is staging green?`

- `x`'s turn receives `b`'s correction, labelled `A`, and `H`'s question,
  labelled `B`. Its envelope carries two replies: in `B`, `to: [H]`,
  "green"; in `A`, `to: [b]`, "right — 404s, my mistake." Each posts in
  its thread and counts there.
- Had `A` been snoozed, or its window full, the `A` reply would not have
  posted; `x`'s next turn would be told so, and `x` would decide whether
  to say it when next named in `A`.

### 6.4 Two gateways, one room

`a` runs on gateway 1, `b` on gateway 2, both rosters list both. `H:
anyone know why the build is red?`

- Each gateway asks its classifier once. Both name `a` and `b` — or
  gateway 1 names `a` and gateway 2 names `b`. Both start; two replies
  arrive. Bounded noise: the classifier was asked to name one, and the
  two gateways cannot confer.
- `a: @b your change touched the build file — did you bump the version?`
  — `b` is named: a turn on gateway 2. `b` replies `to: [a, H]`. Gateway
  1's window counts both posts from the history; so does gateway 2's.
- `H: mode: snooze` — both gateways write the same snapshot; only
  mentions open turns in the thread from here.

---

## 7. Scenario validation

| Scenario | Control | How it runs | Supported |
|---|---|---|---|
| One room, three scenarios in a day | `open` → `mode: snooze` + `orchestrator:` → `mode: open` | Casual talk by classifier; a meeting under silence; casual talk again. | yes |
| One thread, three scenarios | same, on the thread | A debugging thread becomes a vote under a chair and returns; nothing is "running" to finish under the old rules. | yes |
| Casual conversation (production `#nest`) | `open` | Named bots speak; the classifier adds a bot when the post is for it; corrections arrive through catch-up or as a second reply from a turn in another thread. | yes — catch-up tuned higher |
| Brainstorm (lab, five bots) | chair skill; `snooze` | §6.2. | yes — more chair turns, no record |
| Status / decision / roadmap meeting | chair skill; `snooze` | The chair names one member at a time for reports, all at once for a vote by `done`; mutes a member that will not stop. | yes |
| Anonymous vote | — | The room is the unit of visibility. | **not in this version** |
| Debugging session | `open`, `limit: 20` | Most posts are for everyone; the classifier names the bot or two best placed; the rest see the exchange through catch-up and reply when they have something. | yes |
| Divide and collaborate | H or PM skill; `open` | `@a @b` both named; dependencies by addressing; each bot tracks its own task (`FOLLOW_UP`). No deadline and no report to `H`; `H` reads the room. | yes, without the report |
| 1:1 DM | — | Named by definition; window counts the bot's own posts. | unchanged |
| Single-bot channel | per `require_mention` | Mentioned → turn; a listen-all room is configured as attention policy "always". | unchanged, by configuration |
| Support / triage channel | `open` | The answering bot addresses a specialist. | yes |
| Scheduled or agent-only room | `open` or `snooze` | No human: the window and cooldown slow an exchange and nothing ends it but `done`, `mute:` or `snooze`. | yes — needs a cap or `snooze`, as any design |
| Code review room | `open` | Reviewer addresses the test bot. | yes |
| Critic or pairing | `open`; critic's attention policy "every post from `a`" | The classifier names the critic on every post from `a`. | yes — per-bot policy |
| Unattended overnight task | `open` | Handoffs by addressing; each bot's `FOLLOW_UP` checks on what it waits for. | yes — guarantees are the skill's |
| Role-play with a game master | GM skill; `snooze` | Players speak when named; the GM narrates after each. | yes, except hidden information |
| Monitor room (CI, webhooks) | `open` | The integration account is human-class: its mention starts a bot, other bots by classifier, nothing it posts counts. | yes |

---

## 8. Validation and sequence

### 8.1 What the simulator measures

The simulator of `multi-agent-enhancement.md` §8.1 runs both designs over
the same scenario files. For this one it records two kinds of thing,
kept apart:

**Decision quality** — people label the classifier's exact input
snapshots, blind: given this thread and roster at this moment, who should
have spoken? Agreement with those labels, per model and history size, is
the measure of the classifier; nothing observed later is.

**Operational outcomes** — per run:

- named by the classifier → `done` rate; not named → later catch-up with
  text rate; these are costs and delays, not labels;
- corrections: how long after the correcting post the corrected bot
  acknowledged it, and through which path (named, classifier, catch-up,
  second reply from another thread);
- duplicates: replies to one post from more than one bot, on one gateway
  and across two;
- omissions in coalesced turns: requests in a turn's backlog that its
  replies did not address;
- replies not posted by the envelope check, and whether they were said
  later;
- silent stalls: handoffs where the next bot never spoke, and whether a
  `FOLLOW_UP` reminder recovered them;
- chair turns per meeting round, against the two callbacks of the
  plan-based design;
- throttled turns, bypass sources, degraded classifier results;
- posts per human prompt, latency per post, classifier calls and tokens
  per post.

### 8.2 Sequence

| # | Task | Builds | Mechanism tests |
|---|---|---|---|
| 0 | **Simulator**, shared with the other design. | — | the §6 tables reproduced |
| 1 | **Deliver, do not drop.** Cursor per bot; history by post id; the envelope with several replies and the canonical form; the control snapshot's persistence. | CURSOR, REPLY_ENVELOPE, FAILURE_NOTICE, CONTROL_STATE | delivered once on the success path; crash re-delivers; a two-thread turn posts in both, each counted in its own; a reply to a closed thread is not posted and is reported next turn; mentions equal `to`; snapshot survives restart and lapses at `control_ttl_days` |
| 2 | **Gate for agents, silence, mute.** Agent senders through the gate; `mode:`, `mute:`, `unmute:`, `orchestrator:` as posts. | GATE, SILENCE, AUTHORITY | an agent post naming nobody starts no turn; a muted bot runs only on a human's mention; `snooze` read from the snapshot after restart |
| 3 | **Throttle.** Window and cooldown from history; human and orchestrator bypass; roster validation. | THROTTLE, ORCHESTRATOR, AGENT_ROSTER, HINT | counts match across two gateway processes on one room with equal rosters, and status says so when they differ; a human mention starts during a full window; a bot the orchestrator names starts during a full window |
| 4 | **Catch-up, one turn, status.** Threshold and age; one turn per thread; stale non-mention turns dropped. | CATCH_UP, ONE_TURN, STATUS | qualifying set once per pass; ordering by eligibility; a waited non-mention turn is dropped when another agent has answered (exercised with the classifier in task 6) |
| 5 | **Scheduler contract and follow-up.** Jobs carry the thread; discharge on completion; retry with backoff and cap; the `FOLLOW_UP` skill. | FOLLOW_UP | a wake runs in its thread; a failed wake is retried and not lost; the cap posts a notice; a wake starts during a full window |
| 6 | **Classifier in shadow**, then with effect. | WHO_SPEAKS | one call per post per gateway; bounded input; timeout → nobody, degraded result shown; shadow agreement with blind labels before effect |
| 7 | **Skills.** The brainstorm chair; a PM skill for fan-out with follow-ups. | — (policy) | §6.1 and §6.2 reproduced |

Dependencies: 0 first; 1 before everything; 2 and 3 independent; 4 needs
1; 5 needs 1; 6 needs 2, 3, 4; 7 needs 5 and 6.

---

## 9. Undecided

- **Window defaults** (`window_posts`, `window_seconds`,
  `cooldown_seconds`) and `control_ttl_days`: measured in the lab.
- **Classifier model, history size and timeout** (`classifier_posts`,
  `classifier_timeout_seconds`): shadow measurement decides.
- **Whether the orchestrator's reply-turns go through the classifier**
  so that a chair collects a round in one turn; costs the "mention is
  always honoured" rule for one bot.
- **Catch-up frequency** relative to the plan-based design, from the
  not-named → catch-up-with-text rate.
- **Envelope encoding**: as in the other document.

## 10. Out of scope for this version

- **Plans, timeouts and reports** — the plan-based design's `PLAN`,
  `CALLBACK`, `STOP`. Reopened if the simulator shows unattended handoffs
  stalling silently at a rate `FOLLOW_UP` does not recover.
- **Roster synchronisation between gateways** — rosters are configured
  by hand and checked at startup. Reopened when two gateways share rooms
  in production.
- **A per-bot classifier** — one call per bot with its own backlog as
  input. Reopened if the room-level classifier's labels show it cannot
  judge for bots with different attention policies.
- **Ordering within one gateway** — bots named by one post start in
  parallel, and two may answer the same question. Two mechanisms were
  considered and not built: a *stagger* (the next named bot starts when
  the previous turn ends or after `stagger_seconds`, whichever first) and
  a *collision check* (a turn whose thread received another agent's post
  while it ran is shown that post before its reply is posted, and may
  revise or answer `done`). Production turn lengths in a three-bot room
  (615 turns over eleven days): `done` turns median 3 s, 82% within 5 s,
  96% within 15 s; text turns median 10 s, 21% within 5 s, 66% within
  15 s — so a 15-second stagger would let a later bot see the earlier
  reply about two times in three, and a 5-second one would mostly only
  tell it the earlier bot had nothing to say. A conversation among people
  is not turn-taking either; it only looks so because people type at
  different speeds, and a busy bot gives the same effect for free.
  Reopened when the simulator's duplicate rate says the noise is worth a
  mechanism; true sequencing is an orchestrator's job.
- **Coordination across gateways** — two gateways in one room cannot
  confer, so for one post both may name a bot of their own (two
  replies), or each may name a bot the other runs (no reply at all).
  Both outcomes are accepted: coordination across gateways is best
  effort in this version, and a human's mention always reaches its bot.
  Reopened only with a protocol between gateways.
- **Anonymous votes, hidden information** — the room is the unit of
  visibility.

---

## Appendix — Against the plan-based design

| | `multi-agent-enhancement.md` | this document |
|---|---|---|
| Who decides an unaddressed turn | mode (`snooze` / `listen` / `catch-up`) per thread | the room classifier, once per post; `snooze` and `mute:` as the only switches |
| Sequencing and dependencies | `PLAN`: FIFO per thread, parallel / sequential, admission, two timeout classes, member states, early completion | read from the room when a reply arrives; `FOLLOW_UP` for waiting |
| Rate limit | count per thread since the last human post, persisted; pause with notice; `extend:` | sliding window and cooldown computed from history; no notice; humans and the orchestrator outside it |
| Loops | paused at a plan boundary until a human, `extend:` or TTL | slowed, not stopped; ended by `done`, `mute:`, `snooze` or a human |
| A bad actor | the thread pauses for everyone | `mute: @x` by the chair or a human |
| Who is told what happened | plan-end notices, callbacks, `stop:` reports | nobody; the room is the record |
| Replies | one thread per turn | any thread the turn was delivered |
| Persisted state | cursor, rate limit, mode, orchestrator, plan records in a shared room state keyed by (chat server, room id); posts deduplicated into room events | the cursor and a control snapshot per thread with a value, lapsing after `control_ttl_days` |
| Several gateways in one room | non-goal | works, with rosters configured and checked |
| Multiple bots answering one post | `listen` serial | the classifier names few; the rest is noise |
| Classifier | optional, for addressing by name and mode recommendation | required, once per unaddressed post |
| Dropped here | — | `UNADDRESSED_MODE` (three modes), `PLAN`, `CALLBACK`, `STOP`, `THREAD_STATE` (shared record, room events, plan records), `LISTEN_GATE`, the `extend:` / `parallel:` / `sequential:` / `timeout:` / `stop:` control words |
| Added here | — | `WHO_SPEAKS` once per post, `mute:` / `unmute:`, a window computed from history, replies to several threads, `FOLLOW_UP` with a scheduler contract, a configured and checked roster |
| Rules a reader must hold | plan lifecycle (admission, coalescing, early completion, timeout classes, restart), three modes with switching, pause and its three resets | one question, two switches, one window, one cursor |
| What it gives up | simplicity; cross-gateway rooms | deadlines and reports for unattended work; aggregation for a chair; determinism of the gate |
