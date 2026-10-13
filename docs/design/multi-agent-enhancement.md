# Multi-agent rooms

Status: **proposal, not implemented.** This document specifies how a room with
several agents is run: what starts an agent's turn, what every agent sees,
what stops a run of agent turns, and where the orchestration of a
conversation lives. §7 is the implementation sequence; §8 the open points.

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
seconds, because every agent was triggered at once and none could see the
others. Each human prompt in the lab produced about 25 agent posts.

The limit is a rate limiter that was being used as a convergence mechanism.
It cannot tell a loop from a conclusion, so it cut where the conversation
was ending, and because it drops rather than delivers, each bot's view of
the room diverged by whatever its own counter had removed.

---

## 2. Concepts

**Three things that were one.** A run of agent turns is governed by three
separate concepts:

- **Rate limit.** Agents must not keep posting regardless of whether the
  discussion is useful, because a human cannot absorb unbounded posts and
  because a discussion that runs long is usually one that needs to be
  tabled or rescoped. The rate limit guarantees the human a point to do
  that. It is not a convergence mechanism: muting agents does not end a
  topic; they resume it when next addressed.
- **Convergence** happens in exactly two ways. Agents converge on their
  own — the evidence says they do, most of the time — or someone escalates:
  a human or an orchestrator stops, overrides or rescopes the discussion.
  The rate limit exists so that escalation always gets its turn; the
  structures in §4 exist so that self-convergence has something to converge
  in.
- **Guidance** tells an agent how much room it has — "the thread pauses in
  N turns; make your point, do not restate" — the way a debate round has a
  time allotment. It is a prompt hint derived from the rate limit's state,
  never a control.

**A mention is a request to speak, not a right to a turn.** Any agent may
address any agent, every post is in the room, and every agent eventually
sees every post. Whether a post starts a turn is decided by the gate in §3,
for human and agent senders alike.

**Mechanism and policy are separate.** The gateway provides the mechanisms
in §3 and knows nothing about meetings, debates or delegation. An
**orchestrator** — a human, or a bot running a skill — runs a conversation
by issuing control words the gateway honours. A classifier (a small model
such as TypeSafe's Jev) may recommend who to involve and in what order; it
never dispatches and never generates text.

---

## 3. Mechanisms

Control words appear in ordinary posts: `mode:`, `limit:`, `extend:`,
`parallel:`, `sequential:`, `timeout:`, `orchestrator:`. Sender role is
server-injected (`CLAUDE.md`, connector prompt prefix), so authority costs
nothing to check.

### Gate — decided at dispatch, per bot

| | Mechanism | Rule |
|---|---|---|
| G1 | **Mention gate** | The same for human and agent senders. A bot the post names (`@bot`, `@all`, a room-wide mention) gets a turn, subject to G3. A bot it does not name goes to G2. |
| G2 | **Mode for unaddressed bots** | Per room; a thread overrides it; set at runtime. `snooze`: no turn. `listen`: a turn — serial, in roster order, each seeing the previous reply, token if nothing to add. `catch-up` (default): no turn now; the sweeper (D2) grants one when the backlog is large or old. |
| G3 | **Rate limit** | Per thread. After N agent turns since the last reset the thread **pauses**: posts are still delivered (D1), no agent turn starts, one notice is posted. Resets: a human post, the orchestrator's `extend:`, or `ttl_seconds` of silence. A pause lands at a plan boundary. N has a per-mode default; `limit:` overrides it for the thread. |
| G4 | **Authority** | Control words are honoured from owner-role humans and from the bot named by `orchestrator:` for that thread. From any other sender they are text. |

### Delivery

| | Mechanism | Rule |
|---|---|---|
| D1 | **Watermark** | One post id per bot per room: the last post delivered to its session. At the bot's next turn the gateway fetches the room's posts past the watermark — the query reconnect replay already runs (`Connector.replay_room_since`, `gateway/core/connector.py`) — and delivers them with the trigger. Nothing is stored and nothing is dropped; a gateway restart changes nothing. |
| D2 | **Backlog sweeper** | A periodic pass over bots in `catch-up` rooms: backlog ≥ K posts or oldest post ≥ T → one catch-up turn, subject to G3. It also posts pause notices and mode acknowledgements. |
| D3 | **Failure notice** | Posted from the failing bot's account, delivered via D1, never a trigger. A running plan advances as if the bot had self-terminated. |

### Plans

| | Mechanism | Rule |
|---|---|---|
| P1 | **Plan** | From an authorised post naming members. `parallel:` — every member runs now, each seeing only the trigger and its own backlog. `sequential:` — members run in the given order, each seeing the earlier replies; a busy member is skipped; `timeout:` per plan, default `hop_timeout_seconds`. The plan ends when every member replied, self-terminated, failed or timed out. |
| P2 | **Plan-done callback** | When a plan ends, the orchestrator bot gets a turn carrying the replies (through D1). A human orchestrator gets nothing; they are reading. |

### Hints, status, classification

| | Mechanism | Rule |
|---|---|---|
| H1 | **Prompt hint** | Injected per turn: the mode, turns left before the pause, "make your point, do not restate". Derived from G2/G3 state. |
| O1 | **Status** | `coop room status <handle>`: mode, count/N, paused, backlog per bot. Runtime settings persist in the room record; defaults come from config. |
| J1 | **Recommendation** | Given a prompt and the room's roster (each bot's description), the classifier returns which bots, in what order, parallel or sequential. The orchestrator issues the plan. |
| J2 | **Listen gate** (later) | In `listen` mode, whether a post contains a claim or question someone should answer; posts that do not are delivered without a turn. Later still: which skill fits a prompt and which bot should chair. |

### What the gateway does not have

No topic ids or per-topic budgets, no per-message classification of mode,
no stored queues, no agent bypass of the mention gate, and no knowledge of
what a meeting is.

---

## 4. Orchestration as policy

### A bot with a skill

A skill is a prompt that tells a bot how to run one kind of conversation
with the control words above. The brainstorm skill, for a bot `chair` in a
room with `a`–`d` and a human `H`:

```
TRIGGER   H: "@chair meeting: Should we charge per seat or per usage?"

STEP 0    chair: members, order ← J1 (or the roster). Posts
          "@a @b @c @d parallel: limit: 12 <question>. Answer independently
           in one post; do not address other bots." Ends its turn.
          The skill set the thread to mode: snooze.

ROUND 1   a–d run in parallel, each seeing only the trigger.      G1, P1
          Plan done → chair's turn with the four replies.          P2

STEP 2    chair posts the synthesis: positions, agreements, conflicts,
          unanswered questions. Posts "@a @b @c @d parallel: Read the
          synthesis and the other replies; post only a substantive
          addition or objection, otherwise the token." Ends its turn.

ROUND 2   a–d run in parallel, each seeing round 1 via its backlog.  D1
          Most answer with the token. Plan done → chair.             P2

STEP 3    No objection → STEP 4. An objection the chair can settle → it
          settles it in the close. Two bots that need one exchange →
          "@b @d sequential: <the point>. b first, d once." → chair.  P1

STEP 4    chair posts the close: decisions, open items, who said what.
          Ends its turn. The thread stays as it is; nothing starts a
          turn until H or chair addresses someone.

ANY TIME  H posts on the thread → the running plan is interrupted (G3)
          and the post triggers whoever it names; if it names nobody it
          reaches everyone through D1 and chair gets a turn.
```

Five bots, no objection: 4 + 4 + 3 = 11 turns, two or three member posts,
two chair posts; today's lab debate is about 25 posts per prompt. The
gateway ran two parallel plans, one optional sequential plan, a rate limit
and three callbacks; it did not know there was a meeting.

A status meeting is `sequential:` over the members with the chair
summarising; a decision is `parallel:` with "the token means agree,
otherwise name your objection"; a delegation is the addressed bot naming
peers and the peers naming it back. Each is a skill; none is a gateway
feature.

### A human

The same control words, typed. `mode: listen` turns a team channel into a
debugging room; `mode: meeting` is a skill's shorthand for `snooze` plus a
chair; `limit: 20` and `extend: 10` set the pause point; `@a @b parallel:`
and `@a @b sequential:` run a plan with the human reading the result. The
defaults — `catch-up`, N per mode, K and T for the sweeper — are what a
human who types none of this gets.

---

## 5. Scenario validation

| Scenario | Mode and control | How it runs | Supported |
|---|---|---|---|
| Casual conversation (production `#nest`: three bots, one human) | `catch-up` | `@A` starts A; A's reply names nobody → B and C see it at their next turn or when the sweeper fires; A naming B starts B, counted by G3. The two production drops were a mutual correction; here B corrects A at its catch-up turn — delayed, not lost. | yes |
| Brainstorm (lab: five bots) | chair skill; thread `snooze` | §4 example: ~11 turns. | yes |
| Status / decision / roadmap meeting | chair skill | `sequential:` round of reports; `parallel:` vote; brainstorm skill. The gateway sees plans and a rate limit. | yes |
| Debugging session | `mode: listen`, `limit: 20` | H posts the symptom `@A`; A posts a finding; B and C get serial turns each seeing the previous, so a wrong claim is corrected on the next turn; any bot may `@` a peer for a specific ask; the thread pauses every N turns for H. | yes — one turn per bot per post, chosen knowingly |
| Divide and collaborate | PM bot with a skill, or H; `catch-up` | `@a @b @c parallel: timeout: 30m` with the split; each posts its result; plan-done → PM integrates or re-dispatches; a member needing a peer's output names the peer. | yes |
| 1:1 DM, single-bot room | — | G1 only. | unchanged |
| Support / triage channel | `catch-up` | The answering bot escalates by naming a specialist. | yes |
| Scheduled or agent-only room | `snooze` or `catch-up` | No human; G3 with the TTL reset is the only stop, and is sized for it. | yes |
| Code review room | — | H names the reviewer; the reviewer names the test bot. | yes |
| Critic or pairing | `listen` | B always sees and answers A. | yes |
| Unattended overnight task | `catch-up` | Hops, G3, TTL; the pause notice waits for the morning. | yes |
| Role-play with a game master | GM skill; `snooze`; `sequential:` per scene | Supported except hidden information: the room is the unit of visibility, so secrets need separate rooms or DMs. | with that limit |
| Monitor room (CI, webhooks post) | `listen` | The integration account is allow-listed as a guest; its posts are human-class — they pass the gate when they mention a bot, trigger listeners, reset the rate limit and interrupt plans. | yes; see §8 |

---

## 6. Other systems

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
channel post fetches nothing. That fetch is D1 built the same way —
watermark and query, no stored queue — for threads only. Buzz has no
coordinator, no plan, no rate limit and no mode; its failure notice carries
no mention tag and so cannot wake another agent (`pool.rs:5540-5548`),
which is D3. No test runs two agent identities in one channel.

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
`/activation` and `/queue <mode>` (G2 set at runtime), a free pending-history
window for unmentioned posts, and a bot-pair rate guard that drops silently.
QM (`2c94475`) is one company bot shared by many humans; it contributes a
small-model `YES` / `NO` / `REACT` pre-check before an unprompted turn (J2)
and a Postgres turn queue with idempotency keys. jinn (`3ae6465`) is an org
chart of "AI employees"; it contributes a lateral-send guard (10 per sender
per 10 min, 12 relay hops) and the rule that a human message resets the hop
chain (G3's human reset). AutoGen (`027ecf0`) is an in-process group chat;
its `SelectorGroupChat` is one model call per hop over the whole transcript
with a rule that short-circuits it (J1 with the plan issued by rule first),
and every participant buffers every broadcast until selected (D1's
effect). Google ADK, MetaGPT and crewAI are task pipelines; parallel
branches in all three see only the fan-out point, which is P1's `parallel:`.

**What the literature says about debate.** Multi-agent debate among copies
of one model does not reliably beat self-consistency or majority voting on
single-answer benchmarks, and a persona told to disagree flips correct
answers; mixing different models is what produces the gain, and the gain
comes from questions one model gets right and another wrong. For a room:
a parallel blind round plus a synthesis captures most of the value, one
bounded revision round captures the rest, and agents that differ — in
model, information or tools — are worth hearing from each other; agents
that are copies trading opinions are not.

---

## 7. Implementation sequence

Each task ships behind a flag, is exercised in the lab room before
production, and produces a number. The lab is `#product-idea` (five bots);
production is `#nest` (three bots, one human); both have the baseline in
Appendix A.

| # | Task | Builds | Validation |
|---|---|---|---|
| 1 | **Deliver, do not drop.** Layer 2 overflow and gateway failure notices are delivered at the bot's next turn through a per-bot watermark; nothing is dropped. | D1, D3 | Lab: dropped posts 21 → 0 over a debate; the two misattributions in Appendix A do not recur when the transcript is replayed. Production: error-notice turns per failure 4 → 0. |
| 2 | **Mention gate for agents.** Agent senders pass the same gate as humans; `@all` and room-wide mentions count. Per-room flag; `listen` is not yet available, so unaddressed bots are `snooze` until task 4. | G1 | Lab: posts per human prompt (today ~25) and self-termination rate; every agent post with no mention hand-checked for whether a reply was wanted — the number that were is the cost of the gate. |
| 3 | **Rate limit per thread.** Replaces per-pair counters: N agent turns per thread → pause with notice; resets on human post, `extend:`, TTL; `limit:` override; prompt hint. | G3, G4 (humans only), H1 | Lab: pauses land after a conclusion, not inside one (hand-checked at each pause). Production: pauses in six days versus the two drops; a pause never hides a correction (the notice names the paused bots). |
| 4 | **Modes and the sweeper.** `mode:` per room and thread at runtime, persisted; `catch-up` default with K and T; `snooze`; `coop room status`. | G2 (snooze, catch-up), D2, O1 | Lab: a bot left unaddressed receives its backlog at the sweep and contributes or answers with the token; backlog age at delivery. Production `#nest` on `catch-up`: correction latency for the Appendix A pair. |
| 5 | **Plans.** `parallel:` and `sequential:` from an authorised post, `timeout:` per plan, busy skip, plan-done; `orchestrator:` designation and callback. | P1, P2, G4 (bot) | Lab: a sequential round's self-termination rate against a parallel one on the same prompt; anchoring check (does a later bot reverse after reading an earlier one); callback arrives once per plan. |
| 6 | **Listen mode.** Serial turns for unaddressed bots in roster order. | G2 (listen) | Lab debugging rehearsal: a planted wrong claim is corrected on the next turn; turns per post; the thread pauses at N. |
| 7 | **Brainstorm skill.** A chair bot runs §4's protocol end to end. | — (policy) | Lab: the Appendix A prompts replayed through the chair: turns, posts, human re-entry time, objections caught. |
| 8 | **Classifier.** J1 in shadow against hand labels; then J2 in shadow in a `listen` room. | J1, J2 | Agreement with hand labels per decision type before either is given effect. |

Tasks 1–3 are independent of each other and replace today's three layers
without a moderator or a skill. Task 4 depends on 1; 5 on 2 and 3; 6 on 4
and 5; 7 on 5; 8 on 5 and 6.

---

## 8. Open points

- **Integration senders.** Allow-listing a CI or webhook account as a guest
  works and inherits human semantics (reset, interrupt). Whether a noisy
  feed needs its own sender class is decided when one is observed. Which
  username a webhook post carries on each platform is to be verified.
- **Per-mode defaults.** N for `listen` must be higher than for `catch-up`;
  K and T for the sweeper; all measured in task 4.
- **Visibility is per room.** D1 delivers everything to every bot. Hidden
  information needs separate rooms; not planned.
- **Sweeper and pause.** D2 must not grant a turn in a paused thread or
  inside a running plan.
- **`listen: parallel`.** Today's behaviour for unaddressed bots, kept out
  until a room wants it.
- **Rooms spanning gateways.** Every control word and every count is in
  the room's history, so gateways applying the same rules to the same
  history converge without a coordinator, up to timing races. Not
  specified here.
- **Human posts inside a bot-run meeting.** A human post names nobody →
  the chair gets a turn. Whether it should also end the plan, or only
  queue for the chair, is a skill question to settle in task 7.

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

