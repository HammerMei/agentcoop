# Rooms with several agents — the classifier gate

This design asks a small classifier one question about every post in a
room: *who in this room should speak now?* Otherwise every post goes to
every bot, in order, with nothing dropped. The gateway keeps no plan, no
record of who owes whom an answer, and no state that two bots share; a
plan-based alternative is compared in Appendix B. Code references are
collected in Appendix C.

---

## 1. Problem

Today a room with several bots is protected from runaway exchanges by
three layers:

- an agent may answer with the token `<end-of-agent-chain>`, and then
  nothing is posted;
- each bot counts the posts it receives from each sender in a room, and
  drops every post past `max_turns` from that sender;
- those counters clear after `ttl_seconds` of silence.

Below those, the `require_mention` setting makes a bot answer only humans
who mention it; posts written by other bots are not checked and reach
every bot in the room. And a bot's `to:` field lists every @-mention in a
post, so a reply that quotes a peer — "@H, I agree with @b's point" — also
lists `b`. The rule that an agent mentions only its intended recipients
exists in the agent prompt alone.

Two rooms were measured (Appendix A). Bots stop on their own in half to
two thirds of their turns. The damage comes from the drops: every
recoverable dropped post carried substance, the drops fell on the
convergence phase of a debate, and two mutual "corrections" between bots
traced to posts the counter had removed from the correcting bot's context
(issue #190). A `@all` prompt produced four near-identical replies in
seven seconds; each human prompt in the test room produced about 25 bot
posts.

The counter is a rate limiter that was being used as a convergence
mechanism. It cannot tell a loop from a conclusion, so it cut where the
conversation was ending; and because it drops rather than delivers, each
bot's view of the room diverged by whatever its own counter had removed.

---

## 2. Terms

Each term uses only the terms above it.

| Term | Meaning |
|---|---|
| **post** | One message on the platform, written by a human or by a bot. |
| **bot** | An agent's presence on a chat server, as `CONTEXT.md` defines it. Inside the gateway, a bot's presence in one room is a *watcher*; the cursor and backlog below are per bot per room. |
| **thread** | A platform thread, or the room's top-level posts taken together as one thread. The settings and the rate limit apply per thread. |
| **named** | A post mentions a bot. In a human's post: a platform mention, `@all` or a room-wide mention, or the bot's username in the text. In a bot's post: only the mentions the platform records for its canonical form (`REPLY_ENVELOPE`). |
| **cursor** | Per bot, per room: the last post delivered to that bot's session. The one piece of delivery state (`CURSOR`). Not the connector's own `watermark`, which records what the connector has passed on, not what a session has seen. |
| **backlog** | The posts of a room after a bot's cursor: on the platform, not yet delivered to that bot's session. Grouped by thread where a thread's size or age matters. |
| **classifier** | A small model asked once per post in an open thread which bots in the room should speak now (`WHO_SPEAKS`). It can add bots to those the post names; it never removes one, and never writes text. |
| **rate limit** | Per thread: a bot's turn does not start while the thread's **recent agent posts** — those within `rate_limit_seconds` — number `rate_limit_posts` or more (`RATE_LIMIT`). Humans and the orchestrator are neither counted nor held. |
| **sweeper** | A periodic pass, per bot, over the bot's backlog. It starts the turns the rate limit or a busy bot held back, and gives a bot that has not been asked in a while a look at what it missed (`CATCH_UP`). |
| **scheduled job** | A reminder a bot sets for itself through the gateway's existing scheduler — "check on a's report in 30 minutes" — which fires at the set time and starts a turn for that bot with no post behind it (`FOLLOW_UP`). |
| **turn** | One run of a bot's backend. It is started by a post that names the bot, by the classifier, by the sweeper, or by a scheduled job firing. It is told which thread started it, receives its backlog, and may reply in any thread it was delivered. A turn either ends in `done` (posts nothing) or posts one or more replies. |
| **orchestrator** | The bot a human has designated for a thread with `orchestrator: @bot`. The rate limit does not apply to it or to the bots it names, and its control words are honoured. A convenience for meetings, not part of the ordinary path. |
| **muted** | A bot named by `mute: @bot` on a thread: it gets a turn there only from a human's mention. |
| **reply envelope** | The structured form of a turn's output: one or more replies, each `thread`, `to`, `text`; or `done` (`REPLY_ENVELOPE`). |
| **control word** | `mode:`, `limit:`, `orchestrator:`, `mute:`, `unmute:` — written in a post. Recognised only from an authorised sender (`AUTHORITY`); otherwise text. |
| **settings** | A thread's current control words — mode, orchestrator, limit, mutes — each with the post it came from, persisted from the thread's first control word until they expire (`SETTINGS`). |
| **agent list** | The usernames the gateway treats as agents — `agent_usernames` in configuration — with each one's profile (`AGENT_LIST`). |

Parameters:

| Parameter | Scope | Defined in |
|---|---|---|
| `rate_limit_posts`, `rate_limit_seconds`, `cooldown_seconds` | room; `limit:` sets `rate_limit_posts` per thread | `RATE_LIMIT` |
| `catch_up_post_threshold`, `catch_up_age_seconds` | room | `CATCH_UP` |
| `control_ttl_days` | room | `SETTINGS` |
| `classifier_posts`, `classifier_timeout_seconds`, `classifier_retries`, `classifier_fallback`, `classifier_decision_ttl_seconds` | room | `WHO_SPEAKS` |
| `follow_up_retry_cap` | room | `FOLLOW_UP` |

No other parameter is set per thread.

---

## 3. Principles

1. **Who speaks is read from the room, the way a person reads it.** For
   every post, the classifier reads the thread and the agent list and
   names the bots that should speak now — the ones the post names, and
   any it adds; none, one or several. There is no plan and no third
   mode: a thread is `open` (the default) or on `snooze`. Sequencing —
   "a does X, then b does Y" — is read out of the conversation when the
   first bot reports, not held in a gateway record.
2. **A mention is a request to speak.**
   - A human's mention always gives the bot a turn, rate-limited or
     muted or not.
   - The orchestrator's mention gives the bot a turn, rate-limited or
     not, unless the bot is muted.
   - A bot's mention of another bot gives it a turn in an open thread,
     delayed by the rate limit if need be. In a snoozed thread it opens
     nothing — except a mention of the orchestrator, which is how bots
     report to it.
   - The classifier's answer is never used to drop a bot the post names.
3. **Every bot eventually sees everything.** One cursor per bot per
   room; a turn is given everything after the cursor, from every thread,
   and may reply in any thread it was given. Nothing is dropped by
   admission.
4. **The platform is the only shared medium** — apart from the in-memory
   classifier decisions one gateway holds for its own bots. Rate limiting
   is computed from the room's history; control words are posts in the
   room; the orchestrator is whoever a post designated. No two bots share
   persisted gateway state, so bots run by different gateways coexist in
   one room, best effort: two gateways cannot confer, so a post may draw
   two answers or none (§10). The gateway persists three kinds of state
   per room: a cursor per bot, the threads' settings, and the scheduled
   jobs.
5. **Mechanism is separate from policy.** The gateway provides the
   mechanisms in §5; meetings, follow-ups and deadlines are skills.

### Non-goals

- **Guarantees about work.** No plan timeouts, no per-bot reports, no
  "who did what" notice. The room is the report; a bot that is waiting on
  another bot tracks that itself (`FOLLOW_UP`).
- **Hidden information.** The room is the unit of visibility.
- **Convergence by the gateway.** No judgement of whether a bot is
  right; a loop is slowed, not ended.
- **Fairness between bots.** A bot that talks a lot uses the thread's
  allowance; others wait, as in any room — until someone mutes it.
- **Scheduling by the gateway.** Bots named by one post start in
  parallel; the gateway orders nothing and second-guesses nothing. A turn
  that could not start runs later only when a sweeper pass finds it
  eligible, with everything posted meanwhile in its backlog, and the bot
  judges whether there is still something to say. Duplicate answers are
  the classifier's to avoid by naming fewer bots, and are noise when it
  does not.
- **Overrides inside `snooze`.** Snooze is one rule, the admission table
  in `GATE`; a skill can rely on it.

---

## 4. A short example

Room `#team`: bots `a`, `b`, `c`; human `H`; `rate_limit_posts` 6 per
`rate_limit_seconds` 300 per thread.

1. `H: a, can you check the nginx log? When it's done, b, restart the
   backend.` — `a` and `b` are named: a turn each. `b` reads "when it's
   done", finds nothing to say yet (`done`, which posts nothing), and
   sets itself a scheduled job for 20 minutes' time (`FOLLOW_UP`). `c`
   is not named, and the classifier adds nobody. No turn for `c`; the
   post is now in `c`'s backlog.
2. `a` replies `to: [H, b]`: `@H @b 502 burst at 09:12, log is clean now
   — b, go ahead.` — `b` is named: a turn; it receives `H`'s post and
   `a`'s. `b` restarts the backend and replies `to: [H, a]`.
3. `b`'s reply names `a`: a turn for `a`. The hint says a reply that only
   acknowledges is `done`, and `a` answers `done`. Two recent agent posts.
4. The sweeper finds `c` with three undelivered posts: one catch-up turn
   for `c`, which reads them and answers `done`. Still two posts.
5. `H: @all, same as last week's incident?` — `@all` names everyone:
   three turns in parallel, each told in its hint that the others were
   named too. `a` answers; `b` starts a little later, sees `a`'s reply in
   its backlog and adds one line; `c` answers `done`. Four recent agent
   posts.
6. `a` and `b` exchange one more pair: six posts — the rate limit is
   reached. `b`'s next turn does not start; its post waits in the
   backlog. `H: enough, thanks` names nobody and is a human post: it does
   not count, and the classifier names nobody. Five minutes later the
   rate limit has cleared and the sweeper gives `b` a catch-up turn; `b`
   reads the room and answers `done`.

---

## 5. Mechanisms

### Order of handling

Every inbound post, for every bot the gateway runs in that room:

1. **Control words** — from an authorised sender (`AUTHORITY`), apply
   `mode:`, `limit:`, `orchestrator:`, `mute:`, `unmute:` to the thread's
   settings. A post that is only control words goes no further.
2. **Named?** — if the post names the bot, the admission table in `GATE`
   says, from who named it and the thread's state, whether the bot
   speaks.
3. **Silenced?** — if not named and the thread is on `snooze`, or the bot
   is muted there, it does not speak; the post waits in its backlog.
   (`SILENCE`)
4. **Who speaks?** — in an open thread the classifier is asked once per
   post, told which bots the post names; the bots it adds speak too.
   Others wait. (`WHO_SPEAKS`)
5. **Allowed now?** — a turn waits while its thread is rate-limited or
   the bot is in cooldown; the exemptions are in the admission table and
   `RATE_LIMIT`. A turn that cannot start now leaves its post in the
   bot's backlog; a classifier choice also stays in the decision cache.
   The sweeper's next pass asks again. (`RATE_LIMIT`, `CATCH_UP`)
6. **One at a time** — one bot runs one turn at a time in a room.
   (`ONE_TURN`)
7. **Context** — the turn receives everything after the bot's cursor and
   before the post that started it, labelled by thread, then that post.
   A turn the sweeper or a scheduled job started receives everything
   through a high-water mark instead. (`CURSOR`)
8. **Reply** — the envelope is parsed; the reply in the thread that
   started the turn is posted in canonical form; a reply to another
   delivered thread is posted if that thread admits it; `done` posts
   nothing. (`REPLY_ENVELOPE`)

The sweeper (`CATCH_UP`) and scheduled jobs (`FOLLOW_UP`) start turns on
their own timing, outside this order.

### GATE

- *Scope*: every post, every bot, human and bot senders alike; no
  exception for bot senders.
- *Named*: a human's post names a bot by platform mention, `@all` or
  room-wide mention, or the bare username; a bot's post names a bot only
  through the mentions the platform records for its canonical form
  (`REPLY_ENVELOPE`). A 1:1 DM names its bot by definition.
- *Not named*: `SILENCE`, then `WHO_SPEAKS`, decide. Naming a bot puts
  it among the speakers for sure; the classifier can add other bots but
  cannot remove a named one.
- *Admission*: whether a turn's source — who or what started it — may
  open a turn for a bot depends on the thread's mode and on whether the
  bot is muted there. This table is the only rule; "under the rate limit"
  means the turn also waits for `RATE_LIMIT`.

  | source of the turn | thread `open` | thread on `snooze` | bot muted there |
  |---|---|---|---|
  | a human's mention | yes | yes | yes — the only way a muted bot speaks |
  | the orchestrator's mention | yes | yes | no |
  | another bot's mention | yes, under the rate limit | no — unless the named bot is the orchestrator | no |
  | the classifier (`WHO_SPEAKS`) | yes, under the rate limit | not asked | no |
  | the sweeper (`CATCH_UP`) | yes, under the rate limit | no | no |
  | the bot's own scheduled job (`FOLLOW_UP`) | yes | deferred until the thread opens or the task expires | deferred until unmuted or the expiry, then ends silently |

  A muted bot ignores everything except a human's mention, and its
  scheduled job ends silently at expiry; a muted orchestrator wakes only
  for a human's mention. The orchestrator's own turns start regardless
  of the rate limit.

### WHO_SPEAKS — the room classifier

- *Inputs*: the agent list (each bot's username and profile — who it is,
  what it does, when it wants to be asked, in free text: "answers every
  post from `a`", "only when asked", "anything about the build"); the
  thread's recent posts; the new post, with the bots it names marked.
  *Question*: which bots should speak now? *Answer*: a set of usernames,
  possibly empty, each with one line of reason. This document assumes
  each bot's profile reaches the classifier; where the profile lives is
  open (§9).
- *Speakers*: the classifier's answer plus the bots the post names. It
  may add bots ("@bob, how is the market?" may add the bot that gives the
  market update); it cannot remove a named one. When its answer leaves
  out a bot the post names, that bot speaks anyway and the disagreement
  is recorded — post, bot, reason — for the measurement in §8.
- *One call per post*, per gateway, in an open thread; a DM or a
  control-only post asks nothing. Every bot's connector receives its own
  copy of the post; the gateway keeps an in-memory map `(connector, room,
  post id) → decision in flight | decision`, and every local copy awaits
  or reads the same entry, so the question is asked once and each local
  bot gets the same answer. An entry lives `classifier_decision_ttl_seconds`
  and is the only memory of the choice: a chosen bot whose turn the rate
  limit or its own busyness held is started from this entry at a later
  sweeper pass (`CATCH_UP`), so the TTL is several `rate_limit_seconds`;
  a choice that outlives it falls back to catch-up, the floor under
  everything. Another gateway asks its own classifier and may answer
  differently.
- *Bounded input*: the last `classifier_posts` posts of the thread,
  newest first, within a token budget; the agent list; and, for each bot
  this gateway runs, the count of its undelivered posts in other threads
  — a number, not the posts; for a bot another gateway runs the count is
  `not-local`, never a guess. What does not fit is omitted with a count,
  and the classifier is told so.
- *Effect*: every speaker gets a turn, through `RATE_LIMIT` and
  `ONE_TURN`, in parallel. A bot not among the speakers waits: the post
  stays in its backlog, is delivered at its next turn, and `CATCH_UP`
  gives the bot its own look later. The classifier is asked to name few:
  one bot that can answer, not everyone who could.
- *A decision belongs to its post*: it makes a bot eligible only while
  that post is still in the bot's backlog — checked when the result
  lands and again when the sweeper reads the cache. A result that
  arrives after a turn admitted meanwhile has delivered the post starts
  nothing, whatever else the backlog holds; it is recorded with its
  reason.
- *Failure*: the call has `classifier_timeout_seconds` and is retried up
  to `classifier_retries`; after that the post is handled by
  `classifier_fallback`:

  | `classifier_fallback` | bots the post names | every other bot |
  |---|---|---|
  | `nobody` | speak | wait for catch-up |
  | `everyone` | speak | speak — mute and the rate limit still apply |
  | `rule` | speak; if the post names nobody, every bot speaks | — today's behaviour for posts written by bots |

  Each fallback decision is recorded with its reason and shown in
  `STATUS`. Processing of the post never waits on the classifier beyond
  its retries; `CATCH_UP` does not depend on it. The default is chosen
  from the simulator's failure runs (§8.1).
- *Limits*: the classifier judges relevance and timing from the text. It
  cannot know what a bot knows — that a figure is wrong, that the bot has
  the file — so it under-names bots for corrections and expertise. That
  is why `CATCH_UP` exists and is tuned higher here than a mode-based
  design would need.
- *Never*: writes text, starts a turn by itself, removes a bot the post
  names, orders the bots it names, or reads anything but the room.
- *Logged*: every decision, reason and fallback, for `STATUS` and §8.

### SILENCE

- `mode: snooze` on a thread: the orchestrator gets a turn from any
  mention — that is how bots report to it; every other bot gets a turn
  only from a human's or the orchestrator's mention. A bot's mention of
  another bot, the classifier and the sweeper open nothing there; a
  bot's own scheduled job is deferred until the thread opens or its task
  expires (`FOLLOW_UP`); posts are delivered as context by whichever turn
  comes next. `mode: open` (the default) restores the classifier, the
  sweeper and bot-to-bot mentions. This is the instrument an orchestrator
  uses to impose a structure — who speaks is exactly who it names, and a
  bot that addresses another bot starts nothing. Snooze is one rule;
  mute, when set, overrides it for that bot.
- `mute: @x` on a thread: `x` gets a turn there only from a human's
  mention; the classifier does not name it, the sweeper skips it, other
  bots' mentions of it open nothing. `unmute: @x` lifts it. This is how
  an orchestrator or a human stops one bot that is flooding a thread
  without silencing the rest.
- Both are posts, so every gateway in the room reads them, and both live
  in the thread's settings (`SETTINGS`).

### RATE_LIMIT

- *Computed, not stored*: from the thread's history, the gateway counts
  the posts written by bots within `rate_limit_seconds` — the recent
  agent posts — and reads the time of the bot's own last post. Every
  gateway in the room computes from the same history with the same agent
  list; the result converges, best effort — paging limits, API lag,
  edits and fetch failures can make two gateways differ for a while.
  `STATUS` shows the current count, the oldest post fetched and fetch
  failures.
- *Rule*: a turn does not start while the recent agent posts number
  ≥ `rate_limit_posts`, or the bot posted within `cooldown_seconds`. The
  rate limit gates the *start* of a turn only: a started turn always
  posts its reply in its own thread, so the count can go over
  `rate_limit_posts` by the number of turns already running there —
  bounded by the room's bots, and by one more per gateway across
  gateways. A reply to another thread (`REPLY_ENVELOPE`) checks that
  thread's rate limit at posting time, serialised per thread within one
  gateway.
- *Exemptions*, by the turn's source — not by who wrote the post: a bot
  the classifier chose for a human's unaddressed post waits like any
  other.

  | source of the turn | rate limit | cooldown | its posts count | bound |
  |---|---|---|---|---|
  | a human's mention | exempt — the human intervenes and cannot be shut out by noisy bots | exempt | yes | the human |
  | the orchestrator's mention, or its own turn | exempt | exempt | the orchestrator's posts do not count | its recent posts are shown to the owner in `STATUS`; a human can `mute:` or re-designate it at any time |
  | a scheduled job firing | exempt | exempt | the firing is not a post; the reply its turn makes counts | `follow_up_retry_cap`, the expiry and the backoff in `FOLLOW_UP` |

  Posts written by humans, and by integration accounts configured as
  owners or guests but not in `agent_usernames`, never count. A human's
  own control words are never held. `STATUS` names which exemption
  started each recent turn.
- *No notice*: being rate-limited is silence, as it is for a person who
  cannot get a word in. `STATUS` shows it; `HINT` tells a bot that is
  allowed to speak how much room is left.
- *Loops*: two bots trading replies are slowed to `rate_limit_posts` per
  `rate_limit_seconds` and `cooldown_seconds` apart, not stopped. They
  stop when one answers `done`, when someone mutes one of them or snoozes
  the thread, or when a human tells them to. The measured rate at which
  bots stop on their own (§1) is what this relies on.
- `limit: N` sets `rate_limit_posts` for the thread.

### AUTHORITY

- Control words are honoured from humans with the owner role and from the
  thread's orchestrator. `orchestrator:` is honoured from owner-role
  humans only. From anyone else they are text. A thread has one
  orchestrator; `orchestrator:` again replaces it. The same bot may be the
  orchestrator of several threads and is an ordinary bot elsewhere.
- Roles are server-injected (`CLAUDE.md`, connector prompt prefix).

### SETTINGS

- A thread's current control words — mode, orchestrator, `limit:`, the
  muted bots — are the last control word of each kind posted in it. The
  gateway keeps them as the thread's settings, persisted with the
  cursors, written when a control word arrives. A thread gets settings
  at its first control word; each value carries the post it came from.
- *Expiry*: settings are removed after `control_ttl_days` of silence in
  the thread (days, not seconds), or when the platform archives or
  deletes the thread; the defaults apply again. Silence is read from the
  room's history — the age of the thread's newest post — not kept in the
  settings. A thread snoozed and then abandoned reopens on its own; a
  mute lapses with it. `STATUS` lists the threads that hold settings and
  their age.
- *Order*: control words apply in the server's order of their posts, not
  in arrival order: a control is applied only if its post is newer than
  the one the current value came from. A value that returns to its
  default stays in the settings with its post, so `snooze` then `open`
  followed by a replay of the `snooze` leaves the thread open. The rate
  limit is computed with the thread's *current* orchestrator:
  re-designating the orchestrator changes which past posts count, and
  that is accepted over recording a role per post.
- Two gateways in one room each keep their own settings from the same
  posts; they agree because the posts are the same, not because they
  talk to each other.
- *A gateway's own control posts*: a connector drops its own account's
  messages, so a control word an orchestrator posts would never come back
  to the gateway that posted it. After a successful outbound post the
  gateway applies the control word to its settings through the same
  inbound transition — same authority check, same parser — not a
  shortcut. A crash between the platform accepting that post and the
  settings being saved leaves this gateway behind the others until the
  next control post; accepted as best effort.

### ONE_TURN

- A bot runs one turn at a time in a room — one backend session, one
  invocation. When several posts could start a turn for one bot, the
  oldest in the room's event order goes first: the server's order for
  posts, a local sequence for sweeps and job firings, which have no post.
  (The simulator numbers every event; the gateway's connector abstraction
  for this order is an implementation item, §9.)
- *There is no waiting turn.* A turn that cannot start — the bot is
  busy, the thread is rate-limited — leaves behind only its post in the
  backlog and, for a classifier choice, the cached decision. A bot that
  becomes free starts nothing by itself; it and a turn that ends each
  ask the sweeper for a pass on that bot, and the pass reconstructs
  eligibility from the backlog, the admission table and the cache
  (`CATCH_UP`). The turn that then runs is not second-guessed: it has
  everything posted meanwhile in its backlog, and the bot judges whether
  there is still something to say. A reply someone else already gave, or
  a human's "never mind", is in front of it; `done` costs a few seconds.

### CURSOR

- *Scope*: one post id per bot per room — the last post delivered to the
  bot's session. Created at the first delivery, never removed.
- *Delivery*: a turn started by a new post receives the room's posts
  after the cursor and before that post, both exclusive, each labelled
  with its thread, then the post once. A turn started by the sweeper or
  a scheduled job fixes a **high-water mark** — the newest post in the
  room when it starts — and receives everything after the cursor through
  it; the post that made the bot eligible is named as the trigger but
  does not bound delivery, so the turn sees everything posted meanwhile,
  and a paged fetch has a fixed end. A post arriving after the mark waits
  for a later turn. A turn started by a post found at reconnect or
  startup is delivered like one started by a new post. The turn may
  reply in any thread it was delivered (`REPLY_ENVELOPE`); `HINT` names
  the thread that started it and the others present.
- *When the cursor is saved*: the new cursor — the last post delivered —
  is written when the turn ends normally: `done`, or every reply posted,
  rejected by the reply check, or refused by the platform
  (`REPLY_ENVELOPE`). It is not written, and the same posts are
  delivered again, when the backend fails or the reply is still invalid
  after the one re-prompt (`FAILURE_NOTICE`).
- *Contract*: nothing is withheld by admission. A turn that ends
  normally is not delivered again. A fetch failure, a failed turn, or a
  crash before the cursor is saved re-delivers the same posts, and a
  reply already posted before such a crash may be posted again; there is
  no retry counter on ordinary turns, so this is possible, not bounded,
  and sits inside the project's best-effort delivery
  (`docs/requirements.md`). Delivered backlog is context: control words
  and mentions in it are not re-executed.
- *Interface*: the connector history fetch, bounded by post id,
  returning post and thread ids, paging past the first page (Mattermost
  fetches one today).
- *Posts found at reconnect or startup*: the connector's per-room replay
  runs them through the gate like new posts; the gate decides whether a
  turn starts, never whether a post is delivered.
- *Per thread*: backlog size and age per thread are computed from the
  posts after the cursor, for `CATCH_UP` and `STATUS`; nothing per thread
  is stored for it.

### CATCH_UP — the sweeper

- *Scope*: a pass per bot over its undelivered posts grouped by thread —
  on a timer, and on request when the bot becomes free or one of its
  turns ends (`ONE_TURN`). The threads a pass will act on are chosen once
  at its start; immediately before each one, the pass checks that the
  bot still has backlog for it, since an earlier turn of the same pass
  may have delivered it. A thread with nothing left is skipped — no
  empty turn runs — which with one room cursor usually means one turn
  serves several threads.
- *Named or chosen posts first*: a post in the bot's backlog that the
  admission table admits now — one that names the bot, or whose
  classifier decision, not yet expired, picked the bot and whose post is
  still in the backlog — starts a turn as soon as the rate limit, the
  cooldown and the bot allow, with the oldest such post named as the
  trigger. The table is read against the thread's state at the pass, so
  a mention that arrived before `snooze` opens nothing once the thread is
  snoozed. This is how a turn the rate limit or a busy bot held is
  started: nothing was stored, the post was in the backlog all along.
- *Catch-up turns*: otherwise, a thread on `open`, where the bot is not
  muted, with ≥ `catch_up_post_threshold` undelivered posts for the bot
  or an oldest undelivered post older than `catch_up_age_seconds`, and
  room under the rate limit and the bot's cooldown, gets one catch-up
  turn, in newest-post order across threads; the first delivers the
  whole backlog through its high-water mark (`CURSOR`), so the rest are
  usually skipped. The trigger names the thread; the bot reads and
  decides — `done` is the common answer. This is the bot's own look at
  what the classifier passed over. No turn starts for a bot whose
  backlog is empty; a scheduled job is the one exception, since it has
  no post of its own.
- *What it covers*: posts not yet delivered. A post another thread's turn
  has already delivered is not revisited — that turn could reply in its
  thread (`REPLY_ENVELOPE`), and the bot's session remembers it. "Not
  now" therefore means: the bot sees the post at its next turn, whichever
  thread starts it, and may answer there and then.

### REPLY_ENVELOPE

- *Fields*: `replies` — a list, each with `thread` (one of the threads
  delivered to this turn; the thread that started the turn when
  omitted), `to` (required, non-empty: humans or bots in the room, or
  `all`; the sender excluded; not needed for a control-only `text`) and
  `text`. A single reply may be written as top-level `to` / `text`.
  `done` — nothing to post. One of `replies` / `done`. Output that is
  only the token `<end-of-agent-chain>` is read as `done`.
- *Canonical form*: the gateway rewrites each reply's `text` before
  posting so that the post's mentions are exactly its `to`: a recipient
  not mentioned in the text is prepended as `@name`; an `@name` in the
  text that is not a recipient loses its `@`; `all` becomes the
  platform's room-wide mention. A human reads the recipients at the head
  of the post, and every gateway's gate reads the platform's mentions
  and reaches the same set — after a restart and from a history fetch
  alike. Every post is parsed one way, from the platform, whoever wrote
  it.
- *The reply check*: the reply in the thread that started the turn
  **always posts** — the turn was admitted there, and that admission is
  the only check; a `mute:` or `snooze` that lands mid-turn does not stop
  it. A reply to another delivered thread posts only if that thread
  admits it:

  | that thread | posts? |
  |---|---|
  | `open`, bot not muted, room under the rate limit | yes |
  | `open`, rate-limited | no |
  | on `snooze` | only if a human or the orchestrator named the bot there, or the bot is the orchestrator |
  | bot muted there | no |

  The orchestrator is exempt from the rate limit here too. Every posted
  reply counts in its thread. A reply that fails the check is **not
  posted and not held**: the next turn's `HINT` says which reply was not
  posted and why, and the bot decides whether to say it again. The bot
  is told each thread's state before it writes, so this is rare.
- *Partial failure*: the gateway attempts every reply once, in order. A
  reply the platform refuses is reported in the next turn's `HINT` like
  a rejected one; the replies already posted stay, the turn ends
  normally and the cursor is saved (`CURSOR`). Nothing is retried on the
  bot's behalf — a retry could post a reply twice, and delivery is best
  effort.
- *Validation* — returned to the session once with the schema error; a
  second invalid output fails the turn (`FAILURE_NOTICE`):
  - no `replies` and no `done`;
  - a reply without `to`, or with an empty `to`, or naming only the
    sender;
  - a name that is neither a room member nor a bot the gateway knows;
  - a `thread` the turn was not delivered.
- *What is not enforced*: there is no record of "requests this turn
  serves", so the gateway cannot check that `to` covers everyone who
  asked. `HINT` asks for it; the simulator counts the omissions (§8).

### FAILURE_NOTICE

- A failed turn — backend failure, or a reply still invalid after the
  one re-prompt — is announced from the failing bot's account, delivered
  as context, starting no turn. The cursor is not saved, so the posts
  the turn was given are delivered again at the next turn. A platform
  refusing to post a reply is not a failed turn (`REPLY_ENVELOPE`,
  *Partial failure*). A failed turn from a scheduled job also returns
  the occurrence to the scheduler (`FOLLOW_UP`).
- *Identity*: "starts no turn" cannot rest on the notice's text, which
  anyone can type. The notice is a gateway-generated event kind, carried
  in platform metadata where the platform has it and recognised by the
  connector before `GATE`; every gateway in the room treats it as context
  only and executes nothing from it. In the simulator the event kind is
  explicit in the scenario; how each platform carries it is settled in
  the lab (§8.1).

### HINT

Injected into every turn:

- the thread that started the turn, and the other threads delivered;
- each thread's mode, and whether the bot is muted there;
- how many posts each thread's rate limit has left;
- replies from the last turn that could not be posted — one to another
  thread that thread did not admit, or one the platform refused — and
  why;
- the instruction: "make your point; do not restate; answer `done` if
  you have nothing to add; `to` is who the post is for, not who it
  mentions — include everyone who asked you something in this backlog;
  reply in another delivered thread when something there needs you;
  `all` only with a reason; if you are waiting on someone, say so and
  set a scheduled job; if your reply unblocks someone, address them."

### FOLLOW_UP — a skill on the scheduler

- *Skill*: a bot that takes on or waits for work records it — what, for
  whom, expected by when, judged from the task — and schedules a job for
  the follow-up time. When it fires, the bot reports progress, or asks
  the other party for status, and schedules the next check with a longer
  gap; past the expiry it says so and stops. Estimates are the bot's own;
  a wrong one costs a check too early or too late.
- *Scheduler contract* (changes to the existing scheduler): a job
  persists the connector, room, **thread**, the task text, its expiry,
  and its retry state; today a job carries a room and a message and is
  injected without a thread. A firing inherits the thread. An occurrence
  is cleared when its turn ends normally (`CURSOR`) — not when the
  message is queued, which is today's rule; a failed turn keeps the
  occurrence, retries with bounded exponential backoff, keeps the
  original expiry, and past `follow_up_retry_cap` retries posts a notice
  and stops. Clearing an occurrence and saving the cursor are two saves,
  so across a crash an occurrence is at-least-once: the skill tolerates
  a repeated firing.
- *Under the gate*: see the admission table in `GATE`, row "the bot's
  own scheduled job". The firing itself is not a post and counts
  nothing; the reply its turn makes counts in its thread. In a snoozed
  thread the firing is deferred until the thread opens or the task
  expires, whichever first; the turn that then runs posts like any turn
  — the expired one is the terminal report.

### ORCHESTRATOR

- `orchestrator: @bot` on a thread: the admission table and `RATE_LIMIT`
  give the bot and the bots it names their exemptions, and `AUTHORITY`
  honours its control words, including `mute:` of other bots. Policy for
  its turns lives in its skill.
- It gets no special delivery: bots reply to it by naming it, and each
  reply is a turn for it; its skill answers `done` until it has what it
  needs. A meeting is `mode: snooze` plus the orchestrator naming bots,
  all at once or one at a time as the skill decides, muting a bot that
  will not stop.

### STATUS

`coop room status <room>` (the room's handle, `<connector>:<room label>`)
shows:

- per thread — mode, orchestrator, muted bots, settings age, recent
  agent posts / `rate_limit_posts`, the orchestrator's recent posts;
- per bot — backlog size and age per thread, computed on request;
  cooldown remaining;
- the last classifier decisions with reasons, and any fallback
  decisions;
- which exemption started each recent turn;
- the agent list, and bot-like room members not on it.

### AGENT_LIST

- The gateway knows which usernames are agents from configuration
  (`agent_usernames`), which must list every agent in the room, including
  those run by other gateways. A sender on none of the connector's user
  lists is rejected by the connector today, so an agent missing from the
  agent list is not "treated as a human": its posts are rejected by the
  connector and never reach the gate. At startup the gateway checks the
  agent list against the room's members where the platform allows, logs
  a missing or unknown agent loudly, and shows both in `STATUS`. Two
  gateways whose agent lists differ for one room do not converge on the
  rate limit or the classifier, and the status says so. Synchronising
  agent lists between gateways is later work (§10).

---

## 6. Worked examples

### 6.1 A handoff read from the room

`H: @a run the migration; b, when a is done, deploy.` Thread `T`,
`rate_limit_posts` 6.

| # | Event | `a` | `b` | `c` |
|---|---|---|---|---|
| 1 | `H`'s post | named → turn | named → turn: reads "when a is done", answers `done`, schedules a job for 30 minutes' time | classifier adds nobody → wait |
| 2 | `a: @H @b migration done, 14 tables.` | — | named → turn: deploys, replies `to: [H, a]` | classifier: nobody → wait |
| 3 | `b: @H @a deployed, green.` | named → turn → `done` | — | classifier: nobody → wait |
| 4 | sweeper: `c` has 3 undelivered posts | — | — | catch-up turn → `done` |

Two agent posts, three `done` turns, one classifier call per post. Had
`a` posted "migration done" to `H` alone, the classifier would have read
"b, when a is done" in the thread and named `b`; had `a` said nothing for
30 minutes, `b`'s job would have fired and `b` would have asked `a` for
status.

### 6.2 A brainstorm run by an orchestrator

`H: orchestrator: @chair` then `H: @chair run a brainstorm: seat or
usage pricing?` on a thread. `chair` is the orchestrator.

| # | Event | Turns | Posts |
|---|---|---|---|
| 1 | `chair: mode: snooze` · `@a @b @c @d — one post each, independently; address only me.` | — | 1 (chair, not counted) |
| 2 | four bots, each named by the orchestrator → four turns in parallel, none rate-limited; a bot that starts later may see an earlier reply in its backlog — no isolation. "Address only me" is enforced by `snooze`, not by the prompt: a bot that names another bot starts nothing | 4 | 4 |
| 3 | each reply names `chair` → an orchestrator turn per reply; the skill answers `done` three times and synthesises on the fourth | 4 (3 `done`) | 1 (chair, not counted) |
| 4 | `chair: @a @b @c @d — objections only, otherwise done.` | 4 | 1 (`d`) |
| 5 | `d`'s objection names `chair` → an orchestrator turn: the skill notes it. It also named `a`; that opened nothing for `a` (`snooze`). `d` has answered every round at length and off the point, so the orchestrator runs the last round without it: `chair: mute: @d` · `@a @b @c — last round: anything to add?` → three turns, three `done` | 1 + 3 | 1 (chair, not counted) |
| 6 | `chair: @H <close>` · `mode: open` · `unmute: @d` | — | 1 (chair, not counted) |

Eleven bot turns, five orchestrator turns after its first post (three
`done`), five counted posts. The gateway keeps no record of the round;
the orchestrator's skill does.

### 6.3 One turn, two threads

Thread `A` is an incident thread; thread `B` is unrelated. `b` posts in
`A`, naming nobody: "the 09:12 burst was 404s, not 502s" — a correction
of a figure `x` gave. The classifier names nobody; `x`'s cursor stays
before it. Later `H` posts in `B`: `@x is staging green?`

- `x`'s turn receives `b`'s correction, labelled `A`, and `H`'s question,
  labelled `B`. Its envelope carries two replies: in `B`, `to: [H]`,
  "green"; in `A`, `to: [b]`, "right — 404s, my mistake." Each posts in
  its thread and counts there.
- Had `A` been snoozed, or rate-limited, the `A` reply would not have
  posted; `x`'s next turn would be told so, and `x` would decide whether
  to say it when next named in `A`.

### 6.4 Two gateways, one room

`a` runs on gateway 1, `b` on gateway 2, both agent lists hold both. `H:
anyone know why the build is red?`

- Each gateway asks its classifier once and acts on its own bot only.
  Both may name `a` and `b`: two replies. Gateway 1 may name `b` and
  gateway 2 `a`: neither runs the bot it named, and nobody answers until
  `H` names one (§10).
- `a: @b your change touched the build file — did you bump the version?`
  — `b` is named: a turn on gateway 2. `b` replies `to: [a, H]`. Each
  gateway counts both posts toward the rate limit from the history.
- `H: mode: snooze` — both gateways write the same settings; only
  mentions open turns in the thread from here.

---

## 7. Scenario validation

| Scenario | Control | How it runs | Supported |
|---|---|---|---|
| One room, three scenarios in a day | `open` → `mode: snooze` + `orchestrator:` → `mode: open` | Casual talk by classifier; a meeting under snooze; casual talk again. | yes |
| One thread, three scenarios | same, on the thread | A debugging thread becomes a vote under an orchestrator and returns; nothing is "running" to finish under the old rules. | yes |
| Casual conversation (production `#nest`) | `open` | Named bots speak; the classifier adds a bot when the post is for it; corrections arrive through catch-up or as a second reply from a turn in another thread. | yes — catch-up tuned higher |
| Brainstorm (test room, five bots) | orchestrator skill; `snooze` | §6.2. | yes — more orchestrator turns, no record |
| Status / decision / roadmap meeting | orchestrator skill; `snooze` | The orchestrator names one bot at a time for reports, all at once for a vote by `done`; mutes a bot that will not stop. | yes |
| Debugging session | `open`, `limit: 20` | Most posts are for everyone; the classifier names the bot or two best placed; the rest see the exchange through catch-up and reply when they have something. | yes |
| Divide and collaborate | H or a PM skill; `open` | `@a @b` both named; dependencies by naming; each bot tracks its own task (`FOLLOW_UP`). No deadline and no report to `H`; `H` reads the room. | yes, without the report |
| Support / triage channel | `open` | The answering bot names a specialist. | yes |
| Scheduled or agent-only room | `open` or `snooze` | No human: the rate limit and cooldown slow an exchange and nothing ends it but `done`, `mute:` or `snooze`. | yes — needs a cap or `snooze`, as any design |
| Code review room | `open` | The reviewer names the test bot. | yes |
| Critic or pairing | `open`; the critic's profile says "answer every post from `a`" | The classifier names the critic on every post from `a`. | yes — per-bot profile |
| Unattended overnight task | `open` | Handoffs by naming; each bot's `FOLLOW_UP` checks on what it waits for. | yes — guarantees are the skill's |
| Role-play with a game master | GM skill; `snooze` | Players speak when named; the GM narrates after each. | yes, except hidden information |
| Monitor room (CI, webhooks) | `open` | The integration account is configured as a guest: its mention starts a bot, other bots by classifier, nothing it posts counts. | yes |

Single-bot channels and 1:1 DMs are unchanged: the bot is named by
definition or by `require_mention`, and the rate limit counts its own
posts. Anonymous votes are not supported (§10).

---

## 8. Validation and sequence

### 8.1 The simulator comes first

Before any gateway code, §5 is implemented as a standalone simulator
(`tools/roomsim/`, outside the gateway) and the scenarios in §7 are run
through it, to see the conversations this design produces — and the odd
corners — before paying for an implementation.

- **Mechanism**: a deterministic script implementing §5 as written —
  the admission table, the rate limit, the sweeper, the cursor, the reply
  envelope. It is not a language model tracing the rules: a model's
  trace would hide whether a wrong step was a hole in the spec or an
  error in the trace. Where the script and the text disagree, one of
  them is wrong and is fixed.
- **Bots**: each bot turn is one real `claude -p` (or OpenCode) call with
  the bot's persona from the test room's configuration, in one session
  per bot per room as the gateway keeps it; each turn appends what the
  cursor delivers and nothing else. An orchestrator is the same, loaded
  with its skill; its control words are parsed by the script.
- **Humans**: scripted lines in the scenario file, placed at a step; a
  model playing the human is allowed and labelled as such.
- **Output**: a transcript with the script's annotations before each
  event — the turn, its thread and backlog, the recent-post count, every
  classifier decision, every sweep — and a tally of turns, posts, tokens
  and context size per bot. People review the transcript for exchanges
  that read wrong; what reads wrong is fixed in §5 and the scenario is
  run again.
- **Two kinds of run**: a run with live models is for behaviour — what
  the bots do with the rules, over several runs, read by people; it is
  not expected to repeat. A run's inbound events and bot outputs can be
  saved as a recorded run, and replaying it is deterministic; the
  mechanism tests in §8.2 are written against the simulator first and
  carried to the gateway unchanged, and on a recorded run the gateway
  must produce the same annotation sequence as the simulator.
- **What it cannot show**: platform behaviour — threads, the mention
  array each platform computes from a canonical post, history paging,
  webhook identities, how a failure notice is carried — and session
  compaction. Those belong to the lab: the test server with the bots in
  §1's measurements.

Each scenario runs in three configurations, so that the classifier's
value and its failure cost are both measured against the same
conversations:

- **classifier on every post**, with `classifier_retries` and a forced
  failure rate injected, recording the raw failure rate, how often a
  retry recovered, and how many retries it took;
- **no classifier** — each `classifier_fallback` alone, as its own run;
- **classifier with each fallback** under the injected failures, to see
  the experience a room would actually get when the classifier is down.

It records two kinds of thing, kept apart:

**Decision quality** — people label the classifier's exact input
snapshots, blind: given this thread and agent list at this moment, who
should have spoken? Agreement with those labels, per model and history
size, is the measure of the classifier; nothing observed later is.

**Operational outcomes** — per run:

- named by the classifier → `done` rate; not named → later catch-up with
  a reply rate; these are costs and delays, not labels;
- the classifier's would-be vetoes: posts where its answer left out a
  bot the post named, with the reason and what that bot's turn then did
  (`done`, a reply, a handoff) — the evidence for whether the classifier
  should ever override a mention;
- bots the classifier added to a named post, and what they did;
- corrections: how long after the correcting post the corrected bot
  acknowledged it, and through which path (named, classifier, catch-up,
  second reply from another thread);
- duplicates: replies to one post from more than one bot, on one gateway
  and across two;
- omissions in turns that answer several posts: requests in a turn's
  backlog that its replies did not address;
- replies not posted by the reply check, and whether they were said
  later;
- silent stalls: handoffs where the next bot never spoke, and whether a
  `FOLLOW_UP` job recovered them;
- orchestrator turns per meeting round;
- rate-limited turns, exemptions used, fallback decisions;
- posts per human prompt, latency per post, classifier calls and tokens
  per post.

### 8.2 Sequence

| # | Task | Builds |
|---|---|---|
| 0 | **Simulator.** §8.1: the script, a scenario file for every §7 row, persona files from the test room, the classifier in shadow (logged, not acted on) and acted on. | — |
| 1 | **Deliver, do not drop.** Cursor per bot; history by post id; the envelope with several replies and the canonical form; the settings' persistence. | CURSOR, REPLY_ENVELOPE, FAILURE_NOTICE, SETTINGS |
| 2 | **Gate for bot senders, snooze, mute.** Posts from bots through the gate; `mode:`, `mute:`, `unmute:`, `orchestrator:` as posts. | GATE, SILENCE, AUTHORITY |
| 3 | **Rate limit.** Recent-post count and cooldown from history; the exemptions; agent-list validation. | RATE_LIMIT, ORCHESTRATOR, AGENT_LIST, HINT |
| 4 | **Sweeper, one turn, status.** Named or chosen posts first; catch-up by threshold and age. | CATCH_UP, ONE_TURN, STATUS |
| 5 | **Scheduler contract and follow-up.** Jobs carry the thread; cleared on a normal end; retry with backoff and cap; the `FOLLOW_UP` skill. | FOLLOW_UP |
| 6 | **Classifier in shadow**, then acted on. | WHO_SPEAKS |
| 7 | **Skills.** The brainstorm orchestrator; a PM skill for fan-out with follow-ups. | — (policy) |

Dependencies: 0 first; 1 before everything; 2 and 3 independent; 4 needs
1; 5 needs 1; 6 needs 2, 3, 4; 7 needs 5 and 6.

Mechanism tests, by task — written against the simulator first:

- **0**: the §6 tables reproduced.
- **1**: the cursor is saved at each normal end and not at a failed one,
  with a failure injected at every boundary; a two-thread turn posts in
  both, each counted in its own; a reply to a thread that does not admit
  it is not posted and is reported next turn; a two-reply envelope whose
  second post the platform refuses keeps the first, reports the second,
  saves the cursor; the reply in the starting thread posts in a
  rate-limited thread, a second-thread reply does not; mentions equal
  `to`; settings survive restart and lapse at `control_ttl_days`; an
  orchestrator's own outbound control word updates its gateway's
  settings; a replayed older control arriving after the value returned
  to its default is ignored.
- **2**: with the classifier off and the `nobody` fallback, a bot's post
  naming nobody starts no turn; in a snoozed thread a bot's mention of a
  bot starts nothing, a human's or the orchestrator's starts a turn, and
  a bot's mention of the orchestrator starts the orchestrator's turn; a
  muted bot runs only on a human's mention and its reply to that human
  posts despite the mute; `snooze` read from the settings after restart.
- **3**: counts match across two gateway processes on one room with
  equal agent lists, and status says so when they differ; a human's
  mention starts in a rate-limited thread; a bot the orchestrator names
  starts in a rate-limited thread.
- **4**: the threads of a pass chosen once; ordering by event order; a
  turn that could not start runs at a later pass with the meanwhile
  posts in its backlog; a rate-limited bot-to-bot mention in an open
  thread starts at the next pass once under the rate limit, and not at
  all if an earlier turn delivered it; a bot becoming free starts nothing
  without a pass; a mention that arrived before `snooze` opens nothing
  after it; a cached classifier choice starts the held bot at a later
  pass, and lapses to catch-up past its TTL; two qualifying threads yield
  one catch-up turn when the first delivers both; a sweeper turn delivers
  through its high-water mark and a post arriving after it waits.
- **5**: a job fires in its thread; a failed firing is retried and not
  lost; the cap posts a notice; a job fires in a rate-limited thread; in
  a snoozed thread a firing is deferred until the thread opens or the
  task expires, and the one that runs posts; a muted bot's job waits and
  ends silently at expiry — mute before fire, mute during retry, unmute
  before expiry, expiry while muted.
- **6**: one platform post arriving on three local connector streams at
  once → one classifier call and the same decision for each local bot; a
  named bot speaks even when the answer omits it, and the omission is
  recorded; a result arriving after another turn delivered its post
  starts nothing even when newer unrelated posts are in the backlog;
  bounded input, remote bots' counts `not-local`; timeout → retries → the
  configured fallback, shown in status; shadow agreement with blind
  labels before it is acted on.
- **7**: §6.1 and §6.2 reproduced.

---

## 9. Undecided

- **Rate-limit defaults** (`rate_limit_posts`, `rate_limit_seconds`,
  `cooldown_seconds`) and `control_ttl_days`: measured in the lab.
- **Classifier model, history size, timeout, retries and fallback**
  (`classifier_posts`, `classifier_timeout_seconds`,
  `classifier_retries`, `classifier_fallback`): the simulator's forced
  failure runs decide the fallback and the retry count; shadow
  measurement decides the rest.
- **Whether the classifier may ever override a mention**: decided from
  the recorded would-be vetoes (§8.1); until then named bots always
  speak.
- **The room's event order in the gateway**: a connector abstraction
  giving one total order per room — server order for posts, a local
  sequence for sweeps and job firings, a fixed high-water mark for a
  paged fetch. The simulator numbers events; the gateway needs the real
  one before its traces are compared with the simulator's.
- **Whether the orchestrator's reply-turns go through the classifier**,
  so that it collects a round in one turn; costs the "a human's or the
  orchestrator's mention is always honoured" rule for one bot.
- **Catch-up frequency**, from the not-named → catch-up-with-a-reply
  rate.
- **Envelope encoding** (task 1): a fenced JSON block, the whole reply,
  or a backend-native structured output where one exists (`claude
  --json-schema`; the OpenCode HTTP backend has none). The contract is
  the fields; the encoding is chosen when the parser is written.
- **Where a bot's profile lives.** The classifier needs, per bot, more
  than a username: what the bot does and when it wants to be asked —
  "how's the market today?" names nobody, and only a profile tells the
  classifier that one bot gives the daily stock update. The agent list
  configured today is a list of usernames. Candidates: a description per
  agent in each gateway's configuration, which two gateways must then
  keep in step; or the platform's own user profile fields, which every
  gateway reads from the same place. Decided before task 6; the design
  assumes the profile is provided and says nothing about how.

## 10. Out of scope for this version

- **Plans, timeouts and reports** — a gateway record of who was asked
  to do what, with a deadline per step, a cancel command, and a notice to
  the requester saying who answered and who did not (the plan-based
  alternative, Appendix B). Reopened if the simulator shows unattended
  handoffs stalling silently at a rate `FOLLOW_UP` does not recover.
- **Agent-list synchronisation between gateways** — agent lists are
  configured by hand and checked at startup. Reopened when two gateways
  share rooms in production.
- **Skipping the classifier for named posts** — not asking when a post
  names every bot, or names any bot. An optimisation of cost and
  latency, reopened when the data shows the classifier adds nothing to
  such posts; the mechanism is defined as asking every time.
- **A per-bot classifier** — one call per bot with its own backlog as
  input. Reopened if the room-level classifier's labels show it cannot
  judge for bots with different profiles.
- **Ordering within one gateway** — bots named by one post start in
  parallel, and two may answer the same question. Two mechanisms were
  considered and not built: a *stagger* (the next named bot starts when
  the previous turn ends or after a fixed delay, whichever first) and a
  *collision check* (a turn whose thread received another bot's post
  while it ran is shown that post before its reply is posted, and may
  revise or answer `done`). From the turn lengths in Appendix A, a
  15-second stagger would let a later bot see the earlier reply about
  two times in three; a 5-second one would mostly only tell it the
  earlier bot had nothing to say. People do not take turns either; it
  only looks so because they type at different speeds, and a busy bot
  gives the same effect for free. Reopened when the simulator's
  duplicate rate says the noise is worth a mechanism; true sequencing is
  an orchestrator's job.
- **Coordination across gateways** — two gateways in one room cannot
  confer, so for one post both may name a bot of their own (two
  replies), or each may name a bot the other runs (no reply at all).
  Both outcomes are accepted: coordination across gateways is best
  effort in this version, and a human's mention always reaches its bot.
  Reopened only with a protocol between gateways.
- **Anonymous votes, hidden information** — the room is the unit of
  visibility.

---

## Appendix A — Evidence from gateway logs

Lab: `#product-idea`, five bots (three `claude`, two `opencode`),
`max_turns: 5`, one afternoon. Production: `#nest`, three bots and one
human, `max_turns: 2`, one week.

| | Lab | Production |
|---|---|---|
| Bot turns | 207 | 544 |
| Ended by the bot itself (the token alone — today's `done`) | 127 (61%) | 259 (48%) |
| Posts dropped by the counter | 74 events on 21 posts | 2 |
| Dropped posts that carried substance | 18, all recoverable; 3 more posts were not recoverable | 2 of 2 |
| Bot posts per human prompt (debate windows) | 31 / 18 / 23 | — |

Half the lab drops came from a counter-reset bug since fixed (PR #195);
the rest are genuine overflows on roughly ten posts, all in the
convergence phase of the debate. Two misattributions — a bot denying a
peer had given a figure, a bot accusing a peer of two inconsistent
figures — each trace to a post the counter had removed from the accuser's
context (issue #190). The TTL never acted as the reset in either
deployment; a human post always came first.

A closing-round rehearsal in the lab (n=1): the human posted a six-point
summary to `@all` with the rule "agree: token only; object: name the
point". Four of five bots answered with the token alone; one objected to
one point, which started two further chains of mostly token-only turns.
Thirteen turns, two posts.

Turn lengths in `#nest` over eleven days (615 turns): token-only turns
median 3 s, 82% within 5 s, 96% within 15 s; turns with a reply median
10 s, 21% within 5 s, 66% within 15 s.

## Appendix B — Compared with the plan-based alternative

`multi-agent-enhancement.md` solves the same problem with per-thread
modes, a plan record per request, callbacks and a shared room state.
The two are run through the same simulator before either is built.

| | plan-based | this document |
|---|---|---|
| Who decides an unaddressed turn | a per-thread mode: silent, answer every post, or catch up later | the room classifier, once per post; `snooze` and `mute:` as the only controls |
| Sequencing and dependencies | a plan per request: members, parallel or sequential, admission, timeouts, member states | read from the room when a reply arrives; `FOLLOW_UP` for waiting |
| Rate limit | count per thread since the last human post, persisted; pause with notice; an extend command | a rolling count of recent agent posts and a cooldown, computed from history; no notice; humans and the orchestrator exempt |
| Loops | paused at a plan boundary until a human, an extension or a timeout | slowed, not stopped; ended by `done`, `mute:`, `snooze` or a human |
| A bad actor | the thread pauses for everyone | `mute: @x` by the orchestrator or a human |
| Who is told what happened | plan-end notices, callbacks, stop reports | nobody; the room is the record |
| Replies | one thread per turn | any thread the turn was delivered |
| Persisted state | cursor, rate limit, mode, orchestrator, plan records in a shared room state; posts deduplicated into room events | the cursor, the threads' settings, the scheduled jobs |
| Several gateways in one room | non-goal | best effort: a post may draw two answers or none |
| Multiple bots answering one post | serial, each seeing the last | the classifier names few; the rest is noise |
| Classifier | optional, for addressing by name and mode recommendation | required, once per post in an open thread; may add speakers, never remove a named one |
| Rules a reader must hold | plan lifecycle, three modes with switching, pause and its three resets | one question, two controls, one rate limit, one cursor and the threads' settings |
| What it gives up | simplicity; cross-gateway rooms | deadlines and reports for unattended work; aggregation for an orchestrator; determinism of the gate |

## Appendix C — Code references

Where the statements in this document touch today's code.

| Statement | Reference |
|---|---|
| Today's three layers of loop protection | `docs/agent-chain.md` |
| `require_mention` applies to human senders only | `gateway/connectors/rocketchat/normalize.py:197, 213`; `gateway/connectors/mattermost/normalize.py:222` |
| `to:` is computed from every mention in a post | `gateway/connectors/rocketchat/connector.py:1841`; `gateway/connectors/mattermost/connector.py:828` |
| "mention only your intended recipients" is a prompt rule | `gateway/contexts/rc-gateway-context.md:56` |
| One turn at a time per bot per room | `gateway/core/message_processor.py:358` |
| A turn's history is bounded by the post that started it | `Connector.trigger_history_bound`, `gateway/core/connector.py:651` |
| The connector history fetch; Mattermost fetches one page | `Connector.fetch_room_history`, `gateway/core/connector.py:672`; `gateway/connectors/mattermost/rest.py:402-409` |
| Replay of posts missed while offline | `Connector.replay_room_since`, `gateway/core/connector.py:629` |
| A connector drops its own account's messages | `gateway/connectors/rocketchat/normalize.py:156-158` |
| A sender on no user list is rejected | `gateway/connectors/rocketchat/normalize.py:168` |
| The agent list | `agent_usernames`, `gateway/connectors/rocketchat/config.py:97` |
| The scheduler, and injection without a thread | `gateway/core/scheduler.py`; `SessionManager.inject_message(room_id, text)`, `gateway/core/session_manager.py:1556` |
| Backend turn timeout, shared by all backends | `agents.timeout`, `gateway/core/config.py:132` |
| Outbound posting retries and chunking | `gateway/connectors/rocketchat/outbound.py:31`; `gateway/connectors/mattermost/connector.py:173` |
