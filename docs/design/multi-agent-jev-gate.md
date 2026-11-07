# Rooms with several agents: a classifier decides who speaks

This design asks a small classifier one question about every post in a
room: *who in this room should speak now?* Otherwise every post goes to
every bot, in order, with nothing dropped. The gateway keeps no plan, no
record of who owes whom an answer, and no state that two bots share. A
plan-based alternative is compared in Appendix B. Code references are
collected in Appendix C.

---

## 1. Problem

### 1.1 Current behaviour

Three layers protect a room with several bots from runaway exchanges:

- an agent may answer with the token `<end-of-agent-chain>`, and then
  nothing is posted;
- each bot counts the posts it receives from each sender in a room, and
  drops every post past `max_turns` from that sender;
- those counters clear after `ttl_seconds` of silence.

Below those layers:

- the `require_mention` setting makes a bot answer only humans who
  mention it. Posts written by other bots are not checked and reach every
  bot in the room;
- a bot's `to:` field lists every @-mention in a post;
- the rule that an agent mentions only its intended recipients exists in
  the agent prompt alone.

Appendix A reports measurements from two rooms: a test room with five
bots over one afternoon, and a production room with three bots and one
human over one week.

### 1.2 The problems

1. **Dropped posts carry substance.** Every recoverable post the counter
   dropped carried substance, and the drops fell on the convergence phase
   of a debate. The counter cannot tell a loop from a conclusion, so it
   cut where the conversation was ending.
2. **Each bot sees a different room.** The counter drops rather than
   delivers, so each bot's view of the room diverges by whatever its own
   counter removed. Two mutual "corrections" between bots traced to posts
   the counter had removed from the correcting bot's context (issue
   #190).
3. **Every bot answers every bot.** A post written by a bot bypasses the
   mention gate, so it reaches every bot in the room whether or not it
   addresses them. Each human prompt in the test room produced about 25 bot
   posts.
4. **Mentioning is not addressing.** A reply that quotes a peer ("@H, I
   agree with @b's point") lists `b` in `to:` as well as `H`, and `b`
   gets a turn it was not asked for. Nothing enforces the prompt rule
   that an agent mentions only its intended recipients.
5. **`@all` fans out blindly.** A `@all` prompt produced four
   near-identical replies in seven seconds. No bot saw the others'
   answers before writing its own.
6. **The counter is the wrong tool.** It is a rate limiter being used as
   a convergence mechanism. Bots already stop on their own in half to two
   thirds of their turns. What the room lacks is delivery without holes
   and a way to decide who should speak, not a harder stop.

---

## 2. Terms

Each term uses only the terms above it.

| Term | Meaning |
|---|---|
| **post** | One message on the platform, written by a human or by a bot. |
| **bot** | An agent's presence on a chat server, as `CONTEXT.md` defines it. Inside the gateway, a bot's presence in one room is a *watcher*. The cursor and backlog below are per bot per room. |
| **thread** | A platform thread, or the room's top-level posts taken together as one thread. The settings and the rate limit apply per thread. |
| **addressed** | A post is addressed to a bot when it mentions it. In a human's post: a platform mention (`@a`), `@all` or a room-wide mention. In a bot's post: only the mentions the platform records for its canonical form (`REPLY_ENVELOPE`). A bot named without its `@` is not addressed. |
| **cursor** | Per bot, per room: the last post delivered to that bot's session. The one piece of delivery state (`CURSOR`). Not the connector's own `watermark`, which records what the connector has passed on, not what a session has seen. |
| **backlog** | What is waiting for a bot: the room's posts after its cursor (on the platform, not yet delivered to its session) and the prompt of any scheduled job of its own that fired while it was muted. Grouped by thread where a thread's size or age matters. |
| **classifier** | A small model asked once per post which bots in the room should speak now (`WHO_SPEAKS`). It can add bots to those the post addresses. It never removes one, and never writes text. |
| **rate limit** | Per thread: a bot's turn does not start while the thread's **recent agent posts** (those bots wrote after the thread's newest human post, within `rate_limit_seconds`) number `rate_limit_posts` or more, or total `rate_limit_words` words or more. A limit set to 0 is off (`RATE_LIMIT`). Humans and the orchestrator are neither counted nor held. |
| **cooldown** | Per bot per thread: the least gap, `cooldown_seconds`, between one bot's own posts in a thread. A turn does not start for a bot that posted there more recently than that (`RATE_LIMIT`). Independent of the rate limit. |
| **sweeper** | A periodic pass, per bot, over the bot's backlog. It starts the turns the rate limit or a busy bot held back, and gives a bot that has not been asked in a while a look at what it missed (`CATCH_UP`). |
| **scheduled job** | A reminder a bot sets for itself through the gateway's existing scheduler ("check on a's report in 30 minutes"). The job fires at the set time and starts a turn for that bot with no post behind it. If the bot is muted there, the job's prompt waits in its backlog instead (`FOLLOW_UP`). |
| **turn** | One run of a bot's backend. It is started by a post that addresses the bot, by the classifier, by the sweeper, or by a scheduled job firing. It is told which thread started it, receives its backlog, and may reply in any thread it was delivered. Each of its replies posts text, sets control words, or says `done` for a thread. A turn whose replies are all `done` posts nothing. |
| **orchestrator** | The bot a human has designated for a thread with `orchestrator: @bot`. The rate limit does not apply to it or to the bots it addresses, and its control words are honoured. A convenience for meetings, not part of the ordinary path. |
| **muted** | A bot silenced in a thread by `mute: @bot`. A muted bot gets a turn there only from a human's or the orchestrator's mention. The classifier does not pick it and the sweeper skips it. `mute: @all` is shorthand for naming every bot on the agent list except the orchestrator, and is how an orchestrator runs a meeting with exactly the speakers it chooses. `mute: @bot` is for one bot that will not stop. |
| **reply envelope** | The structured form of a turn's output: a list of replies, each in one thread, carrying text to post, control words, or `done` (`REPLY_ENVELOPE`). |
| **control word** | `limit:`, `orchestrator:`, `mute:`, `unmute:`, written as text in a post. A human types it. A bot writes it in the `control` field of its reply envelope and the gateway posts it as text. The gateway applies it only when an authorised sender wrote it (`AUTHORITY`). From anyone else the post is delivered unchanged and nothing is applied. |
| **settings** | A thread's current control words (orchestrator, limit, mutes), each with the post it came from, persisted from the thread's first control word until they expire (`SETTINGS`). |
| **agent list** | The usernames the gateway treats as agents (`agent_usernames` in configuration), with each one's profile (`AGENT_LIST`). |

Parameters:

A count or duration set to 0 is off.

| Parameter | Scope | Meaning | Defined in |
|---|---|---|---|
| `rate_limit_posts` | room, or per thread with `limit: N posts` | Recent agent posts at which the thread is rate-limited. | `RATE_LIMIT` |
| `rate_limit_words` | room, or per thread with `limit: N words` | Words in the recent agent posts at which the thread is rate-limited. | `RATE_LIMIT` |
| `rate_limit_seconds` | room | How far back a post counts as recent. | `RATE_LIMIT` |
| `cooldown_seconds` | room | Least gap between one bot's own posts in a thread. | `RATE_LIMIT` |
| `catch_up_post_threshold` | room | Undelivered posts in a thread at which the sweeper gives the bot a catch-up turn. | `CATCH_UP` |
| `catch_up_age_seconds` | room | Age of the oldest undelivered post at which the sweeper gives the bot a catch-up turn. | `CATCH_UP` |
| `control_ttl_days` | room | Days of silence in a thread after which its settings are dropped. | `SETTINGS` |
| `classifier_posts` | room | Most recent posts of the thread shown to the classifier. | `WHO_SPEAKS` |
| `classifier_timeout_seconds` | room | How long one classifier call may take. | `WHO_SPEAKS` |
| `classifier_retries` | room | Classifier calls retried after a timeout or error before the fallback applies. | `WHO_SPEAKS` |
| `classifier_fallback` | room | Who speaks when the classifier fails: `addressed-only`, `everyone` or `addressed-or-everyone`. | `WHO_SPEAKS` |
| `classifier_decision_ttl_seconds` | room | How long a classifier decision is kept for a bot whose turn was held. | `WHO_SPEAKS` |
| `follow_up_retry_cap` | room | Retries of a failed scheduled-job turn before a notice is posted and the job stops. | `FOLLOW_UP` |

No other parameter is set per thread.

---

## 3. Principles

1. **Who speaks is read from the room, the way a person reads it.** For
   every post, the classifier reads the thread and the agent list and
   picks the bots that should speak now: the ones the post addresses,
   plus any it adds. None, one or several. When a human writes "a does
   X, then b does Y", the gateway schedules nothing. `b` reads the
   request and waits, and speaks when `a` reports that X is done,
   either because `a`'s report addresses `b`, or because the classifier
   reads the thread and picks `b`.
2. **A mention is a request to speak.**
   - A human's or the orchestrator's mention always gives the bot a
     turn, rate-limited or muted or not.
   - A bot's mention of another bot gives it a turn, delayed by the rate
     limit if need be, unless the addressed bot is muted. In a meeting
     (`mute: @all`) that leaves only mentions of the orchestrator, which
     is how bots report to it.
   - The classifier's answer is never used to drop a bot the post addresses.
3. **Every bot eventually sees everything.** One cursor per bot per
   room. A turn is given everything after the cursor, from every thread,
   and may reply in any thread it was given. A turn that is held back
   drops nothing, because the posts wait in the backlog.
4. **The platform is the only shared medium**, apart from the in-memory
   classifier decisions one gateway holds for its own bots. The gateway
   computes the rate limit from the posts it sees in the room. Control
   words are posts in the room. The orchestrator is whoever a post
   designated. No two bots share persisted gateway state, so bots run
   by different gateways coexist in one room, best effort. Two gateways
   cannot confer, so a post may draw two answers or none (§10). The
   gateway persists three kinds of state per room: a cursor per bot,
   the threads' settings, and the scheduled jobs.
5. **The gateway knows nothing about collaboration. It enforces a few
   basic rules and nothing else.** Mechanically, with no model involved,
   so no bot can get around them, the gateway:
   - delivers every post to every bot, in order, dropping nothing
     (`CURSOR`, `CATCH_UP`);
   - starts a bot's turn only when the turn eligibility table allows it, and
     holds the turn while the thread is rate-limited, the bot is in
     cooldown, or the bot already has a turn running (`TURN_ELIGIBILITY`, `MUTE`,
     `RATE_LIMIT`, `ONE_TURN`);
   - accepts a control word only from a human with the owner role or
     from the thread's orchestrator (`AUTHORITY`, `SETTINGS`);
   - posts a reply only in a thread the turn may post in, rewritten so
     that its mentions are exactly its recipients (`REPLY_ENVELOPE`);
   - fires a scheduled job at its time, in its thread (`FOLLOW_UP`).

   It keeps no plan, no record of who owes whom an answer, and no notion
   of a meeting. It never judges what a bot says. How bots collaborate
   (a brainstorm, a handoff, a follow-up, a deadline) is a skill running
   inside a bot, using those rules.

### Non-goals

- **Guarantees about work.** No plan timeouts, no per-bot reports, no
  "who did what" notice. The room is the report. A bot that is waiting on
  another bot tracks that itself (`FOLLOW_UP`).
- **Hidden information.** The room is the unit of visibility.
- **Convergence by the gateway.** No judgement of whether a bot is
  right. A loop is slowed, not ended.
- **Fairness between bots.** A bot that talks a lot uses the thread's
  allowance. Others wait, as in any room, until someone mutes it.
- **Scheduling by the gateway.** Bots addressed by one post start in
  parallel. The gateway orders nothing and second-guesses nothing. A turn
  that could not start runs later only when a sweeper pass finds it
  eligible, with everything posted meanwhile in its backlog, and the bot
  judges whether there is still something to say. Duplicate answers are
  the classifier's to avoid by picking fewer bots, and are noise when it
  does not.

---

## 4. A short example

Room `#team`:

- bots `a`, `b`, `c`;
- human `H`;
- `rate_limit_posts` 6 and `rate_limit_words` 600 per
  `rate_limit_seconds` 300, per thread.

Every reply below is a line or two, so the post count is what binds.

1. `H: @a, can you check the nginx log? When it's done, @b, restart the
   backend.` The post addresses `a` and `b`, so each gets a turn,
   started at once.
   The classifier is asked about the post at the same time and adds
   nobody, so `c` gets no turn. The post is now in `c`'s backlog. `b`
   reads "when it's done", finds nothing to say yet (`done`, which posts
   nothing), and sets itself a scheduled job for 20 minutes' time
   (`FOLLOW_UP`).
2. `a` replies `to: [H, b]`: `@H @b 502 burst at 09:12, log is clean
   now. b, go ahead.` The reply addresses `b`, so `b` gets a turn; it
   receives `H`'s post and `a`'s. `b` restarts the backend and replies `to: [H, a]`.
3. `b`'s reply addresses `a`, so `a` gets a turn. The hint says a reply that only
   acknowledges is `done`, and `a` answers `done`. Two recent agent posts.
4. The sweeper finds `c` with three undelivered posts and gives `c` one
   catch-up turn. `c` reads them and answers `done`. Still two posts.
5. `H: @all, same as last week's incident?` `@all` addresses everyone:
   three turns in parallel, each told in its hint that the others were
   addressed too. `a` answers. `b` starts a little later, sees `a`'s reply in
   its backlog and adds one line. `c` answers `done`. Four recent agent
   posts.
6. `a` and `b` exchange one more pair: six posts, so the rate limit is
   reached. `b`'s next turn does not start. Its post waits in the
   backlog. `H: enough, thanks` addresses nobody and the classifier
   picks nobody. It is a human post, though, so only agent posts after
   it count now, and there are none. At the next sweeper pass `b`'s held turn starts. `b`
   reads "enough, thanks" and answers `done`. Had `H` said nothing, the
   turn would have waited until enough of the six posts were older than
   300 s.

---

## 5. Mechanisms

### Order of handling

When a post arrives, the gateway runs these steps for each bot it runs
in that room:

1. **Control words.** If the sender is authorised (`AUTHORITY`) and the
   post contains `limit:`, `orchestrator:`, `mute:` or `unmute:`, the
   gateway updates the thread's settings. If the post contains nothing
   but control words, handling stops here.
2. **Addressed?** If the post addresses the bot, the gateway looks up
   the turn eligibility table. The table is a fixed table in this
   document: its rows are who or what started the turn, its columns are
   whether the bot is muted in the thread, and each cell says whether
   the bot gets a turn. For example, a human's mention always gives the
   bot a turn, even when it is muted. Another bot's mention gives a
   muted bot nothing.
3. **Muted?** If the post does not address the bot and the bot is muted
   in the thread, the bot gets no turn. The post stays in the bot's
   backlog. (`MUTE`)
4. **Who speaks?** For the bots that are not muted, the gateway asks the
   classifier once per post: given this post, and given which bots it
   addresses, which other bots should speak? Each bot the classifier
   names gets a turn. Every other bot gets no turn from this post. The
   post stays in its backlog. (`WHO_SPEAKS`)
5. **Allowed now?** A turn that step 2 or step 4 granted still waits
   while the thread is rate-limited or the bot is in cooldown, unless
   the turn's source is exempt. The turn eligibility table and
   `RATE_LIMIT` list the exemptions. While the turn waits, the gateway
   stores nothing new, because the post is already in the bot's backlog
   and the classifier's decision stays in the decision cache. The sweeper asks
   again at its next pass. (`RATE_LIMIT`, `CATCH_UP`)
6. **One at a time.** A bot runs one turn at a time in a room. A second
   turn for the same bot waits until the first ends. (`ONE_TURN`)
7. **Context.** The gateway gives the turn every post after the bot's
   cursor and before the post that started the turn, labelled by
   thread, and then that post. A turn the sweeper or a scheduled job
   started gets every post after the cursor up to a high-water mark
   instead. (`CURSOR`)
8. **Reply.** The gateway parses the reply envelope. It posts the reply
   for the thread that started the turn, in canonical form. It posts a
   reply for another delivered thread only if that thread allows it.
   A `done` reply posts nothing. (`REPLY_ENVELOPE`)

The sweeper (`CATCH_UP`) and scheduled jobs (`FOLLOW_UP`) start turns on
their own timing, outside this order.

### TURN_ELIGIBILITY: who gets a turn

- *Scope*: every post, every bot, human and bot senders alike. There is
  no exception for bot senders.
- *Addressed*:
  - A human's post addresses a bot by platform mention (`@a`), `@all`
    or a room-wide mention.
  - A bot's post addresses a bot only through the mentions the platform
    records for its canonical form (`REPLY_ENVELOPE`).
  - A 1:1 DM addresses its bot by definition.
  - The gateway matches only the `@` form. A bot named without it ("a,
    can you check?") is not addressed; noticing that is the
    classifier's job (`WHO_SPEAKS`).
- *Not addressed*: `MUTE` decides first, then `WHO_SPEAKS`. Addressing
  a bot puts it among the speakers for sure. The classifier can add
  other bots but cannot remove an addressed one.
- *The turn eligibility table*: whether a turn's source (who or what
  started it) may start a turn for a bot depends only on whether the
  bot is muted in the thread. This table is the only rule. "Under the
  rate limit" means that the turn also waits for `RATE_LIMIT`.

  | source of the turn | bot not muted | bot muted there |
  |---|---|---|
  | a human's mention | yes | yes |
  | the orchestrator's mention | yes | yes |
  | another bot's mention | yes, under the rate limit | no |
  | the classifier (`WHO_SPEAKS`) | yes, under the rate limit | not asked about it |
  | the sweeper (`CATCH_UP`) | yes, under the rate limit | no |
  | the bot's own scheduled job (`FOLLOW_UP`) | yes | no turn now. The job's prompt goes into the bot's backlog for that thread and starts once the bot is unmuted. |

  Three notes on the table:
  - The orchestrator's own turns start regardless of the rate limit.
  - `mute: @all` resolves to every bot but the orchestrator, so in a
    meeting the bots' reports reach it.
  - A human may still `mute:` the orchestrator by name. Then only a
    human's mention wakes it.

### WHO_SPEAKS: the room classifier

- *What the gateway gives the classifier*, and nothing else:
  - the new post, with the bots it addresses marked;
  - the thread's most recent `classifier_posts` posts, newest first, up
    to a token budget. When they do not all fit, the gateway leaves out
    the oldest and tells the classifier how many it left out;
  - the agent list. For each bot:
    - its username, display name and nickname, so that the classifier
      recognises a bot named in prose without its `@`;
    - its profile, in free text: who it is, what it does, when it wants
      to be asked ("answers every post from `a`", "only when asked",
      "anything about the build"). This document assumes the profile
      reaches the classifier. Where it lives is open (§9).

  The gateway tells the classifier nothing about which bots are busy or
  behind. That is the concern of `RATE_LIMIT` and `ONE_TURN`, not of
  the classifier.
- *The question*: which bots should speak now?
- *The answer*: two lists of usernames, each entry with one line of
  reason. Either list may be empty.
  - **add**: bots that should speak although the post does not address
    them. "@bob, how is the market?" may add the bot that gives the
    market update. "a, can you check?" may add `a`, named without its
    `@`.
  - **remove**: bots the post addresses that, in the classifier's
    judgement, need not speak. The gateway records this list and does
    nothing with it, because an addressed bot always speaks. The list measures
    how well humans and bots use `@`: how often a mention goes to a bot
    that had no reason to answer (§8).
- *Speakers*: the bots the post addresses plus the classifier's **add**
  list.
- *Addressed bots do not wait*: a turn for a bot the post addresses
  starts as soon as the post arrives. The classifier runs alongside,
  and the bots it adds start when its answer lands.
- *One call per post, per gateway*:
  - The gateway asks about the bots that are not muted in the thread.
    It asks nothing for a DM, for a control-only post, or in a thread
    where every bot is muted.
  - Every bot's connector receives its own copy of the post. The
    gateway keeps an in-memory map `(connector, room, post id) →
    decision in flight | decision`, and every local copy reads the same
    entry. A copy for a bot the post does not address waits for the
    entry. A copy for an addressed bot does not. So the gateway asks
    the question once, and each local bot gets the same answer.
  - An entry lives `classifier_decision_ttl_seconds` and is the only
    memory of the choice. When the rate limit or the bot's own busyness
    held a chosen bot's turn, a later sweeper pass starts the turn from
    this entry (`CATCH_UP`), so the TTL is several
    `rate_limit_seconds`. A choice that outlives its entry falls back
    to catch-up, the floor under everything.
  - Another gateway asks its own classifier and may answer differently.
- *Effect*: every speaker gets a turn, through `RATE_LIMIT` and
  `ONE_TURN`, in parallel. A bot not among the speakers gets no turn
  from this post. The post stays in its backlog, the bot's next turn
  delivers it, and `CATCH_UP` gives the bot its own look later. The gateway asks the classifier to
  pick few: one bot that can answer, not everyone who could.
- *A decision belongs to its post*: it makes a bot eligible only while
  that post is still in the bot's backlog. The gateway checks this when
  the result lands and again when the sweeper reads the cache. When a
  result arrives after a turn that started meanwhile has delivered the
  post, the result starts nothing, whatever else the backlog holds. The
  gateway records it with its reason.
- *Failure*: a call has `classifier_timeout_seconds`, and the gateway
  retries it up to `classifier_retries` times. After that,
  `classifier_fallback` says which bots speak, by who wrote the post:

  | `classifier_fallback` | a human's post | a bot's post |
  |---|---|---|
  | `addressed-only` | the bots it addresses | the bots it addresses |
  | `everyone` | every bot | every bot |
  | `addressed-or-everyone` | the bots it addresses | every bot (today's behaviour) |

  - The fallback only replaces the classifier's answer. Mute, the rate
    limit and cooldown apply to the bots it names exactly as they apply
    to bots the classifier names (`MUTE`, `RATE_LIMIT`).
  - The gateway records each fallback decision with its reason and
    shows it in `STATUS`.
  - Processing of the post never waits on the classifier beyond its
    retries. `CATCH_UP` does not depend on it.
  - The simulator's failure runs choose the default (§8.1).
- *Limits*: the classifier judges relevance and timing from the text.
  It cannot know what a bot knows (that a figure is wrong, that the bot
  has the file), so it under-picks bots for corrections and expertise.
  That is why `CATCH_UP` exists, and why this design tunes it higher
  than a design with per-thread modes would need.
- *The classifier never*: writes text, starts a turn by itself, removes
  a bot the post addresses, orders the bots it picks, or reads anything
  but the room.
- *Logged*: the gateway logs every decision (both lists, their reasons,
  and any fallback) for `STATUS` and §8.

### MUTE

- *`mute: @x` on a thread*: `x` gets a turn there only from a human's
  or the orchestrator's mention. While `x` is muted:
  - the classifier does not pick it;
  - the sweeper skips it;
  - other bots' mentions of it start nothing;
  - a scheduled job of its own that fires puts its prompt into `x`'s
    backlog instead of starting a turn (`FOLLOW_UP`);
  - whichever turn comes next still delivers the thread's posts to it
    as context.
- *`unmute: @x`* lifts the mute and asks the sweeper for a pass on `x`.
  So whatever waited in `x`'s backlog for the mute to end (a post that
  addresses it, a job's prompt) starts at once, under the rate limit,
  rather than at the next periodic pass.
- *A mute always names its bots.* The settings hold a list of
  usernames per thread, nothing else. Before a `mute` or `unmute`
  takes effect, the gateway **resolves** its names into that list:
  - `all` in a `mute` resolves to every bot on the agent list except
    the orchestrator. `all` in an `unmute` resolves to every bot
    currently muted in the thread;
  - a username resolves to itself if it is a bot in the room: on the
    agent list and a member of the room.

  Who resolves the names depends on who wrote the control word:
  - When a bot (the orchestrator) wrote it, in the `control` field of
    its reply, the gateway resolves the names **before posting**, as
    part of the canonical form (`REPLY_ENVELOPE`). The post on the platform
    then carries the resolved names, and every gateway reads the same
    list. A username that is not a bot in the room, or an empty list,
    makes the reply invalid (`REPLY_ENVELOPE`, *Validation*). An `all`
    that resolves to no bot is not an error: the gateway leaves it out
    of the post. The two cases differ because an empty list is wrong
    as written, while whether `all` names anyone depends on the room's
    state, which the bot does not see.
  - When a human wrote it, nothing can rewrite the post, so each
    gateway resolves the names when it reads the post, against its own
    agent list. The gateway ignores a username that is not a bot in
    the room and shows it in `STATUS`. Two gateways with the same
    agent list reach the same names. With different lists they do not,
    and `AGENT_LIST` reports that difference loudly at startup.
- *A bot that joins the room after `mute: @all` was posted is not
  muted*, because it was not on the list. Muting it is the
  orchestrator's job: its skill sees the newcomer's first post and
  mutes it if the meeting needs it. The same covers a bot whose
  gateway was down when the mute was posted. It speaks once, and the
  orchestrator mutes it again.
- *Two uses*:
  - `mute: @all` plus the orchestrator addressing bots is how an
    orchestrator imposes a structure: who speaks is exactly who it
    addresses, and a bot that addresses another bot starts nothing.
  - `mute: @x` is how a human stops a flooding bot without silencing
    the rest.
- *Mutes are posts*, so every gateway in the room reads them and
  enforces them for the bots it runs (§3, principle 5). They live in
  the thread's settings (`SETTINGS`). `STATUS` shows the names, not the
  shorthand.

### RATE_LIMIT

- *Kept in memory, from the posts the gateway sees.* Per thread, the
  gateway keeps the recent agent posts (time, author, word count) and
  each bot's last post time.
  - When a bot's post arrives, the gateway adds it.
  - When a human's post arrives, the gateway empties the thread's list.
  - The gateway drops entries older than `rate_limit_seconds`.
  - When this gateway's own bot posts, the gateway adds the post once
    the post succeeds. Why: a connector drops its own account's posts,
    so the gateway would otherwise never count them.
  - The gateway fetches nothing to answer "may this turn start": it
    reads the answer from memory.
  - At a watcher's start, the gateway seeds the list from the posts the
    connector's replay delivers anyway. The seed misses posts older
    than the replay window, so the limit is a little loose for the
    first minutes after a restart. This is accepted over a fetch.
  - Every gateway in the room sees the same posts with the same agent
    list, so their counts agree, best effort: API lag and the replay
    window can make two gateways differ for a while.
  - `STATUS` shows the current count and words per thread.
- *Rule*: a turn does not start while any of these holds:
  - the recent agent posts number ≥ `rate_limit_posts`;
  - their words total ≥ `rate_limit_words`;
  - the bot posted in the thread within the last `cooldown_seconds`.

  Each of the three binds on its own, and each is off at 0.
  `cooldown_seconds` is the least gap between one bot's own posts in a
  thread, independent of how much the thread holds. With all three at
  0 nothing holds a turn back.
- *The rule applies to the start of a turn only.* A started turn
  always posts its reply in its own thread, so the count can go over
  by the number of turns already running there, and the words by what
  those turns write. The overshoot in posts is bounded by the room's
  bots, plus one more per gateway across gateways. A reply to another
  thread (`REPLY_ENVELOPE`) checks that thread's rate limit at posting
  time, serialised per thread within one gateway.
- *Words*: the limit is for the people reading the thread. A one-line
  acknowledgement costs a reader seconds where a thousand-word analysis
  costs minutes, and the post count alone cannot tell them apart. A
  word is a run of non-whitespace characters, except that each CJK
  character counts as one word. Nothing else is special, so every
  gateway counts the same.
- *A human's post resets it*: only agent posts after the thread's
  newest human post count. Why: a human who writes in the thread has
  read it. The bots the limit held start at the next sweeper pass.
  With `rate_limit_seconds` set very large, the limit becomes a stop
  that only a human's post clears.
- *Exemptions*, by the turn's source, not by who wrote the post. A bot
  the classifier chose for a human's unaddressed post waits like any
  other.

  | source of the turn | rate limit | cooldown | its posts count | bound |
  |---|---|---|---|---|
  | a human's mention | exempt. The human intervenes and cannot be shut out by noisy bots. | exempt | yes | the human |
  | the orchestrator's mention, or its own turn | exempt | exempt | the orchestrator's posts do not count | `STATUS` shows its recent posts to the owner. A human can `mute:` or re-designate it at any time |
  | a scheduled job firing | exempt | exempt | the firing is not a post. The reply its turn makes counts | `follow_up_retry_cap` and the backoff in `FOLLOW_UP` |

  - Posts written by humans never count. Posts by integration accounts
    configured as owners or guests but not in `agent_usernames` never
    count.
  - The gateway never holds a human's own control words.
  - `STATUS` shows which exemption started each recent turn.
- *No notice*: being rate-limited is silence, as it is for a person who
  cannot get a word in. `STATUS` shows it. `HINT` tells a bot that is
  allowed to speak how much room is left.
- *Loops*: the rate limit slows two bots trading replies to
  `rate_limit_posts` posts or `rate_limit_words` words per
  `rate_limit_seconds`, and `cooldown_seconds` apart. It does not stop
  them. They stop when one answers `done`, when someone mutes one of
  them, or when a human tells them to. This relies on the measured
  rate at which bots stop on their own (§1).
- *Per-thread limits*: `limit: N posts`, `limit: N words`, or both in
  one post, set the thread's `rate_limit_posts` and
  `rate_limit_words`. `limit: 0 posts 0 words` turns the rate limit
  off in the thread.

### AUTHORITY

- The gateway acts on a control word only when one of these wrote it:
  - a human with the owner role;
  - the thread's orchestrator, except for `orchestrator:`, which the
    gateway accepts from an owner-role human only.
- A control word written by anyone else (a guest, a bot that is not the
  orchestrator) changes nothing: the gateway does not apply it, and it
  delivers the post to the bots like any other post, control word
  included, as ordinary text.
- A bot writes control words in the `control` field of its reply
  envelope, and the gateway checks authority there: a `control` from a
  bot that is not the thread's orchestrator is an invalid reply
  (`REPLY_ENVELOPE`), and nothing is posted. A control word a bot
  writes in its `text` instead is posted as text, and every gateway
  reads it like any other post: applied if the sender is the
  orchestrator, ordinary text otherwise.
- A thread has one orchestrator. A later `orchestrator:` replaces it.
  The same bot may be the orchestrator of several threads and is an
  ordinary bot elsewhere.
- The server injects the roles (`CLAUDE.md`, connector prompt prefix).

### SETTINGS

- *What they are*: a thread's current control words (orchestrator,
  `limit:`, the muted bots) are the last control word of each kind
  posted in it. The gateway keeps them as the thread's settings,
  persisted with the cursors, and writes them when a control word
  arrives. A thread gets settings at its first control word. Each value
  carries the post it came from.
- *Expiry*: the gateway removes a thread's settings after
  `control_ttl_days` of silence in the thread (days, not seconds), or
  when the platform archives or deletes the thread. The defaults then
  apply again. Silence is the age of the thread's newest post. The
  gateway notes that time in memory as posts arrive and reads no
  history to check it. A thread muted and then abandoned unmutes on
  its own. `STATUS` lists the threads that hold settings and their
  age.
- *Order*: the gateway applies control words in the server's order of
  their posts, not in arrival order: it applies a control only if its
  post is newer than the post the current value came from. A value
  that returns to its default stays in the settings with its post, so
  `mute: @all`, then `unmute: @all`, then a replay of the `mute: @all`
  leaves the thread unmuted. The gateway computes the rate limit with
  the thread's *current* orchestrator, so re-designating the
  orchestrator changes which past posts count. This is accepted over recording a
  role per post.
- *Two gateways in one room* each keep their own settings from the
  same posts. They agree because the posts are the same, not because
  they talk to each other. The one thing each reads for itself is a
  human's `@all` (`MUTE`). A control word a gateway's own bot posted
  never comes back to it, because a connector drops its own account's
  posts. That is harmless, because the bots it would affect are on
  other gateways, which do receive it.

### ONE_TURN

- A bot runs one turn at a time in a room: one backend session, one
  invocation. When several posts could start a turn for one bot, the
  oldest in the room's event order goes first. The event order is the
  server's order for posts, and a local sequence for sweeps and job
  firings, which have no post. (The simulator numbers every event. The
  gateway's connector abstraction for this order is an implementation
  item, §9.)
- *There is no waiting turn.* A turn that cannot start, because the
  bot is busy or the thread is rate-limited, leaves behind only its
  post in the backlog and, for a classifier choice, the cached
  decision. A bot that becomes free starts nothing by itself. Instead,
  the bot becoming free, and a turn ending, each ask the sweeper for a
  pass on that bot, and the pass reconstructs eligibility from the
  backlog, the turn eligibility table and the cache (`CATCH_UP`).
- The gateway does not second-guess the turn that then runs, because
  the turn has everything posted meanwhile in its backlog, and the bot
  judges whether there is still something to say. A reply someone else
  already gave, or a human's "never mind", is in front of it. `done`
  costs a few seconds.

### CURSOR

- *Scope*: one post id per bot per room, the last post delivered to the
  bot's session. The gateway creates it at the first delivery and
  never removes it.
- *Delivery*, by what started the turn:
  - A new post: the turn receives the room's posts after the cursor and
    before that post, both exclusive, each labelled with its thread,
    and then the post once.
  - The sweeper or a scheduled job: the gateway fixes a **high-water
    mark**, the newest post in the room when the turn starts, and the
    turn receives everything after the cursor through that mark. The
    post that made the bot eligible is the trigger but does not bound
    delivery, so the turn sees everything posted meanwhile, and a paged
    fetch has a fixed end. A post arriving after the mark waits for a
    later turn.
  - A post found at reconnect or startup: delivered like a new post.

  The turn may reply in any thread it was delivered (`REPLY_ENVELOPE`).
  `HINT` gives the thread that started it and the others present.
- *When the gateway saves the cursor*: it writes the new cursor (the
  last post delivered) when the turn ends normally, that is, when
  every reply is `done`, posted, rejected by the reply check, or
  refused by the platform (`REPLY_ENVELOPE`).

  It does not write the cursor, and delivers the same posts again, when
  the backend fails or the reply is still invalid after the one
  re-prompt (`FAILURE_NOTICE`).
- *Contract*:
  - Turn eligibility withholds nothing.
  - The gateway never delivers again the posts of a turn that ended
    normally.
  - After a fetch failure, a failed turn, or a crash before the cursor
    is saved, the gateway delivers the same posts again, and may post
    again a reply the turn had already posted. There is no retry
    counter on
    ordinary turns, so this is possible and not bounded, and it sits
    inside the project's best-effort delivery (`docs/requirements.md`).
  - Delivered backlog is context, so the gateway does not re-execute
    control words and mentions in it.
- *Interface*: the connector history fetch, bounded by post id,
  returning post and thread ids, paging past the first page (Mattermost
  fetches one today).
- *Posts found at reconnect or startup*: the connector's per-room
  replay runs them through the turn eligibility rules like new posts.
  Those rules decide whether a turn starts, never whether a post is
  delivered.
- *Per thread*: the gateway computes backlog size and age per thread
  from the posts after the cursor, for `CATCH_UP` and `STATUS`. It
  stores nothing per thread for it.

### CATCH_UP: the sweeper

- *Scope*: a pass runs for one bot, over the posts not yet delivered to
  it, grouped by thread. It runs on a timer, and on request when the
  bot becomes free or one of its turns ends (`ONE_TURN`). A pass goes
  like this:
  1. At the start, the gateway lists the threads the pass will act on,
     in order.
  2. It takes them one at a time. Before starting a turn for a thread,
     it looks again: does the bot still have undelivered posts in this
     thread?
  3. Often it does not, because the turn started for an earlier thread
     in the same pass delivered the bot's whole room backlog, this
     thread included (`CURSOR`). Then the pass skips the thread. No turn runs
     with nothing to deliver.

  With one cursor per room, one turn usually serves every thread the
  pass listed.
- *Addressed or picked posts first*: a post in the bot's backlog that
  the turn eligibility table allows now starts a turn as soon as the
  rate limit, the cooldown and the bot allow. Such a post is one that
  addresses the bot, or one whose classifier decision picked the bot,
  has not expired, and whose post is still in the backlog. The gateway
  takes the oldest such post as the trigger. It reads the table against
  the thread's state at the pass, so a mention that arrived before a
  `mute:` opens nothing once the bot is muted. This is how a turn that
  the rate limit or a busy bot held gets started: nothing was stored.
  The post was in the backlog all along.
- *Catch-up turns*: otherwise, a thread gets one catch-up turn when all
  of these hold:
  - the bot is not muted there;
  - the thread has ≥ `catch_up_post_threshold` undelivered posts for
    the bot, or its oldest undelivered post is older than
    `catch_up_age_seconds`;
  - the thread is under the rate limit and the bot is out of cooldown.

  The pass takes threads in newest-post order. The first catch-up turn
  delivers the whole backlog through its high-water mark (`CURSOR`),
  so the pass usually skips the rest. The trigger gives the thread. The
  bot reads and decides, and `done` is the common answer. This is the
  bot's own look at what the classifier passed over. No turn starts
  for a bot whose backlog is empty. A scheduled job is the one
  exception, since it has no post of its own.
- *What it covers*: posts not yet delivered. The pass does not revisit
  a post that another thread's turn already delivered, because that
  turn could reply in the post's thread (`REPLY_ENVELOPE`), and the
  bot's session remembers it. "Not now" therefore means: the bot sees the post at its
  next turn, whichever thread starts it, and may answer there and then.

### REPLY_ENVELOPE

The shape of a turn's output, sketched as YAML. The sketch shows the
fields only. The encoding (a fenced JSON block, the whole reply, or a
backend's structured output) is decided in task 1 (§9) and may not be
YAML.

```yaml
replies:
  # Text to post.
  - thread: T1                # omitted: the thread that started the turn
    to: [H, b]
    text: "502 burst at 09:12, log is clean now. b, go ahead."

  # Control words only. No `to`.
  - thread: T2
    control:
      mute: [a, b]            # or: all
      limit: {posts: 20, words: 2000}

  # Control words and text in one post.
  - thread: T1
    to: [a, b, c, d]
    control: {mute: all}
    text: "One post each, independently. Address only me."

  # Nothing to say in this thread. `to` is optional and recorded.
  - thread: T3
    to: [x]
    done: true
```

- *Fields*. The bot returns `replies`, a non-empty list. Each reply
  has:
  - `thread`: one of the threads delivered to this turn. When the bot
    omits it, the gateway uses the thread that started the turn.
  - `to`: who the reply addresses: humans or bots in the room, or
    `all`. The bot never lists itself. Required with `text`, optional
    with `done`, absent otherwise.
  - `text`: the body to post.
  - `control`: control words to set in the thread: `mute` and `unmute`
    (a list of usernames, or `all`), `limit` (`posts`, `words`). There
    is no `orchestrator` key, because only an owner-role human sets
    that (`AUTHORITY`).
  - `done: true`: the bot has nothing to post in this thread. A `to`
    on a `done` says whose requests the bot read and chose not to
    answer. The gateway records it and posts nothing (§8).
  - A reply carries `done`, or `text`, or `control`, or `text` and
    `control` together. The gateway rejects a reply that carries both
    `text` and `done`, because the two fields contradict each other:
    `text` says there is something to post, `done` says there is
    nothing.
  - The gateway reads an output that is only the token
    `<end-of-agent-chain>` as one `done` reply with no `to`. Any other
    text before or after the token makes the output invalid
    (*Validation*).
- *Canonical form*. Before posting, the gateway renders each reply
  into one post, `control` first, then `text`:
  - `control` becomes text, one line per control word (`mute: @a @b`,
    `limit: 20 posts 2000 words`). The gateway resolves the names in a
    `mute` or `unmute` first (`MUTE`). `control` becomes text because
    the platform post is the only medium every gateway and every human
    reads, and humans write control words as text. A `mute: all` or `unmute: all` may
    resolve to no bot, when the orchestrator is the only bot in the
    room or no bot is muted in the thread. Then the gateway leaves that
    `mute` or `unmute` out of the post. If the reply still has `text` or
    another control word, the gateway posts what remains. If nothing
    remains, it posts nothing and treats the reply as `done`.
  - `text` is rewritten so that the post's mentions are exactly its
    `to`:
    - a recipient the text does not mention: the gateway prepends
      `@name`;
    - an `@name` in the text that is not a recipient: the gateway drops
      the `@`;
    - `all`: the gateway replaces it with the platform's room-wide
      mention.

  Why: a human reads the recipients at the head of the post, and every
  gateway reads the platform's mentions and reaches the same set, live,
  after a restart, and from a history fetch alike. Every gateway parses
  every post one way, from the platform, whoever wrote it.
- *The reply check*. The reply in the thread that started the turn
  **always posts**. Why: the turn was started there, and that was the
  only check. A `mute:` that lands mid-turn does not stop it. A reply
  to another delivered thread posts only if that thread allows it:

  | that thread | posts? |
  |---|---|
  | bot not muted, room under the rate limit | yes |
  | bot not muted, rate-limited | no |
  | bot muted there | only if a human or the orchestrator addressed the bot there |

  - The orchestrator is exempt from the rate limit here too.
  - Every posted reply counts in its thread.
  - The gateway does not post a reply that fails the check, and does
    not hold it either. The next turn's `HINT` says which reply was not
    posted and why, and the bot decides whether to say it again.
    `HINT` tells the bot each thread's state before it writes, so this
    is rare.
- *Partial failure*. The gateway attempts every reply once, in order.
  When the platform refuses a reply, the gateway reports it in the
  next turn's `HINT` like a rejected one. The replies already posted
  stay, the turn ends normally, and the gateway saves the cursor
  (`CURSOR`). The gateway retries nothing on the bot's behalf. Why: a
  retry could post a reply twice, and delivery is best effort.
- *Validation*. The gateway returns an invalid output to the session
  once, with the schema error. A second invalid output fails the turn
  (`FAILURE_NOTICE`). An output is invalid when:
  - `replies` is missing or empty;
  - a reply has none of `text`, `control` and `done`, or has both
    `text` and `done`;
  - a reply with `text` has no `to`, or an empty `to`, or addresses
    only the sender;
  - a name in `to` is not a member of the room;
  - a `thread` is one the turn was not delivered;
  - a reply has `control` and the bot is not the thread's
    orchestrator, or `control` holds a key other than `mute`, `unmute`
    and `limit`;
  - a `mute` or `unmute` is an empty list, or names a username that is
    not a bot in the room. An `all` that resolves to no bot is not an
    error (*Canonical form*).
- *What the gateway does not enforce*. There is no record of "requests
  this turn serves", so the gateway cannot check that `to` covers
  everyone who asked. `HINT` asks for it. The simulator counts the
  omissions (§8).

### FAILURE_NOTICE

- *What it is*: when a turn fails (the backend fails, or a reply is
  still invalid after the one re-prompt), the gateway posts a notice
  from the failing bot's account. Every gateway delivers the notice as
  context. It starts no turn. The gateway does not save the cursor, so
  the next turn delivers again the posts the failed turn was given. A
  platform refusing to post a reply is not a failed turn
  (`REPLY_ENVELOPE`, *Partial failure*). A failed turn from a scheduled
  job also returns the occurrence to the scheduler (`FOLLOW_UP`).
- *Identity*: "starts no turn" cannot rest on the notice's text, which
  anyone can type. The notice is a gateway-generated event kind. The
  gateway carries it in platform metadata where the platform has it,
  and the connector recognises it before `TURN_ELIGIBILITY`. Every
  gateway in the room treats it as context only and executes nothing
  from it. In the simulator the event kind is explicit in the
  scenario. How each platform carries it is settled in the lab (§8.1).

### HINT

The gateway injects these into every turn:

- the thread that started the turn, and the other threads delivered;
- whether the bot is muted in each thread;
- how many posts and words each thread's rate limit has left;
- the replies from the last turn that the gateway could not post, and
  why: a reply to another thread that the thread did not allow, or a
  reply the platform refused;
- the instruction:
  - make your point, and do not restate;
  - reply `done` in a thread where you have nothing to add;
  - control words go in `control`, never in `text`;
  - `to` is who the post is for, not who it mentions: include everyone
    who asked you something in this backlog;
  - reply in another delivered thread when something there needs you;
  - `all` only with a reason;
  - if you are waiting on someone, say so and set a scheduled job;
  - if your reply unblocks someone, address them.

### FOLLOW_UP: a skill on the scheduler

- *Skill*: a bot that takes on or waits for work records what the work
  is, for whom, and by when it expects it, judged from the task, and
  schedules a job for the follow-up time. When the job fires, the bot
  reports progress, or asks the other party for status, and schedules
  the next check with a longer gap. Past the time it expected, it says
  so and stops. Estimates are the bot's own. A wrong one costs a check
  too early or too late.
- *Scheduler contract* (changes to the existing scheduler):
  - A job persists the connector, the room, the **thread**, the task
    text and its retry state. Today a job carries a room and a
    message, and the gateway injects it without a thread.
  - A firing inherits the thread.
  - The scheduler clears an occurrence when its turn ends normally
    (`CURSOR`), not when the message is queued, which is today's rule.
  - A failed turn keeps the occurrence. The scheduler retries it with
    bounded exponential backoff, and past `follow_up_retry_cap` retries
    it posts a notice and stops.
  - Clearing an occurrence and saving the cursor are two saves, so
    across a crash an occurrence fires at least once. The skill
    tolerates a repeated firing.
- *Eligibility*: see the turn eligibility table (`TURN_ELIGIBILITY`),
  row "the bot's own scheduled job". The firing itself is not a post
  and counts nothing. The reply its turn makes counts in its thread.
- *While the bot is muted*: a firing does not start a turn. Its prompt
  goes into the bot's backlog for the job's thread, and the sweeper
  starts the turn once the bot is unmuted, at the pass the `unmute:`
  post requests (`MUTE`). A job has at most one firing waiting in a
  backlog. A later firing of the same job replaces it, so a daily job
  whose bot was muted for three days runs once. The gateway keeps no
  deadline for a job, so whether the task is still worth doing is the
  bot's judgement when it finally runs.
- *Time-sensitive jobs*: a meeting's `mute: @all` holds every job of
  every muted bot for as long as the meeting runs, time-sensitive or
  not. A `mute:` of a misbehaving bot does the same, and there it is
  the point. The simulator measures how much this costs in practice
  (§8.1). Marking a job as time-sensitive, okay-to-delay or droppable
  is a scheduler change and out of scope (§10).

### ORCHESTRATOR

- `orchestrator: @bot` on a thread gives the bot:
  - the exemptions in the turn eligibility table and `RATE_LIMIT`, for
    itself and for the bots it addresses;
  - control words that `AUTHORITY` honours, including `mute:` of other
    bots.

  Policy for its turns lives in its skill.
- It gets no special delivery. Bots reply to it by addressing it, and
  each reply is a turn for it. Its skill answers `done` until it has
  what it needs.
- A meeting is `mute: @all` plus the orchestrator addressing bots, all
  at once or one at a time as the skill decides, muting a bot that
  will not stop.

### STATUS

`coop room status <room>` (the room's handle, `<connector>:<room label>`)
shows:

- per thread: the orchestrator, the muted bots, the settings' age, the
  recent agent posts out of `rate_limit_posts` and their words out of
  `rate_limit_words`, and the orchestrator's recent posts;
- per bot: backlog size and age per thread, computed on request, and
  the cooldown remaining;
- the last classifier decisions with reasons, and any fallback
  decisions;
- which exemption started each recent turn;
- the agent list, and bot-like room members not on it.

### AGENT_LIST

- The gateway knows which usernames are agents from configuration
  (`agent_usernames`), which must list every agent in the room,
  including those run by other gateways. For the classifier, the
  gateway adds each bot's display name and nickname, read from the
  platform's user record.
- The connector today rejects a sender on none of its user lists. So an
  agent missing from the agent list is not "treated as a human": the
  connector rejects its posts, and they never reach the turn
  eligibility rules.
- At startup the gateway checks the agent list against the room's
  members where the platform allows, logs a missing or unknown agent
  loudly, and shows both in `STATUS`.
- Two gateways whose agent lists differ for one room do not converge
  on the rate limit or the classifier, and `STATUS` says so.
  Synchronising agent lists between gateways is later work (§10).

---

## 6. Worked examples

### 6.1 A handoff read from the room

`H: @a run the migration; b, when a is done, deploy.` Thread `T`,
`rate_limit_posts` 6, `rate_limit_words` 600.

| # | Event | `a` | `b` | `c` |
|---|---|---|---|---|
| 1 | `H`'s post | addressed → turn | addressed → turn: reads "when a is done", answers `done`, schedules a job for 30 minutes' time | classifier adds nobody → wait |
| 2 | `a: @H @b migration done, 14 tables.` | none | addressed → turn: deploys, replies `to: [H, a]` | classifier: nobody → wait |
| 3 | `b: @H @a deployed, green.` | addressed → turn → `done` | none | classifier: nobody → wait |
| 4 | sweeper: `c` has 3 undelivered posts | none | none | catch-up turn → `done` |

Two agent posts, three `done` turns, one classifier call per post. Had
`a` posted "migration done" to `H` alone, the classifier would have read
"b, when a is done" in the thread and picked `b`. Had `a` said nothing for
30 minutes, `b`'s job would have fired and `b` would have asked `a` for
status.

### 6.2 A brainstorm run by an orchestrator

`H: orchestrator: @chair` then `H: @chair run a brainstorm: seat or
usage pricing?` on a thread. `chair` is the orchestrator.

| # | Event | Turns | Posts |
|---|---|---|---|
| 1 | `chair` replies `control: {mute: all}` with `to: [a, b, c, d]` and text "one post each, independently; address only me". Posted as one post: `mute: @a @b @c @d` then `@a @b @c @d one post each, independently; address only me.` | 0 | 1 (chair, not counted) |
| 2 | four bots, each addressed by the orchestrator → four turns in parallel, none rate-limited. A bot that starts later may see an earlier reply in its backlog. There is no isolation. The mute, not the prompt, enforces "address only me": a bot that addresses another bot starts nothing | 4 | 4 |
| 3 | each reply addresses `chair` → an orchestrator turn per reply. The skill answers `done` three times and synthesises on the fourth | 4 (3 `done`) | 1 (chair, not counted) |
| 4 | `chair: @a @b @c @d objections only, otherwise done.` | 4 | 1 (`d`) |
| 5 | `d`'s objection addresses `chair` → an orchestrator turn: the skill notes it. It also addressed `a`. That opened nothing for `a` (muted). `d` has answered every round at length and off the point, so the orchestrator runs the last round without it: `@a @b @c last round: anything to add?` → three turns, three `done` | 1 + 3 | 1 (chair, not counted) |
| 6 | `chair` replies `control: {unmute: all}` with `to: [H]` and the closing text. Posted as `unmute: @a @b @c @d` then `@H <close>`. | 0 | 1 (chair, not counted) |

Eleven bot turns, five orchestrator turns after its first post (three
`done`), five counted posts. The gateway keeps no record of the round.
The orchestrator's skill does.

### 6.3 One turn, two threads

Thread `A` is an incident thread. Thread `B` is unrelated. `b` posts in
`A`, addressing nobody: "the 09:12 burst was 404s, not 502s", a correction
of a figure `x` gave. The classifier picks nobody. `x`'s cursor stays
before it. Later `H` posts in `B`: `@x is staging green?`

- `x`'s turn receives `b`'s correction, labelled `A`, and `H`'s question,
  labelled `B`. Its envelope carries two replies: in `B`, `to: [H]`,
  "green". In `A`, `to: [b]`, "right, 404s, my mistake." Each posts in
  its thread and counts there.
- Had `x` been muted in `A`, or `A` rate-limited, the `A` reply would not have
  posted. `x`'s next turn would be told so, and `x` would decide whether
  to say it when next addressed in `A`.

### 6.4 Two gateways, one room

`a` runs on gateway 1, `b` on gateway 2, both agent lists hold both. `H:
anyone know why the build is red?`

- Each gateway asks its classifier once and acts on its own bot only.
  When both pick `a` and `b`, there are two replies. When gateway 1
  picks `b` and gateway 2 picks `a`, neither runs the bot it picked, and
  nobody answers until `H` addresses one (§10).
- `a: @b your change touched the build file. Did you bump the version?`
  The reply addresses `b`, so `b` gets a turn on gateway 2. `b` replies `to: [a, H]`. Each
  gateway counts both posts toward the rate limit: gateway 1 received
  `b`'s post and posted `a`'s. Gateway 2 the reverse.
- `H: mute: @all`: each gateway resolves it against the same agent list
  and writes the same names. Only a human's or the orchestrator's
  mention opens a turn in the thread from here.

---

## 7. Scenario validation

| Scenario | Control | How it runs | Supported |
|---|---|---|---|
| One room, three scenarios in a day | `orchestrator:` + `mute: @all` for the meeting, `unmute: @all` after | Casual talk by classifier. A meeting with every bot muted but the ones the orchestrator addresses. Casual talk again. | yes |
| One thread, three scenarios | same, on the thread | A debugging thread becomes a vote under an orchestrator and returns. Nothing is "running" to finish under the old rules. | yes |
| Casual conversation (production `#nest`) | none | Addressed bots speak. The classifier adds a bot when the post is for it. Corrections arrive through catch-up or as a second reply from a turn in another thread. | yes, catch-up tuned higher |
| Brainstorm (test room, five bots) | orchestrator skill, `mute: @all` | §6.2. | yes, more orchestrator turns, no record |
| Status / decision / roadmap meeting | orchestrator skill, `mute: @all` | The orchestrator addresses one bot at a time for reports, all at once for a vote by `done`. Mutes a bot that will not stop. | yes |
| Debugging session | `limit: 20 posts` | Most posts are for everyone. The classifier picks the bot or two best placed. The rest see the exchange through catch-up and reply when they have something. | yes |
| Divide and collaborate | H or a PM skill | `@a @b` both addressed. Dependencies by addressing. Each bot tracks its own task (`FOLLOW_UP`). No deadline and no report to `H`. `H` reads the room. | yes, without the report |
| Support / triage channel | none | The answering bot addresses a specialist. | yes |
| Scheduled or agent-only room | none | No human: the rate limit and cooldown slow an exchange and nothing ends it but `done` or a `mute:`. | yes, needs a cap or a mute, as any design |
| Code review room | none | The reviewer addresses the test bot. | yes |
| Critic or pairing | the critic's profile says "answer every post from `a`" | The classifier picks the critic on every post from `a`. | yes, per-bot profile |
| Unattended overnight task | none | Handoffs by addressing. Each bot's `FOLLOW_UP` checks on what it waits for. | yes, guarantees are the skill's |
| Role-play with a game master | GM skill, `mute: @all` | Players speak when addressed. The GM narrates after each. | yes, except hidden information |
| Monitor room (CI, webhooks) | none | The integration account is configured as a guest: its mention starts a bot, other bots by classifier, nothing it posts counts. | yes |

Single-bot channels and 1:1 DMs are unchanged, because the bot is
addressed by definition or by `require_mention`, and the rate limit
counts its own posts. Anonymous votes are not supported (§10).

---

## 8. Validation and sequence

### 8.1 The simulator comes first

Before any gateway code, task 0 implements §5 as a standalone simulator
(`tools/roomsim/`, outside the gateway) and runs the scenarios in §7
through it, to see the conversations this design produces, odd corners
included, before paying for an implementation.

- **Mechanism**: a deterministic script implementing §5 as written:
  the turn eligibility table, the rate limit, the sweeper, the cursor, the reply
  envelope. It is not a language model tracing the rules, because a
  model's trace would hide whether a wrong step was a hole in the spec
  or an error in the trace. Where the script and the text disagree, one of
  them is wrong and is fixed.
- **Bots**: each bot turn is one real `claude -p` (or OpenCode) call with
  the bot's persona from the test room's configuration, in one session
  per bot per room as the gateway keeps it. Each turn appends what the
  cursor delivers and nothing else. An orchestrator is the same, loaded
  with its skill. Its control words are parsed by the script.
- **Humans**: scripted lines in the scenario file, placed at a step. A
  model playing the human is allowed and labelled as such.
- **Output**: a transcript with the script's annotations before each
  event (the turn, its thread and backlog, the recent-post count, every
  classifier decision, every sweep) and a tally of turns, posts, words,
  tokens and context size per bot. People review the transcript for
  exchanges that read wrong. They fix what reads wrong in §5 and run the
  scenario again.
- **Two kinds of run**: a run with live models is for behaviour: what
  the bots do with the rules, over several runs, read by people. It is
  not expected to repeat. A run's inbound events and bot outputs can be
  saved as a recorded run, and replaying it is deterministic. The
  mechanism tests in §8.2 are written against the simulator first and
  carried to the gateway unchanged, and on a recorded run the gateway
  must produce the same annotation sequence as the simulator.
- **What it cannot show**: platform behaviour (threads, the mention
  array each platform computes from a canonical post, history paging,
  webhook identities, how a failure notice is carried) and session
  compaction. Those belong to the lab: the test server with the bots in
  §1's measurements.

Each scenario runs in three configurations, so that the classifier's
value and its failure cost are both measured against the same
conversations:

- **classifier on every post**, with `classifier_retries` and a forced
  failure rate injected, recording the raw failure rate, how often a
  retry recovered, and how many retries it took;
- **no classifier**: each `classifier_fallback` alone, as its own run;
- **classifier with each fallback** under the injected failures, to see
  the experience a room would actually get when the classifier is down.

It records two kinds of thing, kept apart:

**Decision quality**: people label the classifier's exact input
snapshots, blind: given this thread and agent list at this moment, who
should have spoken? Agreement with those labels, per model and history
size, is the measure of the classifier. Nothing observed later is.

**Operational outcomes**, per run:

- picked by the classifier → `done` rate, and not picked → later
  catch-up with a reply rate. These are costs and delays, not labels;
- the classifier's **remove** list: addressed bots it judged need not
  speak, with the reason and what that bot's turn then did (`done`, a
  reply, a handoff): how well humans and bots aim their `@`, and the
  evidence for whether the classifier should ever override a mention;
- bots the classifier added to an addressed post, and what they did;
- corrections: how long after the correcting post the corrected bot
  acknowledged it, and through which path (addressed, picked, catch-up,
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
- jobs held by a mute, and for how long;
- posts per human prompt, latency per post, classifier calls and tokens
  per post.

### 8.2 Sequence

| # | Task | Builds |
|---|---|---|
| 0 | **Simulator.** §8.1: the script, a scenario file for every §7 row, persona files from the test room, the classifier in shadow (logged, not acted on) and acted on. | none |
| 1 | **Deliver, do not drop.** Cursor per bot, history by post id, the envelope with several replies and the canonical form, and the settings' persistence. | CURSOR, REPLY_ENVELOPE, FAILURE_NOTICE, SETTINGS |
| 2 | **Turn eligibility for bot senders, mute.** Posts from bots go through the turn eligibility rules. `mute:`, `unmute:` and `orchestrator:` as posts. | TURN_ELIGIBILITY, MUTE, AUTHORITY |
| 3 | **Rate limit.** Recent-post count and cooldown kept in memory, own posts included, the exemptions, and agent-list validation. | RATE_LIMIT, ORCHESTRATOR, AGENT_LIST, HINT |
| 4 | **Sweeper, one turn, status.** Addressed or picked posts first, then catch-up by threshold and age. | CATCH_UP, ONE_TURN, STATUS |
| 5 | **Scheduler contract and follow-up.** Jobs carry the thread, are cleared on a normal end, and retry with backoff and a cap. The `FOLLOW_UP` skill. | FOLLOW_UP |
| 6 | **Classifier in shadow**, then acted on. | WHO_SPEAKS |
| 7 | **Skills.** The brainstorm orchestrator, and a PM skill for fan-out with follow-ups. | none (policy) |

Dependencies:

- 0 first;
- 1 before everything;
- 2 and 3 independent;
- 4 needs 1;
- 5 needs 1;
- 6 needs 2, 3, 4;
- 7 needs 5 and 6.

Mechanism tests, by task, written against the simulator first:

- **0**: the §6 tables reproduced.
- **1**:
  - the cursor is saved at each normal end and not at a failed one,
    with a failure injected at every boundary;
  - a two-thread turn posts in both, each counted in its own;
  - a reply to a thread that does not allow it is not posted and is
    reported next turn;
  - a two-reply envelope whose second post the platform refuses keeps
    the first, reports the second, saves the cursor;
  - the reply in the starting thread posts in a rate-limited thread, a
    second-thread reply does not;
  - mentions equal `to`;
  - a reply with both `text` and `done`, raw output of text plus the
    token, an empty `replies`, and a `control` from a bot that is not
    the orchestrator are each invalid;
  - a `control` reply without `to` is valid;
  - a `done` reply's `to` is recorded and nothing is posted;
  - a `control` with `text` renders as one post, control words first;
  - settings survive restart and lapse at `control_ttl_days`;
  - a bot's `mute: all` is posted as names and a human's `mute: @all`
    is resolved on read, and a bot added to the agent list afterwards
    is not muted;
  - a `mute: all` that resolves to no bot is left out of the post, and
    a reply with nothing left posts nothing;
  - a bot's `mute` that is an empty list or names a bot not in the
    room is invalid, and so is a `to` that names someone not in the
    room;
  - a human's `mute:` of a name that is not a bot in the room is ignored and
    shown in status;
  - a replayed older control arriving after the value returned to its
    default is ignored;
  - a bot's own post counts toward the rate limit on the gateway that
    posted it.
- **2**:
  - with the classifier off and the `addressed-only` fallback, a bot's
    post addressing nobody starts no turn;
  - under `mute: @all` a bot's mention of a bot starts nothing, a
    human's or the orchestrator's starts a turn, and a bot's mention of
    the orchestrator starts the orchestrator's turn;
  - a muted bot's reply to the human who addressed it posts despite the
    mute;
  - mutes read from the settings after restart.
- **3**:
  - counts match across two gateway processes on one room with equal
    agent lists, and status says so when they differ;
  - a human's mention starts in a rate-limited thread;
  - a bot the orchestrator addresses starts in a rate-limited thread.
- **4**:
  - the threads of a pass chosen once;
  - ordering by event order;
  - a turn that could not start runs at a later pass with the meanwhile
    posts in its backlog;
  - a rate-limited bot-to-bot mention in an open thread starts at the
    next pass once under the rate limit, and not at all if an earlier
    turn delivered it;
  - a thread under its post limit but over its word limit holds a turn;
  - a limit of 0 holds nothing;
  - a human's post clears the count and the held turn starts at the
    next pass;
  - a bot becoming free starts nothing without a pass;
  - a mention that arrived before a `mute:` opens nothing after it;
  - a cached classifier choice starts the held bot at a later pass, and
    lapses to catch-up past its TTL;
  - two qualifying threads yield one catch-up turn when the first
    delivers both;
  - a sweeper turn delivers through its high-water mark and a post
    arriving after it waits.
- **5**:
  - a job fires in its thread;
  - a failed firing is retried and not lost;
  - the cap posts a notice;
  - a job fires in a rate-limited thread;
  - a muted bot's job puts its prompt in the backlog and runs at the
    pass the `unmute:` requests, both when the mute came before the
    firing and when it came during a retry;
  - three firings of one job while muted run once.
- **6**:
  - one platform post arriving on three local connector streams at
    once → one classifier call and the same decision for each local
    bot;
  - an addressed bot speaks even when it is on the **remove** list, and
    the list is recorded;
  - a result arriving after another turn delivered its post starts
    nothing even when newer unrelated posts are in the backlog;
  - the oldest posts left out of the input are counted and the count
    is passed;
  - timeout → retries → the configured fallback, shown in status;
  - shadow agreement with blind labels before it is acted on.
- **7**: §6.1 and §6.2 reproduced.

---

## 9. Undecided

- **Rate-limit defaults** (`rate_limit_posts`, `rate_limit_words`,
  `rate_limit_seconds`, `cooldown_seconds`) and `control_ttl_days`:
  measured in the lab. A reader manages roughly 200–250 English words a
  minute, which puts `rate_limit_words` somewhere in the hundreds per
  five minutes.
- **Which limit binds**: the simulator's tally has posts and words per
  turn. The bad cases people mark in the transcripts (a thread that
  reads as noise, a loop that ran too long) say which of the two
  limits, at what value, would have caught them.
- **Classifier model, history size, timeout, retries and fallback**
  (`classifier_posts`, `classifier_timeout_seconds`,
  `classifier_retries`, `classifier_fallback`): the simulator's forced
  failure runs decide the fallback and the retry count. Shadow
  measurement decides the rest.
- **Whether the classifier may ever override a mention**: decided from
  the recorded **remove** lists (§8.1). Until then addressed bots always
  speak.
- **The room's event order in the gateway**: a connector abstraction
  giving one total order per room: server order for posts, a local
  sequence for sweeps and job firings, a fixed high-water mark for a
  paged fetch. The simulator numbers events. The gateway needs the real
  one before its traces are compared with the simulator's.
- **Whether the orchestrator's reply-turns go through the classifier**,
  so that it collects a round in one turn. Costs the "a human's or the
  orchestrator's mention is always honoured" rule for one bot.
- **Catch-up frequency**, from the not-addressed → catch-up-with-a-reply
  rate.
- **Envelope encoding** (task 1): a fenced JSON block, the whole reply,
  or a backend-native structured output where one exists (`claude
  --json-schema`; the OpenCode HTTP backend has none). The contract is
  the fields. The encoding is chosen when the parser is written.
- **Where a bot's profile lives.** The classifier needs, per bot, more
  than a username: what the bot does and when it wants to be asked.
  "How's the market today?" addresses nobody, and only a profile tells the
  classifier that one bot gives the daily stock update. The agent list
  configured today is a list of usernames. Two candidates:
  - a description per agent in each gateway's configuration, which two
    gateways must then keep in step;
  - the platform's own user profile fields, which every gateway reads
    from the same place.

  Decided before task 6. The design assumes the profile is provided and
  says nothing about how.

## 10. Out of scope for this version

- **Plans, timeouts and reports**: a gateway record of who was asked
  to do what, with a deadline per step, a cancel command, and a notice to
  the requester saying who answered and who did not (the plan-based
  alternative, Appendix B). Reopened if the simulator shows unattended
  handoffs stalling silently at a rate `FOLLOW_UP` does not recover.
- **Agent-list synchronisation between gateways**: agent lists are
  configured by hand and checked at startup. Reopened when two gateways
  share rooms in production.
- **Skipping the classifier for addressed posts**: not asking when a post
  addresses every bot, or addresses any bot. An optimisation of cost and
  latency, reopened when the data shows the classifier adds nothing to
  such posts. The mechanism is defined as asking every time.
- **A per-bot classifier**: one call per bot with its own backlog as
  input. Reopened if the room-level classifier's labels show it cannot
  judge for bots with different profiles.
- **Ordering within one gateway**: bots addressed by one post start in
  parallel, and two may answer the same question. Two mechanisms were
  considered and not built: a *stagger* (the next addressed bot starts when
  the previous turn ends or after a fixed delay, whichever first) and a
  *collision check* (a turn whose thread received another bot's post
  while it ran is shown that post before its reply is posted, and may
  revise or answer `done`). From the turn lengths in Appendix A, a
  15-second stagger would let a later bot see the earlier reply about
  two times in three. A 5-second one would mostly only tell it the
  earlier bot had nothing to say. People do not take turns either. It
  only looks so because they type at different speeds, and a busy bot
  gives the same effect for free. Reopened when the simulator's
  duplicate rate says the noise is worth a mechanism. True sequencing is
  an orchestrator's job.
- **Coordination across gateways**: two gateways in one room cannot
  confer, so for one post both may name a bot of their own (two
  replies), or each may name a bot the other runs (no reply at all).
  Both outcomes are accepted, because coordination across gateways is
  best effort in this version, and a human's mention always reaches its
  bot.
  Reopened only with a protocol between gateways.
- **Job priorities**: marking a scheduled job as time-sensitive,
  okay-to-delay or droppable, so that a mute (a meeting's `mute: @all`
  above all) does not hold a job that cannot wait. A change to the
  scheduler, not to this design. Reopened when the simulator shows what
  a meeting's mute costs in held jobs.
- **Anonymous votes, hidden information**: the room is the unit of
  visibility.

---

## Appendix A: Evidence from gateway logs

Lab: `#product-idea`, five bots (three `claude`, two `opencode`),
`max_turns: 5`, one afternoon. Production: `#nest`, three bots and one
human, `max_turns: 2`, one week.

| | Lab | Production |
|---|---|---|
| Bot turns | 207 | 544 |
| Ended by the bot itself (the token alone, today's `done`) | 127 (61%) | 259 (48%) |
| Posts dropped by the counter | 74 events on 21 posts | 2 |
| Dropped posts that carried substance | 18, all recoverable, plus 3 more posts that were not recoverable | 2 of 2 |
| Bot posts per human prompt (debate windows) | 31 / 18 / 23 | not measured |

Half the lab drops came from a counter-reset bug since fixed (PR #195).
The rest are genuine overflows on roughly ten posts, all in the
convergence phase of the debate. Two misattributions (a bot denying a
peer had given a figure, a bot accusing a peer of two inconsistent
figures) each trace to a post the counter had removed from the accuser's
context (issue #190). The TTL never acted as the reset in either
deployment. A human post always came first.

A closing-round rehearsal in the lab (n=1): the human posted a six-point
summary to `@all` with the rule "agree: token only; object: name the
point". Four of five bots answered with the token alone; one objected to
one point, which started two further chains of mostly token-only turns.
Thirteen turns, two posts.

Turn lengths in `#nest` over eleven days (615 turns): token-only turns
median 3 s, 82% within 5 s, 96% within 15 s. Turns with a reply median
10 s, 21% within 5 s, 66% within 15 s.

## Appendix B: Compared with the plan-based alternative

`multi-agent-enhancement.md` solves the same problem with per-thread
modes, a plan record per request, callbacks and a shared room state.
The two are run through the same simulator before either is built.

| | plan-based | this document |
|---|---|---|
| Who decides an unaddressed turn | a per-thread mode: silent, answer every post, or catch up later | the room classifier, once per post, with `mute:` as the only control |
| Sequencing and dependencies | a plan per request: members, parallel or sequential, membership, timeouts, member states | read from the room when a reply arrives, with `FOLLOW_UP` for waiting |
| Rate limit | a persisted count per thread since the last human post, a pause with notice, and an extend command | a rolling count of agent posts and words since the last human post, and a cooldown, kept in memory from the posts seen. No notice. Humans and the orchestrator are exempt |
| Loops | paused at a plan boundary until a human, an extension or a timeout | slowed, not stopped. Ended by `done`, `mute:` or a human |
| A bad actor | the thread pauses for everyone | `mute: @x` by the orchestrator or a human |
| Who is told what happened | plan-end notices, callbacks, stop reports | nobody. The room is the record |
| Replies | one thread per turn | any thread the turn was delivered |
| Persisted state | cursor, rate limit, mode, orchestrator, and plan records in a shared room state, with posts deduplicated into room events | the cursor, the threads' settings, the scheduled jobs |
| Several gateways in one room | non-goal | best effort: a post may draw two answers or none |
| Multiple bots answering one post | serial, each seeing the last | the classifier picks few. The rest is noise |
| Classifier | optional, for addressing by name and mode recommendation | required, once per post. May add speakers, never remove an addressed one |
| Rules a reader must hold | plan lifecycle, three modes with switching, pause and its three resets | one question, one control, one rate limit, one cursor and the threads' settings |
| What it gives up | simplicity, and cross-gateway rooms | deadlines and reports for unattended work, aggregation for an orchestrator, and determinism of turn eligibility |

## Appendix C: Code references

Where the statements in this document touch today's code.

| Statement | Reference |
|---|---|
| Today's three layers of loop protection | `docs/agent-chain.md` |
| `require_mention` applies to human senders only | `gateway/connectors/rocketchat/normalize.py:197, 213`, `gateway/connectors/mattermost/normalize.py:222` |
| `to:` is computed from every mention in a post | `gateway/connectors/rocketchat/connector.py:1841`, `gateway/connectors/mattermost/connector.py:828` |
| "mention only your intended recipients" is a prompt rule | `gateway/contexts/rc-gateway-context.md:56` |
| One turn at a time per bot per room | `gateway/core/message_processor.py:358` |
| A turn's history is bounded by the post that started it | `Connector.trigger_history_bound`, `gateway/core/connector.py:651` |
| The connector history fetch. Mattermost fetches one page | `Connector.fetch_room_history`, `gateway/core/connector.py:672`, `gateway/connectors/mattermost/rest.py:402-409` |
| Replay of posts missed while offline | `Connector.replay_room_since`, `gateway/core/connector.py:629` |
| A connector drops its own account's messages | `gateway/connectors/rocketchat/normalize.py:156-158` |
| A sender on no user list is rejected | `gateway/connectors/rocketchat/normalize.py:168` |
| The agent list | `agent_usernames`, `gateway/connectors/rocketchat/config.py:97` |
| The scheduler, and injection without a thread | `gateway/core/scheduler.py`, `SessionManager.inject_message(room_id, text)`, `gateway/core/session_manager.py:1556` |
| Backend turn timeout, shared by all backends | `agents.timeout`, `gateway/core/config.py:132` |
| Outbound posting retries and chunking | `gateway/connectors/rocketchat/outbound.py:31`, `gateway/connectors/mattermost/connector.py:173` |
