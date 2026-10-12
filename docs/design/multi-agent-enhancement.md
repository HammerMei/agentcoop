# Multi-agent enhancement

Status: **proposal, not implemented.** This document proposes replacing the
per-sender agent-chain limit with a moderated model for multi-agent rooms. It is a design
direction, not a specification; §6 lists the assumptions that have to hold
before any of it is built. Appendix B compares each decision against nine
projects read at code level.

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

**The queue is bounded.** A bot that is never selected would otherwise
accumulate the whole conversation unseen. When its queue passes
`queue_flush_threshold` posts, the gateway grants it a catch-up turn at the
next hop boundary: it receives the queued batch with the usual instruction
— add something, or answer with the token. If it posts, the post counts
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
  conversation; the degraded window is logged. Queues are the exception,
  because the platform already holds every post durably: a queue is
  rebuilt from room history since the bot's last delivered post, the way
  history handoff rebuilds a new session today, so "nothing is dropped"
  survives a restart. Whether reconciliation of counts is ever worth its
  cost is for the step 7 PR to decide against an observed outage.
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
| 3 | **Per-bot message queue.** A message can enter a bot's queue without starting a turn; the queue is delivered as a batch at the bot's next turn; `queue_flush_threshold` grants a catch-up turn. First consumer: the gateway's own failure notices, which today each start a turn (§3.3). | — | The "no turn, still seen" primitive works; the error cascade is gone. | Error-notice turns per failure: today 4, target 0. |
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

## Appendix B — Prior art

Nine projects were read at code level on 2026-10-12, keyed to the
decisions in §3.1 and the open questions in §7. Five are chat gateways in
the same position as this gateway — several agents in a shared room; four
are in-process multi-agent frameworks, where "who speaks next and when to
stop" is the framework's whole job. Citations are `path:line` at the
commit below; `[docs-only]` marks a claim that was found in a README and
not traced to code. Where the project's own description disagreed with
its code, the code is reported.

| Project | Commit | What it is |
|---|---|---|
| `block/buzz` | `799cfd7` | Nostr-relay team chat; each agent is a channel member with its own keypair, bridged to an ACP harness (`crates/buzz-acp`) |
| `openclaw/openclaw` | `5559404e` | self-hosted personal agent gateway into ~25 chat channels |
| `yc-software/qm` | `2c94475` | one Slack bot = one core; Postgres-backed turn queue |
| `hristo2612/jinn` | `3ae6465` | gateway daemon running "employee" agents behind Slack / Discord / Telegram |
| `chaitanyagiri/munder-difflin` | `241d1e5` | Electron desktop; CLI agents with file mailboxes and a privileged "god" agent. Its site describes peer-to-peer encrypted agent messaging; the code is a central file router with no transport encryption found |
| `microsoft/autogen` | `027ecf0` | AgentChat group chats: RoundRobin, Selector, Swarm, Magentic-One, GraphFlow |
| `google/adk-python` | `b3cbb47` | agent trees: Sequential / Parallel / Loop agents, LLM-driven transfer |
| `FoundationAgents/MetaGPT` | `11cdf46` | role agents on a publish/subscribe environment, run in rounds |
| `crewAIInc/crewAI` | `744bf60` | ordered task list, sequential or manager-driven |

### B.1 Decision by decision

**Who speaks.** None of the five gateways has a selector: the sender
decides by mentioning. Buzz filters non-mentions at the relay, so an
unmentioned post costs the harness nothing
(`crates/buzz-acp/src/relay.rs:3366-3372`); OpenClaw routes each message
to exactly one agent by a first-match binding rule
(`src/routing/resolve-route.ts:389-435`) and gates bot-authored posts with
`allowBots: true | false | "mentions"`
(`extensions/discord/src/monitor/message-handler.preflight.ts:197-199`);
jinn drops every bot-authored post at the connector
(`packages/jinn/src/connectors/slack/index.ts:103-106`). QM is the
exception that matters here: a bot with a stake in a thread runs a
**small-model `YES` / `NO` / `REACT` classifier** before any real turn
(`src/core/orchestrator.ts:2474-2533`, verdict parse
`src/harness/pi-harness.ts:263-282`), and a classifier error yields `NO`
(`:2577-2579`). Among the frameworks, AutoGen's `SelectorGroupChat` is the
moderator of §3.1 in full: one model call per hop over the whole
transcript, name parsed from the reply, up to `max_selector_attempts`
(default 3) and then a fallback to the previous speaker or the first
participant (`_selector_group_chat.py:163-214, 302-308`). A deterministic
`selector_func` runs first and skips the model when it answers
(`:163-177`) — the shape of step 4 (rule-based plan) feeding step 7.
MetaGPT's default environment routes every message through a TeamLeader
agent that picks recipients with a tool call
(`metagpt/roles/di/team_leader.py:75-85`) — an orchestrator, but a full
agent turn, the opposite cost trade-off from a classifier.

**Ordering, and what a later speaker sees.** The gateways have no
ordering: agents mentioned together fire in parallel from independent
processes, and a later one sees an earlier reply only by re-reading the
platform at its own dispatch time (Buzz `pool.rs:4105-4170`; QM re-imports
unseen Slack history as "overheard", `src/core/orchestrator.ts:2924-2935`).
AutoGen is strictly one speaker at a time, and every participant buffers
every broadcast so the next speaker has read everything before it
(`_base_group_chat_manager.py:149-164`). Parallel branches do not see each
other anywhere they exist: ADK's `ParallelAgent` filters history to the
branch's ancestors (`src/google/adk/flows/llm_flows/context/_contents.py:752-765`),
crewAI's async tasks get only the last synchronous output
(`lib/crewai/src/crewai/crew.py:1610-1613`), AutoGen's GraphFlow fan-out
hands each branch the buffer as of the fan-out
(`_digraph_group_chat.py:392-425`). §3.3's "parallel participants receive
the triggering post only" is the same rule.

**Budget, and what happens at it.** The gateways cap by rate, not by turn:
OpenClaw counts events per unordered bot pair per conversation, 20 per 60 s
with a 60 s cooldown (`src/plugin-sdk/pair-loop-guard-runtime.ts:83-88`);
QM 60 turns per actor per 60 s (`src/core/orchestrator.ts:684-690`); jinn
10 agent-initiated sends per sender per 10 minutes plus a 12-hop relay cap
(`packages/jinn/src/gateway/session-comm-guards.ts:48-50, 59-69`).
munder-difflin declares `HOP_CAP = 12` but increments hops only when a
message bounces to the god agent (`src/main/hive.ts:205, 1659-1668`), so
agent-to-agent chains are unbounded in code; its stated rule is
`[docs-only]`. At the cap, the two gateways that act both **drop
silently** — OpenClaw also wipes the room's pending-history window on the
drop (`src/channels/turn/execution.ts:208-216`) — which is the behaviour
§1.2 measured the cost of. The frameworks never drop: AutoGen emits a
`StopMessage` and the tripping response stays in the thread
(`_base_group_chat_manager.py:214-227`); Magentic-One spends one more call
on a final answer (`_magentic_one_orchestrator.py:478-512`); crewAI forces
a final answer with one extra call at `max_iter`
(`utilities/agent_utils.py:366-402`); ADK raises
(`invocation_context.py:57-88`); MetaGPT exits the round loop
(`metagpt/team.py:128-138`). Two prior-art details bear on §1.2 directly.
crewAI's A2A wrapper warns the remote agent at `max_turns - 1` and demands
a conclusion at `max_turns` (`a2a/wrapper.py:713-727`) — the same prompt
pattern as today's "wrap up gracefully", in a point-to-point setting where
there is no counterpart counter to collide with. And Magentic-One's stall
counter is the only progress-based budget found: `+1` when the ledger says
no progress or a loop, `-1` otherwise, and at `max_stalls` (default 3) it
**re-plans and clears every participant's buffer** rather than stopping
(`_magentic_one_orchestrator.py:393-406, 451-476`).

**Termination signal and its cost.** Everyone pays a full model call for
"nothing to add": Buzz never auto-posts, so silence is the agent ending
its turn without calling the send tool (`crates/buzz-acp/src/acp.rs:1836-1841`);
OpenClaw's `NO_REPLY` token (`src/auto-reply/tokens.ts:6`) is this
gateway's `<end-of-agent-chain>`; an AutoGen participant that is selected
must produce a response or the team errors
(`_chat_agent_container.py:139-142`); ADK rewrites an empty response into
an error (`_model_call.py:60-101`). The two cheaper paths are QM's
classifier above and MetaGPT's TeamLeader addressing a reply to `no one`,
which the environment drops (`mgx_env.py:48-51`) — still a full turn. QM's
`REACT` verdict, an emoji instead of a post decided in the same classifier
call (`pi-harness.ts:255-260`), is an acknowledgement that costs no turn
and no room post.

**Topic segmentation.** No project has a conversation-topic id. Session
scope is the platform's thread or channel everywhere (Buzz
`crates/buzz-acp/src/scope.rs:1-22`; OpenClaw session keys
`resolve-route.ts:99-123`; QM `src/slack/message-gating.ts:136-142`). The
nearest things: MetaGPT roles subscribe by **message type** — the action
that produced the message — not by addressee (`metagpt/roles/role.py:410-412`);
QM keeps a per-channel operator policy for other bots' posts,
`ignore | rollup | action | user`
(`src/surface-cache/channel-policy-store.ts:4-6`); munder-difflin carries
`conversation` and `in_reply_to` fields that routing never reads
(`src/main/hive.ts:1623-1624`). §3.2 is therefore the least-precedented
part of this proposal, which is consistent with §6.1 putting it first.

**Non-selected agents: queue, context-only delivery, durability.** This
is where the prior art is most uniform. Every framework delivers context
without a model call: AutoGen buffers every broadcast per participant and
hands the whole buffer over when that participant is next selected
(`_chat_agent_container.py:57-73, 161-165`); ADK's agents re-read the
shared event log (`_contents.py:458-523`); MetaGPT's
`observe_all_msg_from_buffer` stores unreacted messages to memory
(`metagpt/roles/role.py:413-418`). OpenClaw has both prices in one
codebase: a pending-history window that is injected into the next
dispatched turn at no cost — capped at 50, cleared after every turn, lost
on restart (`preflight.ts:733-744`; `src/channels/turn/execution.ts:50,204`) —
and `unmentionedInbound: "room_event"`, which spends a quiet turn per
unmentioned post (`src/config/zod-schema.messages.ts:25`). Nobody has a
count threshold that earns a queued agent a turn; OpenClaw's `collect`
mode coalesces after a quiet window [docs-only `docs/concepts/queue.md:46`],
a time-based cousin of `queue_flush_threshold`. Overflow is summarised,
not dropped (`src/auto-reply/reply/queue/state.ts:47-49`). Durability
splits three ways. In-memory with explicit save: AutoGen (`save_state`,
`_chat_agent_container.py:197-213`), MetaGPT (buffer `exclude=True`,
`role.py:100-102`), OpenClaw, jinn's guard state (deliberately,
`session-comm-guards.ts:10-20`). Durable queue: QM's `runs` table with
`FOR UPDATE SKIP LOCKED` claims, lease expiry, a reaper, and a unique
`idempotency_key` so an at-least-once connector cannot double-run a turn
(`src/runs/postgres-run-store.ts:64-70, 252-278, 302-306, 527-557`);
jinn's SQLite `queue_items`; munder-difflin's inbox directory committed
to git (`src/main/hive.ts:1647-1652, 2893`). **Durability as a pull from
the platform**: Buzz keeps no agent-side state and instead has the relay
persist a per-pubkey mention index that the agent catches up from on a
heartbeat (`schema/schema.sql:90`; `crates/buzz-acp/src/lib.rs:5940-5948`)
— the same answer §7 gives for queues (rebuild from room history), with
jinn's "guard state is a fresh start after restart" matching §7's "reset,
not reconciliation" for counts.

**Failure notices.** Every gateway posts a failure into the room from the
failing bot's own account, and in all but one the notice is an ordinary
bot post that other bots can act on — OpenClaw gates it only by `allowBots`
and the pair guard (`src/auto-reply/reply/agent-runner-error-handler.ts:142-145`),
QM passes it through the other bots' classifiers
(`src/slack/message-gating.ts:55`). Buzz is the exception: the notice is
built with an empty mention list, so it carries no `p` tag and cannot wake
a mention-gated agent (`crates/buzz-acp/src/pool.rs:5540-5548`). That is
§3.3's rule — a failure notice queues, never dispatches — implemented at
the addressing layer. The frameworks either stop the whole team on a
participant error (AutoGen `_chat_agent_container.py:150-159`; ADK
`parallel_agent.py:120-131`; MetaGPT `base_env.py:210`, `gather` without
`return_exceptions`) or return the failure to the delegating agent as a
tool result (crewAI `base_agent_tools.py:121-124`; jinn `child-error`
callback `sessions/callbacks.ts:106-118`).

**Human messages.** No project resets a budget on a human post, because
no project keeps a per-sender budget. The closest precedents: jinn clears
the relay-hop chain and supersedes a running turn when a human writes via
the web API — but a human writing via Slack only queues behind the running
turn (`packages/jinn/src/gateway/api.ts:4185-4190, 4280-4286`;
`sessions/manager.ts:263-271`); Buzz's default `steer` cancels the running
turn and re-prompts with the new message framed as having arrived mid-work
(`crates/buzz-acp/src/config.rs:67-80`); OpenClaw's `steer` injects at the
next tool boundary and bare stop words abort
(`src/auto-reply/reply/abort-trigger-text.ts:4-22`). In QM only a human
can halt a run (`src/api/app-turn.ts:497`). Nobody has a `parallel:` /
`sequential:` directive; OpenClaw's per-room `/activation` and `/queue
<mode>` commands (`src/auto-reply/group-activation.ts:10-27`) are the
nearest human-set dispatch control.

**Coordinator topology.** No project coordinates across processes. Each
gateway's shared state is a module-level or process-global map (OpenClaw
`src/channels/turn/bot-loop-protection.ts:20`; jinn
`session-comm-guards.ts:10-20`); two gateways in one room share nothing.
AutoGen's manager is one in-process agent per team; the runtime is
pluggable and a gRPC runtime exists, but group-chat tests run only the
single-threaded one (`tests/test_group_chat.py:214-222`). QM scales one
bot across hosts through the database, not across bots
(`postgres-run-store.ts:269`). §3.1's "one moderator per room, in one
gateway" is where every project is; §7's cross-gateway question is open
everywhere.

**Scale.** No project has a test or benchmark with more than five agents
in one shared conversation. AutoGen tests reach four participants
(`tests/test_group_chat.py:712, 747, 763`); MetaGPT's default roster is
five roles (`metagpt/software_company.py:45-53`); jinn's storm test is a
five-session relay chain (`gateway/__tests__/session-comm-storm.test.ts:28-33`);
OpenClaw's live scenario is one bot plus one observer; munder-difflin
says "5–15 agents" [docs-only `HIVE.md:55`] and tests three. Buzz's
`--agents 1..=32` is N subprocesses sharing one identity
(`crates/buzz-acp/src/config.rs:302-305`), throughput rather than
collaboration. The ten-agent room is untested ground for everyone.

### B.2 What this changes

Each line is one thing the prior art does that this proposal lacks or
contradicts, with the response.

| Finding | Response |
|---|---|
| Context-only delivery costs no model call in every framework and in OpenClaw's pending-history window; only OpenClaw's `room_event` mode pays a turn per post, as an explicit opt-in. | **Evidence for §7 "context-only delivery"**: the proposal's default — a suppressed turn, costed as one — is the minority choice. The default stands until the step 3 PR, which should weigh a context-only backend input against this. |
| Buzz's durable inbox is a pull from the relay's persisted mention index; jinn drops guard state on restart by design. | **Confirms §7 "state durability"** on both halves: queues rebuilt from room history, counts reset. Two independent precedents; no change. |
| QM's `shouldRespond` classifier runs on a small auxiliary model, returns a one-word verdict, and fails closed to `NO`. | **Confirms §3.1's moderator as a classifier**, and adds a rule the proposal lacks: **what a moderator error means per decision**. AutoGen's selector gives the shape — retry up to N, then fall back to the rule-based choice (previous speaker / step 4 order). Added to §7. |
| AutoGen's `selector_func` runs before the model and short-circuits it. | **Confirms the step 4 → step 7 order**: the rule-based plan is the selector function, and the moderator only decides where the rule does not. No change. |
| Buzz's failure notice carries no mention tag, so it cannot wake a mention-gated agent. | **Confirms §3.3** "a failure notice queues". The addressing-layer implementation is a candidate for step 3: a notice with no mention is, under the step 4 rules, a post that starts no turn. No change to the design. |
| Magentic-One's budget is progress-based with hysteresis, and at the cap it re-plans instead of stopping. | **Open question for §3.2**: `extend` / `close` is decided by the moderator at the ceiling; the ledger's `is_progress_being_made` / `is_in_loop` fields are the structured output shape to ask for, and would make the principle "a loop is zero new information" the moderator's actual input rather than a turn count. Added to §7. |
| All three gateways that act at a cap use a **rate** guard (events per pair or per sender per window), orthogonal to any turn count. | **Deliberately different**: §3.2's per-topic per-sender budget is finer, and §1.2 showed the cost of a blind cap. A rate guard as a last-resort backstop under §3.6 is cheap and does not conflict; whether it is worth its tuning surface is for step 7. Added to §7. |
| OpenClaw's `allowBots: "mentions"`: a bot-authored post starts a turn only when it explicitly mentions the receiver. | **Open question for step 4**: the rule table dispatches unmentioned posts in random sequential order. "Bot post without a mention queues, does not dispatch" is a cheaper rule that two gateways (OpenClaw, Buzz) default to. Added to §7. |
| Parallel siblings never see each other in ADK, crewAI or AutoGen GraphFlow. | **Confirms §3.3** parallel semantics. No change. |
| crewAI's A2A wrapper uses the same "warn at n−1, conclude at n" prompt as today's Layer 2. | **Confirms §1.2's diagnosis** as a pattern, not a local bug: the prompt is standard; the drop comes from pairing it with per-receiver counters that reach their final turn together. No change. |
| QM's `REACT` verdict acknowledges with an emoji from the classifier, no turn and no post. | **Noted, not adopted**: the closing round's `agree` is a token-only reply (§3.4) which the token path already suppresses; an emoji ack would save the turn, not the post. Out of scope. |
| munder-difflin's closing time: god broadcasts, every worker ACKs, the harness counts ACKs and rejects a premature completion (`src/main/closingTime.ts:10-18, 182-204`). | **Confirms §3.4's structure** — the only shared-room closing round found, used for shutdown. No change. |
| No project has a conversation-topic id, a per-topic budget, or a human-set `parallel:` / `sequential:` directive. | **No precedent, by design.** §6.1 already puts topic tagging first among the assumptions to validate; this is the confirmation that it is the proposal's main bet. |
| Nobody has exercised more than five agents in one shared conversation. | **No precedent.** The ten-agent question stays empirical; the lab is the only instrument. |

Four lines in §7 were added from this appendix: moderator error handling
per decision, a progress-based input to `extend` / `close`, a rate guard
as a §3.6 backstop, and unmentioned bot posts as queue-only under step 4.
