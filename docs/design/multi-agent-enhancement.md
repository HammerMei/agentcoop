# Multi-agent enhancement

Status: **proposal, not implemented.** This document proposes replacing the
per-sender agent-chain limit with a moderated model for multi-agent rooms. It is a design
direction, not a specification; §6 lists the assumptions that have to hold
before any of it is built.

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
   A parallel plan ends when every selected bot has replied, timed out or
   self-terminated; a sequential plan ends at `done` or when its required
   participants are exhausted. When a plan ends, the moderator looks at its
   replies once and decides `next` / `done` for the topic — so an `@all`
   round is followed by at most one sequential continuation, not by one
   plan per reply. One topic has at most one plan in flight.
3. *Does it start a plan?* Only a post attributed to no running plan — a
   human post, or an agent post made outside any plan, such as a scheduled
   fire — is dispatched by the table above.

The rules that follow — hop timeouts, late replies, the human gap — are
cases of this paragraph, not exceptions to it.

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
sees the message in a batch together with the replies made meanwhile, and
decides as usual whether to add anything. A bot that was explicitly
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
  closing round when it closes. A mistake in it will be acted on.
- A topic that was **discussed** without a decision waits for the end of the
  conversation. A late correction costs nothing until someone acts on the
  error.

**A conversation ends when its last open topic closes** — by `close`, by
TTL, or at the ceiling — or when a human says it has. That is the whole
boundary rule, and it is provisional (§7): a room is one conversation at a
time, the next human post after the end opens the next one, and an
agent-only room ends its conversations by TTL. TTL expiry is observed at
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

The turn runs; its reply is suppressed by the same path that already handles
a self-terminated reply. The bot's context stays complete, it knows what
happened, and there is nothing to buffer. The cost is one suppressed turn,
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
- **Age-based queue flush.** The queue bound is a count. Whether a queue
  that is small but old also warrants a catch-up turn is open; nothing in
  the logs says yet that it does.
- **Scheduled and agent-only rooms.** With no human to open topics or tune
  limits, the moderator and the TTL are the only controls. Whether that is
  enough, or whether such rooms need a stricter default, is untested.

---

## 8. Work breakdown

Each step ships on its own, behind a config flag, and produces a number
from the lab room before the next one starts. The current three layers
stay in force until step 7 replaces Layer 2; nothing before that changes
what a bot sees. Production stays on today's behaviour throughout.

| # | Step | Needs | What it proves | Measured by |
|---|---|---|---|---|
| 1 | **Topic tags in the agent prompt.** Agents name the topic they answer and any they open; the gateway parses and logs the tag, nothing else changes. | — | Agents tag reliably enough to be a signal (§6.1 precondition). | Tag presence rate; hand-check a sample. |
| 2 | **Moderator in shadow.** The classifier runs on every inbound message and logs every decision it can make from messages alone — topic tags `accept` / `retag`, `extend` / `close` and `decided` / `discussed` at each point today's Layer 2 or TTL closes an exchange, `parallel` / `sequential` for each multi-mention message — and none of it has any effect. This is the first moderator wiring, without authority. | 1 | Accuracy of each decision type (§6.1, §6.3), measured separately. | Agreement with hand labels on the lab transcript, per decision type. |
| 3 | **Per-bot message queue.** A message can enter a bot's queue without starting a turn; the queue is delivered as a batch at the bot's next turn; `queue_flush_threshold` grants a catch-up turn. First consumer: the gateway's own failure notices, which today each start a turn (§3.3). | — | The "no turn, still seen" primitive works; the error cascade is gone. | Error-notice turns per failure: today 4, target 0. |
| 4 | **Rule-based dispatch plan.** The plan is computed from addressing alone — single mention, `@all`, multi-mention in mention order, no mention in random order, explicit `parallel:` / `sequential:` — with no moderator (§3.3 table). | 3 | The plan model fits the room without classification. | Posts per human prompt (today ~25 in the lab); self-termination rate. |
| 5 | **Sequential engine.** Runs a plan hop by hop: busy skip, `hop_timeout_seconds`, `human_gap_seconds`, late replies joining the running sequence (§3.3). The moderator shadows `next` / `done` here, against the rule-based order, since this is the first point those decisions have an input. | 3, 4 | Sequential raises self-termination and leaves room for the human (§6.2, §6.4); `next` / `done` accuracy. | Self-termination rate under sequential vs today; human posts landing inside a gap; shadow `next` vs the bot that actually had something to say. |
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
| `next` / `done` | 5 | 7 (`next` / `done`) | shadow `next` vs the bot that had something to say |
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
