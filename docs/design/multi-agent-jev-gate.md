# Rooms with several agents — the classifier gate

A second design for the same problem as `multi-agent-enhancement.md`. It
keeps that document's problem statement, evidence (its Appendix A) and
reply envelope, and replaces its coordination machinery — modes, plans,
callbacks, a shared room state — with one question asked of a small
classifier before every unaddressed turn: *should this bot speak now?*
Everything a gateway needs to answer the question is in the room's
history, so the gateway keeps one piece of state per bot and nothing is
shared.

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

1. **A bot decides whether to speak the way a person does: by reading the
   room.** For a post that does not name it, a per-bot classifier reads
   the recent history and answers *speak now* or *not now*. There is no
   plan, no mode beyond a silence switch, and no coordinator in the
   ordinary case. Sequencing — "a does X, then b does Y" — is read out of
   the conversation when a reports, not held in a gateway record.
2. **A mention is a request to speak and is always honoured.** The
   classifier is never consulted for a post that names the bot; the bot
   itself may answer `done`.
3. **Every bot eventually sees everything.** One delivery cursor per bot
   per room; a turn is given everything after the cursor. Nothing is
   dropped by admission.
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
  allowance; others wait, as in any room.

---

## 3. A short example

Room `#team`: bots `a`, `b`, `c`; human `H`; `window_posts` 6 per
`window_seconds` 300 per thread.

1. `H: a, can you check the nginx log? When it's done, b, restart the
   backend.` — `a` is named: a turn. `b` is named too, so `b` gets a turn
   as well; its hint says the thread also holds `a`'s pending work, and
   `b` answers `done` — "waiting for a" — and sets itself a 20-minute
   reminder (`FOLLOW_UP`). `c` is not named; its classifier reads the
   post: not for `c`. No turn; the post is past `c`'s cursor.
2. `a` replies `to: [H, b]`: `@H @b 502 burst at 09:12, log is clean now
   — b, go ahead.` — `b` is named: a turn; it receives `H`'s post and
   `a`'s. `b` restarts the backend and replies `to: [H, a]`. `c`'s
   classifier: not for `c`.
3. `b`'s reply names `a`: a turn for `a`. The hint says a reply that only
   acknowledges is `done`, and `a` answers `done`. Two agent posts in the
   window.
4. The sweeper finds `c` with three undelivered posts: one catch-up turn
   for `c`, which reads them and answers `done`. Still two posts.
5. `H: @all, same as last week's incident?` — `@all` names everyone:
   three turns, one at a time (`SERIAL`), each seeing the previous reply;
   `a` answers, `b` adds one line, `c` has nothing new and answers
   `done`. Four posts in the window.
6. `a` and `b` go back and forth twice more: six posts — the window is
   full. `b`'s next turn does not start; its post waits in the backlog.
   `H: enough, thanks` names nobody and is a human post: it does not
   count, and the classifiers for `a` and `b` read it as closing. Five
   minutes later the window has drained and the sweeper would give `b` a
   catch-up turn; `b` reads the room and answers `done`.

---

## 4. Terms

| Term | Meaning |
|---|---|
| **post** | One message on the platform. |
| **turn** | One run of a bot's backend in one coordination thread, started by a post that names the bot, by the classifier, or by the sweeper. It produces zero or one post, in that thread. |
| **addressed** | A post names a bot. In a human post: a platform mention, `@all` or a room-wide mention, or the username in the text. In an agent post: the platform's mention set or the room-wide mention; the canonical form (`REPLY_ENVELOPE`) makes an agent post's mentions exactly its envelope's `to`. |
| **coordination thread** | The unit of the silence switch, the window and the orchestrator. A post inside a platform thread belongs to that thread; every top-level post belongs to the room's own thread. A reply inherits the thread of its trigger. |
| **classifier** | A small model asked, per bot and per unaddressed post, whether that bot should speak now (`SPEAK_NOW`). It never writes text and never overrides a mention. |
| **window** | The thread's recent agent posts: those within `window_seconds`. The gate compares their count with `window_posts` (`THROTTLE`). |
| **orchestrator** | The bot a human has designated for a thread with `orchestrator: @bot`. Its posts are not counted by the window and its control words are honoured. It is a convenience for meetings, not part of the ordinary path. |
| **backlog** | The posts of a room past a bot's cursor: delivered to the platform, not yet delivered to that bot's session. Grouped by thread where a thread's size or age matters. |
| **reply envelope** | The structured form of a turn's output: `to`, `text`, or `done` (`REPLY_ENVELOPE`). |
| **control word** | `mode:`, `limit:`, `orchestrator:`. Recognised only from an authorised sender (`AUTHORITY`); otherwise text. |
| **agent roster** | The usernames the gateway treats as agents, from configuration (`AGENT_ROSTER`). |

Parameters, per room with per-thread override: `window_posts`,
`window_seconds`, `cooldown_seconds`, `settle_seconds`,
`catch_up_post_threshold`, `catch_up_age_seconds`.

---

## 5. Mechanisms

### Order of handling

Every inbound post, for every bot the gateway runs in that room:

1. **Control words** — from an authorised sender, apply `mode:`, `limit:`,
   `orchestrator:` to the thread. (`AUTHORITY`)
2. **Named?** — if the post names the bot, the bot speaks. (`GATE`)
3. **Silenced?** — if not named and the thread is on `snooze`, the bot does
   not speak; the post waits in its backlog. (`SILENCE`)
4. **Speak now?** — if not named and the thread is open, ask the
   classifier. *Not now*: the post waits in the backlog. (`SPEAK_NOW`)
5. **Allowed now?** — a turn that is not from a human's mention and not
   the orchestrator's does not start while the thread's window is full
   or the bot's cooldown is running; the post waits. (`THROTTLE`)
6. **One at a time** — several bots speaking on one post go one at a
   time, each re-asked after the previous reply; one bot runs one turn
   at a time in a room. (`SERIAL`, `ONE_TURN`)
7. **Context** — the turn receives everything after the bot's cursor and
   before the trigger, labelled by thread, then the trigger. (`CURSOR`)
8. **Reply** — the envelope is parsed; `text` is posted in canonical form
   in the turn's thread; `done` posts nothing. (`REPLY_ENVELOPE`)

The sweeper (`CATCH_UP`) runs apart from this, on time.

### GATE (G1)

- *Scope*: every post, every bot, human and agent senders alike.
- *Named*: a human post names a bot by platform mention, `@all` or
  room-wide mention, or the bare username; an agent post names a bot by
  its canonical mentions only. A named bot gets a turn — subject to
  `THROTTLE` unless the sender is a human. A 1:1 DM names its bot by
  definition.
- *Not named*: the thread's `SILENCE` switch, then `SPEAK_NOW`, decide.
- *Agent senders* pass the same gate as humans; no bypass.

### SPEAK_NOW (J1) — the classifier

- *Question*: given this bot's roster entry — persona, description, and
  its **attention policy** (free text: "answer every post from `a`",
  "only when asked", "anything about the build") — the thread's recent
  history, the bot's undelivered posts, and the new post: should this
  bot speak now? Answer: `speak` or `wait`, with one line of reason.
- *Scope*: one call per bot per unaddressed post in an open thread,
  calls for different bots in parallel. History is a bounded window of
  the thread (`classifier_posts`), the bot's backlog always included.
- *Effect*: `speak` starts a turn (through `THROTTLE` and `SERIAL`);
  `wait` leaves the post in the backlog. `wait` is "not now", not
  "never": the post is delivered at the bot's next turn, and `CATCH_UP`
  gives the bot its own look later.
- *Limits*: the classifier judges relevance and timing from the text. It
  cannot know what the bot knows — that a figure is wrong, that the bot
  has the file — so it under-triggers on corrections and expertise. That
  is why `CATCH_UP` exists and is tuned higher here than a mode-based
  design needs.
- *Never*: writes text, starts a turn by itself, overrides a mention, or
  reads anything but the room.
- *Logged*: every decision and reason, for `STATUS` and for the shadow
  measurement in §8.

### SILENCE (G2)

- `mode: snooze` on a thread: only a mention opens a turn; the classifier
  is not consulted and the sweeper gives no catch-up turn there; posts
  are delivered as context by whichever turn comes next. `mode: open`
  (the default) restores the classifier. The switch is a post, so every
  gateway in the room reads it.
- This is the hard silence a meeting, a role-play or a human who wants
  quiet needs. It is the only mode.

### THROTTLE (G3)

- *Computed, not stored*: from the thread's history, the gateway counts
  agent posts within `window_seconds` and reads the time of the bot's
  own last post. Every gateway in the room computes the same numbers.
- *Rule*: a turn does not start while the count is ≥ `window_posts`, or
  the bot posted within `cooldown_seconds`. The post stays in the
  backlog; the sweeper returns to it when the window has drained.
- *Bypass*: a turn started by a **human's** mention always starts — the
  human is the one who intervenes and cannot be shut out by noisy bots.
  The orchestrator's turns always start and its posts do not count.
  Human-class posts (humans, allow-listed integration accounts) never
  count.
- *No notice*: being throttled is silence, as it is for a person who
  cannot get a word in. `STATUS` shows it; `HINT` tells a bot that is
  allowed to speak how much room is left.
- *Loops*: two bots trading replies are slowed to `window_posts` per
  `window_seconds` and `cooldown_seconds` apart, not stopped. They stop
  when one answers `done`, when a human posts `mode: snooze`, or when a
  human tells them to. The measured self-termination rate (§1) is what
  this relies on.
- `limit: N` sets `window_posts` for the thread.

### AUTHORITY (G4)

- Control words are honoured from humans with the owner role and from the
  thread's orchestrator. `orchestrator:` is honoured from humans only.
  From anyone else they are text. A thread has one orchestrator;
  `orchestrator:` again replaces it. The same bot may be the orchestrator
  of several threads and is an ordinary bot elsewhere.
- Roles are server-injected (`CLAUDE.md`, connector prompt prefix).

### SERIAL (G5)

- When several bots are to speak on the same post because the classifier
  chose them or because of `@all`, they go one at a time in roster order;
  before each, the bot is asked again with the previous reply in its
  history (`@all` members too — the sender asked everyone, not for four
  copies). Bots a post names individually run in parallel: the sender
  chose them. This is the whole of the "four near-identical replies"
  defence.
- Across gateways there is no shared order. Before a classifier-chosen
  turn starts, if another agent posted in the thread within
  `settle_seconds`, the gateway waits that long and asks again. Two bots
  on two gateways can still both speak; that is bounded noise, not
  damage.

### ONE_TURN (D3)

- A bot runs one turn at a time in a room — one backend session, one
  invocation (`gateway/core/message_processor.py:358`). Turns waiting for
  one bot start in the order of the post id of the event that made each
  eligible, ties by thread id.

### CURSOR (D1)

- *Scope*: one post id per bot per room — the last post delivered to the
  bot's session. Created at the first delivery, never removed; the only
  per-bot state the gateway persists.
- *Delivery*: a turn receives the room's posts after the cursor and
  before the trigger, both exclusive (today's `trigger_history_bound`,
  `gateway/core/connector.py:651`), each labelled with its thread, then
  the trigger once; a sweep fetches to the newest post. Once the backend
  has accepted the turn the cursor advances to the last post delivered.
  The reply goes to the turn's thread only; `HINT` names it.
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
  thread is stored.

### CATCH_UP (D2)

- *Scope*: a periodic pass per bot over its undelivered posts grouped by
  thread; the qualifying set is taken once per pass.
- *Qualifying*: a thread on `open` with ≥ `catch_up_post_threshold`
  undelivered posts for the bot, or an oldest undelivered post older than
  `catch_up_age_seconds`, and a window with room.
- *Turn*: one per qualifying thread, in newest-post order; the first
  delivers the whole backlog, later ones nothing new; the trigger names
  the thread; the bot reads and decides — `done` is the common answer.
  This is the bot's own look at what the classifier passed over, and the
  reason a `wait` is never a drop.
- The sweeper also removes nothing and stores nothing; there is no state
  to collect.

### REPLY_ENVELOPE (R1)

- As in `multi-agent-enhancement.md`: `to` (required with `text`,
  non-empty, humans or bots in the room or `all`, sender excluded; not
  needed for a control-only `text`), `text`, `done`. The gateway posts
  the canonical form — recipients prepended, non-recipient `@name`
  demoted, `all` mapped — so the post's mentions are exactly `to` and
  every gateway reads the same addressing. Invalid output is returned to
  the session once with the schema error; a second invalid output fails
  the turn (`FAILURE_NOTICE`). Token-only output is `done`.
- *What is not enforced here*: with no plan record there is no list of
  "requests this turn serves", so the gateway cannot check that `to`
  covers everyone who asked. `HINT` asks for it; the simulator counts the
  omissions (§8).

### FAILURE_NOTICE (D4)

- A failed turn — backend failure, or an unparseable reply after one
  re-prompt — is announced from the failing bot's account, delivered as
  context, starting no turn. The cursor does not move, so the posts the
  turn was given are delivered again at the next turn.

### HINT (H1)

- Injected into every turn: the thread this turn replies in, its mode,
  how many posts the window has left, and: "make your point; do not
  restate; answer `done` if you have nothing to add; `to` is who the post
  is for, not who it mentions — include everyone who asked you something
  in this backlog; `all` only with a reason; if you are waiting on
  someone, say so and set a reminder (`FOLLOW_UP`); if your reply unblocks
  someone, address them."

### FOLLOW_UP (S1) — skill, not gateway

- A bot that takes on or waits for work records it — what, for whom,
  expected by when, judged from the task — and schedules its own wake
  (the existing scheduler, `from: scheduler`) for the follow-up time. At
  the wake it reports progress, or asks the other party for status, and
  schedules the next check with a longer gap; past the expiry it says so
  and stops. Estimates are the bot's own; a wrong one costs a check too
  early or too late.
- The gateway contributes only the scheduler. There is no plan timeout,
  because the party best placed to estimate a task's duration is the one
  doing or awaiting it.

### ORCHESTRATOR (O1) — for meetings

- `orchestrator: @bot` on a thread: the bot's posts do not count in the
  window, its turns start regardless of the window, and its control
  words are honoured. It gets no special delivery: members reply to it
  by addressing it, and each reply is a turn for it; its skill answers
  `done` until it has what it needs. A meeting is `mode: snooze` plus the
  orchestrator addressing members, in parallel or one at a time as the
  skill decides.

### STATUS (O2)

- `coop room status <handle>`: per thread — mode, orchestrator, agent
  posts in the window / `window_posts`; per bot — backlog size and age
  per thread, computed on request; the last classifier decisions with
  reasons; bots currently cooled down.

### AGENT_ROSTER (C1)

- The gateway knows which usernames are agents from configuration
  (`agent_usernames`, `gateway/connectors/rocketchat/config.py:97`),
  which must list every agent in the room, including those run by other
  gateways. An unlisted agent is treated as a human: its posts do not
  count and its mentions bypass the window. Synchronising rosters
  between gateways is later work (§10).

### What the gateway does not have

No plans, no member states, no callbacks, no `stop:`, no timeouts, no
shared room state and no deduplication of posts between bots, no
persisted copy of the backlog, no per-thread records, no modes other than
`snooze`/`open`, no agent bypass of the mention gate, no knowledge of
what a meeting is.

---

## 6. Worked examples

### 6.1 A handoff read from the room

`H: @a run the migration; b, when a is done, deploy.` Thread `T`,
`window_posts` 6.

| # | Event | `a` | `b` | `c` |
|---|---|---|---|---|
| 1 | `H`'s post | named → turn | named → turn: reads "when a is done", answers `done`, schedules a 30-min check | classifier: not for `c` → wait |
| 2 | `a: @H @b migration done, 14 tables.` | — | named → turn: deploys, replies `to: [H, a]` | classifier: wait |
| 3 | `b: @H @a deployed, green.` | named → turn → `done` | — | classifier: wait |
| 4 | sweeper: `c` has 3 undelivered posts | — | — | catch-up turn → `done` |

Three agent posts, four `done` turns, one classifier call per post for
`c`. Had `a` posted "migration done" to `H` alone, `b`'s classifier would
have read "b, when a is done" in `b`'s backlog and answered `speak`; had
`a` said nothing for 30 minutes, `b`'s reminder would have fired and `b`
would have asked `a` for status.

### 6.2 A brainstorm run by a chair

`H: orchestrator: @chair` then `H: @chair run a brainstorm: seat or
usage pricing?` on a thread.

| # | Event | Turns | Posts |
|---|---|---|---|
| 1 | `chair: mode: snooze` · `@a @b @c @d — one post each, independently; address only me.` | — | 1 (chair, not counted) |
| 2 | four members, each named → four turns in parallel; a member that starts later may see an earlier reply in its backlog — no isolation, as in the other design | 4 | 4 |
| 3 | each reply names `chair` → a chair turn per reply; the skill answers `done` three times and synthesises on the fourth | 4 (3 `done`) | 1 (chair) |
| 4 | `chair: @a @b @c @d — objections only, otherwise done.` | 4 | 1 (`d`) |
| 5 | `d`'s reply names `chair` → chair turn: closes | 1 | 1 (chair) |
| 6 | `chair: mode: open` | — | 1 |

Eight member turns, five chair turns (three `done`), five counted posts.
The plan-based design does the same meeting with two callbacks instead
of five chair turns; this one keeps no record of the round.

### 6.3 Two gateways, one room

`a` runs on gateway 1, `b` on gateway 2, both rosters list both. `H:
anyone know why the build is red?`

- Both classifiers answer `speak`. Each gateway checks its window and
  `settle_seconds`: nothing posted in the last few seconds, both start.
  Two replies arrive; the second gateway's `settle` re-ask would have
  caught it only if `a`'s post had landed first. Bounded noise.
- `a: @b your change touched the build file — did you bump the version?`
  — `b` is named: a turn on gateway 2. `b` replies `to: [a, H]`. Gateway
  1's window counts both posts from the history; so does gateway 2's.
- `H: mode: snooze` — both gateways read it; only mentions open turns in
  the thread from here.

---

## 7. Scenario validation

| Scenario | Control | How it runs | Supported |
|---|---|---|---|
| One room, three scenarios in a day | `open` → `mode: snooze` + `orchestrator:` → `mode: open` | Casual talk by classifier; a meeting under silence; casual talk again. | yes |
| One thread, three scenarios | same, on the thread | A debugging thread becomes a vote under a chair and returns; nothing is "running" to finish under the old rules. | yes |
| Casual conversation (production `#nest`) | `open` | Named bots speak; the classifier adds a bot when the post is for it; corrections arrive through catch-up, where the bot itself judges. | yes — catch-up tuned higher |
| Brainstorm (lab, five bots) | chair skill; `snooze` | §6.2. | yes — more chair turns, no record |
| Status / decision / roadmap meeting | chair skill; `snooze` | The chair addresses one member at a time for reports, all at once for a vote by `done`. | yes |
| Anonymous vote | — | The room is the unit of visibility. | **not in this version** |
| Debugging session | `open`, `limit: 20` | Most posts are for everyone; classifiers say `speak`, `SERIAL` runs them one at a time, each seeing the last. | yes |
| Divide and collaborate | H or PM skill; `open` | `@a @b` both named; dependencies by addressing; each bot tracks its own task (`FOLLOW_UP`). No deadline and no report to `H`; `H` reads the room. | yes, without the report |
| 1:1 DM | — | Named by definition; window counts the bot's own posts. | unchanged |
| Single-bot channel | per `require_mention` | Mentioned → turn; a listen-all room is configured as attention policy "always". | unchanged, by configuration |
| Support / triage channel | `open` | The answering bot addresses a specialist. | yes |
| Scheduled or agent-only room | `open` or `snooze` | No human: the window and cooldown slow an exchange and nothing ends it but `done` or `snooze`. | yes — needs a cap or `snooze`, as any design |
| Code review room | `open` | Reviewer addresses the test bot. | yes |
| Critic or pairing | `open`; critic's attention policy "every post from `a`" | The classifier speaks for the critic on every post from `a`. | yes — per-bot policy |
| Unattended overnight task | `open` | Handoffs by addressing; each bot's `FOLLOW_UP` checks on what it waits for. | yes — guarantees are the skill's |
| Role-play with a game master | GM skill; `snooze` | Players speak when named; the GM narrates after each. | yes, except hidden information |
| Monitor room (CI, webhooks) | `open` | The integration account is human-class: its mention starts a bot, other bots by classifier, nothing it posts counts. | yes |

---

## 8. Validation and sequence

### 8.1 What the simulator measures

The simulator of `multi-agent-enhancement.md` §8.1 runs both designs over
the same scenario files; for this one it records, per run:

- **Classifier false negatives**: posts the classifier passed over that
  the bot, given a catch-up turn, answered with text — the measure of
  what pure classification would have silenced.
- **False positives**: `speak` decisions that ended in `done`.
- **Duplicates**: replies to one post from more than one bot, on one
  gateway and across two.
- **Omissions in coalesced turns**: requests in a turn's backlog that its
  `to` did not cover.
- **Silent stalls**: handoffs where the next bot never spoke, and
  whether a `FOLLOW_UP` reminder recovered them.
- **Chair turns per meeting round**, against the two callbacks of the
  plan-based design.
- **Posts per human prompt, latency per post, classifier calls and
  tokens per post.**

### 8.2 Sequence

| # | Task | Builds | Mechanism tests |
|---|---|---|---|
| 0 | **Simulator**, shared with the other design. | — | the §6 tables reproduced |
| 1 | **Deliver, do not drop.** Cursor per bot; history by post id; envelope and canonical form. | CURSOR, REPLY_ENVELOPE, FAILURE_NOTICE | delivered once on the success path; crash re-delivers; mentions equal `to` |
| 2 | **Gate for agents, and silence.** Agent senders through the gate; `mode: snooze` / `open` as posts. | GATE, SILENCE, AUTHORITY | an agent post naming nobody starts no turn; `snooze` read from history after restart |
| 3 | **Throttle.** Window and cooldown from history; human mentions bypass; orchestrator exempt. | THROTTLE, ORCHESTRATOR, HINT | counts match across two gateway processes on one room; a human mention starts during a full window |
| 4 | **Catch-up.** Threshold and age; one turn per thread. | CATCH_UP, ONE_TURN, STATUS | qualifying set once per pass; ordering by eligibility |
| 5 | **Classifier in shadow**, then with effect. | SPEAK_NOW, SERIAL | shadow agreement with hand labels and with catch-up outcomes; serial re-ask changes the second decision when the first bot answered |
| 6 | **Skills.** `FOLLOW_UP`; the brainstorm chair. | — (policy) | §6.1 and §6.2 reproduced |

Dependencies: 0 first; 1 before everything; 2 and 3 independent; 4 needs
1; 5 needs 2, 3, 4; 6 needs 5.

---

## 9. Undecided

- **Window defaults** (`window_posts`, `window_seconds`,
  `cooldown_seconds`, `settle_seconds`): measured in the lab.
- **Classifier model and history size** (`classifier_posts`): shadow
  measurement decides what it needs to see.
- **Whether the orchestrator's mention-turns go through the classifier**
  so that a chair collects a round in one turn; costs the "mention is
  always honoured" rule for one bot.
- **Catch-up frequency** relative to the plan-based design, from the
  false-negative rate.
- **Envelope encoding**: as in the other document.

## 10. Out of scope for this version

- **Plans, timeouts and reports** — the plan-based design's `PLAN`,
  `CALLBACK`, `STOP`. Reopened if the simulator shows unattended handoffs
  stalling silently at a rate `FOLLOW_UP` does not recover.
- **Roster synchronisation between gateways** — rosters are configured
  by hand. Reopened when two gateways share rooms in production.
- **A room-level classifier** — one call deciding for all bots at once,
  instead of one per bot. Cheaper; reopened once per-bot accuracy is
  known.
- **Anonymous votes, hidden information** — the room is the unit of
  visibility.
- **Replies to several threads from one turn** — a turn is delivered the
  room and replies in its own thread.

---

## Appendix — Against the plan-based design

| | `multi-agent-enhancement.md` | this document |
|---|---|---|
| Who decides an unaddressed turn | mode (`snooze` / `listen` / `catch-up`) per thread | the classifier, per bot; `snooze` as the only mode |
| Sequencing and dependencies | `PLAN`: FIFO per thread, parallel / sequential, admission, two timeout classes, member states, early completion | read from the room when a reply arrives; `FOLLOW_UP` for waiting |
| Rate limit | count per thread since the last human post, persisted; pause with notice; `extend:` | sliding window and cooldown computed from history; no notice; human mentions bypass |
| Loops | paused at a plan boundary until a human, `extend:` or TTL | slowed, not stopped; ended by `done`, `snooze` or a human |
| Who is told what happened | plan-end notices, callbacks, `stop:` reports | nobody; the room is the record |
| Persisted state | cursor, rate limit, mode, orchestrator, plan records in a shared room state keyed by (chat server, room id); posts deduplicated into room events | the cursor |
| Several gateways in one room | non-goal | works, with rosters configured |
| Multiple bots answering one post | `listen` serial | `SERIAL` re-ask; across gateways `settle` |
| Classifier | optional, for addressing by name and mode recommendation | required, for every unaddressed turn |
| Dropped here | — | `UNADDRESSED_MODE` (three modes), `PLAN`, `CALLBACK`, `STOP`, `THREAD_STATE` (shared record, room events, plan records), `LISTEN_GATE`, the `extend:` / `parallel:` / `sequential:` / `timeout:` / `stop:` control words |
| Added here | — | `SPEAK_NOW` on every unaddressed turn, `SERIAL` with re-ask, a window computed from history, `FOLLOW_UP` as a skill, a configured roster |
| Rules a reader must hold | plan lifecycle (admission, coalescing, early completion, timeout classes, restart), three modes with switching, pause and its three resets | one question, one switch, one window, one cursor |
| What it gives up | simplicity; cross-gateway rooms | deadlines and reports for unattended work; aggregation for a chair; determinism of the gate |
