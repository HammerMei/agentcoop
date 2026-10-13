# Multi-agent enhancement

Status: **proposal, not implemented.** This document proposes replacing the
per-sender agent-chain limit with a moderated model for multi-agent rooms. It is a design
direction, not a specification; §6 lists the assumptions that have to hold
before any of it is built. Appendix B reads nine other projects at code
level, sorted by whether they position agents as teammates, and evaluates
the two that do against the questions this document asks.

---

## 1. Problem

### 1.1 The current model

Loop protection in a multi-agent room is three layers (`docs/agent-chain.md`):

1. **Self-termination.** The agent is told to answer with
   `<end-of-agent-chain>` when it has nothing to add; the gateway then posts
   nothing.
2. **Per-sender turn budget.** Each receiving bot counts posts per sender per
   room (or per platform thread). Past `max_turns` the post is dropped: it is
   not dispatched and never reaches that bot's session.
3. **TTL.** Counters are cleared after `ttl_seconds` of silence.

### 1.2 What the gateway logs show

Two deployments were measured — a five-agent lab debate with deliberately
opposed personas, and a production room with three agents and one human —
across one day and six days respectively. Appendix A has the numbers; the
findings are:

- **Layer 1 is disciplined in both.** Roughly half to two thirds of all agent
  turns end in self-termination. In production the budget was reached twice
  in six days.
- **A dropped post is gone.** Nothing feeds it into the receiver's session
  later; the bot's view of the room is permanently missing it
  (issue #190). Every dropped post whose content could be recovered — 18 of
  21 in the lab, both in production — carried substance: corrections,
  decisions, syntheses; none was an acknowledgement. In the lab,
  two mutual "corrections" between agents were traced directly to posts the
  limit had removed from the correcting agent's context.
- **The drops fell on the convergence phase.** In the lab the first twenty
  posts after a human prompt were all delivered; the drops began when the
  agents were writing their closing statements. This is the two mechanisms
  interacting rather than chance: the gateway tells the agent its turn
  number and, on the last one, to "wrap up gracefully"; on that turn the
  self-termination hint is withheld (`build_agent_chain_context`,
  `gateway/core/agent_chain.py:73`), so the final budgeted turn is steered
  towards a text conclusion. The counters are per receiver per sender, so
  the counterpart is at its own final turn when it reads that conclusion,
  is steered the same way, and its reply is the first post past the budget
  — the one the limit removes. A wrap-up invites the reply the budget
  cannot deliver. The counterpart may self-terminate instead, so this is a
  likely outcome, not a guaranteed one; the limit has no notion of content.
- **Parallel dispatch produces redundancy and leaves no room for the human.**
  One `@all` prompt produced four near-identical replies within seven
  seconds. Each agent is triggered independently and cannot see what the
  others are writing. Each human prompt in the lab debate produced about 25
  agent posts; the human's own assessment was that re-entering took ten to
  twenty minutes.
- **Self-termination costs a full turn.** The agent runs a complete model
  call to decide it has nothing to say.

### 1.3 Why patching does not reach it

- A per-sender count cannot tell "the same argument, repeated" from "a new
  topic the same sender opened". Both exhaust the same counter.
- Dropping conflates two decisions — *do not start a turn* and *do not show
  the agent* — that the loop-protection intent only needs the first of.
- Parallel triggering is structural. No prompt instruction can make an agent
  avoid duplicating a reply it cannot see.
- A fixed TTL is the only reset in a room with no human; one hour is
  arbitrary there.

---

## 2. Principles

These come from how human conversation stops, since that is the behaviour
the room is meant to approximate.

- **The default is silence; speaking needs a reason.** An agent today is
  triggered by the existence of a message, not by having something to say.
- **A loop is zero new information per turn — not the presence of a reason
  to reply.** Two children claiming the same toy both have a reason; the
  exchange loops because nothing changes between turns. The signal is
  repetition, not turn count.
- **Turn-taking: a speaker sees what was said before them.** This is what
  makes "I have nothing to add" decidable.
- **Intervene by informing, not by removing.** A chair says "let's stop
  there"; they do not delete the last speaker's words from everyone else's
  memory.
- **The moderator is a chair, not a police officer.** It decides who speaks
  next and when a topic is finished. It does not judge whether a speaker is
  right.

---

## 3. Design

### 3.1 The moderator

A moderator is a classifier that returns structured decisions and does not
generate text — a small, fast model such as TypeSafe's Jev, or any model
constrained to the same contract. It runs once per inbound message and, in
sequential mode, once per hop.

What it decides:

| Decision | Output | Section |
|---|---|---|
| Which topics a message touches | a list: each topic id answered or closed, and `new` for each one opened — a message can do several | §3.2 |
| Whether an agent's own topic tag is plausible | `accept` / `retag: <id>` | §3.2 |
| Whether a topic at its budget should continue | `extend` / `close` | §3.2 |
| How to dispatch a message | `parallel` / `sequential`, and the order | §3.3 |
| In sequential mode, who speaks next | `next: <bot>` / `done` | §3.3 |
| Whether a closing topic contained a decision | `decided` / `discussed` | §3.4 |

What it does not decide: whether an agent is correct, and how a topic at
its ceiling should be concluded. The first needs domain
reasoning; the second is a judgement call. Both belong to a full model or a
human, and both are rare enough not to need a per-message path (§3.4, §3.6).

**There is one moderator per room, and it lives in one gateway.** Agents in
a room may be run by independently deployed gateways (`docs/agent-chain.md`),
and a moderator that each gateway ran for itself would give each its own
tags, counts and `next` decisions — the per-receiver divergence this design
exists to remove, one layer up. This proposal therefore assumes that every
moderated bot in a room runs under the gateway that moderates it. A room
whose bots span gateways keeps today's behaviour (§3.6). Moderating across
gateways needs an owner for the room's decisions and a way to deliver them;
that is an open question (§7), not something this document specifies.

### 3.2 Topic-level budget

"Topic" here is a conversation topic, not a platform thread. One platform
thread holds several topics; one message can close one topic and open
another.

- **The agent tags.** Each agent post names the topic it answers and any
  topic it opens. The agent is the party that knows what it is replying to;
  agents already address by `@mention` reliably, and the self-termination
  token is emitted in a majority of turns, so a tagging instruction is
  within what they follow.
- **The moderator validates.** It checks the tag against the topic's recent
  history and retags when the tag is missing or implausible. This is the
  guard against an agent opening a "new" topic to escape a budget.
- **Budget is per topic, per sender, held by the moderator.**
  `max_turns_per_topic` replaces the per-room count. One count per topic per
  sender, not one per receiving bot: every bot gets the same budget decision.
  Today each receiver keeps its own counter, which is what allowed
  receivers' views of a room to diverge.
- **Reaching the budget is a checkpoint, not a stop.** The moderator
  compares the new post with the topic's history and answers `extend`
  (same topic, no repetition, progress) or `close`. A post that has moved to
  a different subject is a retag, not an extend.
- **Extends are bounded.** `extend_limit` caps how many times a topic can be
  extended. It is tunable at runtime by a human in the room, since the right
  value depends on the conversation. Reaching it is the one case that needs
  a conclusion rather than another turn (§3.4).

### 3.3 Dispatch plan

The addressing the human already uses selects the dispatch mode. Nothing new
to learn.

| Message | Plan |
|---|---|
| `@a …` | sequential, `[a]` — as today |
| `@all …` | parallel, every bot — the human explicitly wants every voice |
| `@a … @b …` with independent tasks | parallel, `[a, b]` |
| `@a and @b …` asking for views | sequential, `[a, b]` in mention order |
| no mention | sequential; the moderator picks each hop |
| explicit mode, e.g. `parallel:` / `sequential:`, **from a human sender** | as stated; the moderator does not classify. In an agent's post the same words are ordinary text — the sender's role is server-injected, so the check is free |
| gateway failure notice | no dispatch; queues for every bot |

**Both modes are needed, and the human can name one.** Sequential lets a
bot build on what was said; it also lets a bot anchor on it. In the lab's
closing round one bot agreed with every point when it answered alone, then
reversed to join the one objection once it had seen it. Parallel is the
mode for independent judgement — votes, estimates, reviews, confirmations
— where seeing a peer's answer first is contamination, not context.
The moderator's criterion ("do later replies gain from seeing earlier
ones?") has a negative side, and the human often knows which side applies
before the moderator can; an explicit mode marker in the message overrides
classification.

**A failure notice is context, not a trigger.** When a turn fails, the
gateway posts a notice from the bot's own account. Other bots should see it
— it explains why a peer went quiet — but it is not a reason for anyone to
speak, and today it is dispatched like any agent post: the receiving bots
run turns on it, fail or self-terminate, and in the failing case post their
own notices, which trigger the next round. The plan for a notice the
gateway itself produced is a fixed rule, not a classification: no turn,
queue only. A notice from a bot another gateway runs is outside this
proposal's scope (§3.1); it would become a moderator decision only if
cross-gateway moderation is built (§7).

**Attribution comes before dispatch.** Every inbound post is attributed
before any dispatch decision is made, and the attribution answers three
questions in order:

1. *Which topics?* The moderator's topic list (§3.1), applied to the post's
   tags. Counts belong to the topic, and nothing but `close` ends them.
2. *Under which plan?* A reply made by a bot that a plan selected is that
   plan's output, not a new trigger: it joins the plan and never starts one.
   Each selected bot is in exactly one state, and a plan ends when every
   participant is in a terminal one:

   | Participant state | Terminal | How it is reached |
   |---|---|---|
   | `selected` | no | the plan named the bot; in parallel mode it receives the triggering post only — not the other replies, which would contaminate the independent judgement parallel exists for; a busy bot receives that same snapshot when its turn ends |
   | `replied` | yes | its reply was posted |
   | `self-terminated` | yes | it answered with the token alone |
   | `failed` | yes | its turn failed; the failure notice queues (§3.3), and the plan advances as if the bot had self-terminated — no retry inside the plan |
   | `timed-out` | yes | `hop_timeout_seconds` elapsed; a reply that arrives later is attributed to the plan if it is still running, otherwise to the ended plan (below) |
   | `interrupted` | yes | a human post attributed to the plan's topic ended the plan, at any point — not only in the gap |

   A sequential plan also ends at `done`, subject to its required
   participants (§3.3). When a plan ends, the moderator looks at its
   replies once and decides `next` / `done` for the topic — so an `@all`
   round is followed by at most one sequential continuation, not by one
   plan per reply. One topic has at most one plan in flight.
3. *Does it start a plan?* Only a post attributed to no running plan — a
   human post, or an agent post made outside any plan, such as a scheduled
   fire — is dispatched by the table above.

The rules that follow — hop timeouts, late replies, the human gap — are
cases of this table, not exceptions to it. Where a rule below and the table
disagree, the table is wrong and should be fixed; it is the one place the
plan's states are enumerated.

**Parallel versus sequential for multiple mentions** is decided by the
moderator on one criterion: *do the later replies gain from seeing the
earlier ones?* "Check the log" and "look at the code" are independent;
"your views on …" are not. When the message is ambiguous the default is
sequential — its failure mode is latency, parallel's is redundancy, and
redundancy is paid by the human who reads it.

**Sequential order is re-decided at every hop**, not fixed up front. The
first reply changes who is best placed to speak next. The moderator sees the
replies so far and answers `next: <bot>` or `done`. When it cannot decide,
the order is random; a wrongly chosen bot costs one turn that ends in
self-termination, not a redundant post. `done` ends the sequence — a
question is answered when it is answered, not when every bot has had a turn
— with one constraint: a bot the human mentioned explicitly is a required
participant, and `done` is not accepted while one of them has neither had
its turn nor reached its hop timeout. The `[a, b]` plan above is a promise
to the human, and the moderator cannot shorten it.

**A busy bot is skipped, not waited for.** A bot with a turn in flight is
passed over; the message still enters its queue, and when its turn ends it
sees it — in a sequential plan, in a batch together with the replies made
meanwhile; in a parallel plan, alone, as the snapshot the other
participants answered (attribution table, `selected`) — and decides as
usual whether to add anything. A bot that was explicitly
mentioned is moved to the end of the sequence rather than skipped, so the
promise that it replies is kept without stalling the others.

**A slow bot is not waited for either.** Each hop has `hop_timeout_seconds`,
a gateway rule separate from the agent's own turn timeout. The busy rule
acts before the hop starts (a turn is in flight); this one acts inside it
(the bot was free but has not answered). When it fires the sequence moves
to the next bot; the slow bot's turn keeps running, and its reply, when it
arrives, is posted — classified into its topic and counted against the
budget — not dropped. It is attributed to the plan that selected the bot
(attribution rule 2): while that plan is still running it is part of what
the moderator sees at the next hop decision; after the plan has ended it
is a reply to an ended plan, and the moderator decides once whether it
warrants a `next`. It never starts a plan of its own. A mentioned bot
moved to the end of the sequence is under the same timeout, otherwise
"moved to the end" is the stall by another name.

**A gap between hops leaves room for the human.** Sequential dispatch holds
`human_gap_seconds` after each reply, or after a hop timeout, before
triggering the next bot. A human post during the gap takes the next slot
and ends the running plan. It resets the per-sender counts of **the topics
it is attributed to** — deliberately, as today: a human in the exchange is
the strongest evidence it is not a runaway, and the reset is the human's
lever for keeping a topic alive. Topics the post does not touch keep their
counts, and extends already consumed are not returned, so repeated
interjections buy one `max_turns_per_topic` window each, never a fresh
`extend_limit`. The gap's value is a tuning question (§6).

**`@all` keeps its redundancy.** Parallel dispatch cannot avoid it. The
human chooses it when they want every voice; the cost is theirs to accept.

### 3.4 Closing round

Sequential dispatch removes the chance for an unaddressed bot to correct a
mistake it noticed. That chance is restored at the end of a topic rather
than after every message: a **closing round** gives each remaining bot one
batched turn over the whole topic — add something or answer with the token.

The cost moves from "every message × every bot" to "every topic × every
bot", and is reduced again by giving the round only to bots whose declared
role is relevant to the topic (a moderator classification, or a
subscription the agent declares).

When the round runs is decided by whether the topic produced a decision:

- A topic that **decided** something — locked, accepted, assigned — gets its
  closing round when it closes. A mistake in it will be acted on. A
  substantive reply in that round — anything but the token — **reopens the
  topic**: it is retagged to it, the topic runs under its remaining budget
  (at the ceiling, under §3.5), and the reply is dispatched sequentially
  like an objection at the conversation's end. Without this the round
  could catch a mistake that nothing is then allowed to discuss.
- A topic that was **discussed** without a decision waits for the end of the
  conversation. A late correction costs nothing until someone acts on the
  error.

**A conversation ends when its last open topic closes** — by `close`, by
TTL, or at the ceiling — or when a human says it has. That is the whole
boundary rule, and it is provisional (§7): a room is one conversation at a
time, the next post attributed to no running plan — a human's, or an
agent's scheduled fire — opens the next one, and an agent-only room ends
its conversations by TTL. TTL expiry is observed at
the next inbound post, as today (`_gc` in `gateway/core/agent_chain.py`
runs only from the budget check; scheduled fires are inbound posts): a
room that falls silent is closed late, when something next arrives, and
its closing round runs before the new conversation opens. Nothing in a
silent room is waiting on that round, so the delay costs nothing; a timer
would be a new mechanism for no observable gain.

At the end of a conversation the round is structured: a full model
summarises the conclusions (once per conversation — this is the judgement
call the moderator does not make), and each bot answers `agree` or
`object: <reason>`. Agreement ends the conversation. An objection is a
post like any other: the moderator tags it, and one that re-argues a
closed topic is retagged to that topic and runs under its remaining
budget — at the ceiling, under §3.5 — rather than under a fresh one.
Only an objection the validator accepts as a new point opens a topic.
Without this the closing round would be the escape hatch §3.2 closes:
close, object under a new label, close again.

The closing round itself runs **parallel**: it asks for independent
confirmation, and a bot that has seen a peer's objection before answering
is no longer confirming independently (§3.3). The topic an objection
reopens or opens is then dispatched sequentially, where building on the
objection is the point.

### 3.5 Intervention

Nothing is dropped, and two cases are distinct.

A bot the dispatch plan does not select runs **no turn**. The message queues
and the bot sees it in its next batch or in the closing round (§3.3, §3.4).
This is where the saving over today comes from.

**The queue is not stored.** "Queue" throughout this document names the
room's posts since the bot's **watermark** — the id of the last post
delivered to its session — and nothing else. At the bot's next turn the
gateway fetches the room's posts after the watermark from the platform,
the query that history handoff and reconnect replay already run
(`Connector.replay_room_since`, `gateway/core/connector.py`), delivers them
with the triggering post, and advances the watermark. The platform holds
every post durably, so the gateway holds one id per bot per room, nothing
is copied into N per-bot structures, and a gateway restart changes
nothing: the next turn runs the same query. This is how Buzz delivers a
thread's history to an agent that was not addressed (Appendix B.2), and
it was adopted from there on 2026-10-13 in place of a stored per-bot
queue. The cost is one history fetch per dispatched turn, bounded by the
flush rule below.

**The queue is bounded.** A bot that is never selected would otherwise
accumulate the whole conversation unseen. The gateway sees every post in
the room, so it counts posts past each bot's watermark; when the count
passes `queue_flush_threshold`, the gateway grants the bot a catch-up turn
at the next hop boundary: it receives the batch with the usual instruction
— add something, or answer with the token. The count is a cache of the
query and is re-derived by it after a restart. If it posts, the post counts
against its topic budget like any other. Every queue is drained at every
boundary: a bot in the closing round receives its queue there, and a bot
the relevance filter left out receives it as a plain batch — context, no
turn — before the next conversation's first post, so nothing arrives as
stale context later. This rule therefore only matters in a long
conversation that has not reached a boundary; it is a bounded-lag versus
turn-cost knob, and it also restores, bounded, the chance for an
unaddressed bot to correct a mistake it has been watching.

A bot that was about to speak — selected by the plan, or explicitly
mentioned — when **every topic it was selected for** is closed or at its
ceiling runs its turn with a suffix:

> Agent chain limit reached: your reply to this message will not be posted.
> If it matters, say it again the next time you are allowed to speak. You
> may answer with only `<end-of-agent-chain>`.

The turn runs; its reply is suppressed **whole**, by a forced-suppression
result that this turn carries from the moment it is dispatched. The
existing token path is not reused: it strips the token and posts any text
around it (`gateway/core/agent_turn_runner.py:151-160`), which is right
for self-termination and wrong here — a bot that ignores the suffix, or
adds a correction beside the token, would post past the ceiling. The
bot's context stays complete, it knows what happened, and there is
nothing to buffer. The cost is one suppressed turn,
which the suffix invites the agent to make cheap.

When only some of the bot's topics are closed, the turn runs normally with
a note naming the closed ones. The reply is posted; the moderator tags it,
and a part that continues a closed topic is charged to that topic as any
post would be. Suppressing the whole reply for one closed topic would lose
the open topic's contribution, which is the divergence this section exists
to remove.

### 3.6 Fallbacks

- **Hard ceiling.** `extend_limit` (§3.2). Reaching it closes **that
  topic** and runs its closing round with a topic-scoped summary (§3.4).
  Other topics in the conversation are untouched; the conversation-level
  round waits for the conversation's own end.
- **Rooms whose bots span gateways.** No moderator (§3.1): today's
  behaviour throughout — parallel trigger, per-sender budget — and logged
  as such at watcher start.
- **TTL, per topic.** A topic silent for `ttl_seconds` is closed. With a
  moderator in place the TTL is the fallback for a moderator that is
  unavailable, not the primary reset, so it can stay long.
- **Moderator unavailable.** Dispatch falls back to today's behaviour:
  parallel trigger, per-sender budget. The per-sender store keeps counting
  while the moderator is healthy — it already exists and costs nothing to
  leave running — so the fallback starts from live counts, not empty ones.
  Degraded, logged, never silent.

---

## 4. Relationship to the current layers

| Today | Proposed |
|---|---|
| Layer 1: self-termination | Kept. Becomes more effective under sequential dispatch, because the agent can see what was already said. |
| Layer 2: per-sender turn budget, drop on overflow | Replaced by the per-topic budget with a moderator checkpoint; overflow informs and suppresses instead of dropping. |
| Layer 3: TTL per room | Kept, per topic, as a fallback. |
| — | Dispatch plan: addressing selects parallel or sequential; sequential is moderated hop by hop. |
| — | Closing round: per topic when a decision was made, per conversation otherwise. |
| — | Conversation summary and agree/object confirmation, once per conversation. |

---

## 5. Trade-offs

- **Latency for redundancy.** Sequential dispatch of five bots is five
  model calls in series instead of in parallel. The redundancy it removes
  costs tokens and, more, the human's reading time. `@all` keeps the
  parallel path for when speed matters.
- **One more hop per message.** The moderator runs on every inbound
  message. This is acceptable only if it is small and fast — the design
  assumes a classifier, not a general model.
- **Classifier accuracy carries the design.** Topic validation is what makes
  the per-topic budget enforceable; a classifier that cannot tell a
  re-opened old topic from a new one leaves the budget escapable. This is
  the first assumption to test (§6).
- **Corrections can still be missed.** A relevance filter on the closing
  round, or a mistake outside any bot's declared role, leaves an error
  uncorrected until a human or a later trigger finds it. The residual is
  accepted because a late correction has a cost only when the error was
  acted on in the meantime.
- **A suppressed turn is not free.** It costs the input tokens of a full
  turn. The suffix lets the agent answer with the token alone, which bounds
  the output side.
- **A late reply can still duplicate.** When a hop times out and the slow
  bot's reply lands while the next bot is mid-turn, the next bot cannot see
  it. That is the redundancy parallel dispatch has today, confined to the
  timeout case.

---

## 6. Assumptions to validate, in order

1. **Topic validation accuracy.** Can the classifier distinguish a post that
   continues an existing topic under a new tag from a post that genuinely
   opens one? Measure on the lab transcript, where topics and their
   re-openings are known.
2. **Sequential dispatch raises the self-termination rate.** If a bot that
   sees the prior replies declines more often than one that does not, the
   redundancy problem largely resolves itself and the closing round is
   rarely needed.
3. **`parallel`/`sequential` classification of multi-mention messages.**
   Measure precision against a hand-labelled set; the ambiguous cases
   default to sequential, so what matters is not misclassifying a request
   for views as a task split.
4. **Human gap.** How long the gateway should hold between hops so a human
   can enter. Only measurable in a live room.
5. **`extend_limit` and `max_turns_per_topic` defaults.** No topic-level
   counts exist yet. The lab debate ran five to eight posts per sender per
   human-prompt window before the budget cut in; the production room almost
   never exceeded two per sender. Topic-level values have to be measured
   once tagging exists.
6. **`hop_timeout_seconds` and `queue_flush_threshold` defaults.** The
   first is bounded below by typical turn latency per backend, which the
   logs already record; the second depends on how many posts a bot can
   absorb in one batch and still reply usefully, which is untested.

---

## 7. Open questions

- **Runtime tuning interface.** `extend_limit` is tunable by a human in the
  room. Whether that is a chat command, a `coop` CLI call, or both is a
  usability question, not a design one.
- **Topic tag format.** The tag has to be something every backend will emit
  and the moderator can parse. A line in the reply versus a structured
  field depends on what the backends support.
- **Relevance for the closing round.** Moderator classification versus
  agent-declared subscription. The second is cheaper and predictable; the
  first needs no configuration.
- **Summary model.** The conversation summary is one full-model call per
  conversation. Which agent, or which separate model, produces it.
- **Glossary.** "Moderator" is a new term. If this proceeds it goes into
  `CONTEXT.md` with the synonyms to avoid (arbiter, judge, router).
- **Moderation across gateways.** This proposal assumes one gateway runs
  every moderated bot in a room (§3.1). A room whose bots span gateways
  needs one owner for its tags, counts and `next` decisions and a channel
  to deliver them — the room's platform is the obvious one, since every
  gateway already reads it. Whether that is worth building before step 7
  is for the owner to decide; until then such rooms are unmoderated (§3.6).
- **Conversation boundary.** "Last open topic closes, or a human says so"
  (§3.4) is the simplest rule that gives step 8 a terminal event. It is
  untested; a room where topics overlap continuously may never reach it,
  and would need a time-based or human-declared boundary instead.
- **Context-only delivery (step 3).** §3.5 delivers a left-out bot's queue
  "as a plain batch, no turn". No backend has that today: `send` and
  `stream` both run a model turn, and history handoff uses `send`. Either
  step 3 adds a context-only input, or the batch is an ordinary suppressed
  turn and is costed as one (§5). The second is the default until a second
  consumer of the primitive appears.
- **State durability and recovery (steps 3 and 7).** Topic counts, the
  plan in flight and every bot's queue are in-memory in this proposal, as
  today's counters are. A gateway restart loses them, and a moderator
  outage (§3.6) leaves them stale for the messages Layer 2 handled
  meanwhile — one question, two triggers. The default answer is **reset,
  not reconciliation**: on recovery every topic is closed without a
  closing round, no plan is in flight, and the next post opens a new
  conversation; the degraded window is logged. Queues need no recovery:
  a queue is a watermark and a query (§3.5), so the next turn after a
  restart delivers exactly what it would have, and the only per-bot state
  to persist is the watermark itself — one post id per bot per room,
  alongside the room watermark the connector already keeps. Whether
  reconciliation of counts is ever worth its cost is for the step 7 PR to
  decide against an observed outage.
- **Age-based queue flush.** The queue bound is a count. Whether a queue
  that is small but old also warrants a catch-up turn is open; nothing in
  the logs says yet that it does.
- **Scheduled and agent-only rooms.** With no human to open topics or tune
  limits, the moderator and the TTL are the only controls. Whether that is
  enough, or whether such rooms need a stricter default, is untested.
- **Moderator error, per decision (Appendix B).** §3.6 covers a moderator
  that is absent. A moderator that is present and returns an unparseable
  or out-of-contract answer on one decision needs a rule per decision
  type. The prior-art shape is: retry a bounded number of times, then
  fall back to the rule-based choice from step 4 — the mention order for
  the plan, the previous speaker for `next`, `close` at a ceiling. QM's
  classifier fails closed to silence; AutoGen's selector falls back to the
  previous speaker after three attempts. Which fallback each decision gets
  is for the step 2 PR, where the error rate is first measured.
- **Progress as the input to `extend` / `close` (Appendix B).** §2 defines
  a loop as zero new information per turn, but §3.2 reaches the moderator
  only through a count. Magentic-One's ledger asks the model two
  structured questions per hop — is progress being made, is this a loop —
  and counts stalls with hysteresis. Asking the same two questions at the
  ceiling would make the principle the moderator's actual input. Whether
  the small model answers them reliably is a §6.1-class assumption and
  belongs in step 2's shadow measurement.
- **Rate guard as a backstop (Appendix B).** Every gateway surveyed that
  acts at a cap uses a rate — events per bot pair or per sender per
  window — not a turn count. It is orthogonal to the topic budget and
  would catch a moderator that keeps extending. It also adds a tuning
  surface the budget was meant to remove. Not in the design; the step 7
  PR decides against an observed case where the budget alone was not
  enough.
- **Unmentioned bot posts under step 4 (Appendix B).** The rule table
  dispatches a post with no mention in random sequential order. Two
  gateways default the other way for bot-authored posts: no mention, no
  turn — the post is context only. That rule is cheaper and is what §2's
  "default is silence" says; it also means a bot's reply never starts a
  chain unless it names someone. Whether lab agents mention reliably
  enough for it, or whether it silences legitimate follow-ups, is a
  step 4 measurement.

---

## 8. Work breakdown

Each step ships on its own, behind a config flag, and produces a number
from the lab room before the next one starts. The current three layers
stay in force until step 7 replaces Layer 2, and the moderator's decisions
have no authority before step 7. Steps 3–6 do change what a bot sees —
which messages start turns, when queued messages enter its context, which
earlier replies a sequential bot has read, and (step 6) a message Layer 2
used to hide — so each is measured against the step before it, not
against today, and nothing measured before step 7 is attributed to the
moderator. Production stays on today's behaviour throughout.

| # | Step | Needs | What it proves | Measured by |
|---|---|---|---|---|
| 1 | **Topic tags in the agent prompt.** Agents name the topic they answer and any they open; the gateway parses and logs the tag, nothing else changes. | — | Agents tag reliably enough to be a signal (§6.1 precondition). | Tag presence rate; hand-check a sample. |
| 2 | **Moderator in shadow.** The classifier runs on every inbound message and logs every decision it can make from messages alone — topic tags `accept` / `retag`, `extend` / `close` and `decided` / `discussed` at each point today's Layer 2 or TTL closes an exchange, `parallel` / `sequential` for each multi-mention message — and none of it has any effect. This is the first moderator wiring, without authority. | 1 | Accuracy of each decision type (§6.1, §6.3), measured separately. | Agreement with hand labels on the lab transcript, per decision type. |
| 3 | **Per-bot watermark.** A message can reach a bot without starting a turn: the bot's watermark stays behind it, and at the bot's next turn the gateway fetches the room's posts past the watermark and delivers them as a batch (§3.5); `queue_flush_threshold` grants a catch-up turn. First consumer: the gateway's own failure notices, which today each start a turn (§3.3). | — | The "no turn, still seen" primitive works; the error cascade is gone. | Error-notice turns per failure: today 4, target 0. |
| 4 | **Rule-based dispatch plan.** The plan is computed from addressing alone — single mention, `@all`, multi-mention in mention order, no mention in random order, explicit `parallel:` / `sequential:` — with no moderator (§3.3 table). | 3 | The plan model fits the room without classification. | Posts per human prompt (today ~25 in the lab); self-termination rate. |
| 5 | **Sequential engine.** Runs a plan hop by hop: busy skip, `hop_timeout_seconds`, `human_gap_seconds`, late replies joining the running sequence (§3.3). The moderator shadows `next` / `done` here, against the rule-based order, since this is the first point those decisions have an input. | 3, 4 | Sequential raises self-termination and leaves room for the human (§6.2, §6.4); `next` / `done` accuracy. | Self-termination rate under sequential vs today; human posts landing inside a gap; shadow `next` against hand labels on replayed snapshots — the live room cannot score it, because the rule-selected bot speaks first and changes the state the shadow choice was made in. |
| 6 | **Inform instead of drop.** Layer 2 overflow runs the turn with the suffix and suppresses the reply (§3.5). Independent of the rest; can ship any time. | — | A bot's context no longer diverges from the room (issue #190). | Dropped posts: today 21 of 207 lab turns, target 0. |
| 7 | **Moderator with authority, one decision type at a time.** Each decision the shadow measured is switched on separately, behind its own flag, in the order its shadow accuracy earned: topic budget with `extend` / `close` and `extend_limit` (replaces Layer 2), then multi-mention mode, then `next` / `done`. A type whose shadow accuracy is not known stays in shadow. | 2, 5, 6 | Each decision type beats the rule it replaces from step 4. | Posts per prompt and self-termination rate vs step 4, per switch-on. **Provisional until step 8**: until the closing round exists, a `close` silences the correction it would have invited, so a reduction in posts measured here is an upper bound, re-measured at step 8. |
| 8 | **Closing round.** Parallel per-topic / per-conversation round, conversation summary, `agree` / `object` (§3.4). | 7 | Converged topics go quiet; objections are caught at the right time. | Token-only share in the round (lab, 2026-10-12: 4 of 5). |

Steps 1–2 and 3–5 are independent chains and can run in parallel; 6 is
independent of both. The moderator exists from step 2 but decides nothing
until step 7, and step 7 gives it authority only over decision types whose
shadow accuracy was measured first — in step 2 for the ones that need only
messages, in step 5 for `next` / `done`.

Every row of the §3.1 decision table has a shadow step, an activation step
and a measurement; this table is the check that none was skipped:

| §3.1 decision | Shadowed in | Activated in | Measured by |
|---|---|---|---|
| topics a message touches; `accept` / `retag` | 2 | 7 (budget) | agreement with hand labels |
| `extend` / `close` | 2 | 7 (budget) | hand-checked at each ceiling; posts per prompt, provisional until 8 |
| `parallel` / `sequential` for multi-mention | 2 | 7 (mode) | precision against a hand-labelled set (§6.3) |
| `next` / `done` | 5 | 7 (`next` / `done`) | hand labels on replayed snapshots of the decision point (§6) |
| `decided` / `discussed` | 2 | 8 | objections caught at topic close vs at conversation end |

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

## Appendix B — How other projects run several agents

Nine projects were read at code level on 2026-10-12. Each is described on
its own terms — what it is for, how its agents are arranged, how an agent
reaches another agent and what keeps that from looping, and whether a
human can address each agent directly — and judged against the use case
it claims, not against this proposal. B.1 sorts them by product intent:
the two that position agents as **teammates** — several agents with
personas, in one place, talking to humans and to each other — come first
and are read in depth, because that is this gateway's position; the seven
that position agents as a single assistant or as workers given tasks are
summarised after. B.2 draws out how the two teammate products handle the
questions this document asks; B.3 is the only part that looks back at it. Citations are `path:line` at the commit in
the table; `[docs-only]` marks a claim found in a README and not traced to
code. Where a project's own description disagreed with its code, the code
is reported.

| Project | Commit | Claimed use case |
|---|---|---|
| `block/buzz` | `799cfd7` | team chat (Nostr relay) for software teams, where agents are members alongside humans and git/CI/approval events share the log |
| `openclaw/openclaw` | `5559404e` | one person's assistant, reachable from every messenger they already use |
| `yc-software/qm` | `2c94475` | one company bot in Slack that many employees share, with personal and shared scopes |
| `hristo2612/jinn` | `3ae6465` | "AI employees" on an org chart, reached through Slack / Discord / Telegram / web |
| `chaitanyagiri/munder-difflin` | `241d1e5` | a desktop "office" of digital clones of team members, running overnight |
| `microsoft/autogen` | `027ecf0` | in-process group chats of agents solving one task |
| `google/adk-python` | `b3cbb47` | building agent applications as trees of agents |
| `FoundationAgents/MetaGPT` | `11cdf46` | a simulated software company turning a requirement into code |
| `crewAIInc/crewAI` | `744bf60` | task pipelines (research → write → review) for business automation |

A bare file name resolves under the project's main directory: AutoGen
`python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/`
(Magentic-One under its `_magentic_one/` subdirectory, tests under
`python/packages/autogen-agentchat/tests/`); ADK `src/google/adk/agents/`
and `src/google/adk/flows/llm_flows/`; MetaGPT `metagpt/roles/`,
`metagpt/environment/` and `metagpt/environment/mgx/`; crewAI
`lib/crewai/src/crewai/`; QM `src/`; jinn `packages/jinn/src/`; OpenClaw
`preflight.ts` is `extensions/discord/src/monitor/message-handler.preflight.ts`.

### B.1 Project by project

**Positioned as teammates.** The README of each says so in its own words:
Buzz — "people and agents building together in the same room… the same
affordances as a human teammate, a different keypair" (`README.md`,
`ARCHITECTURE.md:5` "first-class equals"); munder-difflin — "an office of
your clones… agents that message, route, and remember" (`README.md:7,24`).

#### Buzz

**Multi-agent, and for what.** Yes. The product is a team chat where an
agent is a channel member with its own keypair, and the use case is a
software team whose humans and agents share one event log — chat, git
patches, CI status and approvals are all signed Nostr events. An agent is
one `buzz-acp` harness process bridging relay events to an ACP-speaking
CLI agent; N agents are N processes.

**Topology.** Mesh on a shared channel. There is no orchestrator and no
room-level coordinator; any member can address any member by `p`-tag
mention, and the relay fans out.

**What an unmentioned agent sees.** In the default `mentions` mode,
nothing at the time: the harness subscribes with a relay-side filter on
its own pubkey, so a post addressed to agent A never reaches agent B's
process (`crates/buzz-acp/src/relay.rs:3366-3372`; mode default
`config.rs:336-340`). B learns of it in one of three ways. If B is later
mentioned **in the same thread**, its harness fetches that thread's
history before prompting — all messages under the root, including other
agents' and humans' non-mention posts, up to `--context-message-limit`
(default 12, max 100) (`crates/buzz-acp/src/pool.rs:4105-4135`;
`config.rs:388-392`). If B is mentioned in a **top-level channel post**,
no context is fetched at all — `ContextTarget::None`, "plain channel"
(`pool.rs:4155-4162`) — and B answers knowing only the triggering post.
Otherwise B can pull: the prompt tells it to catch up once with
`buzz messages get --channel <UUID> --since <ts>` (`base_prompt.md:78`).
`--subscribe all` makes B receive every event of the configured kinds,
each as a turn it may end silently (`config.rs:55-59`), still subject to
the author gate.

So an agent's view of a channel is incomplete by construction, and the
design says so to the agent rather than repairing it. The session-model
prompt tells each session it is "not the only copy", that sessions "do
NOT share conversation context", and to "answer from what you can verify
(core memory, workspace files, relay messages)"
(`crates/buzz-acp/src/session_model_channel.md`). There is no rule to
fetch the channel before replying: the harness fetches thread history
automatically when the turn is in a thread, and otherwise the prompt's
only pointer is "to catch up once, use `buzz messages get --channel
<UUID> --since <ts>`", offered as the alternative to waiting on a tool
call (`base_prompt.md:78`); the heartbeat, off by default, checks the
feed for unanswered mentions and pending approvals, not for channel
context (`lib.rs:5940-5952`; `config.rs:308-310`). The mitigation is
structural: `SessionPolicy::Thread` makes the thread the unit of
collaboration, with its history fetched on every mention, and the prompt
confines agent-to-agent traffic to completed work. The gap this leaves —
two agents mentioned in one top-level post, or an agent joining a thread
late past the context limit — is the same divergence issue #190
describes here, with one difference: in Buzz the missing posts are the
ones that did not name the agent, which the agent is told to expect; here
they are whichever posts exceeded a counter.

**Agent to agent.** Yes, by mention, and it is a designed path: the prompt
requires an agent that finishes delegated work to `@mention` the
delegator, and says task assignments to other agents go in the same
channel (`base_prompt.md:53-54, 66`). The receiving side accepts the
mention only if its author gate admits the sender — by default the
agent's registered owner and "sibling" agents with the same owner
(`crates/buzz-acp/src/config.rs:93-105`; `crates/buzz-acp/src/lib.rs:3702-3725`);
`allowlist`, `anyone` and `nobody` are the other settings. The harness
never auto-posts, so an agent's text reaches the room only when it calls
`buzz messages send` itself (`crates/buzz-acp/src/acp.rs:1836-1841`).
What keeps a sibling pair from looping, or from trading empty
acknowledgements, is prompt text and nothing else (`base_prompt.md:71-75`):
"publishing is optional and silence is usually correct — when a message
leaves you nothing new to contribute, end the turn without publishing";
"never publish a bare acknowledgement … it re-triggers everyone you
mention", with a prohibited list ("Got it", "Confirmed", "Aligned",
"Standing by", "I won't reply again"); mention the delegator "for
completed work only — do not `@mention` to accept an assignment, confirm
receipt, or close a loop conversationally"; and "only `@mention` when you
need their attention — naming someone while talking about them is
narrative, drop the `@`". The one asymmetry is for humans: "if a human
asked you something, you MUST reply — even if the reply is only that you
have nothing to add" (`base_prompt.md:72`). In code there is no hop,
depth or turn counter; the guards are ignore-self, the author gate, a
500-event queue per scope with drop-oldest, and a two-hour wall clock per
turn (`crates/buzz-acp/src/queue.rs:34-42`; `config.rs:35-40`). A failure
notice is posted without any mention tag, so it cannot wake another agent
(`crates/buzz-acp/src/pool.rs:5540-5548`). No test runs two agent
identities in one channel (`crates/buzz-acp/tests/`; `--agents 1..=32` is
N subprocesses sharing one identity, `config.rs:302-305`).

**Humans.** The owner talks to their agents freely, by mention or DM. A
human who is not the owner is dropped by default and needs an allow-list
entry. A human posting while the agent is mid-turn cancels and re-prompts
it with the new post framed as having arrived while it was working
(`config.rs:67-80`).

**Against its own use case.** Pros: zero cost for everything not addressed
to you, which is what makes a busy team channel affordable; identity and
permissions per agent rather than per bot flag; a durable per-agent
mention index on the relay that an agent catches up from on a heartbeat,
so nothing is lost across a restart without the harness keeping state
(`schema/schema.sql:90`; `lib.rs:5940-5948`). Cons: a top-level channel
post gives the agent no context of what other agents have already replied
— only thread replies fetch context (`pool.rs:4105-4170`), so two agents
mentioned together answer blind; the owner-only default means a team's
agents do not hear the team unless configured; and loop safety between
same-owner agents rests on the prompt.

#### munder-difflin

**Multi-agent, and for what.** Yes: a desktop app that runs real CLI
agents in terminals as "clones" of team members, each with a mailbox and
memory, meant to keep working while the team is away. Its site describes
peer-to-peer encrypted agent messaging with no central orchestrator; the
code is a central router in the Electron main process moving JSON files
between per-agent `outbox/` and `inbox/` directories (`src/main/hive.ts:1832-1893`),
and no transport encryption was found.

**Topology.** Mechanically a mesh, by design a hub with a blackboard.
The router delivers to whatever `to:` names — any agent id, `god`, or
`broadcast` to every live agent but the sender (`hive.ts:1684-1693`) — so
any clone can mail any clone. But the protocol text every agent receives
steers traffic to the hub: "for anything ambiguous, cross-cutting, or
needing sign-off, message `god`"; `board.md` "is the shared plan — don't
edit it directly, `propose` changes to `god`, who is its sole scribe"
(`hive.ts:2985-2999`). The god's own prompt is the hub-and-spoke
contract in full: "decompose work and fan it out to the hive agents via
their inboxes… own only the important, high-leverage things — task
decomposition, dispatch decisions, sign-offs, conflict resolution, branch
integration, and final QA — and remain the sole scribe of board.md"; each
dispatch is a four-part contract (objective, output, tools, boundaries)
(`hive.ts:1585`). The design document names the pattern: a blackboard
architecture after Hearsay-II (`HIVE.md:23, 159`). Nothing in prompt or
code asks workers to deliberate among themselves; the message schema has
`agree` and `refuse` acts (`hive.ts:2979`) but no protocol uses them, and
there is no round, vote or synthesis step. A five-agent discussion of the
kind in Appendix A would, in this design, be the god sending one
`request` to each clone, each replying to the god's inbox, and the god
writing the conclusion to `board.md` alone — the clones would see each
other's answers only if someone used `broadcast`. The only all-hands
exchange in code is the shutdown handshake (below).

**Agent to agent.** A message carries a speech act: `request`, `query` and
`propose` expect a reply, `inform` and `done` are terminal — but only by
prompt; the router derives `requires_reply` and enforces nothing
(`hive.ts:1638, 2989-2991`). `HOP_CAP = 12` exists but hops are
incremented only when a message bounces to god (`hive.ts:205, 1659-1668`);
the regression test says the fuse "can only fire on a REAL relay loop"
(`test/hive-hop-cap.test.cjs:3-18`). What actually stops a runaway is a
per-agent circuit breaker on behaviour rather than on messages: repeated
identical tool calls, an error storm, token velocity, two no-progress
beats, escalating steer → constrain → stop (`src/main/breaker.ts:89-108`).
Delivery is a file write; a recipient is nudged when idle and its inbox is
non-empty, every 4 s (`src/renderer/src/hooks/useHive.ts:690-731`), so an
agent with no mail costs nothing and an agent with an `inform` spends a
turn reading it.

**Humans.** To any clone, yes — each has a terminal in the UI. Slack is
one bot and everything from it goes to god. A human prompt exempts the
agent from the breaker's no-progress arm (`breaker.ts:384-390`).

**Against its own use case.** Pros: the inbox is on disk and committed to
git, so an overnight crash loses nothing and the history is inspectable;
shutdown is a closing round — god broadcasts, each worker ACKs, the
harness counts ACKs and rejects a premature "complete"
(`src/main/closingTime.ts:10-18, 182-204`); the god's prompt carries every
agent's context occupancy so it can route by headroom (`src/main/hooks.ts:535-544`).
Cons: the stated loop rule is not implemented; the breaker is heuristic
and off (`hardStop: false`) by default; the product's own description of
its transport is wrong, which matters for a tool meant to hold company
code and credentials.

**Positioned otherwise.** OpenClaw is "your assistant" with optional
isolated personas; QM is one company bot ("multiplayer" means the
humans); jinn is "a persistent AI company" whose named employees receive
tickets from a Todo ledger; AutoGen, ADK, MetaGPT and crewAI are
frameworks whose agents are roles in a task. They are kept here because
each solves one of this document's sub-problems well, not because they
share its position.

#### OpenClaw

**Multi-agent, and for what.** Partly. The use case is a single person's
assistant across messengers. "Multi-agent" means two things: several
configured agents with separate workspaces, each bound to a channel
account by a routing rule (`src/routing/resolve-route.ts:389-435`), and an
agent spawning sub-agents for delegation. Several agents in one group chat
is not a designed use case; the QA suite exercises one bot plus one
observer (`extensions/qa-lab/src/live-transports/matrix/scenarios/scenario-runtime-allowbots.ts:15-29`).

**Topology.** Hub for delegation: a spawner and its children, depth at
most 5, and a child at the depth limit gets the role `leaf` and loses the
spawn tool entirely (`src/config/agent-limits.ts:20-24`;
`src/agents/subagents/spawn/subagent-capabilities.ts:154-158`). Peer
messaging exists as `sessions_send` between allow-listed sessions. Two
agents on two accounts in one group form a mesh by accident: each sees
the other as a bot-authored message.

**Agent to agent.** Three paths with three different brakes. Sub-agents:
bounded structurally by the leaf role, so a chain cannot deepen past the
limit. `sessions_send`: no hop or depth counter; the brakes are
allow-lists, a 20-item system-event queue per target, and the fact that
the tool-loop detector treats its results as volatile — that is, repeated
sends are exempt from loop detection (`src/agents/tool-loop-detection.ts:283-286`).
In-room bot-to-bot: a per-channel `allowBots` switch — `true`, `false`, or
`"mentions"` (bot posts need an explicit @-mention) — and a pair-loop
guard that counts events per unordered bot pair per conversation, 20 per
60 s then a 60 s cooldown (`src/plugin-sdk/pair-loop-guard-runtime.ts:83-88`).
When it trips the turn is dropped silently and the room's pending-history
window is cleared (`src/channels/turn/execution.ts:208-216`). An agent's
failure text goes out as its ordinary reply, so another bot can react to it.

**Humans.** Yes, to any agent, through the account it is bound to. Per
group a human sets `/activation mention|always` and `/queue <mode>`;
default queue mode `steer` injects a mid-run message at the next tool
boundary, bare stop words abort (`src/auto-reply/reply/abort-trigger-text.ts:4-22`).

**Against its own use case.** Pros: delegation cannot run away, because
the capability is removed rather than counted; messages that arrive while
the agent is busy are steered, collected or queued, and queue overflow is
summarised rather than dropped (`src/auto-reply/reply/queue/state.ts:47-49`);
an unmentioned group post is kept in a free pending-history window and
shown on the next real turn, or — opt-in — given a quiet turn of its own.
Cons, which only matter outside the single-user use case: the pair guard
is a rate limit that cannot tell a runaway from a long legitimate
exchange, it drops silently and discards context when it fires, and
nothing in the design lets several agents in one room coordinate.

#### QM

**Multi-agent, and for what.** Two different things. The Slack product is
one bot per workspace that many humans share — "multiplayer" means the
humans. Multi-agent exists only as an internal swarm API (`/v1/swarm`),
parent-spawned workers that message each other and never touch Slack.

**Topology.** Slack: single agent. Swarm: hub — a parent spawns members,
every message carries an explicit audience list, and the whole swarm has
fixed budgets set at spawn: 32 agents, depth 4, 128 messages, 256
notifications, one hour (`src/swarms/swarm-settings.ts:1-12`).

**Agent to agent.** In the swarm, a `send` names its recipients; the
sender is never notified of its own message, nothing replies
automatically, and when a budget is exhausted the store transaction
throws so the send is refused to the caller (`src/swarms/swarm-service.ts:617-644`).
In Slack, another bot's post is a first-class trigger: QM admits
`bot_message` events (`src/slack/message-gating.ts:48-58`), and if the bot
has a stake in the thread — it posted or was mentioned there — it runs a
small auxiliary model that answers `YES`, `NO` or `REACT <emoji>` before
spending a real turn (`src/core/orchestrator.ts:2474-2533`;
`src/harness/pi-harness.ts:263-282`). A classifier error means `NO`
(`:2577-2579`). There is no cap on bot-to-bot exchanges in Slack beyond a
per-actor rate limit of 60 turns per 60 s (`orchestrator.ts:684-690`);
two QM bots whose classifiers keep saying `YES` continue until one says
`NO`. The classifier is also harness-dependent: the OpenCode and Codex
harnesses do not implement it, and every stake-thread reply then runs a
full turn (`src/harness/harness-router.ts:278-279`).

**Humans.** To the one bot, yes, by mention or in a thread; only a human
can halt a running turn (`src/api/app-turn.ts:497`). Per channel an
operator sets standing orders and a ledger that classifies other bots'
posts as `ignore`, `rollup`, `action` or `user`
(`src/surface-cache/channel-policy-store.ts:4-6`).

**Against its own use case.** Pros: the turn queue is a Postgres table
with leases, a reaper and a unique idempotency key, so a redelivered
Slack event cannot run twice and a crashed turn is picked up
(`src/runs/postgres-run-store.ts:64-70, 252-278, 302-306, 527-557`); the
cheap classifier means an unprompted thread reply rarely costs a full
turn; `REACT` acknowledges with an emoji and no post. Cons: bot-to-bot in
Slack was not designed for and is guarded only by the classifier; the
swarm's budgets bound the whole swarm, not a conversation, and refuse
rather than degrade.

#### jinn

**Multi-agent, and for what.** Yes, as an org chart: each "employee" is a
CLI agent session with a title and a manager; work flows down by
delegation and back up by completion callbacks. Humans reach an employee
through Slack, Discord, Telegram or the web UI.

**Topology.** Tree with lateral links. Delegation goes parent → child via
`delegate_task`; a child's completion or error returns to the parent as a
callback turn (`sessions/callbacks.ts:101-118`). Lateral `send_to_session`
lets any session message any other, subject to guards. Through the chat
platforms, agents never see each other: every bot-authored `message`
event is dropped at the connector (`connectors/slack/index.ts:103-106`).

**Agent to agent.** Lateral sends are guarded at the HTTP route, so MCP
and curl are bounded alike: no self-message, 10 sends per sender per 10
minutes, a relay-hop budget of 12 carried as a tag with a 30-minute TTL
(`gateway/session-comm-guards.ts:48-50, 59-69`). Past the budget the send
is refused to the sending tool with a 429, and the receiver's prompt says
to stop and report to a parent or operator if "this exchange is going in
circles" (`:150-157, 255`). The cap was raised from 4 to 12 because a
"COO ↔ implementer ↔ QA" review is a legitimate three-actor cycle (`:53-56`).
Delegation cycles are outside both guards: parent → child → callback →
parent delegating again is bounded only by a stop-nudge limit and
workflow concurrency (`sessions/stop-nudge.ts:20`; `workflows/model.ts:56`).
Guard state is in memory and intentionally not persisted (`:10-20`).

**Humans.** To any employee, yes — each is its own Slack app, and the web
UI can message any session. The two doors differ: a human message through
the web API resets the target's relay-hop chain and supersedes its running
turn (`gateway/api.ts:4185-4190, 4280-4286`); the same message through a
chat connector queues behind the running turn (`sessions/manager.ts:263-271`).
An operator-authenticated relay carries hop 1 and bypasses the lateral cap
— the escape hatch once two agents have spent their budget (`api.ts:4153-4180`).

**Against its own use case.** Pros: the org tree gives every piece of work
one owner and one return path, which is what an "employees" product needs;
the hop count is shown to the human in the message banner
(`session-comm-guards.ts:250-256`); the per-session turn queue is SQLite,
so queued work survives a restart. Cons: the lateral cap had to be
loosened to admit real review loops and is still a bound rather than a
judgement; delegation cycles are not capped; a human in chat cannot
interrupt what a human in the web UI can.


#### AutoGen

**Multi-agent, and for what.** Yes, in process: a "team" of agents solving
one task through a group chat, with the pattern chosen by the developer —
round robin, model-selected speaker, handoff swarm, Magentic-One's
orchestrator, or a graph.

**Topology.** Managed group chat. One manager per team receives every
message, picks the next speaker and publishes a request to it; every
participant receives every message and buffers it (`_base_group_chat_manager.py:172-193`;
`_chat_agent_container.py:57-73`). Only GraphFlow runs several speakers
at once (`_digraph_group_chat.py:392-425`). The `Swarm` variant is a mesh
in intent — an agent hands off to a named peer by tool call — but still
one speaker at a time through the manager (`_swarm_group_chat.py:82-98`).

**Agent to agent.** Through the manager only. The selection is a rule
(round robin, handoff target, graph edge) or one model call per hop over
the whole transcript, with a deterministic `selector_func` that
short-circuits the model when it answers and a three-attempt fallback to
the previous speaker (`_selector_group_chat.py:163-214, 302-308`). Loops
end by team-wide `max_turns` or by composable termination conditions —
message count, a keyword, a stop message, a handoff to a non-participant
(`conditions/_terminations.py`). At `max_turns` a stop message is emitted
and the last response stays (`_base_group_chat_manager.py:214-227`).
Magentic-One instead asks its orchestrator two structured questions per
hop — is progress being made, is this a loop — counts stalls with
hysteresis, and at `max_stalls` (3) re-plans and clears every buffer
rather than stopping (`_magentic_one/_magentic_one_orchestrator.py:393-406, 451-476`).
A participant that is selected must respond; silence is an error
(`_chat_agent_container.py:139-142`). A participant error stops the team.

**Humans.** Not freely. A human is a `UserProxyAgent` participant who
speaks only when selected (`agents/_user_proxy_agent.py:169`); outside
that there is a cancellation token and pause/resume.

**Against its own use case.** Pros: every speaker has read everything
before speaking, which is what makes "I have nothing to add" decidable;
the stop rules are explicit and composable; Magentic-One's ledger is the
one place a loop is detected by content rather than by count. Cons: the
whole thing is one process and one task — there is no room, no
concurrent conversations, and no human who can walk in; a failure ends
the team; persistence is an explicit `save_state` call.

#### Google ADK

**Multi-agent, and for what.** Yes, as a tree: a developer composes agents
into Sequential, Parallel and Loop containers or lets an LLM agent
transfer control to a named sub-agent, parent or peer.

**Topology.** Tree. A user message is routed to one agent by scanning the
session log (`_agent_router.py:133-231`); within a run an LLM agent may
call `transfer_to_agent`, validated against the targets it declared
(`flows/llm_flows/core/_function_call_postprocessor.py:31-59`). Parallel
branches do not see each other's events — history is filtered to the
branch's ancestors (`flows/llm_flows/context/_contents.py:752-765`) — and
pass results through shared state keys.

**Agent to agent.** Transfer is the only path, and it is a tool call the
model makes inside one invocation. Loops are bounded by `LoopAgent.max_iterations`
or an `escalate` action, by a per-invocation `max_llm_calls` of 500 that
raises when exceeded (`invocation_context.py:57-88`), and structurally by
`disallow_transfer_to_parent` / `_peers` flags. An empty response is an
error, not a decline (`flows/llm_flows/core/_model_call.py:60-101`).

**Humans.** One user message per invocation, routed by the framework; a
human cannot pick which agent to address. An abort signal cancels a run.

**Against its own use case.** Pros: transfers are enum-constrained in the
tool schema and re-checked at execution, so an agent cannot reach an agent
it was not given; the event log is durable when a database session
service is used. Cons: parallel agents are blind to each other by design
and silence is forbidden — fine for a pipeline, wrong for a conversation.

#### MetaGPT

**Multi-agent, and for what.** Yes: roles (product manager, architect,
engineer, …) that turn a requirement into a code base, run in rounds.

**Topology.** Publish/subscribe with an optional hub. In the base
environment every message lands in every role's buffer and a role reacts
to it if it is subscribed to the message's *type* — the action that
produced it — or named in `send_to` (`metagpt/roles/role.py:410-412`;
`metagpt/environment/base_env.py:175-195`). The default environment adds
a TeamLeader that receives everything and chooses recipients with a tool
call (`metagpt/roles/di/team_leader.py:75-85`) — an orchestrator, but a
full agent turn, not a classifier.

**Agent to agent.** Through the environment, by publishing. Roles in a
round run concurrently with `asyncio.gather`, each having drained its
buffer before its first model call, so a peer's output from this round is
seen next round (`base_env.py:197-211`; `role.py:399-406`). Loops end by
`n_round` (exits silently), by a dollar budget checked between rounds
that checkpoints the team and stops, or when every role is idle
(`metagpt/team.py:98-100, 128-138`). A role's "nothing to do" is a model
call that returns state `-1` (`role.py:359-378`). A role exception aborts
the round.

**Humans.** The requirement at the start; a human `@role` message goes to
that role directly, bypassing the TeamLeader (`mgx_env.py:30-38`); a role
at its react limit asks the human whether to continue
(`metagpt/roles/di/role_zero.py:327-334`).

**Against its own use case.** Pros: subscription by message type means a
role reacts to "a design was produced", not to "someone mentioned me",
which is the right trigger for a pipeline; idle detection ends a run
without a model call; `observe_all_msg_from_buffer` lets a role be aware
of messages it does not act on (`role.py:413-418`). Cons: in-memory
buffers, a failure aborts the round, and the cost budget can overshoot by
a round.

#### crewAI

**Multi-agent, and for what.** Yes: an ordered list of tasks, each owned
by one agent, run sequentially or by a manager that assigns them.

**Topology.** Pipeline or hub. In the hierarchical process the manager is
the executing agent of every task and the only model-chosen hop is its
decision to call a delegation tool (`crew.py:1727-1730`;
`tools/agent_tools/agent_tools.py:19-36`). Async tasks run in threads and
see only the last synchronous output (`crew.py:1610-1613`).

**Agent to agent.** Delegation is a tool call that runs the delegate
synchronously and returns its answer — or its error — as the tool result
(`tools/agent_tools/base_agent_tools.py:112-124`). A delegate cannot
delegate back, because delegation tools are injected per task and the
delegate's executor is built from its own tools, which lack them
(`agent/utils.py:309-310`) — a structural bound, with no counter. Per
agent `max_iter` (25) ends in one forced final answer, not a drop
(`utilities/agent_utils.py:366-402`). Remote agents over A2A get a
`max_turns` of 10 with a prompt warning at 9 and a demand to conclude at 10
(`a2a/wrapper.py:713-727`).

**Humans.** Only as a review step after a task finishes, blocking on
terminal input (`core/providers/human_input.py:319-367`); nothing is
interrupted.

**Against its own use case.** Pros: simple, every task has one owner, a
SQLite log of task outputs allows replay from any step. Cons: not a
conversation at all — agents meet only through context strings and tool
results.

### B.2 The two teammate products, against this document's questions

| | Buzz | munder-difflin |
|---|---|---|
| Who hears a post addressed to one agent | nobody else (relay filter); a thread-mate catches up at its next mention, a channel-mate only by pulling | the addressee only; `broadcast` is explicit |
| Can agent A address agent B | yes, by mention, if B's author gate admits A (default: same owner) | yes, by id; nothing restricts it |
| Does a later agent see earlier agents' replies | in a thread, yes, at its own dispatch time (a race, not an order); in the channel, no | only if mailed to it |
| What stops A ↔ B | prompt: silence is correct, no bare acknowledgement, mention only for completed work; no counter | prompt: `inform` / `done` are terminal; a behavioural breaker on repetition and no-progress; the hop cap does not apply |
| What stops talk without substance | the same prompt rules; a human's question must always be answered, an agent's need not | nothing specific; the breaker's no-progress arm |
| Human to each agent | the owner, freely; others by allow-list | any clone has a terminal; the design says brief Michael only |
| A conclusion from several agents | none — each answers its mention | the god synthesises into `board.md`; no round among the clones |
| Agents tested together | none with two identities | three in tests; "5–15" `[docs-only]` |

Both products put the whole weight of loop prevention and of "nothing to
add" on the prompt, and both make silence free at the transport: in Buzz
an agent that does not call `send` has posted nothing, in munder-difflin
an agent that writes no file has sent nothing. Neither counts turns.

**Buzz against this gateway today.** The two look alike — peers on a
shared chat, addressed by mention, no coordinator, a full model call to
decide to stay silent — and differ on one rule that explains why one has
a chain limit and the other does not. Here, the mention gate applies to
humans only: a post from another agent is dispatched to every bot in the
room whether or not it names them (`gateway/connectors/rocketchat/normalize.py:196-214`,
"agent senders bypass @mention requirement (listen-all for agents)";
`gateway/connectors/mattermost/normalize.py:222`). In Buzz the gate
applies to agents exactly as to humans, at the relay, and the receiver's
author gate applies on top. So an agent's post here wakes N−1 bots, each
of which must spend a turn deciding whether to answer, and the per-sender
budget exists to bound what that creates; an agent's post in Buzz wakes
the agents it names and no one else, and the cost of that is the other
half of B.1's Buzz entry — an agent that was not named has not seen the
post, and catches up only in a thread or by pulling. §7's "unmentioned
bot posts as queue-only" is this one rule flipped, with the queue
supplying the context Buzz's design leaves to the agent.

Buzz does have the queue's mechanism, for threads, and it is built the
other way round. When an agent is mentioned in a thread the harness
fetches that thread from the relay, drops every event this session has
already been given, and injects the remainder as `<thread-context>` in
the same prompt (`pool.rs:4055-4073`, `:4136-4150`). The only state is
the set of delivered event ids per session (`pool.rs:119, 240`); there is
no stored queue, because the relay already holds every post and the
fetch is the queue. That is §7's recovery answer for a restart —
"rebuilt from room history since the bot's last delivered post" — used
as the primary path, with the same effect as §3.5's "delivered as a
batch at the bot's next turn": late-arriving posts, including other
agents' replies and humans' unaddressed ones, reach the agent on its next
turn without a turn of their own. Three things differ. It covers a thread
only; a top-level channel post fetches nothing (`pool.rs:4155-4162`),
whereas the queue is room-wide. It is bounded by a message limit (12,
max 100), and what falls outside it is silently gone, whereas
`queue_flush_threshold` turns a long gap into a catch-up turn so nothing
is truncated. And it fills only when the agent is mentioned anyway;
nothing earns a turn from the size of the gap. The first and second are
the design choices that separate the two; the construction — watermark
plus fetch, no stored structure — is one this gateway already has half
of, in the history handoff that seeds a new session
(`gateway/core/history_context.py`) and in the reconnect replay
(`Connector.replay_room_since`). §3.5 adopted it on 2026-10-13: the queue
is a watermark and a query, not a stored structure.
Neither has a mechanism for several agents to converge on one answer in
the room; Buzz has no synthesis step at all, munder-difflin's happens in
one agent's head. Neither has been exercised with the number of agents
that Appendix A measures.

### B.3 Where this proposal sits

This gateway is the third product in B.1's first tier: agents with
personas, on a chat platform humans already use, addressed by mention,
talking to humans and to each other. Appendix A is the measurement the
two teammate products do not have — what happens when five such agents
are actually put in one room and asked to converge — and §1.2 is what it
showed: the prompt-only discipline that Buzz and munder-difflin rely on
holds for roughly half to two thirds of turns, and the convergence phase
is where it fails. The proposal keeps everything the tier shares —
silence is free, humans address anyone, the platform is the transport,
nothing is removed from anyone's view — and adds the two things neither
teammate product has: a reader of the room that decides who speaks next
and whether a topic is done, and a round in which agents converge. Those
two things exist only in AutoGen's group chats, which have no room and no
human visitor; the mesh gateways outside the tier (OpenClaw, QM) stop at
per-receiver rules and rate guards.

Four of the seven other projects contributed a mechanism to §7: QM's
fail-closed classifier and AutoGen's selector fallback (moderator error
per decision), Magentic-One's progress ledger (the input to `extend` /
`close`), OpenClaw's and QM's rate guards (a backstop), and OpenClaw's
`allowBots: "mentions"` together with Buzz's relay filter (unmentioned
bot posts as queue-only). Two things no project changes: every framework
delivers context to an unselected agent without a model call, so §7's
"suppressed turn" default is the minority choice and is marked as such;
and no project has run more than five agents in one shared conversation.
