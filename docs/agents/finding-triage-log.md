# Finding triage — log

Step 6 of the `finding-triage` skill: the five quantities and the verdict for every
finding decided, the one least sure of as a range, and every control result,
convergence and escalation. Entries are marked **settled** once their outcome is
observed — the drop that bit or did not, the fix that saved what it promised or did not.
Settled entries are the only legitimate source of a control finding for a blind second
rater; until one exists, every round runs without a control and says so.

Constants and anchors: `finding-triage.md` in this directory.

## Revisit ledger

Every DROP and FILE, with the observation that would prove it wrong and the date it
settles (twelve months, per `finding-triage.md` § *Revisit window*). Sweep quarterly:
grep the reversal column against issues, `gateway.log` and the tree; an entry whose
observation appeared is `bit`, one whose date has passed is `held`. `silent` entries
settle `held (unobservable)`. Rows marked *untrue* or *stale* in their round made no rate
claim and are not listed. The table is derived from the outcome cells of the round
tables below — every `**DROP`/`**FILE` there has a row here; a hand backfill on
2026-10-07 missed two and mis-copied one (PR #196 round 1), so re-derive, do not patch.

| decided | finding | verdict | reversal — what would prove it wrong | revisit | status |
|---|---|---|---|---|---|
| 2026-09-14 | #166 r1 F1 — concurrent `resume`/`reset` on a still-broken agent queue a second ~40 s attempt | DROP | a `_recover_agent` attempt logged within 60 s of a failed one for the same agent, or an operator reporting a verb that waited on a prior attempt | 2027-09-14 | open |
| 2026-09-14 | #167 r1 F3 — a pure `{"*": "deny"}` plugin copy re-introduces `ask` on bash | DROP (`cannot-occur`) | any hook sees a bash call under a pure-deny permissions file | 2027-09-14 | open |
| 2026-09-23 | #181 r6 F1 — relative `working_directory` validated in a scratch dir when the config dir is absent | DROP | a bootstrap into an absent config dir with a relative `working_directory` where `validate` passes and the applied config resolves elsewhere (issue or log) | 2027-09-23 | open |
| 2026-09-23 | #181 r8 F1 — `_has_cycle` and the raw render are exponential on a doubling *acyclic* alias graph | DROP (scored, 0.003–0.1/yr) | an issue or report of `config show --raw`, `redact` or `_has_cycle` taking seconds or exhausting memory on a config.yaml whose aliases are shared but acyclic | 2027-09-23 | open |
| 2026-09-23 | #181 r8 F3 — a dry run creates `~/.agentcoop` via `validate_config` | DROP | anyone reports a dry run's directory creation as a defect | 2027-09-23 | open |
| 2026-09-23 | #181 r9 F1 — `save()` validates beside the resolved target, so a relative `working_directory` through a symlinked config resolves against the wrong directory | FILE (→ #182) | — | — | **settled: implemented** — #182 closed 2026-09-25 by PR #186, which moved the base to COOP_HOME. The FILE verdict held: the owner judged it worth its own increment. First settled row; a candidate control (a rater who DROPs it is wrong) |
| 2026-09-24 | #184 r1 F8 — shared agent-chain list lets a same-named human bypass the sender allow-list (security) | DROP (design §3.7) | the keeper template's `filter_sender` default changes from `false`, or a report of a human posting under an agent's name getting through | 2027-09-24 | open |
| 2026-09-24 | #184 r3 F1 — a symlinked keeper dir or `agents/` defeats the ancestor guard (chain deleted; symlinks followed by ruling) | DROP | a report of a keeper sync writing somewhere the operator did not point a link at | 2027-09-24 | open |
| 2026-09-24 | #184 r3 F3 — `server.team` given as an id plans a duplicate bot; runtime refuses at reload | DROP | an operator hits the duplicate-bot refusal and the message does not lead them to the fix (issue) | 2027-09-24 | open |
| 2026-09-24 | #184 r3 F4 — `_remove_path` then `copytree`: a failed copy leaves the old skill gone until the next upgrade | DROP | a failed skill copy that the next `coop upgrade` did not repair, or that needed a manual repair | 2027-09-24 | open |
| 2026-09-24 | #184 r3 F7 — a hand edit between plan steps re-shares an account before `delete-user` | DROP (§3.8/§4 best effort) | a shared account deleted because of a mid-plan hand edit | 2027-09-24 | open |
| 2026-09-24 | #184 r4 F2 — a plan past its 10-minute lease taken over mid-execution | DROP | a keeper report showing a takeover of a plan whose first keeper was still running | 2027-09-24 | open |
| 2026-09-25 | #186 r3 F1 (ii) — a non-default COOP_HOME under an ancestor another local user can write to (parent walk) | DROP — owner's ruling, boundary in SECURITY.md; `silent` | a report or audit showing `repo/.venv/bin/coop` replaced via a writable ancestor | 2027-09-25 | open |
| 2026-09-25 | #186 r3 F1 (i-b) — 0700 on create | DROP; `silent` | a report of secrets or config read through the directory mode (secrets are 0600) | 2027-09-25 | open |
| 2026-09-25 | #186 r4 F1 — a group-writable COOP_HOME (0770/0775) passes `runtime_dir_is_ours` (boundary written in SECURITY.md) | DROP; `silent` | a report of a lower-privileged group member altering COOP_HOME contents | 2027-09-25 | open |
| 2026-09-25 | #186 r4 F2 — a container started with `COOP_HOME` set splits where the entrypoint writes and `coop` reads | DROP, recorded on #183 | #183's image ships still honouring `COOP_HOME`, or a user report of `coop start` failing in the container | 2027-09-25 | open |
| 2026-10-07 | #195 r1 F1 — RC replay re-runs the human reset for an unmentioned post; an in-flight agent delivery keeps a free turn | DROP | `Agent chain counters reset … had turns` logged directly after `replaying missed messages` for a room whose human post was unmentioned, or any chain observed past `max_turns+1` | 2027-10-07 | open |
| 2026-11-14 | #205 r1 F7 — `session/resume` without `mcpServers` is rejected by a conforming agent | DROP (`cannot-occur`: ACP v1 schema `ResumeSessionRequest.required = [sessionId, cwd]`) | an ACP agent answering `invalid_params` naming `mcpServers` on a `session/resume` that omits it | 2027-11-14 | open |
| 2026-11-14 | #205 r4 J3 — a project `.opencode/` config defining `coop-<path_key>` outranks the gateway's watcher entry | DROP (ADR-0001: the working directory is the agent's own; the ruleset is unaffected) | a `coop-<path_key>` entry found in a project config that neither the operator nor the agent wrote | 2027-11-14 | open |
| 2026-11-20 | #208 r1 F1 — a hosted bot removed from the room keeps getting wakes and reads through the core | FILE (→ S3: derive or validate `hosted` against the roster) + MAKE IT LOUD (`status.hosted_not_members`, a warning per wake) | a `turn_start` or the warning for a bot absent from `platform.members()` in the design §6 step-3 lab run | 2027-11-20 | open |
| 2026-11-20 | #208 r1 F2 — `restart()` leaves an in-flight classifier result to repopulate the cache and pick a bot | DROP (`cannot-occur`: no production caller of `restart`; a process restart ends the task; a late pick is for an undelivered post) | a non-test caller of `RoomCore.restart` (`grep -n "\.restart(" coop/`), or two `classifier_decision` events for one post id after a restart | 2027-11-20 | open |
| 2026-11-20 | #208 r1 F3 — an archived thread's pending decision or unleased wake still produces a turn and a refused post | DROP (scored, 1–10/yr × 0.02 h; spec 5.9 names the archived-thread refusal as a normal end; MM/RC archive rooms, not threads) | `platform_refused` for the same thread on two consecutive turns of one bot, or a bot stuck on an archived thread | 2027-11-20 | open |
| 2026-11-20 | #208 r1 F4 — a `done` that runs after `expires_at` under a stalled loop is accepted | DROP (outcome acceptable: the agent finished; the fix converts a gateway stall into a failure plus a duplicate reply) | a `turn_end outcome=normal` seconds after its wake's `lease_started.expires_at` | 2027-11-20 | open |
| 2026-11-20 | #208 r1 F5 — a replayed thread older than `control_ttl_days` seeds `last_activity` with now, so its stale controls live another TTL | DROP (scored, 0–0.3/yr; the one-line fix drops live mutes when traced; needs S3's replay window > 7 d to occur) | `control_applied` with `age` > `control_ttl_seconds` followed by `turn_held gate=mute` in that thread after a restart | 2027-11-20 | open |
| 2026-11-20 | #208 r2 G3 — `read --wake` omits the bot's own posts inside `[cursor, hwm]`, so an unsolicited post the bot's scheduler made is in no later wake's delivery | FILE (owner decision: design §3.3 "every post after the cursor" vs spec 5.4 "does not receive its own bot's post back"; unsolicited posts are the new case; `read --room` shows them) | a `read` whose `delivered` omits a `kind=bot` post of the same bot inside `[cursor, hwm]`, or an agent that "does not remember its own report" | 2027-11-20 | settled: decided (owner, 2026-11-21: an own post is context, never cause — `read` returns it, it wakes nobody and closes nothing; PR #209) |
| 2026-11-20 | #208 r2 G5 — the classifier decision cache is never evicted | DROP eviction (scored 0–0.3/yr, net ≤ 0 in S1; needs a position on the entry and an invariant) + MAKE IT LOUD (`status()["decisions"]`) | `status()["decisions"]` above ~100k on a running gateway, or an RSS complaint | 2027-11-20 | open |
| 2026-11-21 | #208 r3 H1 — agent bios are not in the persisted document; a process restart loses every bio until the agents re-push | FIX (owner, 2026-11-21: this increment — `dump_agents`/`load_agents` in `store.py`, bio and version, the bios dict updated in place; escalated first: rater 1 FIX — the approved plan's `store.py` item lists bio and design §3.1 persists it; rater 2 FILE → S3 — no process restart exists in S1) | `status()["agents"][*]["bio_version"] == 0` after a restart of a gateway whose agents had pushed bios, or an S3 restore path that loads rooms and not agents | 2027-11-21 | fixed 2026-11-21 |
| 2026-11-21 | #208 r4 I1 (status half) — `load_agents` restores an entry for an agent this gateway no longer hosts, so `status()["agents"]` lists it as a local agent | FILE (→ S3: the gateway gets the configured agent set from §4 `agents:` and `load_agents` filters on it; the classifier half fixed here in `_with_bio`) | `status()["agents"]` on a restarted daemon names a bot its configuration no longer lists | 2027-11-21 | open |

---

## 2026-09-14 — PR #166 round 1

First live run of the skill on this repository.

**Chain detector:** 1 finding over 1 round, no chain.
**Control finding:** none — the log was empty. Agreement in this round is *not*
evidence of independence; recorded as **uncorroborated**.

### F1 — Coalesce concurrent failed recovery attempts

Codex P2, `gateway/service.py` `_recover_agent`. Two `resume`/`reset` verbs racing for
the same still-broken agent: the first attempt fails and leaves the agent unavailable,
so the second waiter on `_recover_lock` runs `start_some` again — one more ~40 s
attempt per additional concurrent verb, and a drain waits the sum.

| | rater 1 (author) | rater 2 (blind) |
|---|---|---|
| `cheap` | no — an in-flight future per agent: new state + tests | no — an attempt-token counter, 6–8 lines, but a new attribute, an invariant and a test |
| `cannot-occur` | no — two shells needed; the CLI glob batch is sequential | no — traced `control.py` one task per connection; batch sequential; nothing else sends lifecycle verbs |
| `silent` | no — each attempt is logged and the verb is refused with the remedy | no |
| `hours_per_hit` | 0.05–0.2 | 0.04 (0.02–0.1) |
| `hits_per_year` | **0.1–1** — correlated path: a second shell or an agent-issued verb | **0.7 (0.06–7.5)** — correlated path: the impatient Ctrl-C re-run |
| `discount` | 1.0 — no clause covers operator verbs | 1.0 |
| `fix_hours` | 1.5–2 | 1.5 |
| `tax_hours_per_year` | 0.2 | 0.25 |
| `net_annual_saving` | ≤ 0 | −0.22 (−0.25 … +0.5) |
| verdict | **DROP** | **DROP** |

Verdicts agree and the `hits_per_year` ranges overlap → settled per the Step 4 table,
**uncorroborated** (no control).

Notes:
- Rater 2 named a correlated path rater 1 had not considered (an operator who thinks
  the verb hung, Ctrl-C's the client and re-runs; the daemon-side attempt runs on and
  the re-run queues on the lock).
- Layer (rater 2): if ever revisited, a per-agent start backoff belongs in
  `AgentRuntimeManager` or the adapter's restart circuit-breaker, shared by a reload's
  start pass and a verb's — not in the verb callback, whose lock exists to prevent two
  brokers, not to rate-limit failing starts.
- Least-sure quantity: `hits_per_year`, both raters' widest range.
- Thread: replied with the scored reason; resolved by id.
- Adoption this round: 0 of 1 acted on.

**Status: open.** Becomes settled when a queued recovery wait is observed in the field,
or after a year without one (which would confirm the rate's upper bound was generous).

---

## 2026-09-14 — PR #167 round 1

**Chain detector:** 3 findings over 1 round, no chain.
**Control finding:** none — PR #166's only entry is still open. Agreement in this
round is *not* evidence of independence; recorded as **uncorroborated**.
**Rater independence caveat:** rater 2 was given the reviewed commit to score against
but read the working tree, where the deletion fix was already sitting uncommitted. It
did not see rater 1's verdicts or quantities, but it saw the fix, and a fix implies a
verdict. Weaker than the #166 round on that axis.

All three findings were decided at Step 1; no scoring arithmetic was needed. Codex
labelled all three P1; re-ranked by consequence below.

### F1 — Deny-mode `coop send *` allow is reachable by guests

Codex P1, `adapter.py` `_build_safe_opencode_config`. Moving the `"*"` catch-all first
made `_DEFAULT_BASH_ALLOW_PATTERNS` effective for the first time; the sidecar runs as
`COOP_ROLE=owner` for every chatter, roles come from the connector's `owners`/`guests`
lists regardless of `permissions.enabled`, and an opencode-level allow never reaches
the broker — so a guest could run `coop send`.

| | rater 1 (author) | rater 2 (blind) |
|---|---|---|
| true? | yes — verified the old order silenced the patterns (1.18.13), so this PR is what would make it live | yes |
| this change's job? | yes — introduced by this diff | yes, in scope |
| `cheap` | **yes** — cheapest correct fix is deleting the pattern set (one constant, one merge line), not role-gating it | **yes** — same deletion, ~11 lines, no new concept |
| verdict | **FIX** (deletion) | **FIX** (deletion) |

### F2 — "Read-only" git allow patterns are write-capable via `--output=<path>`

Codex P1, same site. Confirmed locally: `git diff --output=victim.txt` truncated the
file; `git log`/`git show` take the same diff options. The broker's builtin owner rules
carry no git patterns, so the adapter's set was the only place this existed.

| | rater 1 (author) | rater 2 (blind) |
|---|---|---|
| true? | yes — reproduced | yes — reproduced |
| this change's job? | yes — same lineage as F1 | yes |
| `cheap` | **yes** — same deletion | **yes** — same deletion; Codex's option-filtering fix is the expensive one |
| `silent` | yes — looks like a safe read-only op; fixed, so not priced | yes |
| verdict | **FIX** (deletion) | **FIX** (deletion) |

Both raters rank F2 above F1 on consequence: arbitrary file truncation, reachable by a
legitimate owner, under a mode whose purpose is denying unapproved writes — versus a
guest-only unauthorized send.

### F3 — Legacy v1.0.0 plugin copy re-introduces `ask` on bash in deny mode

Codex P1, `role-enforcement.ts` bash early-return. Claim: with the wizard-installed
`~/.opencode/plugins/role-enforcement.ts` still present, both hooks run, the legacy one
sets `output.status = "ask"` for bash, and the call waits on a broker that does not
exist.

| | rater 1 (author) | rater 2 (blind) |
|---|---|---|
| true? | **no** — ran both plugins together (legacy as a second `file://` entry) in deny mode: an allowed bash pattern completed in 18 s, `write` refused by the current plugin's throw. Outcome proven; that the legacy hook executed is not | **no** — same experiment read as the exact adversarial case; inconsistent with the claimed mechanism |
| gate | refuted on evidence → **DROP**; also moot after F1/F2: a pure `{"*": "deny"}` offers no bash tool, so no hook sees a bash call | `cannot-occur` (refuted by trace) → **DROP** |
| residual | `_verify_plugin_accepted` already logs a warning naming the stale file and `docs/migration-v1.md` | same; "duplicate hook, redundant but inert" adequately covered |

Verdicts agree on all three → settled per the Step 4 table, **uncorroborated** (no
control; rater 2 saw the fix).

Notes:
- Both `FIX` verdicts resolve to one deletion. What was lost is measured against
  `main`, where bash hung entirely with permissions disabled: nothing. Against the
  Claude backend the OpenCode deny mode is now one notch stricter (Claude's built-in
  read-only classifier still runs `git status`); stated in the PR and CHANGELOG rather
  than replicated.
- Layer (F1/F2): the concern is per-message authorization, which the role-blind sidecar
  cannot do at the opencode ruleset layer; the correct move at that layer is to assert
  nothing, which is what deletion does.
- Threads: replied with the reason; resolved by id. F3 is a conscious decline.
- Adoption this round: 2 of 3 acted on (one fix).

**Status: open.** F1/F2 settle if a guest send or a `--output` truncation is ever
observed on a release that still carried the patterns (none should — they were dead
there). F3 settles if a deny-mode bash hang with the legacy copy present is ever
reported, which would show the experiment missed a plugin-order dependency.

---

## 2026-09-23 — PR #181 round 4

Rounds 1–3 of this PR (25 findings) were triaged **without the skill**: CLAUDE.md's
two questions and an ad-hoc severity re-rank, no chain detector, no second rater, no
log entry. Recorded here as a process failure, not re-scored after the fact. The
author's text-based framing had concluded that rounds 2 and 3 each landed ~half their
findings "on the previous round's fix" and was about to stop the loop on that.

**Chain detector** (run at round 4, from the working tree): 32 findings over 5 rounds,
**no chain** — round 2 and round 4 each put one finding on the author's own last fix in
one file (`gateway/admin/config.py`, `gateway/cli.py`), streak 1 both times; every
other finding was new ground. The text framing and the detector disagree; the detector
is the deterministic one and is what decides whether the loop continues.

**Control finding:** none — no settled entry in this log yet. Agreement in this round
is *not* evidence of independence; recorded as **uncorroborated**.

All five findings were decided at Step 1 by both raters (gate `cheap`); no scoring
arithmetic was needed. Codex labelled all five P2.

| | F1 `!!int abc` escapes `read_document` | F2 `Path.exists()` in the `show --raw` error branch | F3 `--command ./claude` checked against cwd | F4 `password: !!int hunter2` in a fragment → traceback carrying the value | F5 `--entry` checked before `--file` is applied |
|---|---|---|---|---|---|
| rater 1 (author) `cheap` | yes — broad clause at the load, 2 lines | yes — try/except, 2 lines | yes — refuse a relative path with a separator, 3 lines | yes — same clause as F1 | yes — apply the file first, 3 lines |
| rater 2 (blind) `cheap` | yes — 2 lines; class is 3 exception types, not 1 | yes — 2 lines, and a `chmod 000` parent is a realistic trigger Codex did not name | yes — refuse, 2–3 lines; do **not** thread `working_directory` through `resolve_command` (shared with `backends`) | yes — but fix at the three `safe_load` sites as one sweep; also found the same hole at `parse_set`, and the round-2 boundary echoing `{exc}` in a controlled-looking `ok: false` | yes — ~3 lines, and a simplification |
| `cannot-occur` | no (both) | no (both) | no — rater 2 traced `adapter.py:537` `cwd=working_directory` | no (both; reproduced) | no (both; reproduced) |
| `silent` | no | no | no — deferred to first use, but loud | no | no |
| verdict | **FIX** / **FIX** | **FIX** / **FIX** | **FIX** / **FIX** | **FIX** / **FIX** | **FIX** / **FIX** |

Verdicts agree on all five → settled per the Step 4 table, **uncorroborated**.

Notes:
- Rater 2's sweep was adopted over five patches: one `load_yaml` for the file, the
  `--set` value and the `--file` fragment; both `{exc}` boundaries reduced to the
  exception type; a test enumerating PyYAML's tag constructors at all three sites.
- Layer, F3 (rater 2): the add-time gate owns the check; `resolve_command` stays the
  one lookup `backends` shares.
- Layer, F4 (rater 2): the load seam owns it, not the CLI branch the finding pointed at.
- Adoption this round: 5 of 5 acted on — as the `cheap` gate predicts for a batch of
  two-line fixes; not a sign the triage is idle, since the gate was applied to the
  cheapest correct fix rather than the reviewer's proposal (F3, F4 differ from Codex's).
- On continuing: rater 2 read the round as "two consecutive rounds landing in the
  previous fix" from the *text*; the detector says otherwise from the *metadata*. Both
  recommend one more round after the sweep. Owner's call.

**Status: open.** Settles when a round on the swept code finds no further member of the
class, or finds one (which would say the sweep was incomplete).

---

## 2026-09-23 — PR #181 round 5

**Chain detector:** 32 findings over 5 rounds, no chain (unchanged from round 4).
**Control finding:** none — still no settled entry. Agreement in this round is *not*
evidence of independence; recorded as **uncorroborated**.

Both findings decided at Step 1 by both raters; no scoring arithmetic.

### F1 — `--credentials-from` copies a live credential to whatever `--server-url` names

Codex P1, `gateway/config_edit.py` `_credentials_of`. Reproduced by rater 2: a
Mattermost token copied under a typo'd host, and a Rocket.Chat password copied into a
Mattermost connector, both `ok: true`.

| | rater 1 (author) | rater 2 (blind) |
|---|---|---|
| `cheap` | yes — same canonical origin (§3.4: scheme/host lower-cased, trailing slash dropped) and same type, ~8–10 lines, no new concept; deriving the URL from the source is the expensive form (conditional `required`) | yes — same fix, ~8 lines; flagged that two existing tests copied across servers (`localhost:3000` → `rc-2:3000`) and read the design text over the tests |
| `cannot-occur` | no — expressible (a typo) | no — reproduced |
| `silent` | no — login fails loudly on the wrong host, but the token has been sent | partial: disclosure silent, symptom loud |
| severity vs Codex P1 | P2 — needs a hand-typed flag the keeper derives from one profile | P2 — ADR 0001's security definition is cross-agent reach; a mistyped private hostname is far more often NXDOMAIN than an attacker; the one real point is that the credential is *live* |
| layer | add-time gate owns "same installation" | same — the file knows both URLs, as it does for `remove`'s referential check |
| verdict | **FIX** | **FIX** |

Verdicts agree. The two cross-server tests were the author's own convenience, not a
requirement: on Rocket.Chat the validator already refuses two connectors on one
account, so the flag can only ever produce a valid file on Mattermost with a second
team — exactly §3.10's stated purpose. Tests retargeted to that.

### F2 — `--set 'server.password=!!int hunter2'` escapes `parse_set`

Codex P2, citing commit `85cfa53`, no current line. Both raters: **`cannot-occur` at
HEAD** — fixed by round 4's `load_yaml` sweep (`d22f24d`), traced
`parse_set → load_yaml → _YamlLoadFailure → PatchError → ok:false`, covered by
`TestTaggedScalarsNeverEcho`. **DROP as stale**; the actionable item is CLAUDE.md's
"check which commit was reviewed".

Notes:
- Adoption: 1 of 2 acted on.
- Both raters and the detector: F1 is first contact on a flag from the feature commit,
  not a finding on a previous round's fix; the stop signal is not live. Continue.

**Status: open.** Settles with round 4's entry.


---

## 2026-09-23 — PR #181 round 6

**Chain detector:** 40 findings over 6 rounds, **no chain** — round 6 put one finding
on the author's own last fix in each of `gateway/admin/cli.py` and
`gateway/config_edit.py`, streak 1 both; two older findings could not be blamed (lines
since rewritten), so their absence of a chain is unproven. Codex reported hitting its
review usage limit during this round.
**Control finding:** none. Agreement is **uncorroborated**.

Codex: 1 × P1, 7 × P2. Re-ranked by consequence below.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F1** `validate_document` validates in a scratch dir when the config dir is absent, so a relative `working_directory` means something else | `cheap` yes: mkdir the parent during the dry run (3 lines) — or FILE | `cheap` **no**: a dry run that creates a directory breaks §3.8 "nothing is written before the yes"; rewriting relative paths against the intended parent is a new rule. Scored: `hours_per_hit` 0.5 · `hits_per_year` 0.006–0.03 (bootstrap into an absent dir 0.02–0.1 × P(relative wd) 0.3; install.sh mkdirs `~/.agentcoop`, the keeper writes absolute paths) · `discount` 0.56 (nearest ladder row, TUI save §14.2 — `add/patch` is not on the ladder) · `fix_hours` 2 · `tax` 0.2 → net negative | **DROP.** Rater 1 conceded on the §3.8 clause — checkable evidence, not capitulation. Layer note (rater 2): `validate_config`'s `config_dir` derivation owns this; the correct fix is a `config_dir` parameter there, not a scratch-branch patch |
| **F2** a bare command resolving through a relative `PATH` entry | `cheap` yes, 2 lines | `cheap` yes, 3 lines; reproduced with `PATH=bin:…` | **FIX** — refuse when `which()` returns a relative path |
| **F3** `json_safe_keys` collapses `1:` and `'1':` | DROP — twin of the anchor's int-vs-string-key digest collision | `silent` yes → priced **MAKE IT LOUD**: one collision finding appended, ~6 lines, passes `cheap`; the behavioural fix (typed keys) is the anchor's DROP | **MAKE IT LOUD.** Rater 1 had not priced the loud version — the skill's `silent` obligation, cited by rater 2 |
| **F4** `&loop [*loop]` → RecursionError in `redact_raw_document` | `cheap` yes: a boundary on `show --raw` | `cheap` yes: a cycle check in `load_yaml` (~8 lines), since every later walk (`redact`, `find_sentinel`, `resolve_from_file`) would recurse too; the finding points at the cli.py redact call — wrong layer | **FIX** at `load_yaml` (rater 2's layer) |
| **F5** `--entry rule:w1 --set name=w2` edits w2 | `cheap` yes; `silent` yes | `cheap` yes (refuse, do not silently override); `silent` yes | **FIX** — `name` is the selector, refused as a path |
| **F6** `yaml_error_summary`'s `problem` quotes source text (`*hunter2` → "found undefined alias 'hunter2'") | `cheap` yes: drop `problem` | `cheap` yes: strip the single-quoted fragments (1 line), keep the diagnostic; **also found** `gateway/admin/config.py` printing `str(e)` for the profiles file — worse, and on the `profiles` path | **FIX** both (the admin loader was refactored by this increment, so in scope). `_DuplicateKeyError`'s own message is kept: it names a key, never a value |
| **F7** `profiles --json` with a nested date key | `cheap` yes: `json_safe_keys` at the print | `cheap` yes: coerce in `masked_profiles` — the view owns it | **FIX** in the view |
| **F8** `{from_file:}` under a non-credential key prints the file back in every report | `cheap` yes (~4 lines); **P2** not P1 | `cheap` yes (~6 lines); reproduced on a refused write too; **P2**: attacker = fragment author = the operator or coop-keeper as the operator's user on the operator's machine, who can already `cat` the file — a contract breach that puts file contents in the keeper's transcript, not exfiltration; no ADR 0001 boundary crossed | **FIX** — `from_file` only under a key the redactor masks |

Both raters: F3/F4/F6/F7 are one shape — a legal YAML value the input boundary did not
anticipate — and CLAUDE.md's answer is one enumerating test, not another round.
Added: `TestLegalYamlShapesTheBoundaryDidNotAnticipate` (cycles, shared aliases,
alias/tag names in `problem`, non-string and colliding keys) beside the round-4
constructor enumeration.

Notes:
- Adoption: 7 of 8 acted on (6 FIX, 1 MAKE IT LOUD), 1 DROP.
- Two disagreements, both converged on cited evidence (§3.8 for F1; the `silent`
  obligation for F3) — the concession row, with the evidence written down.
- Both raters and the detector: stop. Codex is at its usage limit in any case.

**Status: open.** Settles with the round-4 and round-5 entries.

---

## 2026-09-23 — PR #181 round 7

**Chain detector:** 43 findings over 7 rounds, **no chain** — round 7 put two findings on
the author's last fix in `gateway/admin/config.py`, streak 1. Rater 2's blame: F1's
handler predates the branch (`5d9cb84b`); F2/F3 sit in original increment commits, not
review-fix commits.
**Control finding:** none. Agreement is **uncorroborated**.

All three decided at Step 1 by both raters (gate `cheap`), all **FIX**.

| | verdicts | notes |
|---|---|---|
| **F1** the profiles loader's broad backstop interpolates `str(e)` — `password: !!int hunter2` → the credential in `coop-provision profiles` output | FIX / FIX | Both: 1 line, type only. Rater 2: do **not** route through `config_edit.load_yaml` (that would need a `Loader` parameter for `_StrictLoader` — a new concept); the arm's own comment already enumerated the leaky exception types, the code contradicted it. Not a chain — a **sweep gap** in round 4's "one YAML loader" claim, which covered `config_edit` and, in round 6, this file's `YAMLError` arm but not its backstop. Test enumerates the tag constructors against `load_profiles` |
| **F2** `init` accepts any profile name; design §2 says the same single path component as an agent name | FIX / FIX; `silent` yes (bites later, when the keeper derives `<agent>@bad:name`) | Both: reuse `AGENT_NAME_RE`, ~5 lines. Rater 2's bound adopted: check in `init_profile` only — §2 keeps a hand-written off-pattern profile usable |
| **F3** `AGENT_NAME_RE.match` accepts `"bob\n"` | FIX / FIX; `silent` yes | `fullmatch`, one token |

Notes:
- Adoption: 3 of 3.
- The pre-existing `unhashable` test assertion changed to assert the exception type: the
  backstop may no longer echo what PyYAML choked on.
- Both raters: one more round; stop if round 8 lands on any of these three fixes.

**Status: open.** Settles with the round-4 entry.

---

## 2026-09-23 — PR #181 round 8

**Chain detector:** 46 findings over 8 rounds, **no chain** by its rule (code written since
the *previous* round). Rater 2 notes F1 sits in `_has_cycle`, added by the round-6 fix
`b2e8629` — a finding on a fix two rounds back, which the detector does not count.
**Control finding:** none. Agreement **uncorroborated**.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F1** `_has_cycle` is exponential on a doubling acyclic alias graph | `cheap` yes: memoize proven-acyclic containers (4 lines) → FIX; the render explosion is the operator's own alias bomb → drop that half | `cheap` **no** — measured: n=20 doc, `_has_cycle` 1.3 s, redact 2.2 s, `show --raw` 27.7 s / 88 MB; the memo removes ~⅓, the render still expands the DAG; a correct fix is a traversal budget (new named constant) or refusing shared containers (contradicts "a shared alias is fine"). Scored: `hours_per_hit` 0.5 · `hits_per_year` 0.003–0.1 (3–10 operators × P(author such a graph) 0.001–0.01) · `discount` 1.0 · `fix_hours` 1 · `tax` 0.1 → net ≤ 0, lighter than the DROP anchor | **DROP.** Rater 1 conceded on the measurement: the memo alone does not remove the defect, and the full fix carries a tax the harm never repays. Input is the operator's own file; no ADR 0001 boundary |
| **F2** `save()` through a symlinked `config.yaml` replaces the link with a regular file | `cheap` yes, fix in `save()` (resolve to the target) | `cheap` yes — 1–2 lines with a precedent (`config_migrate.py` resolves first for the same reason); pre-existing TUI behaviour, but Docker mode 1 symlinks config.yaml and the increment introduces the scripted write §3.8 relies on; `silent` yes | **FIX in `save()`** — temp, backup and replace all on the resolved target; the link stays a link. Test: save through a link edits the target and leaves the link |
| **F3** a dry run creates `~/.agentcoop` via `validate_config`'s state checks | `cheap` fails the file rule (`core/state.py` is outside the increment); scored ≈ 0 harm → DROP; §3.8 is about edits waiting to be applied, not an install directory | same: three call sites, `coop start` mkdirs it anyway, `config validate` always did; `hours_per_hit` ~0 → any fix net ≤ 0 → DROP; **but** the comment at `validate_document` claimed "a dry run must not create the directory", which the default path does not deliver — a wrong rationale outranks the small bug | **DROP the behaviour; the comment corrected** to say what is and is not guaranteed |

Notes:
- Adoption: 1 of 3 (F2). Two drops by scoring, both with the reason on the thread.
- Round 6's `_has_cycle` fix is where F1 landed; the round-4/6 YAML-boundary work is
  now: refuse cycles (kept), and accept that a pathological but acyclic alias graph is
  the operator's own problem (documented on the thread).
- Both raters: one more round, then stop regardless.

**Status: open.** Settles with the round-4 entry.

---

## 2026-09-23 — PR #181 round 9

**Chain detector:** 49 findings over 9 rounds, round 9 `--`, no chain by its rule. **Its
all-clear does not cover F1**: the finding's line (`validate_config(str(tmp_path))`) is
unchanged, but what `tmp_path` means changed in the round-8 fix `175aa6d` one line
above — F1 is a finding on the previous round's fix by inspection, streak 1 for
`configtool/model.py`. The CLAUDE.md tell for a chain (each fix creates the next
finding's precondition) is present even though the "twice consecutively" rule has not
fired. Recorded as a detector limitation: a blame on the finding's own line misses a
finding whose *meaning* the previous fix changed.
**Control finding:** none. Agreement **uncorroborated**.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F1** `save()` now validates beside the resolved target, so a relative `working_directory` through a symlink into another directory resolves against the wrong directory (Codex P1) | leaning FIX (a): validate beside the link, copy to a temp beside the target, replace (~8 lines) | **FILE**: (a) is a probe in the wrong layer (Step 3 — the loader's `config_dir` policy owns this); the naive form risks EXDEV across a bind mount; (b) revert restores a *silent* loss (edit lands on a container-local file the entrypoint re-links away) — worse; (c) refusal blocks every mode-1 save. Reachability: the CLI pre-validates through the link path, so accept-then-fail is unreachable from it and the refuse direction is loud; the shipped Docker mode-1 config uses absolute paths. `hits_per_year` 0.006–0.15 · `hours_per_hit` 1 · `discount` 0.56 → ≤ 0.08 h/yr | **FILE, decay 6 months** — rater 1 conceded on reachability (the CLI's own pre-validation) and on Step 3. Owning-layer fix for the issue: `config_dir = Path(path).resolve().parent` in the loader, a released-semantics change (§14.2) for its own increment. Thread left open pending the owner's OK to file |
| **F2** `masked_profiles` renders a mis-indented `team: {token: …}` verbatim | `cheap` yes | `cheap` yes, 1 line | **FIX** — containers render as `<dict>`/`<list>` |
| **F3** `init` through a symlinked profiles file replaces the link | `cheap` yes | `cheap` yes, ~3 lines; `silent` yes; safe to resolve here (nothing directory-relative in that file) | **FIX** — write to the resolved target |

Notes:
- Adoption: 2 of 3; 1 FILE.
- Both raters: one confirming round on the two one-liners, then stop regardless.

**Status: open.** F1 filed as #182 (ready-for-agent) with the owner's decision: relative paths resolve against `$COOP_HOME` (default `~/.agentcoop`) for config and runtime alike — the owning-layer fix, with a constant base rather than a resolved one. Decay 2027-03-23.

---

## 2026-09-23 — PR #181 final internal review (code-reviewer agent)

Codex round 10 (a confirming round after the quota returned) completed on `15d2c83`
with zero findings and, unusually, no 👍 reaction either — CLAUDE.md's clean signal
never arrived. The owner asked for one independent internal look with the question
"anything more critical than the previous rounds found?", to decide between shipping
and another round. **Chain detector:** no chain. **Control finding:** none.

Verdict from the reviewer: nothing that holds the PR. Five findings, all outside
rounds 1–10; all decided at Step 1 (`cheap`), one rater — the owner chose to fix and
wrap rather than run a second rater or another Codex round.

| | outcome |
|---|---|
| **F1** `--unset a.b.c` with `a.b` absent CREATED `a: {b: {}}` and reported ok (merge-patch is right for a fragment, wrong for the CLI's own deletion verb); `silent` | **FIX** — an absent parent is refused; an absent leaf on a present parent stays the RFC no-op |
| **F2** first write through a *dangling* symlink replaced the link (rounds 8/9 fixed `save()` and `init_profile`; `_create_file` was the third write site, unswept); `silent` | **FIX** — write the resolved target, as the other two do; not reachable from the shipped Docker path (it links only when the target exists) |
| **F3** `--set connectors=[]` was a silent no-op (nothing to merge by name) | **FIX** — an empty list for a named block is refused with the pointer to `op: remove` |
| **F4** masking is by key name; a credential in `server.url` userinfo or `new_session_args` is not a credential field and prints as written | documented in the user guide, one sentence — outside "the schema's credential fields" the contract names |
| **F5** the zero-rule start refusal said "empty deployment" for any rule-less file | **FIX** — message says what it checks |

Notes:
- Adoption: 4 of 5 fixed, 1 documented.
- Owner's ruling: no further Codex round; ship on green tests + CI.

**Status: open.** Settles with the round-4 entry.

## 2026-09-24 — PR #184 round 1 (coop-keeper PR ②)

**Chain detector:** 8 findings (7 review + 1 security) on `3572ae5`, first round, `--`, no chain.
**Control finding:** none — still no settled entry. Agreement **uncorroborated**.
Increment: design §6 item 2. Seven of eight findings land on the keeper's prose (skills,
AGENTS.md, design §3.8) — the artifact under review is instructions read by a model, so
"cheap" is lines of prose.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F1** `create-user` prints "already exists — skipping", exit 0, when an active account with the expected email exists and no connector uses it; the skill then writes a password the account does not have (P1) | true (`admin/cli.py:309-328`); loud but late (auth fails at reload); `cheap`: stop before the write, offer another name or a confirmed take-over (MM deactivate→reactivate, RC delete→create) — the owner's direction | true; `cheap`: one bullet + a §3.8 correction ("fails loudly" is wrong about the CLI) | **FIX** — both; §3.8 corrected too |
| **F2** second team + "none" rooms → no team membership → connector cannot connect (P1) | true (only `add-to-channel` reaches `add_user_to_team`; lab saw the degrade); `cheap` text | true; `cheap` two lines; an `add-to-team` subcommand is PR ① surface | **FIX** — "none" is not an answer in the second-team case |
| **F3** a symlinked ancestor inside the keeper dir lets the sync write outside it (P2) | scored → DROP (0.01–0.05 hits/yr, discount 0.32, unsupported manipulation per the owner) or a 2-line loud refusal | true; `cheap` runs first: one `is_relative_to` check + one test satisfies the owner's one/two-line ruling | **FIX** — rater 1 conceded on the cheaper fix (one line, existing test file), not on the score |
| **F4/F5** a two-write plan's second fragment cannot dry-run clean before the yes and its digest is stale after write 1 (P1, P1) | true as a protocol gap; the lab keeper already did the right thing; `cheap` text, owned by `coop-apply-config` not the two callers | same, layer: apply-config; one ~4-line rule fixes both | **FIX** — one rule in apply-config: later fragments may show only the findings the earlier write removes; re-dry-run after it; write against the digest the previous write returned |
| **F6** the post-yes dry run's new digest is adopted, bypassing the guard on the creation path (P2) | true, `silent`; `cheap`: the second digest must equal the first | true, `silent`; cheaper: write with the first digest | **FIX** — the digest must equal the first dry run's; a difference means someone else wrote |
| **F7** presence check on `tool_presets` instead of `tool_presets.readonly-builtins` (P2) | true, `cheap` | true, `cheap` | **FIX** |
| **F8** (security) shared agent-chain list across installations lets a same-named human bypass the sender allow-list (P2) | design §3.7 accepts and documents it; not this increment's job | true mechanism (`sender_policy.py:28`); scored: 0.001–0.1 hits/yr, keeper connectors set `filter_sender: false` so unaffected, fix 4–8 h + tax → net ≤ 0 | **DROP** — accepted in §3.7, mitigated by the template's `filter_sender: false`; revisit if that default changes |

Notes:
- Adoption: 7 of 8 (all seven through the `cheap` gate; none rests on a number). 1 DROP by design + score.
- Finding kinds: 6 protocol/text gaps in the skills (the multi-write plan was under-specified — one concept, five findings), 1 code guard, 1 accepted design trade-off. No finding on production Python except F3.
- Owner weighed in before triage on F1 (offer a take-over, not just a stop — adopted) and F3 (only if one/two lines — it was).

## 2026-09-24 — PR #184 round 2

**Chain detector:** 12 findings over 3 detector rounds (the security review counts as
its own), `--` throughout, no chain by its rule. **By inspection, R2-F2 is the
round-1 F3 fix found incomplete** — a symlinked *file* target where round 1 guarded a
symlinked *ancestor*; its line (`copy2`) predates the fix, so blame cannot see it.
Streak 1 for `gateway/upgrade.py`, the detector limitation already logged on #181
round 9 (a finding whose *meaning* the previous fix changed). Security review: clean.
**Control finding:** none. Agreement **uncorroborated**.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **R2-F1** `plan.lock` taken at the config write (apply-config step 4), after persona, account and rooms; two keepers can both create server state (P2) | true vs §3.8 "before executing"; `cheap` text: lock first thing after the yes, released after the last step incl. those outside config.yaml | same; adds that remove-bot's step 3 also ran after release; consequence milder than claimed (loser re-plans, password file kept) | **FIX** — apply-config §4 reworded; add-bot and remove-bot cross-reference it |
| **R2-F2** a symlinked in-use *file* is written through by `copy2` (P2) | true; the symlink surface is now enumerated (ancestor / dir / file); one line, within the owner's bound | same; `if target.is_dir() or target.is_symlink(): _remove_path(target)`, one test | **FIX** — one line + test; noted as the round-1 fix's incompleteness, streak 1 |
| **R2-F3** install.sh warns and exits 0 when the keeper sync fails, then says "Installation complete" (P2) | true; owner's quiet-success/loud-failure rule; `cheap`: `error` | same, plus: the block must move below the `install_meta.json` write or the remedy it names (`coop upgrade`) has nothing to read | **FIX** — moved after the meta write, `warn` → `error` (exit 1) |
| **R2-F4** AGENTS.md's canonicalisation (lower-case, strip slash) is weaker than the runtime's `canonical_origin` (default port, root dot, IP form, path kept); removal step 3 can delete a shared account, permanently on Rocket.Chat (P1) | true, `silent` at planning time, high consequence; `cheap` text naming the runtime rule | same; layer: the concern is `bot_identity.canonical_origin`'s; also make `config_edit._canonical_origin` delegate to it (one line) so `--credentials-from` agrees with the daemon; durable fix (an `origin` field in `show --json`) is a named concept → FILE separately | **FIX** — AGENTS.md, design §3.4, both skills name the runtime rule; `config_edit` delegates. The `origin`-field idea is noted here, not filed: nothing in this PR needs it yet |

Notes:
- Adoption: 4 of 4, all `cheap`, none scored. Rater 2 re-ranked by consequence F4 ≥ F2 > F3 > F1 (Codex: F4 P1, the rest P2 — agrees on the top, and F1's P2 is generous for what is a documented re-plan).
- Finding kinds: 2 protocol gaps in the skills (lock span, canonicalisation), 1 installer exit code, 1 follow-on of the previous round's fix. The follow-on is the streak to watch; a symlink finding in round 3 stops the patching.

## 2026-09-24 — PR #184 round 3

**Chain detector:** 19 findings over 4 detector rounds; this round fires on
`coop-add-bot/SKILL.md` (streak 1) and `gateway/upgrade.py` (streak 1). By inspection
`upgrade.py` is **streak 2 on the symlink theme** — R1 ancestor guard → R2 file symlink →
R3 the keeper root itself — each guard the precondition of the next finding; the detector
sees 1 because R2's line predated R1's fix. The CLAUDE.md rule applies: no further patch.
Security review: clean. **Control finding:** none. Agreement **uncorroborated**.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **R3-F1** the keeper directory or `agents/` itself a symlink defeats the ancestor guard (P2) | DROP — third link of the chain; owner: unsupported manipulation | DROP — same; the writes land where the operator pointed the link; owning layer is docs: one sentence in §3.1 | **DROP, and the chain deleted** — owner's ruling after the round: symlinks *are* supported and followed; an owned path linked to the operator's own file is overwritten, which is what the link asked for. The R1 ancestor guard and R2 file-symlink replace (and their tests) are removed; §3.1 says so. The chain went negative by its second link, as the skill predicts |
| **R3-F2** `coop-add-bot`'s "no connector uses this username" compares case-sensitively; Rocket.Chat does not — the take-over branch could delete an account `ProbeBot`'s connector still uses (P2) | true, `cheap` text (remove-bot already says case-insensitive) | true, traced to a permanent RC deletion on false information; `cheap`; outranks F7 | **FIX** — add-bot and §3.5 say case-insensitively |
| **R3-F3** a hand-written `server.team` given as an id vs the profile's name → keeper plans a duplicate bot; runtime refuses at reload (P2) | FIX as prose ("when the team field looks like an id, ask") | DROP: `config_validate.py:290` documents this blind spot as the runtime's to catch, loudly; the fix that removes it is a team-id lookup — PR ① surface; 0.002–0.04 hits/yr | **DROP** — rater 1 conceded on the cited validator comment and the loud refusal |
| **R3-F4** `_remove_path` then `copytree`: a failed copy leaves the old skill gone (P2) | FIX — copy to a sibling, then swap (~3 lines) | DROP: the sibling is an unlisted path §3.1 promises never to touch (a crash leaves it forever); loud (`coop upgrade` warns, install.sh exits 1 since R2) and the next upgrade repairs it; failure-of-a-failure, net ≤ 0 | **DROP** — rater 1 conceded on the §3.1 promise |
| **R3-F5** a plan running past its 10-minute lease removes another keeper's lock at cleanup (P2) | true, `cheap` text | true and correlated (the `.claude/settings.json` prompt on Claude Code can stall a plan); `cheap`: remove only if `holder` still names this plan; lease refresh is a new concept — skip | **FIX** — apply-config §8 |
| **R3-F6** a deactivated account discovered only when `create-user` fails after the yes is revived without a new confirmation (P1) | true; `cheap`; consistent with "a yes covers the plan shown" | true, strongest of the round: the CLI deliberately refuses to auto-reactivate (`admin/base.py:100–130`), and the skill reinstated that decision unconfirmed | **FIX** — stop, show the revival, ask again; §3.5 says when the plan can and cannot know |
| **R3-F7** a hand edit between step 2's write and step 3 can make the account shared again before `delete-user` (P1) | FIX as prose (re-read, compare digest) | DROP: §3.8/§4 declare best effort against mid-plan hand edits; nothing correlates an operator adding a connector for the username they just confirmed deleting, ~0.001/yr; the outcome is reasonable for the trigger | **DROP** — rater 1 conceded on §4 |

Notes:
- Adoption: 3 of 7. Three concessions by rater 1 (F3, F4, F7), each on a cited clause or comment, none on a number.
- Severity vs Codex: F6 P1 stands; **F7's P1 is inflated** (declared best effort); F2 outranks F7 (silent at planning, permanent deletion).
- Finding kinds: 3 "a rule stated in one skill/section and missing from its sibling" (fixed as one sweep), 4 edge cases (a chain link, a hand-written id, a failed upgrade's failure, a sub-minute hand-edit race). **Converged by the owner's rule** — both raters: fix the three, stop requesting reviews.

## 2026-09-24 — PR #184 round 4 (final, at the owner's request)

**Chain detector:** 24 findings over 6 detector rounds, `--` on this one; no chain. The
symlink chain of rounds 1–3 was **deleted** before this round (owner: symlinks are
followed, not guarded), and no symlink finding returned. Security review: one finding.
**Control finding:** none. Agreement **uncorroborated**.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **R4-F1** step 2's chain patch drops every username "no surviving bot uses", so a hand-written entry for another deployment's bot goes too (P2) | true, `cheap` text: subtract only this bot's name | true; asymmetric with add-bot, which preserves hand-written entries; `cheap`, lowest priority | **FIX** — remove-bot, §3.6, §3.7: only the removed bot's username, never another name |
| **R4-F2** a plan running past its 10-minute lease can be taken over mid-execution; last round's fix only stops the first keeper deleting the replacement (P2) | DROP: follow-on edge of the round-3 fix; the design sells the expiry as the dead-keeper remedy | DROP, scored: 0–0.4 hits/yr (both factors guessed) × ~1 h × 0.56 → ≤ 0.2 h/yr against a new invariant in three skills; not a chain link by blame; the takeover is visible in the first keeper's report since round 3 | **DROP** — §3.8 best effort; a 30-minute lease would triple the dead-keeper stall the expiry exists to bound |
| **R4-F3** `Bash(coop *)` auto-approves `coop send --attach ~/.agentcoop/config.yaml` — credentials to a chat room past the `Read` deny (P1) | true (`coop send --attach` exists; room names come from `coop list --all`); `cheap`: name the subcommands the skills run; the both-directions test enforces it | true and traced (`cli.py:150`, `control.py:741–749` pass the path unrestricted); falsifies §3.3's "none is known to"; `cheap`, first; OpenCode stays inside its documented residual (`head` already reads the file), no bash rules there | **FIX** — allow list is `coop config *`, `status`, `start`, `stop`, `reset *`; §3.3/§3.9 say why never `coop *`. `coop list --all` (AGENTS.md only) now prompts once |
| **R4-F4** "none" to the rooms question still writes `include: ["*"]`, which serves the default rooms both servers auto-join a new account to, while the plan says DM-only (P1) | true; `rooms: {include: [], direct: true}` validates (checked); `cheap` | true on both platforms (MM `create_user` joins the team; RC `users.create` defaults `joinDefaultChannels`); contradicts §3.5's promise; `cheap` | **FIX** — add-bot and §3.5 |
| **R4-F5** (security) `filter_sender: false` admits any server user as a guest; the built-in guest rule auto-approves `coop fetch-history --watcher <any>`, which the control handler serves on the honor system → another room's history (P2) | true mechanism; gateway's, tracked as #34 (token auth); the keeper default is §3.5's; not this change's | chain verified (`role_of` → guest rule `core/config.py:113–116` → global watcher lookup `control.py:371–379`, honor-system by its own docstring); RC only; security under ADR 0001 but a gateway defect the keeper widens the population for; FILE against #34 | **Not this PR** — replied with the chain and #34. *Correction 2026-09-25:* not RC only — Mattermost implements history too (`supports_history()` → True). It is a security issue, **deferred**: AgentCoop targets chat servers whose members are mostly trustworthy, and known gaps of this kind wait until security is taken on as a whole. Owner's rulings: no per-command guest fix (a guest can as well ask for what others told the agent or what it saved to memory — ADR 0001's point); `filter_sender: false` stays |

Notes:
- Adoption: 3 of 5. Both raters: F3 and F4 contradict a stated promise and are cheap; F1 is a consistency slip; F2 and F5 are the tail. **Not corner cases only** — but by the owner's instruction this was the last round.
- Severity vs Codex: F3 P1 stands (the only finding in four rounds on the permission files that bit); F4 P1 stands on the broken promise, not on harm; F5's P2 belongs to the gateway.
- Four rounds: 24 findings, 16 fixed, 7 dropped, 1 declined as another issue's. The symlink chain (3 findings) was deleted after the owner's ruling, so 2 of the 16 fixes were later removed.

## 2026-09-24 — PR #184 round 5 (after an internal consistency sweep)

**Before the round:** the previous two rounds each held "a rule stated here, missing there"
findings, so an internal sweep enumerated that class (7 rows, all fixed in `677da46`) before
Codex was asked again.
**Chain detector:** fires — `coop-remove-bot/SKILL.md` streak 1 (R5-F1 lands on `677da46`),
and `coop-add-bot/SKILL.md` carried over at streak 2 from rounds 3–4, so the tool prints
"Do not patch again". Blame check (rater 2): the remove-bot paragraph was original until
`a083725` and `677da46` touched other lines — one link on a fix, not two. Security: clean.
**Control finding:** none. Agreement **uncorroborated**.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **R5-F1** step 2 keeps the removed bot's chain name only if "a surviving connector of the installation" uses it; a `bob` bot on another server loses its loop protection (P1) | true — my round-4 wording conflated the account's scope (installation) with the name's (global, §3.7); `cheap`, one phrase | same; the owning layer is §3.7, which never stated the retention condition: fix it there, mirror in the skill, add a §7 item | **FIX** — §3.7 states the condition, remove-bot mirrors it and tells the plan to say "account deleted, name kept", §7 item 16 |
| **R5-F2** the manual install path no longer writes `install_meta.json`; `coop upgrade` exits "not found" (P2) | true — the removed `coop onboard --repo-path` wrote it; `cheap` doc step | same, loud (`upgrade.py`), ours because §3.12 removed the writer | **FIX** — INSTALL.md step 5 writes the three fields `install.sh` writes |
| **R5-F3** `init`/`profiles` accepted as profile names (P2) | FIX as a bootstrap sentence (I had checked `init_profile` in `admin/config.py`, which does not refuse them) | **untrue**: `admin/cli.py:379–383` refuses both with a clear message before `init_profile` is reached | **DROP** — rater 1 conceded on the cited lines; the CLI owns the check and has it |
| **R5-F4** user-guide prerequisites still tell users to create a bot account first (P2) | true, `cheap` | true; a wrong rationale outranks a small bug; keep the bot-account sentence for the hand-written path | **FIX** — administrator access is the prerequisite; a bot account only for a hand-written `config.yaml` |
| **R5-F5** `Read(~/.agentcoop/agents/**)` allows reading `agents/.bot-password.*`; OpenCode has no `read` deny for it (P2) | true, silent, contradicts §3.3; `cheap`: one deny per file + the test lists | same; matches the owner's guardrail rule verbatim | **FIX** — denies in both files, both test lists, §3.3 names the file |

Notes:
- Adoption: 4 of 5. One concession by rater 1 (F3), on cited code. Severity: F1 P1 stands; F5 should outrank F2/F4 (silent, a promise); F3 was not a finding.
- **Not corner cases only**: F1 and F5 contradict stated promises, F2 contradicts INSTALL.md's own text. By the stop-loss agreed with the owner ("stop when a round has no promise-contradicting finding") this round does not end the review on its own; the chain detector's "do not patch again" on `coop-add-bot` is a carry-over streak with no add-bot finding this round.
- Five rounds: 29 findings, 20 fixed, 8 dropped, 1 declined as #34's.

## 2026-09-25 — PR #184 round 6 (final)

Codex review on `b09c72d`: 👍, "Didn't find any major issues"; security review clean; no
inline findings. Reviewed commit = the merged head (squash-merged as `a4cbc4a`).
The stop-loss agreed with the owner ("stop when a round has no promise-contradicting
finding") is met.

**Totals over six rounds:** 29 findings — 20 fixed, 8 dropped with written reasons, 1
declined as #34's (guest `fetch-history --watcher` on the honor system). One chain (three
symlink guards in rounds 1–3) was deleted on the owner's ruling that symlinks are followed.
An internal consistency sweep between rounds 4 and 5 found 7 more of the class the last
rounds kept producing; round 5 then found one of that class, caused by round 4's own
wording.

**Status: settled for this PR.** The `filter_sender` question raised from round 4 F5 is
closed by the owner: the default stays `false`. The finding is a real security issue,
deferred under the deployment assumption of mostly-trustworthy chat members, and it will be
solved with the rest of security rather than by a per-command guest restriction.

## 2026-09-25 — PR #186 round 1 (#182: relative paths resolve against COOP_HOME)

**Chain detector:** 3 findings on `ad5c0a3`, first round, `--`, no chain.
**Control finding:** none — the log still has no settled *finding* (the PR #184 entry
is settled as a PR status, not as an observed outcome of one verdict). Agreement
**uncorroborated**.
**Protocol slip, recorded as such:** the fixes were implemented in parallel with the
blind rating, and the second rater saw the working tree change under it — the shape of
the F3 fix and part of the F1/F2 warning text — before writing its report. Its F3
conclusion predates that; F1/F3 agreement still cannot be logged as independent
(Step 4: one rater had an input the other lacked). Rule for next time: **do not touch
the tree until the second rater has reported.**

Increment: "Relative paths in config.yaml resolve against RUNTIME_DIR (`$COOP_HOME` or
`~/.agentcoop`); the base is a constant the loader owns" + rulings A–D on #182.

| | rater 1 (author) | rater 2 (blind, see slip) | outcome |
|---|---|---|---|
| **F1** `persist_coop_home` interpolates the directory inside double quotes, so `/srv/$USER/coop` expands in the next shell (P2) | true; `cheap`: single-quote with `'\''` escaping, grep the same form for idempotence, ~6 lines; silent-ish (next `coop` looks in another directory); reachability near zero | true; `cheap` (~3 lines with `%q`); semi-loud — `coop start`'s preflight names a directory the operator never typed; same layer; this increment's | **FIX** — single-quoted line; a test sources the rc file in real bash and zsh with `$USER`, a quote and a backtick in the path |
| **F2** a fish user with a dormant `.bashrc` gets the export in an unrelated file and no warning; the banner names `config.fish`, never written (P2) | true; `cheap`: decide by `$SHELL` whether the *active* rc file took the line, warn otherwise, ~8 lines; silent for keeper-written files | true; ranks **first** — the only finding with a population; `cheap` (~4 lines); FIX the warning, **FILE full fish support** (PATH block has the same gap) with a 6-month decay | **FIX** the warning (active-shell check); fish support not filed yet — the owner decides whether to open it (`feedback_confirm_before_filing_issues`) |
| **F3** `COOP_HOME=/` leaves opencode's read/edit denies unrewritten (P2) | true but the proposed fix is the wrong layer; `cheap`: refuse `/` in `paths.py` and `coop_home_dir`, 2+1 lines; expressible, so not `cannot-occur` | same; traced live that `/` also yields junk for Claude rules and `external_directory`, so "covering deny patterns" is wrong regardless; owning layer `paths.py`, finding points at `upgrade.py` | **FIX** at the loader and installer — `/` refused by name in both; the `runtime_dir.name` guard in `upgrade.py` removed as unreachable by construction |

Notes:
- Adoption: 3 of 3, all `cheap`, none scored. Severity re-ranked F2 > F3 > F1 by both
  raters; Codex's flat P2 flattened a real-population finding and two near-zero ones.
- No promise-contradicting finding; by the stop-loss agreed on PR #184 ("stop when a
  round has no promise-contradicting finding") one confirming round then stop.

**Status: open.** Settles with the confirming round.

## 2026-09-25 — PR #186 round 2

**Chain detector:** 3 findings on `f601622`; `install.sh` 2 and `gateway/upgrade.py` 1 on
our own last fix, **streak 1 each** — once is noise by the rule, but all three land on
round 1's fixes and the "same kind" test fires (below).
**Control finding:** none. Agreement **uncorroborated**. The tree was not touched until
the second rater had reported (the round-1 slip, not repeated).

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F1** `COOP_HOME=/tmp/..` passes the root refusal; basename `..` in the keeper globs (P2) | true; `cheap`: refuse non-canonical components, not normalise (bash has no normpath in reach) | same; traced live — and **the two validators already disagreed** (`paths.py` refused `//`/`/.`, `coop_home_dir` did not); Codex's normaliser is ~12 lines of bash | **FIX** via the class rule |
| **F2** `"` or `\` in COOP_HOME corrupts the generated permission JSON; `\b` parses as another path (P2) | true, silent; `cheap`: reject at the validators; JSON serialisation is a new concept and misses `install_meta.json`'s heredoc | same; per-writer escaping is O(writers) ≥ 5; owning layer is the loader, finding points at a writer | **FIX** via the class rule |
| **F3** a commented-out exact export counts as active; `/srv/coop-old` matches `/srv/coop` as a prefix (P2) | true; `cheap`: compare each active export's value for equality, ~8 lines | same; ranks **first** — a prefix match silently leaves a live install unmentioned; not an anchored regex (escaping the path is the same bug class) | **FIX** — `rc_exports_coop_home` extracts each uncommented export's value and compares it whole |

**The pattern.** Round 1: `$`, quotes, backticks, `/`. Round 2: `/tmp/..`, `"`, `\`,
`//`. Both raters: one class — *a COOP_HOME spelling some writer downstream did not
anticipate* — and one rule closes it. Rater 2 proposed a denylist (`"`, `\`, control
characters) plus canonical form and handed the glob metacharacters up; rater 1 a
whitelist. **Taken: the whitelist** — absolute, canonical, letters/digits/`.`/`_`/`-`/`/`
only — because it subsumes the denylist, settles the glob question, and is one sentence
in the guide. Enforced identically by `gateway/paths.py` (`COOP_HOME_RULE`) and
`install.sh` (`coop_home_dir`); `tests/helpers.py` `COOP_HOME_SPELLINGS` is the
enumerated surface both suites run. Cost ~4 lines each side plus the table; tax one
invariant. In the increment: "the base is a constant the loader owns" includes what a
valid base is, and the owner's framing (a rare, conflict-only setting) makes a strict
spelling acceptable.

Notes:
- Adoption: 3 of 3, all `cheap`, none scored. Rater 2's built rates for the log:
  F1 0.0003–0.04/yr, F2 ≤ 0.004/yr, F3 0.003–0.2/yr (operators 5–20 × P(sets COOP_HOME)
  0.05–0.2 × P(spelling)); `hours_per_hit` 0.5–3; discount 1.0 (no clause covers install).
- Severity re-ranked by both: F3 > F2 > F1. Codex: three P2.
- No promise-contradicting finding. Next: one confirming round. **If round 3 lands on
  `install.sh` or `upgrade.py` again, that is streak 2 — stop patching and re-read the
  increment with the owner.**

**Status: open.** Settles with the confirming round.

## 2026-09-25 — PR #186 round 3

**Chain detector:** code review on `74b0920` clean; one **security-review** finding, on
`install.sh:174` — a line from the PR's first commit, not from either round's fix; `--`,
streak reset. (Round 2's note "if round 3 lands on `install.sh` again, streak 2" was
overbroad: the rule is *on our own last fix*, and rater 2 said so.)
**Control finding:** none. Agreement **uncorroborated**. Tree untouched until rater 2
reported.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F1** a non-default COOP_HOME another local user pre-created, links or can write into lets them replace `repo/.venv/bin/coop`, which `~/.local/bin/coop` runs (security review) | true, silent; not promise-contradicting (SECURITY.md lists local co-tenants in neither column); split: owner/symlink/others-writable refusal is `cheap` (~8 lines, installer only); the parent walk is not | same split, three ways: (i-a) owner + symlink refusal `cheap`, **FIX**; (i-b) others-writable + 0700 **DROP** — `stat` is not portable, secrets are already 0600, 0700 would change the default dir's mode; (ii) parent walk **FILE** 6 months; scored (ii): hits 1e-5–0.12/yr, hours_per_hit 20–40, tax 0.5 → net ≤ 0 at the midpoint | **FIX** (i-a) and the others-writable half of (i-b): `runtime_dir_is_ours` refuses a non-directory, a directory not owned by the invoker, or one writable by others — `find -L -maxdepth 0 -perm -o+w`, which BSD and GNU find both take; a symlink is followed and its target judged (the first cut refused symlinks outright, which would have refused a `~/.agentcoop -> elsewhere` on the default path against the PR #184 symlink ruling — caught by the closing advisor pass, not by a review round), so the portability objection does not hold (concession on checkable evidence, in rater 2's direction on 0700 and rater 1's on the mode check). 0700-on-create **DROP** per rater 2. Parent walk: **DROP** — owner's ruling (2026-09-25): a shared parent without a sticky bit is the host's flaw, and a directory the operator chose under one is the operator's; nothing special is done there. Recorded as the boundary in SECURITY.md (see round 4) |

Notes:
- Adoption: 1 of 1 fixed in part; one part dropped with the reason, one part a FILE
  candidate awaiting the owner. Verdicts agree on every part; the one disagreement
  (others-writable) resolved on a cited, checkable fact.
- The reachable input is a COOP_HOME under a sticky world-writable parent
  (`/tmp/coop`): stock `/srv` and `/opt` are root 0755, and the default `~/.agentcoop`
  sits under `$HOME`. The exposure is new with this PR. Both raters: hardening, not a
  stated promise — rate applies; escalation condition recorded: if the owner brings a
  hostile local shell user in scope, reachability is shown and the parent walk is forced.
- Both raters: one confirming round after the fix (CLAUDE.md: commits in response to a
  review most need a pass; CI does not exercise `-O`).

**Status: open.** Settles with the confirming round.

## 2026-09-25 — PR #186 round 3, code review (arrived after the security review)

Codex's code review of `74b0920` (`5321952266`) landed after its security review of
the same commit and after round 3 above was triaged and fixed. Four findings.
**Chain detector:** `--` by its rule (the code since the previous round was `7710a3b`,
not what these land on). By inspection F1 is on round 2's `rc_exports_coop_home` and
F2 on round 1's append — the second and third findings in a row on the rc-file
persistence in `install.sh`. Rater 2 corrected the author's "three consecutive rounds"
framing: by metadata the streak is 1, so Step 0 does not fire; what follows is an
**economic re-score of the feature**, the DROP anchor's logic.
**Control finding:** none. Agreement **uncorroborated**. Tree untouched until rater 2
reported.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F1** several active exports: the first equal one satisfies the check, the shell uses the last (P2) | true, silent; `cheap` (compare the last); moot if persistence goes | same; 0.003–0.2/yr | **moot** — persistence deleted (below) |
| **F2** an unwritable rc file aborts the installer under `set -e` after the symlink, before install_meta.json (P2) | true, loud; `cheap`; the PATH block's `>>` has the same gap, pre-existing | same; ≤ 0.02/yr; PATH copy out of scope | **moot** — persistence deleted |
| **F3** `coop-add-bot/SKILL.md:227,233` keep `/Users/alice/.agentcoop/…`, which the rewrite does not touch (P2) | true; the author had left it as illustrative in the internal review — Codex is right that the keeper copies examples; `cheap`: spell the example `~/.agentcoop/…` | same; owning layer is the skill text, not `upgrade.py` | **FIX** — the example uses the replaceable spelling, marked "written out absolute" |
| **F4** `export COOP_HOME="$RUNTIME_DIR"` exports the DEFAULT too; a `$HOME` the explicit-value rule refuses (a space, a non-ASCII name) aborts the keeper install (P2) | true, loud, this PR's; `cheap`: export only when set | same; ranks **first** — the one finding with a population (0.05–0.5/yr) | **FIX** — exported only when the operator set it |

**The rc-persistence chain, re-scored whole.** Ruling B's words — "install.sh sets
COOP_HOME to that location", "set once before the first install" — read as *uses*;
writing the operator's shell startup file was a reading of it, and that reading drew
every `install.sh` finding: `0a967cb`, round 1 (2), round 2 (1), this review (2) — six
findings on ~45 lines that embed an operator-supplied value in a file the installer does
not own, plus 12 of the suite's 19 tests. Harm prevented: the operator ignores the
printed line — `coop start` then fails loudly (no config); rc files never covered cron
or a service manager. Rater 2: 0.05–6 h/yr, midpoint ~0.5; tax already paid ≈ 6 findings
× 1–2 h, ongoing 0.5–2 h/yr — **net ≤ 0 at the midpoint.** The PATH block does not change
the answer: it writes a constant line, which has none of the value-comparison surface.
Owner's ruling (2026-09-25), after asking what the concern was and whether the shell file
could be determined (bash/zsh only were written; fish and csh got a warning): **delete
it — "ask the user, it is not a common use case anyway."** `persist_coop_home` and
`rc_exports_coop_home` are gone; `coop_home_hint` prints the line in the login shell's
syntax (`export` / `set -gx` / `setenv`) next to the existing `source` hint.

Notes:
- Adoption: 2 of 4 fixed, 2 moot by deletion. Severity re-ranked by both: F4 > F3 > F1 > F2.
- Increment-definition check, as Step 0 asks: rc persistence was not in it. The
  deletion is a reduction of ~45 lines and 12 tests.

## 2026-09-25 — PR #186 round 4 (confirming round on `7710a3b`)

**Chain detector:** `install.sh` 1 finding on our own last fix (round 3's
`runtime_dir_is_ours`), streak 1. By inspection this is the directory-safety check's
second link (owner/symlink/world-writable → group-writable → setgid/ACL/parents next).
**Control finding:** none. Agreement **uncorroborated**. Tree untouched until rater 2
reported.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F1** a 0770/0775 COOP_HOME whose group holds a lower-privileged user passes (`-o+w` only); refuse `g+w` and lock created modes (P2) | same class as the parent walk: the host's and the operator's layer | a `g+w` refusal is **incorrect**: Debian/Ubuntu/Fedora private user groups (umask 002) make every operator-made directory 0775 with group == user, so the refusal lands on COOP_HOME's own use case; MAKE IT LOUD with a "group == `id -gn`" suppression, ~5 lines, one heuristic; 0700-on-create DROP as in round 3 | **DROP, with the boundary written** — owner's ruling: weak host permissions are the host's flaw, nothing special is done. Rater 2's warning was a defensible alternative; the owner is the tie-break. SECURITY.md out-of-scope now names other accounts on the same host (§14.5); `runtime_dir_is_ours`'s comment says it guards the accidental case |
| **F2** a container launched with `COOP_HOME` set: the entrypoint writes `/root/.agentcoop`, `coop` reads `$COOP_HOME` — `coop start` fails (P2) | true; `cheap`: `unset COOP_HOME` in the entrypoint, one line; this PR made the variable mean something | true but **loud** (`[ERROR] …config.yaml: FileNotFoundError`); trigger is `docker run -e COOP_HOME` only; the file is outside the PR; Docker fixed by ruling → DROP here, ride along with the next Docker change | **DROP for this PR, recorded on #183** — owner's direction: the image is for e2e runs only, will use defaults throughout and may lose its volume bindings, so the split disappears by construction |

Notes:
- Adoption: 0 of 2; both dropped with written reasons and one boundary written where a
  third finding of the class would otherwise land.
- Where the directory check stops (both raters): a documented boundary, not another
  bit — `install.sh` cannot own "safe against whom"; SECURITY.md does now.
- **Stop-loss met**: round 4 was round 3's confirming round and neither finding
  contradicts a promise. No further Codex round; the closing commit (deletion of the rc
  persistence, F3, F4, the boundary) ships on green CI by the owner's standing rule for
  a last round.

**Totals over four rounds:** 13 findings — 7 fixed, 2 moot by deleting the feature they
were on, 4 dropped with reasons (one of them a FILE candidate the owner declined). One
chain (rc-file persistence, six findings across three rounds) deleted on the owner's
ruling; one class (directory safety against a local co-tenant) closed by writing the
boundary. Two blind rounds contaminated by a protocol slip in round 1, none after.

**Status: settled for this PR.**

## 2026-10-07 — PR #195 round 1 (#187: human reset before the mention gate)

**Chain detector:** 1 finding on `360c8a1`, first round, `--`, no chain.
**Control finding:** none — the log still has no settled finding. Agreement
**uncorroborated**. Tree untouched until rater 2 reported (the only working-tree change
during the rating was the unrelated `test_coop_keeper_shipped.py` assertion, not committed
and not on the finding's path).

Increment: "a human post in a room resets the agent-chain counters for that room/thread
whether or not it @mentions this bot."

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F1** RC: an unmentioned human post now resets and is then rejected, so it is neither remembered in `seen_ids` nor does it advance the watermark; a reconnect replay (filter pinned to the outage boundary) resets a second time and an in-flight agent delivery keeps a free turn (P2) | true, and this PR created the class (before, only accepted human posts reset, and those are always remembered) — Q2 passes, not droppable on scope. MM immune: `_remember_seen` runs before the filter (`mattermost/connector.py:1744`). Harm needs H to be the last pre-outage message with no accepted post after it *and* a live agent post inside the replay's dispatch window; consequence is one extra turn, chain still cut at `max_turns+1`. Not `cheap`: the 2-line `sub.remember` on the rejected branch sits in `connector.py`, outside the increment, and reverses the documented "filtered posts are not remembered" rule at `:2452`. Not silent: `reset_all` now logs INFO when a counter was spent. `hours_per_hit 0.05` · `hits_per_year 0.15 (0.01–1)` · `discount 1.0` · `fix 1` · `tax 0.1` → net ≤ 0 | same trace, narrower window: per agent delivery it is filter → normalize → `enqueue` (`put_nowait`) → watermark commit, 5–50 ms for a text post; nothing correlates a reconnect with a live agent post inside it (LLM latency ≫ replay dispatch). `hours_per_hit 0.05 (0.02–0.2)` · `hits_per_year 0.005 (1e-4–0.5)` · `discount 1.0` · `fix 2` · `tax 0.1` → net ≤ 0 across the whole range; lighter than the DROP anchor. Owning layer is the connector's replay idempotence (`seen_ids`/watermark), not the filter line the finding points at. Flagged that remembering a rejected post may suppress an edit that adds the @mention (untraced) | **DROP** — written on the thread. Reopen for free if RC ever adopts MM's pre-filter `_remember_seen` order |

Notes:
- Adoption: 0 of 1. Verdicts agree and the `hits_per_year` ranges overlap (0.01–1 vs
  1e-4–0.5) → settled per Step 4, uncorroborated.
- Severity re-ranked: Codex's P2 reads as a loop-guard weakening; the traced outcome is a
  bounded `+1` turn in a sub-second race, with the reset line now visible at INFO.
- No promise-contradicting finding (`requirements.md` §14.3/§14.4 promise reconnect
  handling and make no loop-bound promise beyond "cut the chain"). By the stop-loss
  agreed on PR #184, no further Codex round: the only commits after `360c8a1` are a stale
  test assertion from `250851a` and this log.

**Status: settled for this PR.**

## 2026-10-07 — PR #196 round 1 (triage can measure itself: ledger, complexity gate, C901)

**Chain detector:** 4 findings on `593b0b8`, first round, `--`, no chain.
**Control finding:** none at the time of rating (this round's F3 sweep produced the
first settled row — #181 r9 F1, implemented — for the *next* round). Agreement
**uncorroborated**. Tree untouched until rater 2 reported.
**Cost:** ~45 min, 1 round, 2 raters (author + blind agent, ~93k tokens noted).

Increment: "the triage log can measure its own verdicts — every DROP/FILE is
falsifiable and time-bound, the cheap gate accounts for the function it lands in, and
CI backstops that limit on code still under it."

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F1** `per-file-ignores` suppresses every `C901` in a listed file, so a new over-limit function in `connector.py` passes CI; the documented ratchet is not delivered (P2) | true; two fixes: reword the doc to "file-level" (cheap, dishonest to the increment) or `# noqa: C901` per offending function (~88 lines, 39 files outside the increment) — the second is what "last resort" needs, since the listed files are where fixes land | true, reproduced with `--stdin-filename`; the doc's "bites only on code still under it" is false for 88/88 offenders. Correct fix for the one-liner is per-function: one command, `ruff --add-noqa`, 88 directives, re-check clean. Fails `cheap` (39 files) → scored: hits 1.5–15/yr (new >10 functions in those files, undetected) × 0.5–1 h, fix 0.3 h, tax ≈ 0 → payback 0.02–0.3 yr; `silent` | **FIX** — `--add-noqa` over `gateway tests scripts`, the `per-file-ignores` block deleted; verified a 12-branch probe appended to `rocketchat/connector.py` now fails CI. Caveat on the thread: growth *inside* a marked function is still the gate's job, not CI's |
| **F4** the gate is defined on radon, CI on ruff's mccabe; they disagree (handlers 46/43 vs 32/29; a 12-term `and` passes `C901` at 10, radon 12) (P2) | true, reproduced; the doc teaches a metric CI does not enforce; `cheap`: define the gate on `C901` with `--ignore-noqa`, renumber | true; whole-tree gap 131 functions radon>10 but `C901`≤10, ~40 in unmarked files; cheapest: gate on the backstop's metric, ~6 doc lines, in-increment → `cheap`. Running radon in CI is a new dependency for nothing the increment needs | **FIX** — gate measured with `uv run ruff check --select C901 --ignore-noqa <file>`; radon kept as a named second opinion, not the number compared |
| **F2** ledger row for #186 r3 F1 (ii) says FILE / 6 months; the round and the PR totals say the owner declined the FILE → DROP (P2) | true, mis-copied; `cheap` | true; also "6-month" contradicts the doc's own 12-month rule; loud (the file contradicts itself) | **FIX** — DROP, revisit 2027-09-25 |
| **F3** #181 r8 F1 (`_has_cycle` exponential on an acyclic alias graph) is a scored DROP with no ledger row (P2) | true, missed by a truncated grep; `cheap` | true; **pattern**: a sweep of every outcome cell finds 16 scored DROP/FILE verdicts, the ledger had 15 rows, one wrong, one missing and a third absent — #181 r9 F1, FILE → #182, closed by PR #186: the first `settled` row and a candidate control. Fix by re-deriving from the round tables, not by patching rows | **FIX** — r8 F1 and r9 F1 added; the ledger intro now says it is derived from the outcome cells and records the miss |

Notes:
- Adoption: 4 of 4. Severity re-ranked by both: F4 > F1 > F3 > F2 (Codex: flat P2).
  F4 and F1 are wrong rationales in a doc that defines the method — a reader would
  measure with a tool CI does not run and trust a backstop that does not cover the
  files it was enabled for. F2/F3 are one-row, loud or near-loud.
- Rater 2 proposed enabling `RUF100` (unused-noqa) so a marker left behind after a
  function is brought down fails CI, making "remove when brought down" mechanical. 38
  pre-existing unused `noqa` in `gateway/`/`tests/` would need removing first — a sweep
  outside this increment; left for the owner to decide.
- Rater 2's enumerating test (every `**DROP`/`**FILE` outcome has a ledger row) is the
  MAKE IT LOUD for the ledger class; a new test file, not taken this round.

**Status: open.** Settles with round 2 below.

## 2026-10-08 — PR #196 round 2 (confirming round on `bddb38a`)

**Chain detector:** 5 findings over 2 rounds, round 2 `--`, no chain.
**Control finding:** #181 r9 F1 is now `settled: implemented` and would serve; not used
— one finding, decided at the `cheap` gate, no second rater convened (one `cheap`
config/doc finding on a confirming round; stop-loss as on PR #184/#186).
**Cost:** ~10 min, 1 round, 1 rater.

| | rater 1 (author) | outcome |
|---|---|---|
| **F1** three `scripts/` functions carry ratchet markers and the doc counts them in the 88, but CI and the Makefile lint only `gateway/ tests/`, so `scripts/` is never checked (P2) | true (`ci.yml:28`, `Makefile:24`). `cheap`: the honest fix is to stop claiming a baseline CI does not enforce — remove the three markers, count 85, say `scripts/` is out of scope; a separate C901-only CI step for probe scripts that are not shipped would be a rule for code the backstop is not for | **FIX** — markers removed, doc says 85 in `gateway/`+`tests/` and that `scripts/` is not ratcheted |

Notes:
- Adoption: 1 of 1. Codex's P2 is generous for a count in a doc and three markers on
  unshipped probes; ranked below every round-1 finding.
- Also in this commit, from the owner (2026-10-08): failing the complexity condition
  is **not** a DROP — look for a shape that adds no complexity first, and if none,
  the refactor is its own scored item whose cost is never charged to the fix. Written
  into the skill's `cheap` gate and `finding-triage.md`.
- Security review did not run (Codex usage limit); the code review did, on `bddb38a`.

**Status: settled for this PR** — the confirming round found nothing on round 1's
fixes and nothing that contradicts a promise.

## 2026-10-12 — PR #198 round 1 (docs/design/multi-agent-enhancement.md, a proposal; no code)

**Chain detector:** 9 findings, 1 round, `--`, no chain (single commit).
**Control finding:** #181 r9 F1 (`settled: implemented`), presented as a code
finding F10. Rater 2 decided it DROP-as-stale with "had it been live: FILE,
owned by the loader" — the settled answer — but **disclosed** it had seen the
ledger row while verifying, so the control is contaminated and this round's
agreement is **uncorroborated**. Rater 1 (author) did not score F10.
**Gates for a doc:** `cheap` = ≤10 doc lines, no new named design concept;
`silent` = a rule an implementer would follow without noticing it is wrong.
**Cost:** ~40 min, 1 round, 2 raters.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F1** moderator authority undefined for a room whose bots span gateways (P1) | true — prod is multi-host; the guarantee "one count per topic per sender" is unscoped. Not `cheap`: needs an ownership decision → owner | true; `cheap` as a scope statement ("one moderator per room, in one gateway; cross-gateway rooms keep today's behaviour") + §7; the protocol Codex asks for is not the cheapest correct fix; `silent` | **FIX** as scope statement (§3.1, §3.6, §7); cross-gateway design left to the owner |
| **F2** moderator returns one topic id; §3.2 lets a message close one and open another (P1) | true, output shape; `cheap` | true, internal inconsistency; `cheap`; `silent` | **FIX** — list of (topic, action) |
| **F3** "end of the conversation" undefined (P2) | true; `cheap` | mostly true (ceiling is one terminal event); `cheap` | **FIX** — last open topic closes or a human says so; §7 marks it provisional |
| **F4** `parallel:` override not restricted to humans (P2) | true; 2 lines | true; role is server-injected so no new concept; `silent` | **FIX** |
| **F5** late reply after hop timeout starts a second plan on the same topic (P2) | true; ranked above Codex — defeats sequential ordering, not one duplicate | true; §5's residual covers a different consequence | **FIX** — late reply joins the running sequence; one plan per topic |
| **F6** `done` can skip an explicitly mentioned bot (P2) | true, table vs prose contradiction | true; mostly loud to the human | **FIX** — mentioned bots are required participants |
| **F7** objection always opens a topic with a fresh budget (P1) | true, bypasses §3.2's own retag guard | true; fix is "tag and validate like any post", not a round cap (new concept) | **FIX** — objection retagged to its topic, runs under remaining budget |
| **F8** one topic's ceiling triggers the conversation-level round (P2) | true, wrong layer | true; `silent` | **FIX** — ceiling closes that topic only |
| **F9** step 2 shadows tags only, step 7 grants four decision types (P1) | true; the §8 claim "accuracy known before authority" is false as written | true; wrong rationale is the consequence to price | **FIX** — step 2 shadows every message-only decision, step 5 shadows `next`/`done`, step 7 switches on one type at a time |

Notes:
- Adoption: 9 of 9. The skill says a rate near 100% usually means triage is not
  running; recorded as a flag, not excused. The counter-reading: a proposal's first
  external read, where every finding is a gap in a model that has no code yet and no
  fix touches a function. The next round is the test — if it also adopts everything,
  the triage is rubber-stamping.
- Severity re-ranked by both: F1 > F9 > F7 top three (agree); rater 1 F5 fourth, rater
  2 F8 fourth (F5 sixth). Both demote F2 from P1 (a table row out of sync with its
  section) and rater 2 promotes F7/F8 above it. Differences are in ordering, not
  verdict.
- Only disagreement: F1's gate — rater 1 sent it to the owner, rater 2 gated it
  `cheap` as a scope statement. Rater 1 conceded on the method's own rule ("judge
  against the cheapest correct fix, not the one the finding proposes"): the
  scope statement is correct for a status-proposal doc, and the owner still decides
  whether to design cross-gateway moderation (§7). Converged, not escalated.
- Pattern (rater 2): three roots, not nine findings — (a) topic vs conversation
  conflated (F2, F3, F8); (b) "the moderator" assumed singular, co-located and
  pre-validated (F1, F9); (c) rule rows whose wording drops a constraint the prose
  asserts — which sender, which topic, one plan, mentioned bots reply (F4, F5, F6,
  F7). The fixes were applied as that sweep: every rule row re-read for sender,
  topic and scope. §3.1's "a conversation that has exhausted its budget" was
  caught by the sweep and was not a finding.

**Status: open.** Settles with the confirming round.

## 2026-10-12 — PR #198 round 2 (on `18b8761`)

**Chain detector:** 18 findings over 2 rounds; round 2 fires once — 5 of 9 on the
round-1 fixes, **streak 1**. Once is noise by the rule; a second consecutive fire
is the stop signal, so round 3 decides whether this PR patches or re-reads the
increment.
**Control finding:** #181 r9 F1 again as G10, to a fresh rater told not to open
this log. Rater 2 DROPped it as `cannot-occur` with the code chain
(`config_base_dir()` → `RUNTIME_DIR`, ADR 0003, PR #186) — the settled answer
reached from the code, uncontaminated. **First corroborated round.**
**Cost:** ~45 min, 1 round, 2 raters.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **G1** human post in the gap resets per-topic budgets; repeated interjections bypass them (P1) | true as a leftover rule; fix differs from Codex's — keep the reset (human presence is the designed reset, as today and PR #195) but scope it to the topics the post touches; extends not returned | partly true: the "bypass" is by the human, who already holds `extend_limit`; the doc must *choose* a rule; `silent` | **FIX** — scoped reset, stated as deliberate (§3.3) |
| **G2** one closed topic suppresses a whole multi-topic reply (P1) | true; suffix only when every selected topic is closed | true on wording; consequence overstated (context stays complete, one turn's contribution lost) | **FIX** — mixed case runs with a note; moderator charges the closed part (§3.5) |
| **G3** `decided`/`discussed` never shadowed (P2) | true | true, low stakes (a misclassified topic still gets the conversation-end round) | **FIX** — step 2 shadow list |
| **G4** step 7 activates cross-gateway notices the scope forbids (P2) | true, own round-1 inconsistency | true, loud (implementer finds no input) | **FIX** — clause removed (§3.3, §8) |
| **G5** `close` authoritative in 7, closing round in 8 (P1) | true; proposed moving the per-topic round into step 7 | half: "strands" is wrong — queues are delivered at next turn/flush and step 6 ships first, so strictly better than today's drop; the measurement confound is real; one line, no reorder | **FIX** as rater 2's one line — rater 1 conceded on §3.5's queue delivery and step order: the cheaper fix is correct; step 7's measurement marked provisional until 8 |
| **G6** TTL is lazy, no timer; "ends by TTL alone" has no event (P1) | true — verified `_gc` has one caller, `check_and_increment` | true; `silent`; doc must choose timer (new concept → scored) or lazy-with-stated-cost | **FIX** — lazy, stated: closed at next inbound, round runs before the new conversation; a silent room has nothing waiting (§3.4) |
| **G7** fallback starts with empty/stale per-sender counters (P1) | true; consequence bounded to one budget window, degraded+logged — state it and accept (§14.3) | true; scored: `hours_per_hit` 0.3 · `hits_per_year` 0.6–9 (outages 2–10 × 3 operators × P(chain active) 0.1–0.3) · discount 1.0 · one sentence 0.1 h vs dual-write 4 h + 1 h/yr tax → sentence FIX, dual-write FILE at best | **FIX** — one sentence: the per-sender store keeps counting under moderation, fallback starts from live counts (§3.6). Codex's P1 demoted by both |
| **G8** `@all` replies each start a competing sequential plan (P1) | true, the round's biggest; same root as r1 F5 | true; `silent`; "followed literally it reproduces today's fan-out, squared" | **FIX** — structural: "attribution before dispatch" paragraph (§3.3) |
| **G9** queue of a bot the relevance filter excluded survives the boundary (P2) | true; deliver, never clear | true; "clear" contradicts "nothing is dropped" — only deliver is correct | **FIX** — every queue drained at every boundary (§3.5) |

Notes:
- Adoption: 9 of 9 again (18 of 18 over two rounds). Flagged in round 1 as the
  rubber-stamp signal; this round's test was whether the raters would *disagree
  with Codex's fixes*, and they did on G1 (direction), G5 (reorder not needed),
  G6 (no timer), G7 (no dual-write) and G9 ("clear" rejected). The verdicts are
  FIX; the fixes are not Codex's.
- Severity re-ranked by both: G8 first; rater 1 G5 > G6, rater 2 G6 > G2 > G1 > G5.
  Codex's six P1s hold for three (G8, G6, G2/G5 by one rater each); G7 demoted
  by both to a one-line statement.
- One concession, with evidence: rater 1 dropped the step-7 reorder for G5 on
  §3.5's queue-delivery rule and the step order (6 before 7) — the "strands"
  half of the finding is false, which leaves only the measurement confound.
- Pattern (rater 2, taken): two roots. (1) *Attribution before dispatch was never
  stated* — G1, G2, G8, G9 and r1 F5 are one paragraph: every post is attributed
  to topics, then to a running plan, before any dispatch; replies made under a
  plan join it; a plan ends when its selected bots have replied, timed out or
  self-terminated. Written into §3.3; the late-reply and human-gap paragraphs are
  now cases of it. (2) *§8 did not enumerate the §3.1 decision table* — G3, G4,
  G5 are one missing cross-check; a table mapping each decision to its shadow
  step, activation step and measurement is now in §8 (the doc equivalent of the
  "test that enumerates the surface").
- Two rounds, two patterns, both structural: a sign the proposal was written
  rule-by-rule and the invariants were implicit. If round 3 fires the chain, the
  response is to re-read §2's principles for a third implicit invariant, not to
  patch.

**Status: open.** Settles with round 3.

## 2026-10-12 — PR #198 round 3 (on `65d98b4`) — chain stop

**Chain detector:** 28 findings over 3 rounds; round 3 fires again — 6 of 10 on
the round-2 fixes, **streak 2 — STOP**. Per the rule, no patch round: the
increment was re-read and the owner chose how to end.
**Control finding:** none convened — no second rater this round, because the
stop rule replaces triage-and-fix with a decision about the chain itself.
**Cost:** ~25 min, 1 round, 1 rater + owner decision.

**What the chain is.** Not out-of-scope work: every finding is inside the doc.
It is a proposal being specified by review — round 2 added a plan lifecycle in
prose, round 3 enumerated the states the prose missed (`failed`, a busy bot in
parallel mode, a mid-hop human post, a reopened decided topic). A round 4 would
enumerate what the state table misses. The increment is "a design proposal";
the specification of each mechanism belongs to its §8 step's PR, where it can
be tested instead of argued.

**Owner's decision (option A):** one structural close-out, no further Codex
round, then merge. Three wrong statements fixed (wrong rationale outranks a
small gap — CLAUDE.md); the plan lifecycle written once as a participant-state
table so the states are enumerated rather than implied; implementation-level
findings recorded in §7 with a default answer and the step that owns them.

| | author | outcome |
|---|---|---|
| **H8** "suppressed by the same path as self-termination" — the token path posts surrounding text (P1) | true, verified `agent_turn_runner.py:151-160`; a wrong rationale in the doc | **FIX** — forced-suppression result, token path explicitly not reused (§3.5) |
| **H9** "nothing before step 7 changes what a bot sees" (P2) | true, false claim; steps 3–6 all change it | **FIX** — invariant restated: moderator non-authoritative before 7; each step measured against the previous (§8) |
| **H10** shadow `next` metric is counterfactual (P1) | true; the rule-selected bot speaks first and changes the state | **FIX** — hand labels on replayed snapshots (§8 step 5, coverage table) |
| **H5** failed turn is not a terminal plan state (P1) | true | **FIX** — `failed` row in the participant-state table (§3.3) |
| **H6** a busy bot in a parallel plan later sees peers' replies (P2) | true, contradicts §3.3's own independence rationale | **FIX** — parallel participants receive the triggering post only; busy bot gets the same snapshot (§3.3) |
| **H1** mid-hop human post vs the one-plan invariant (P1) | true; the gap rule was the only interrupt | **FIX** — `interrupted` row: a human post on the topic ends the plan at any point (§3.3) |
| **H7** decided topic corrected in its closing round has no reopen (P1) | true | **FIX** — substantive closing-round reply reopens the topic, sequential dispatch (§3.4) |
| **H3** only a human post opens the next conversation; agent-only rooms have none (P2) | true, one phrase | **FIX** — any post attributed to no running plan (§3.4) |
| **H2** context-only delivery primitive does not exist (P2) | true (`send`/`stream` both run a turn; handoff uses `send`); step 3's concern | **§7 open question** with default: costed as a suppressed turn until a second consumer appears |
| **H4** moderator recovery after fallback undefined (P1) | true; the owner added that an in-memory queue has the same problem on restart — one question, two triggers | **§7 open question** with default: reset, not reconciliation; queues rebuilt from room history (the platform is durable); step 7 decides against an observed outage |

Notes:
- Adoption: 8 FIX + 2 recorded with defaults; nothing dropped. Codex's six P1s:
  H8 holds; H1/H5/H7 are table rows; H4/H10 are design-level but belong to a
  step's PR. H6 (P2) ranked above several P1s — it contradicts the doc's own
  stated reason for parallel mode.
- Third structural root, as predicted in round 2: *the plan's states were
  implied, not enumerated.* The table in §3.3 now says "where a rule below and
  the table disagree, the table is wrong and should be fixed" — one place to
  look, which is what an enumerating test gives code.
- The chain's lesson for the next design doc: write the state tables first and
  the prose as their cases; prose rules invite a reviewer to find the state
  the prose skipped, one per round, indefinitely.

**Status: settled for this PR** — closed by the stop rule and an owner decision,
not by a clean round. The ten findings are decided; none is open.

## 2026-11-10 — PR #202 round 0 (internal review before Codex, `tools/roomsim/`)

**Increment:** §8.2 row 0 — the simulator, all of section 5, outside the runtime.
**Rater:** one internal reviewer (three lenses: spec-vs-code, event ordering,
rule-count correctness), probes run against scripted sandboxes. No blind second
rater and no control for this round: every finding passed the `cheap` gate or was
a spec reading, so nothing was scored. **Cost:** ~10 min reviewer, ~40 min fixes.

Question 2 passes for every finding: the increment owns all of section 5.

| | finding | gate | outcome |
|---|---|---|---|
| F1 | a gateway counted its own post after the other gateways received it; the same scenario traced differently by hosting (5.3, 8.3 "equal lists converge") | cheap | **FIX** — `Simulation.append`/`deliver` split; own post counted between |
| F2 | `restart` mid-turn left the dead turn's end event on the heap: two concurrent turns, a duplicate reply | cheap (one counter) | **FIX** — per-bot generation; a stale end is dropped, its job goes back to pending |
| F3 | parseable output with wrong types crashed the run and left `running=True` | cheap | **FIX** — type checks raise `ParseError`; `limit` shape validated |
| F4 | a thread archived during a turn re-entered the unfinished set | cheap | **FIX** |
| F5 | `counted_holds.clear()` on any start re-counted unrelated holds | cheap | **FIX** — only the started key is dropped |
| F6 | the sweep tried only the oldest addressing post; a held bot mention hid a newer human mention (4.1 row 1) | cheap | **FIX** + §5.7 item 1 reworded ("each checked against the turn table") |
| F7 | the orchestrator was held by cooldown (4.1) | cheap | **FIX** |
| F8 | an owner's control-only post neither cleared the window nor released held bots (5.3) | cheap | **FIX** — clear before the control-only return |
| F9 | job cap: no notice, off by one, `0` dropped the job (3.1, 5.11) | cheap | **FIX** |
| F10 | a degraded bot was still offered to the classifier (5.10) | cheap | **FIX** — and mode-independent (F16c) |
| F11 | pending jobs came before addressing posts in the sweep order; 5.7 lists no jobs | cheap | **FIX** — after unfinished, before catch-up; §5.7 item 3 added |
| F12 | a DM post without `@` started no turn (section 3) | cheap | **FIX** |
| F13 | the classifier request was built at result time, so later posts leaked in | cheap | **FIX** — built at call time |
| F14 | a new human mention held by `one_turn` behind a turn that then failed waits for the backoff | — | **DROP** — the §5.10 text written in this PR says "like any other sweep-started turn"; the backoff is about the backend, not the trigger. Reversal: a lab transcript where a human's follow-up mention sits unanswered through a backoff and the human notices |
| F15 | enforcement off counted a turn started where enforcement on would have held it as `violated`, whose counterpart is `held`, not `rejected` | cheap (two columns) | **FIX** — `held_complied` / `held_violated`; report and §8.1 reworded |
| F16 | denominators differed by mode: catch-up/unfinished filters, classifier offers, items dropped between attempts, missing `to` structural | cheap | **FIX** — classifier offers and candidate filters mode-independent (a closed thread is not a candidate in either mode, per 5.7's wording; my first fix re-offered it every sweep and looped); items counted across attempts; missing `to` is a `recipients` rule |

Notes:
- F1 and F6 are the ones with an operator-visible consequence in a live room (a
  hold that depends on hosting; a human mention that waits for a second mention).
  Both were invisible to the 106 tests because every test hosted all bots on one
  gateway and addressed a bot once at a time.
- Tests that passed by sharing a bug (reviewer's list): `test_enforce.py` parity
  assertions, `test_scheduler_classifier.py` cap count, `test_envelope.py` `to`
  as structural — all rewritten with the fix. 128 tests after the round.
- Spec amendments carried in the PR: §5.7 candidate order, §8.1 count wording.
  Two points left to the owner: invalid-envelope retries without backoff; a live
  measure-only run.

## 2026-11-11 — PR #202 round 1 (Codex on `3f0ae08`, `tools/roomsim/`)

**Increment:** §8.2 row 0 — the simulator, all of section 5, outside the runtime,
plus the enforcement switch and per-mechanism counts.
**Chain-check:** 11 findings, one round, no chain (run from the working tree).
**Rater:** Codex (11 inline, 4×P1 7×P2; the security review did not run — the
paying account had no credits). Blind second rater: a fresh agent given the
findings, the method, the constants and the owner's scoping sentence, not the
verdicts. **Control:** the log's one settled row (#181 r9 F1, `settled:
implemented`) was slipped in as a twelfth finding. It did not work as a control:
presented against HEAD, a settled-*implemented* entry can only be found "already
fixed" (the rater traced it to #182 and DROPped it as `cannot-occur`, correctly
for HEAD). A control must be a DROP/FILE whose code still exists. **Agreement in
this round is therefore uncorroborated.** **Cost:** ~25 min author, ~21 min rater
(169k tokens), ~45 min fixes and tests.

Owner's input this round, verbatim: "let's not too tie up on too many corner case
with the simulator.. I mainly want to use simulator to get a sense of how well the
mechanism works, but not intend to use it to exercise all corner cases." Applied
as a `hits_per_year` factor (what a scenario file can express — the loader has
only `human`/`archive`/`post`, so restart and replay are API/test-only) and to
question 2, never as a coined discount. Both raters: it changes no verdict here,
because every finding but one cleared `cheap`; it would have bitten on F3 and F6
had `cheap` been refused.

Question 2 passes for all eleven (§5.1, 3.1, 5.9, 8.1, 5.3, 5.11, 5.4).

| | finding | gate | outcome |
|---|---|---|---|
| F1 | `"schedule": null` raised `TypeError`, not `ParseError`; the whole run aborted (live model only; loud) | cheap | **FIX** — `null` reads as empty, any other non-list is a schema error |
| F2 | a gateway hosting only the author returned before `_count_for_rate` on replay: the window after `restart` depended on hosting (5.3; 8.3 "equal lists converge") | cheap (2-line move) | **FIX** — count, then return on no copies; test parametrised on `hosted` |
| F3 | `classifier_timeout_seconds` was read nowhere; the test named "timeout" exercised errors only (8.3 "timeout … observable") | cheap at the edge (~6 lines), silent | **FIX** — a call slower than the timeout fails at the timeout and is never answered; test gap closed |
| F4 | `_drop_stale` put the job back to pending and requested no sweep; with `sweep_interval_seconds=0` it never re-fired | cheap | **FIX** — one `_request_sweep`; test pins the second start at the drop time |
| F5 | `Decision.reasons` reached no trace or report (5.1 "every fallback and reason is logged") | cheap | **FIX** — one field on `classifier_decision` |
| F6 | `turn_hash` omitted `TurnInput.error`, so a retry turn replayed against a changed prompt | cheap | **FIX** — included only when set, so clean-turn hashes are unchanged. The rater flags this as the one the owner's sentence most plausibly waives; taken because it is the advertised contract and one line |
| F7 | a replay that used a prefix of the recording exited 0 (silent) | cheap, silent | **FIX** — `[ERROR] replay used N of M recorded turns`, exit 1 |
| F8 | a control first seen in replay got `last_activity=now`, so a mute older than the TTL stayed for a fresh TTL (5.4) | **not cheap** — the one-line fix (`setdefault(last_activity=post.time)`) is wrong: with a second replayed post inside the TTL of the mute, it expires a mute that §5.4 keeps (rater's trace, re-traced by the author; not run). Silent | **MAKE IT LOUD** — `age` field on `control_applied` (0 live; > TTL only in a replay). Behaviour not fixed. Rater's score: 0.01–0.1 h/y against 0.2 h/y tax, net ≤ 0 |
| F9 | `classifier_posts=0` showed nothing instead of everything (3.1 "0 disables") | cheap | **FIX** — `or None` |
| F10 | `"done": "false"` was truthy; an item with no other action was accepted as done and the cursor saved (silent discard) | cheap, silent | **FIX** — `done` must be a JSON boolean; wrong-types test extended |
| F11 | `to` listing the sender among others passed; §5.9 said both "never the sender" (contract) and "addresses only the sender" (invalid list) | cheap; layer = §5.9 | **FIX** — code follows the contract sentence; the invalid-list line now says "names the sender among its recipients". Owner's call to reverse |

**Where the two raters differed:** F8 only. Author had it `cheap` with exactly the
one-line fix the rater showed wrong (checkable: two replayed posts, one thread,
`_thread()` compares `now()` with the mute's time on the second post). Converged
by evidence, not deference. Pattern for the file: *a one-line fix in replay
bookkeeping that changes a §5.4 outcome is not `cheap` — the gate's "correct"
clause, not its line count, is what fails.*

**Where this differs from Codex's badges:** the three silent ones (F3, F10, F7)
rank highest here; two carried P2. F1, F2 and F4 carried P1 and are bounded: F1
fails loudly and needs a live model; F2 and F4 are unreachable from scenario YAML,
and F4 is a ≤30 s delay under every shipped scenario.

**Adoption:** 10 FIX + 1 MAKE IT LOUD of 11 — near 100 %, which the skill says to
watch. Every one cleared `cheap` on the touched function (highest C901 in
`tools/roomsim` after the round is 8, `_item` and `render_hint`) and lands as one assertion in an existing test file; the
pattern response for envelope types (round 0 F3, this round F1/F10) is the
extended wrong-types parametrize rather than two more point fixes. 137 tests after
the round.

Also in the round, not a finding: live bot sessions now start with `--agent claude`
so the operator's configured persona does not reach the first turn (owner's ask).
It does not cover later turns: the adapter resumes with `--system-prompt-snapshot
off` and without `--agent`, so the system prompt is rebuilt and the `agent` setting applies again — the brainstorm live run's session log shows the full
persona in the resumed turn's system prompt. A self-report probe had said otherwise;
the request shape in the log is the evidence, not the model's answer.

---

## 2026-11-13 — PR #202 round 2 (Codex on `7aa069e`, `tools/roomsim/`)

Twelve inline findings, posted 2026-11-11 09:25, thirteen minutes after the
`@codex review` request. **They sat unread for two days**: the author checked the
issue's reactions and not `pulls/202/reviews`. Triaged at HEAD `c3bd791`, five
commits later (the `limit` control moved to owners in between).

**chain-check** (working tree): 23 findings over 2 rounds, no chain; round 2 has
2 findings on `gateway.py` and 1 on `__main__.py` landing on the previous round's
fixes, streak 1 each — noise, not the signal.

**Depth rule for this round, set by the owner before the verdicts:** the simulator
exists to give a rough understanding of the mechanisms; a count off by one in a
rare case is acceptable; do not re-request a Codex pass unless a major gap appears;
spend the effort on running the scenarios. The cut line applied: **fix what is
true and a few lines in a function well under the complexity limit, file or drop
the rest in this entry.** F7 (two lines) is of the same off-by-one class as F5
(seven lines plus a restart-only trigger); the line count, not the class, put them
on different sides.

**Blind second rater** (general-purpose agent; findings, method, constants, HEAD;
no verdicts of the author's; it did not have the owner's depth rule). **No control
finding was available; agreement in this round is not evidence of independence.**
All twelve re-verified by the rater at HEAD with run reproductions.

| | finding | true at HEAD | author | rater | why they differ |
|---|---|---|---|---|---|
| F1 | replay classifier history included posts newer than the trigger | yes (Python API only) | **FIX** (1 line) | FIX | — |
| F2 | an in-flight classifier result survives `restart` and starts a picked turn on a post the replay excluded | yes; silent; Python API only | **FILE** | FILE | — |
| F3 | `"in_seconds": NaN` passed and stalled the whole run at the heap root; the report looked normal | yes; silent; live model or recording | **FIX** (1 line, finiteness) | FIX | — |
| F4 | `--show-rules` never reached `report` | yes; every live run this week | **FIX** (3 lines) | FIX | — |
| F5 | a stale turn dropped by `restart` kept its token/cost out of the totals | yes; silent; Python API only | **FILE** | FIX (cheap) | owner's depth rule |
| F6 | `done` with `to` recorded nowhere (8.3 names it) | yes; silent | **FIX** (2 lines + trace kind `done`) | FIX | — |
| F7 | a first attempt's rule rejections uncounted when the retry fails at the backend | yes; silent | **FIX** (2 lines, `attempt > 1` guard — the bare call miscounts first-attempt failures) | FIX | — |
| F8 | a picked turn held by mute leaves the cached selection usable after unmute (§5.1: mute recovery does not reuse the decision) | yes; silent; needs a `classifier:` block no scenario has | **FILE** | FIX (~5 lines) | owner's depth rule; revisit with the model-backed classifier |
| F9 | an owner may designate a guest as orchestrator (the `limit` half closed by `c3bd791`; the `mute` half stands) | partly | **DROP** | FIX (low value) | owner's depth rule; the state is owner-created and traced |
| F10 | `classifier_fallback: everybody` accepted; the trace printed the typo while behaving as addressed-only | yes; silent | **FIX** (3 lines in `Params.__post_init__`, the layer that owns the contract) | FIX at `Params` | — |
| F11 | delayed turns start in archived threads | true as mechanics, not as a defect: §5.9 prescribes the refusal, it terminates, `refused` is loud | **DROP** | DROP | — |
| F12 | `BackendBot` kept a session whose introductory send failed; every later prompt lacked identity, roster and contract | yes; silent; live only | **FIX** (3 lines) | FIX | — |

Exit obligations:

- **FILE F2** — reversal: a restart test where a `classifier_decision` for post N
  precedes the replay's own `classifier_call` for N. **FILE F5** — reversal: a restart
  experiment whose totals are compared across runs. **FILE F8** — reversal: a
  scenario with a `classifier:` block and a mute inside a hold. All three revisit
  **2027-02-13** or when the model-backed classifier lands, whichever first. No
  issues opened (owner's call); the threads stay open.
- **DROP F9** — reason: the designation is an owner's visible post and every
  control it enables is traced with the author; reversal: a guest orchestrator in
  a live run that nobody noticed from the trace; revisit 2027-02-13.
- **DROP F11** — reason: the observed behaviour is §5.9's; reversal: an archived
  thread producing more than one `refused` per bot, or a job in one firing twice;
  revisit 2027-02-13.

**Badges vs consequence:** the three P1s are a replay-only input difference (F1),
a restart-only stale callback (F2) and a model emitting `NaN` (F3). Of the twelve,
F12 (silent corruption of every later live prompt) and F4 (a flag that did nothing
in six live runs) had the only consequences anyone met this week; both carried P2.

**Adoption:** 7 FIX, 3 FILE, 2 DROP. Highest C901 after the round: 8. 146 tests.
No `@codex review` re-requested: commits after `7aa069e` are unreviewed by Codex,
by the owner's decision, so the project's 👍 bar is not met on this PR as it stands.

**Cost:** one rater run (~18 minutes, ~220k tokens), ~40 minutes of author time
including the fixes.

## 2026-11-14 — PR #203 round 1 (Codex on `bfaa140`, `tools/roomsim/`)

Nine inline findings, all on the flagged catch-up experiment and the `--param`
override that commit `bfaa140` added. The security review Codex triggers beside
the code review failed on the paying account's credits and was not retried.

**chain-check** (working tree): 9 findings over 1 round, no chain.

**Depth rule for this round, set by the owner before the review:** one Codex
round, focused on major issues, not corner cases; a second round only for
something the team feels it must do. Applied as: fix what passes `cheap`, reply
on every thread, re-request nothing.

**Blind second rater** (general-purpose agent; findings, method, constants, HEAD;
no verdicts of the author's; it did not have the owner's depth rule). **The
control was compromised:** the ledger's one settled row (`#181 r9 F1`) was sent
as a tenth finding, but the rater had read this log during orientation and
reported that it knew the answer, so its verdict on the control is not evidence.
**No valid control; agreement in this round is not evidence of independence.**

| | finding | true at HEAD | author | rater | why they differ |
|---|---|---|---|---|---|
| F1 | a busy or backed-off head bot was removed from the serial queue for good (`one_turn`/`backoff` hold) | yes; the turn's own end already requests a sweep | **FIX** (defer to the back of the queue; skip only muted or degraded) | FIX | — |
| F2 | a failed catch-up turn completed the flag and was summed as "done" | yes; coupled to F1 | **FIX** (complete only on a normal outcome) | FIX | — |
| F3 | `restart` kept the flags whose decisions it dropped; an in-flight flagged turn stalled the serial queue | yes; silent; Python API only (no restart event in scenarios) | **FIX** (1 line: drop flags with decisions; replay rebuilds them) | FIX | — |
| F4 | a late `revises` decision for an earlier post cleared later posts' flags (`on-revision`) | yes; expressible scripted via injected latency | **FIX** (clear only lower seqs) | FIX | — |
| F5 | `bool("false")` opened a flag | yes; silent | **FIX** (booleans or `ParseError`, the `_names` shape) | FIX | — |
| F6 | the summary counted `flag_cleared` events, not flags | yes | **FIX** (1 line) | FIX | — |
| F7 | a boolean `--param` typo ran as `false` | yes; silent | **FIX** (true/false or `ValueError`) | FIX | — |
| F8 | the code the live `v` reads was outside the tree | yes; the live result was not reproducible from the PR | **FIX** (fixture shipped beside the scenario, `--workdir` named in its header) | FIX (least sure; on the 10-line bound) | — |
| F9 | `score` order with no scores silently ran in roster order | yes; silent | FIX at the CLI (`claude` + `score`) → **conceded to the rater's layer**: `_apply_flag` raises when the decision carries no scores, which also covers scripted decisions without `scores:` | MAKE IT LOUD at `_apply_flag` | the author had the reviewer's shape; the rater named the owning layer and the scripted case the CLI check misses (checkable: `Decision.scores` defaults empty) |

Exit obligations: none open — nine FIX, no FILE, no DROP. The rater's pattern note
stands as the next-round rule: F1, F2, F3 are one class (the queue's bookkeeping
confusing *temporarily unable* with *won't ever*); a further finding on any of
the three is the signal to replace the patches with one test enumerating hold
cause × turn outcome × queue effect.

One defect the fixes introduced and the suite caught before push: a deferred head
that re-woke the next head at the same instant looped without end when every
queued bot was held; the deferral now leaves the start to the next periodic
sweep or turn end.

**Badges vs consequence:** the two P1s (F1, F4) are a real loss and a bias in a
rejected variant; of the P2s, F2 (a backend timeout reported as a `done` look)
and F8 (an irreproducible live result) carried the consequences that mattered to
the experiment's conclusion.

**Adoption:** 9 FIX of 9, which the skill flags as "triage not running"; here the
batch was fresh code with every touched function under C901 10 and no fix over
ten lines. 181 tests. No `@codex review` re-requested (owner's rule).

**Cost:** one rater run (~13 minutes, ~155k tokens), ~50 minutes of author time
including the fixes and the reruns that confirmed the experiment's numbers did
not move.

## 2026-11-14 — PR #204 round 1 (Codex on `fc70661`, `docs/design/multi-agent-enhancement.md`)

Six inline findings on the flagged catch-up rewrite of the design document.
Documentation only; the simulator merged in #203 is the reference for what the
prose meant. The security review Codex triggers beside the code review failed
again on the paying account's credits.

**chain-check** (working tree): 6 findings over 1 round, no chain.

**Depth rule, set by the owner before the review:** one round, to make sure
nothing was overlooked. The `cheap` gate for a design document was read as:
≤10 lines, no new named concept, mechanism or section.

**Blind second rater** (general-purpose agent; findings, method, constants,
the file at HEAD and the simulator as reference; no verdicts of the author's,
and told not to read this log). **Control:** `#181 r9 F1` (settled: FILE held)
sent as a seventh finding against its own commit. The control was
distinguishable in kind (code among prose) and the rater disclosed that one
`git grep` over `docs/` printed one row of this log — a *sibling* finding on
#181, not the control's row. The rater's verdict on the control was FIX, the
direction the settled row calls right (a rater who drops it is wrong), so the
control passed, weakly: it does not test the bias that matters for a batch of
prose findings. The author's verdicts were written to a scratch file before the
rater reported.

| | finding | true at HEAD | author | rater | why they differ |
|---|---|---|---|---|---|
| F1 | an unflagged post in an idle room stays behind a bot's cursor; the invariant "every bot eventually sees everything" over-promises | yes as a description; the consequence is the increment's deliberate trade (the live `all` run: same corrections, 10 posts for 3) | **FIX** the wording (delivery on the next turn, none started only to deliver); the safety net is a decision for the owner | FIX the wording; decline the mechanism on scope, record it in §9 with a reversal observation | — (the rater's §9 entry adopted; the owner still decides) |
| F2 | the document gives the flag and its queue no lifetime; an implementer tying them to the decision TTL silently loses looks | yes | **FIX** (a bullet in §5.5, a restart paragraph in §5.7) | FIX | — |
| F3 | "the next at the previous turn's end" and "keeps its place" are ambiguous; one reading stalls the queue on a failing bot | yes; the same class as #203 F1/F2, now in prose | **FIX** (normal end; a held or failed head moves to the back, as the simulator does) | FIX, same shape; not the finding's (advance only on completion would stall the queue) | — |
| F4 | `catch_up_age_seconds = 0` reads as "disabled" under the preamble, "at once" under the algorithm | yes; silent either way | **FIX** (one phrase) | FIX | — |
| F5 | the author of a flagged post is queued for it and can never leave the head | yes; the simulator excludes the author | **FIX** (three words, two places) | FIX | — |
| F6 | the flag makes the classifier judge content, against goal 3, the no-model invariant and the "does not decide who is correct" non-goal, and the document does not reconcile them | yes as a rationale defect; goal 3 and the non-goal still hold (a flag is a look, not a ruling) | **FIX** wording in §2; no redesign, the owner wants the classifier to grade correctness | FIX wording in §2; the non-semantic trigger is the increment inverted, DROP | — |

Verdicts agree on all six. Control passed, with the caveats above.

Exit obligations: six FIX. One open decision recorded in §9 (whether an
unselected, unflagged post needs a safety net; §8.1's unselected-to-later-reply
rate decides).

**Badges vs consequence:** of Codex's P1s, F5 is the one a literal
implementation turns into a stalled queue; F1 is a promise the document no
longer kept. F3 carried the consequence among the P2s (a persistently failing
bot blocking every later look).

**Adoption:** 6 FIX of 6; every fix a wording change under ten lines.

**Cost:** one rater run (~14 minutes, ~146k tokens), ~35 minutes of author time
including the fixes.

**Postscript (2026-11-15):** the owner then replaced the flagged queue with a
periodic catch-up (a bot idle for `catch_up_idle_seconds` gets one look at what
it missed) and a neutral speak question, and set the post-level grade aside
(section 10). Two findings this round declined on scope, F1's non-speaking
delivery and F6's non-semantic trigger, describe what the design now does.
Both raters were right that neither belonged to the flagged increment; neither
asked whether the flagged increment itself was the simplest design that met
the goal, and the owner's "are we making this too complex" was the question
that changed the answer. Pattern for the ledger: a scope gate protects an
increment, it does not vouch for it.

## 2026-11-15 — PR #206 round 1 (Codex on `5ba8c90`, `tools/roomsim/`)

Three inline findings on the periodic catch-up that replaced the flagged queue.
The security review Codex triggers beside the code review failed again on the
paying account's credits.

**chain-check** (working tree): 3 findings over 1 round, no chain.

**Blind second rater** (general-purpose agent; findings, method, constants,
the checkout at HEAD with the simulator runnable; no verdicts of the author's,
told not to read this log). **Control:** `#181 r9 F1` (settled: FILE held) sent
as a fourth finding against its own commit. **Compromised again, the same way as
on #204:** the rater's `git grep` over `docs/` at the old commit printed this
log's rows for #181, including the control's own row, before it scored. It
disclosed this and its control verdict (FIX, not DROP) does not count. The
author's verdicts were written to a scratch file before the rater reported.
Rule for the next round: tell the rater to exclude `docs/agents/` from every
search, not only from reading.

| | finding | true at HEAD | author | rater | why they differ |
|---|---|---|---|---|---|
| F1 | `brainstorm_kibitz` and `status_meeting` omit `catch_up_idle_seconds`; the 1800 default exceeds their horizons, so d's `source: catch-up` rules never run and the reports look normal | yes, both run; silent | **FIX** (idle override in both scenarios) | FIX; idle 15 (= the sweep interval; 10 lets c answer before d asks), and `status_meeting`'s header still described the deleted count trigger | — (the rater's value and the header taken) |
| F2 | `debugging`: idle 120 lands on the t=120 human post; the catch-up turn delivers it before its decision and c's pick is dropped, so c never answers | yes, ran it; silent | **FIX** (idle 90 in the scenario, comment on the event boundary) | FIX; **wider:** `support_triage` regresses the same way (6 posts → 4), and 90 does not cover it: 150 is the only value post-for-post identical to main on both | the rater ran the sweep of all eleven scenarios against main; the author ran only the one Codex named (checkable: the four scenarios now match main post for post) |
| F3 | the design document on main still names `catch_up_post_threshold` and count/age eligibility | yes on main; loud (`unknown param`) | not this PR's: the document change is #204 | FILE, already tracked as #204; merge #204 first | — |

Verdicts agree on all three, with F2's extent corrected by the rater.
**No valid control; agreement in this round is not evidence of independence.**

Exit obligations: F1 and F2 FIX (`catch_up_idle_seconds` 15 / 15 / 150 / 150 in
the four scenarios, with the reason in each file; all four match main's run post
for post); F3 tracked in #204.

Two items the rater raised beyond the findings, for the owner, not fixed here:

- **Design:** a sweep that starts a catch-up turn while a post's classification
  is pending delivers the post and the pick for it is then dropped (#204 §5.7:
  a choice is usable only while its post is in the backlog). The window is the
  classifier's latency against the sweep interval, so in production this is a
  per-post probability, not a scenario artefact; the consequence is the hint
  (catch-up instead of picked), not silence. Candidate rule: a thread whose
  newest post is still being classified is not due for catch-up. Filed against
  #204 for the owner; decay 2027-11-15; reversal observation: a live trace with
  `turn_start source=catch-up` at a sweep tick followed by `turn_held
  gate=one_turn source=picked` for the same bot and post.
- **Pattern (two findings of one kind):** a scenario whose scripted `once:`
  rule never fires reports nothing. A report line counting unconsumed rules
  would have caught all four regressions. New concept; a candidate for the
  simulator's next increment.

**Badges vs consequence:** both P1s are silent measurement losses in scenario
files, not gateway defects; for a simulator whose job is measurement the label
is fair. F3 is a sequencing note.

**Adoption:** 2 FIX of 3, F3 tracked.

**Cost:** one rater run (~15 minutes, ~176k tokens), ~30 minutes of author time.

## 2026-11-15 — PR #206 round 2 (Codex on `e9fcda8`) and PR #204 round 2 (Codex on `bca7523`)

The round the owner asked for so Codex could see the periodic design. One
finding on the simulator, three on the document; two of the four are the same
defect in both layers. **chain-check:** #206 no chain; #204 "3 findings on our
own last fix, streak 1" (once is noise; noted).

**Blind second rater** (one run for both PRs; findings, method, constants, both
checkouts runnable; no verdicts of the author's; told to exclude `docs/agents/`
from every search). **Control:** `#181 r9 F1` sent as a fifth finding. The
rater printed no line of this log, but the control's own commit message
(`818d833`) states how that round disposed of the finding, and the rater read
it while locating the change. **Third compromise in three rounds, a different
leak each time.** This control is burnt: its disposition is in the log, in the
commit message and in the PR thread. The next round needs a control whose
history does not discuss it, or runs without one and says so. The author's
verdicts were written to a scratch file before the rater reported.

| | finding | true at HEAD | author | rater | why they differ |
|---|---|---|---|---|---|
| F1 | (#206) the pending-classification guard is per thread, but the catch-up turn delivers the whole room backlog, so a post being classified in thread A is swallowed by a catch-up for thread B | yes (the rater's probe: decision `add=['a']` after `cursor_saved` past the post, no picked turn); silent | **FIX**, room-level guard | FIX, same shape; P1 → P2 (the post is delivered, as optional; the picked framing is what is lost) | — |
| F2 | (#204) the same, in §5.7 and §8.3 | yes | **FIX** | FIX | — |
| F3 | (#204) the timer resets on restart; deployments more frequent than `catch_up_idle_seconds` defer the catch-up without bound, against §2.2's "bounds the wait" | yes as written; a stated decision (§5.5 keeps the timer in memory) | **FIX**: persist the last normal turn end beside the cursor, saved at the same moment; a new bot starts at the gateway's start; a restart then leaves an overdue bot overdue | **MAKE IT LOUD**: keep it in memory, state the caveat (the bound holds per gateway process); scored the persistence at ≈7 years payback at the midpoint (`hits_per_year` 3–75, `fix_hours` 3–4, tax 0.3–0.5 h/yr) and names the startup herd it reintroduces | escalated to the owner; **owner chose the rater's exit (MAKE IT LOUD, 2026-11-16)**: no persisted field, the caveat in §2.2/§3.1/§5.7/§8.3 (#204 `81c302c`). The rater's `fix_hours` priced a mechanism; the author's reading was one field in a record already persisted at that moment. Neither side's evidence was a file or a test; the owner decided on preference for fewer persisted fields |
| F4 | (#204) a lone failure notice makes a thread due, so one failure wakes every idle peer, against §5.10 and §8.2 | yes (the rater's probe: two catch-up turns delivering only the notice) | **FIX**: notices do not count toward the due check | FIX, same | — |

F1, F2, F4 fixed in #206 (the commit carrying this entry) and #204 `ada3f6f`; the simulator's
own test for the single-thread case was kept and a two-thread case added. F3
was FILE while the escalation was open; the owner settled it the same day
(caveat, no field).

**Badges vs consequence:** F3 is the one with an operator consequence; the
others cost one lost pick framing or one wasted turn per idle peer.

**Adoption:** 3 FIX of 4, one escalated and closed as MAKE IT LOUD by the owner.

**Cost:** one rater run (~18 minutes, ~190k tokens), ~35 minutes of author time.

---

## 2026-11-14 — PR #205 round 1 (design: ACP agent backend for opencode 2.x)

**Chain detector:** 8 findings on `37aadb3`, first round, `--`, no chain.
**Control finding:** #181 r9 F1 (`settled: implemented`) was included as F9, blind by
intent. Rater 2 disclosed that it hit the ledger row while grepping for the finding's
file, so it was **not blind on the control**. Its F9 answer (FILE at the time, not a
DROP) matches the settled outcome, but the control did not test independence this
round. Agreement **uncorroborated**. Tree untouched until rater 2 reported.

Increment: "design doc for a generic ACP agent backend (`type: acp` + per-agent
profile) that supports opencode 2.x, plus the manual probes behind its live evidence.
No gateway code changes." A finding on the doc is true when the specified mechanism
cannot be built from the gateway as it is, or the text cites something that does not
exist; the "fix" is the document.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F1** a user `allow` rule in `OPENCODE_CONFIG_CONTENT` placed after the gateway's `ask` catch-alls wins, bypassing the broker, "directly contradicts R4/ADR-0002" (P1) | ADR claim untrue: ADR-0002 Decision para says `ask` is injected "unless the user set a value", and the design copies the 1.x merge. Operator-authored env is inside the trust boundary. **DROP** | ADR claim untrue (same clause). Then: the "user value" has **no source** — `AgentConfig` has no `env` field, and the 1.x function reads only `sidecar_env` (`adapter.py:327`), hardcoded by `service.py`, so the respect-user branch is unreachable from the gateway; the design's "the agent's `env:`" (P5) names nothing. `silent`. MAKE IT LOUD: name the input, specify a WARNING, FILE the ADR question | **FIX (doc)** — rater 1 conceded on checkable evidence (`gateway/core/config.py` `AgentConfig`, `adapter.py:327`): the design referenced an input that does not exist. Outcome simpler than either verdict: P3 now states there is no operator input, the profile sets the variable over any inherited value and logs the override at INFO, and R4's gap is "no supported way to weaken a rule", as on 1.x. ADR unchanged |
| **F2** the lifecycle sends the history handoff before `ensure_durable_instructions`, so the watcher entry cannot be selected "after `session/new`" and the handoff turn runs on the operator's default mode (P2) | true (`watcher_lifecycle.py` steps 3, 3.6, 4; `create_session` carries no `path_key`). On 2.x the default mode is the operator's own agent, a foreign identity. **FIX**: reorder the lifecycle in this increment | true, same trace. The handoff turn runs without the header on every backend today and its reply is discarded, so that half is pre-existing. `cheap` at the backend layer: selection happens in `ensure_durable_instructions` (session attached, so the `config_option_update` wait applies) and on re-attach; the reorder is a lifecycle change for its own increment | **FIX (doc)** on rater 2's shape, the cheapest correct fix inside the increment. P5 steps 2-4 rewritten; the reorder is listed in §9 as later work. Both raters rank this first: a wrong mechanism in the design outranks the bugs |
| **F3** probe: `clear()` inside `wait_option_update` drops an update that lands before the wait (P2) | `cheap` | partly: real in step 3's fallback branch (an `await` sits between write and wait), `cannot-occur` in steps 4-5 (no yield point). `cheap` | **FIX** — clear moved to the callers |
| **F4** `backend_identity()` is called with `agent_cfg.type` at all three sites, so `acp:<profile>` cannot be produced (P1) | true (`watcher_lifecycle.py:704`, `:1728`, `config_validate.py:586`). `silent` replay into the wrong store on a profile switch. **FIX (doc)** | true, same sites. Replay unreachable until a second profile ships and then loud (`resume` of a foreign id errors). The real consequence: the persisted string must carry the profile from release one, or a later change refuses every stored ACP session. `cheap` (doc) | **FIX (doc)** — `AgentConfig.identity_type`, the three sites named, "persisted with the profile from the first release". Rater 2's consequence adopted as the stated reason. Also fixed from rater 2's aside: §3.1 said `working_directory` "may be empty today"; the loader requires it (`config.py` `load_config`) |
| **F5** on expiry the lifecycle reclaims the prompt file even when `delete_session` is unconfirmed, so "entry removed only after the session is closed" cannot be enforced (P2) | premise true (`:731-751`), consequence unreachable: `_maps.remove_session` runs first and the record is gone, so nothing prompts the orphan. `cheap`: reword the guarantee | premise true, plus the reload-removal path keeps the session on purpose (`session_manager.py:786`). Consequence near zero for the same reason. `cheap` at the backend layer: reclaim sends `session/close` to every session it selected onto the entry, then removes it | **FIX (doc)** on rater 2's shape: the backend closes, it does not rely on the caller's order. Invariant 9 reworded. Codex's conditional-reclaim fix declined: it would leak entries forever |
| **F6** probe: a relative `<config path>` diverges from the child's cwd (P2) | `cheap` | true, not silent (step 1 fails loudly). `cheap` | **FIX** — `os.path.abspath` on both arguments |
| **F7** `session/resume` must carry `mcpServers`, "a conforming strict agent rejects every reattachment" (P1) | untrue: ACP v1 schema `$defs.ResumeSessionRequest.required = ["sessionId", "cwd"]`. **DROP** | untrue, same field, plus opencode `service.ts` defaults an absent list to `[]`. `cannot-occur`. **DROP** | **DROP** — written on the thread, ledger row added. One phrase added to the §3.4 row so the next reader does not re-derive it |
| **F8** probe: `kill()` without `wait()` before respawn (P2) | `cheap` | partly: opencode's private server exits on the pipe's EOF (`standalone.ts:24-31`), so the overlap is bounded; a zombie remains. `cheap` ×3 files | **FIX** — `await proc.wait()` after `kill()` in all three probes |
| **F9** (control) `save()` validates beside the resolved target; a relative `working_directory` through a symlink resolves against the wrong directory | — (known) | not blind (see above). On HEAD untrue: `config_base_dir()` resolves against `$COOP_HOME` (#182, PR #186). Historically FILE, agrees with the ledger | control not independent this round |

Notes:
- Adoption: 7 of 8 (6 doc or probe fixes, 1 DROP). Verdicts agree on 6; F1 and F2
  were disagreements on the fix's shape, both converged on rater 2's evidence
  (F1: the input does not exist; F2: the cheapest correct fix is inside the backend).
  No rates were needed: every surviving finding passed `cheap` against its cheapest
  correct fix.
- Severity re-ranked: Codex's three P1s are one false claim (F7), one false ADR claim
  with a true unnamed-input problem under it (F1), and one true but unreachable-today
  defect whose real consequence is the persisted identity format (F4). Its P2 F2 is the
  top finding of the round.
- Found outside the nine by rater 2: the PR's CI lint was red. `tests/manual/` is
  inside `ruff check gateway/ tests/`, and the three probes carried 46 E701/E702/E401/
  I001 errors plus three C901 (`main` 17 and 16, `pump` 11). Fixed by restructuring
  the probes (dispatch helpers), no `noqa` added. Lesson already in memory: run lint
  before push.
- Cost: 1 round, 2 raters, about 75 minutes of attention.

**Status: open** — fixes pushed, confirming Codex round requested.

---

## 2026-11-14 — PR #205 round 2 (confirming round on `6836a3c`)

**Chain detector:** 14 findings over 2 rounds, round 2 `1 finding(s) on our own last fix, streak 1` for the design doc. Once is noise by the rule. By inspection the six findings land on sections round 1 did not touch (§3.4 create_session row, §3.5 cancel, §3.6 ask shape and invariant 1, §3.8 rung (a), §4 P6), so the round is new ground.
**Control finding:** #181 r9 F1 again, as G7, with rater 2 told not to open this log. Rater 2 traced it to `config_base_dir()` and #186, called it false on HEAD and outside the PR, and flagged it as the likely control. **Passed**: the category prior points at FIX, the trace points at the fix that already landed. First corroborated agreement in this log. Tree untouched until rater 2 reported.

Increment unchanged from round 1.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **G1** `query` and `pattern` are not in `_CLAUDE_PARAM_FIELD`, so a `websearch` ask falls back to the whole input and a `params` rule stops matching (P2) | true, the design's "already match" sentence is wrong (`tool_match.py:45-54`). `cheap`: two table rows, shared with `type: claude` | true, three rows (`websearch`, `grep`, `glob`); the shipped `tool: "WebSearch"` preset has no `params` and is unaffected; owning layer is the core table, which a profile cannot reach. `cheap` | **FIX (doc)** — P6 corrected, three rows named as a core change that also changes `type: claude`, called out as such |
| **G2** rung (a) cannot send `systemPrompt` on `session/new` because the content exists only at `ensure_durable_instructions`; close+re-attach is a "change" that Claude applies at the next compaction (P2) | true, same root as round-1 F2. FIX: close + re-attach with `systemPrompt` at ensure time; the lifecycle reorder stays later work | partly: claude-agent-acp records the prompt on the **first request** (`src/client-system-prompt.ts`), so close+re-attach before any turn is an initial delivery and works; the defect is the history handoff being that first request. `hits_per_year` 0 now (no rung-(a) profile ships), 0–18/yr once one does. FIX (doc): state the constraint | **FIX (doc)** on rater 2's evidence: §3.8 (a) now states the first-request rule and that **a rung-(a) profile is unsupported until the lifecycle delivers the durable instructions before the history handoff (§9)**. Converged on a checkable source |
| **G3** `ToolCallUpdate` fields are optional, so a partial ask patch would normalise to `other` (P2) | schema-true; the fold keeps a per-`toolCallId` record anyway. `cheap`, one sentence | schema-true, `cannot-occur` for the three known adapters without the AIR capability (`permission.ts` `ask`, claude `renderer.ts`, codex `renderPermissionToolCall`), which the backend does not advertise. FIX-cheap anyway: cheaper than writing the assumption down | **FIX (doc)** — §3.6 merges the ask over the fold's record and records the AIR assumption |
| **G4** `new_session_args` on `type: acp` is ignored at DEBUG, a migrating operator loses a model flag silently (P2) | true as to silence. 1.x opencode already ignored `extra_args`, at WARNING (`adapter.py:872`). FIX: WARNING at start | true as to silence, consequence mostly wrong: on 1.x opencode the field held `opencode serve` flags, not a model; the model case is Claude→acp (§5.1). Owning layer: config load. MAKE IT LOUD at load, do not map onto `opencode acp` | **FIX (doc)** on rater 2's shape: config loading warns on a `type: acp` agent with a non-empty `new_session_args`, beside the `tool: external_directory` warning. Not mapped onto argv |
| **G5** a saved `always` from any interactive opencode run in the same project beats the injected `ask`, so R4 is false for this profile (P1) | true, `permission.ts:187-188`, silent, operator-caused. FIX: `XDG_DATA_HOME` isolation, operator logs in under the same variable | true, same lines, plus: credentials live in the same database (`credential.ts`), so the env line alone breaks P2; spec the pair with `auth_hint`. 1.x `always` in memory `[unverified]` by rater 2, confirmed by rater 1 (`v1.18.13 permission/index.ts:145-150`). Reachability: one precondition. **Rank 1** | **FIX (doc)** — owner's decision 2026-11-14: `XDG_DATA_HOME=<RUNTIME_DIR>/opencode/data` on the spawn environment (P1), `auth_hint` carries the variable (P2), P3 states the mechanism and why; reading the opencode database at start or per turn was declined by the owner (detection only, and a dependency on opencode's schema). §8 item 4 becomes a runnable probe. Added on top, as the runtime R4 assertion both raters priced as MAKE IT LOUD: the fold logs ERROR when a gated tool runs without an ask (§3.5) |
| **G6** a cancel that does not drain leaves the session wedged, the next turn fails "indefinitely" (P2) | true. FIX: `session/close` + re-attach, process restart as fallback | partly: opencode's own cancel path bounds the drain at 5 s and frees the slot; the wedge needs the core to hang inside the turn (the Appendix A trigger is a removed entry with no cancel sent). Loud, not silent (`-32603` every turn). `session/close` is the same `FiberMap.remove` on the same fiber (`turn.ts:397-398`) and hangs the same way. FIX (one sentence): terminate the process, `AgentUnavailableError`, lazy re-attach | **FIX (doc)** on rater 2's shape, converged on `turn.ts:397-398`: §3.5 step 5 terminates the process. The finding's "indefinitely" on the cancel path is unsupported and the thread says so |
| **G7** (control) `save()` validates beside the symlink target | — (known) | false on HEAD (`config_base_dir()` → `RUNTIME_DIR`, #186), outside the PR, DROP; flagged as the likely control | control passed |

Notes:
- Adoption: 6 of 6. Verdicts agree on all six; the fix shape differed on G2, G4 and G6, and rater 1 conceded each on a source line (`client-system-prompt.ts`, `adapter.py:296/474`, `turn.ts:397-398`). Rates were not needed: every surviving finding passed `cheap` against the cheapest correct fix.
- Severity re-ranked: G5's P1 stands and leads. The other five P2s are wrong-rationale fixes in a design doc, which CLAUDE.md ranks above small bugs; G6's P2 overstates (loud).
- Two owner decisions this round: `XDG_DATA_HOME` isolation (G5) and no database reads from the gateway.
- Cost: 1 round, 2 raters, about 90 minutes of attention, most of it on G5's source trace and the owner's questions about scope and undo.

**Status: open** — fixes pushed, confirming Codex round requested.

---

## 2026-11-14 — PR #205 round 3 (confirming round on `175f29b`)

**Chain detector:** 16 findings over 3 rounds, round 3 `1 finding(s) on our own last fix, streak 2 — STOP` for the design doc. **Checked and found to be an artifact.** `git blame` on the two doc findings' lines (`:210`, `:760`) gives `50f45a1`, the 2026-11-07 text, so neither sits on a round-2 fix. The hit is the three round-2 doc comments (ids 4575367977/85/90, `created_at` 19:41, `original_commit_id 6836a3c`) that GitHub re-anchored to `175f29b` when the file changed. The detector keys on `commit_id`, which moves with the anchor. Same class as the #181 r9 limitation, in the other direction: that one missed a chain, this one invented one. Recorded for the detector; the stop rule was not applied because its precondition did not hold.
**Arrival check failed once:** the first listing read only two findings. `gh api` without `--paginate` returns one page of 30, and the PR had passed that. The GraphQL count of unresolved threads (4, not 2) caught it before any thread was resolved. Written into memory.
**Control finding:** none this round. **Rater 2 not run**: all four findings pass the `cheap` gate (two one-liners on probes, two sentences in the design) and the method scores nothing for a `cheap` FIX. Noted as a deliberate shortcut, not a precedent for scored findings.

| | rater 1 (author) | outcome |
|---|---|---|
| **H1** probe step 5 waits on a shared event, so when `sid1`'s `config_option_update` lands first the probe reads `c.options[sid2]` before it exists and records `current=None` (P2) | true, and it explains the `current=None` in both live runs (the notification for `sid2` printed `current=watcher_a`). The design does not cite that value, so no doc change. `cheap`: a per-session event | **FIX** — `option_event(sid)`, callers wait on the session they inspect; the re-run reads `current=watcher_a` |
| **H2** `probe_resume.py` and `probe_new_session.py` pass a relative `<cwd>` through unchanged, and resume's `cwd` must equal the stored absolute directory (P2) | true, loud (attach fails). `cheap` | **FIX** — `os.path.abspath` in both, as `probe_agent_mode.py` already does |
| **I1** `profile` is mandatory for `type: acp`, but the §3.1 wiring list only adds `acp` to the type enumerations; neither `coop config add agent` (`cli.py:318`, `--type` only) nor the TUI form (`agent_detail.py:122`, type enum, no profile field) can produce a loadable agent (P2) | true. A wrong wiring list the implementer would follow verbatim. `cheap` (doc) | **FIX (doc)** — §3.1 names `--profile` (required with `--type acp`) and a registry-backed `profile` enum on the TUI form shown for `acp` |
| **I2** the alias "`tool: edit` matches `Edit` and `Write`" cannot come from the shared predicate, which is one `re.fullmatch(rule.tool, tool_name, IGNORECASE)` (`tool_match.py:554`); the design promises the alias without specifying the matcher change (P2) | true. Same class as round-2 G1: a promise with no mechanism. `cheap` (doc). Owning layer: the predicate, with the map per profile so a `tool: edit` rule on a Claude agent keeps meaning `Edit` | **FIX (doc)** — P6 specifies `profile.rule_aliases = {"edit": ("Edit", "Write")}` and the predicate trying each alias before the plain match |

Notes:
- Adoption: 4 of 4. Two probe one-liners, two design sentences. The round was not "all nits": I1 and I2 are the same class as round 2's G1 (a stated behaviour with no mechanism behind it), so a confirming round is requested rather than stopping here.
- Codex's security review did not run in any of the three rounds (its paying account reported insufficient credits); the code review ran each time.
- Cost: 3 rounds, 2 raters in rounds 1 and 2, about 3.5 hours of attention across the PR.

**Status: open** — fixes pushed, confirming Codex round requested.

---

## 2026-11-14 — PR #205 round 4 (confirming round on `adca85a`)

**Chain detector:** 21 findings over 4 rounds, round 4 `1 finding(s) on our own last fix, streak 3 — STOP`. `git blame` on the five anchors: `:683` and `:422` are the 2026-11-07 text, `:878` and `:920` are `37aadb3` (the commit the PR opened on, before any round), `:220` is `adca85a` (round 3's I1). So the real chain is **streak 1** (round 3's was the re-anchoring artifact), which the rule calls noise. The stop rule was not applied. What the one real hit shows is a **pattern**: I1 added an entry to the hand-written wiring list, J5 found the next missing entry. The pattern fix is not a longer list. §3.1 now says the tests own that enumeration (`RELOAD_ACTIONS`' exhaustiveness test, `check_backend_signatures`, the config-load tests) and the list is orientation.
**Arrival check:** the `created_at > 00:30` filter returned nothing because the review landed at 23:44. The unresolved-thread count (5) and an id-based `--paginate` fetch recovered all five. Filter on the review's own timestamp, not a guess.
**Control finding:** none. **Rater 2 not run**: every surviving item is a sentence in the design, and one is a DROP on an ADR's stated boundary. Said here so the shortcut is visible.

| | rater 1 (author) | outcome |
|---|---|---|
| **J1** the ruleset leaves `read`, `grep`, `glob` and subagent calls at opencode's default allow, so a guest runs them with no ask; "add force-ask entries for every tool category governed by the broker" (P1) | half wrong, half right. ADR-0002's Decision paragraph names the opencode injection scope as bash, edit, webfetch, websearch, so `read`/`grep`/`glob` at default is the ADR's own scope, not this profile's gap. But the P3 sentence "R4's gap statement is then only the action gap" was false: 2.x adds `question`, `subagent`, `skill` as actions (`[docs]` v2 permissions "Defaults"), and on `type: claude` the matching calls go through the ladder. P1 inflated. `cheap` (doc) | **FIX (doc)** — P3 gains "The scope gap", listing the six ungated actions, naming three as 2.x-new, and leaving whether to ask for them as the owner's policy call (put to the owner with this round). The sentence J1 caught was replaced, not appended to |
| **J2** `RUNTIME_DIR/opencode/<agent id>.jsonc` interpolates the raw config key; the loader does not validate it as one path component (P2) | true: `_parse_one_agent` has no name rule, the CLI does (`cli.py:317`). `cheap` (doc) | **FIX (doc)** — the loader applies the CLI's regex to a `type: acp` agent's key |
| **J3** a project `.opencode/` config defining `coop-<path_key>` outranks the gateway's entry; the prefix does not enforce the claim (P2) | true as a mechanism, inside the trust boundary: the working directory is the agent's own (ADR-0001) and a project config there is the same class as an `AGENTS.md` the agent can write. The ruleset is unaffected (`OPENCODE_CONFIG_CONTENT` outranks project config). Random names would defend the agent from itself | **DROP** — one sentence in P5 states the boundary. Reversal: a report of a `coop-<path_key>` entry in a project config that the operator did not write and the agent did not write |
| **J4** re-pointing the Claude broker at the shared predicate loses the `_CLAUDE_META_TOOLS` unconditional allow (P1) | true at `claude/broker.py:195-199`, and already guarded: the design requires the brokers' tests to prove the decision table did not move. P1 inflated. `cheap` (doc) | **FIX (doc)** — §3.6: the predicate is the ladder and nothing before it; the meta-tool pre-check stays in the Claude broker |
| **J5** `profile` must be classified in `RELOAD_ACTIONS[AgentConfig]` (`config_diff.py:51-71`, exhaustive, tested); the wiring list omits it (P2) | true. Same pattern as I1. `cheap` (doc) | **FIX (doc)** via the pattern move above, with `config_diff.py` added to the orientation list |

Notes:
- Adoption: 4 of 5, 1 DROP. Verdict kinds: one ADR-scope misread with a true sentence under it (J1), one trust-boundary DROP (J3), three single-sentence spec completions. The useful signal is thinning: nothing this round changed a mechanism.
- J1 leaves a policy question for the owner (ask for `question`/`subagent`/`skill` on this profile, or not). The next Codex round waits for that answer, because it changes P3.
- Owner's answer (2026-11-15): ask for `subagent` and `skill`, leave `question`. The reason that settled `subagent` was found while answering, not in the finding: an agent entry's own `permissions` are pushed after the global rules and a child session evaluates against its own agent's ruleset, so the agent name is a permission-profile switch and the gateway's `ask` rules do not reach the child (2.2.6 `config/plugin/agent.ts`, `permission.ts` `configured`; 1.18.13 `subagent-permissions.ts` inherits only `deny`). `question` stays out because on 1.18.13 the tool never asks and the key only hides it. P3 gains the ruleset rows and the rationale, P6 gains the `Task`/`Skill` field mapping, §8 gains item 8.
- Cost: 4 rounds, about 4 hours across the PR.

**Status: closed** — settles with round 5 below.

## 2026-11-15 — PR #205 round 5 (final round on `05223a2`, after the owner's J1 answer)

**Chain detector:** 28 findings over 5 rounds, round 5 `1 finding(s) on our own last fix, streak 1`, no chain. The one finding (K1) lands on the paragraph `05223a2` added, which is where a finding on a new paragraph has to land.
**Arrival check:** five inline comments, five unresolved threads, review on `05223a2`. All read with `--paginate`, filtered on the review's own timestamp.
**Control finding:** none in the batch. Rater 2 pointed out the log does have a settled entry (`#181 r9 F1`, `settled: implemented`) that could have served; it was not sent. Agreement this round is therefore uncorroborated.
**Rater 2:** blind, same five findings, method and constants, no verdicts. Verdicts agreed on all five (FIX). Ranking agreed on the top two (K5, K1); the tail differed (rater 1 K4 > K3, rater 2 K3 > K4), both far from a band edge. Three fix shapes came from rater 2 and were taken: the K1 load-time warning, the K2 relabel, the K5 "early room check stays in front of the shared call" choice.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **K5** §3.6 ladder puts "no room → deny" first; `ClaudePermissionBroker._decide` checks guest list, owner skip, owner list and `human_approval=false` before needing a room (P2) | true, and the sentence "both existing brokers implement the same ladder" is false: the 1.x opencode broker checks the room first (`opencode/broker.py:247`), Claude only at the ask step (`claude/broker.py:176-230`). Rank 1: a wrong rationale that would change a 1.x broker's row if built as written. `cheap` (doc) | same evidence, same rank. "Pick and state it": fold the early check into the predicate (a 1.x behaviour change) or keep it in front like the meta-tool pre-check | **FIX (doc)** — ladder rewritten in the Claude order, the 1.x divergence named, the 1.x opencode broker keeps its early room check in front of the shared call so its table does not move |
| **K1** a guest allow-listed for a custom `Task` such as `deploy` runs that entry's `shell: allow` with no ask; "gate or reject subagents with overriding permissions, or apply force-ask after each entry's rules" (P1) | true, needs two operator lines (`agents.deploy.permissions` and a guest `Task` rule); the shipped guest rules carry no `Task`. Silent. The gateway cannot re-apply after an entry's rules (they load last). A profile rule "guests get built-ins only" couples the profile to opencode's built-in names. Rank 2. `cheap` (doc) | same reachability, `hours_per_hit` 5–40 h, `silent` until §8 item 8 is run. FIX (doc) + MAKE IT LOUD: name it in the R4 gap statement, state the `Task`-rule divergence from `type: claude`, extend P6's load-time warning to a guest `Task` rule on a `type: acp` agent. FILE the built-ins-only rule | **FIX (doc) + MAKE IT LOUD** — P3 paragraph on what a guest `Task` rule grants here, the config-load warning, the scope gap names it. The built-ins-only rule is not filed: the warning plus a scoped rule is the guardrail, and a rule bound to opencode's built-in names would drift |
| **K3** `tool_call_update` `failed` folds to `✓ <name>` like `completed` (P2) | true, inherited: the 1.x parser emits `✓` for `error` too (`adapter.py:1387`). The row is this increment's. `cheap` (doc) | same. Take the `✗` cell, FILE the 1.x parser | **FIX (doc)** — `failed` → `✗ <name>`, the 1.x parity stated and declined. The 1.x parser is not filed: the 1.x backend's changes are its retirement |
| **K4** cost `None` on the first turn after attach, also for a session created with `session/new`, whose baseline is zero (P2) | true. `cheap` (doc) | same, loud (`n/a` in the usage log) | **FIX (doc)** — zero baseline for `session/new`, `None` only after `load`/`resume` |
| **K2** README labels a three-action ruleset "the §4 force-ask ruleset" (P2) | true. `cheap` | true, with a trap: `probe_agent_mode.py` carries the same three actions and produced the `[live]` evidence, so expanding the README alone misstates what ran | **FIX** — README shows the seven-action ruleset and says the live evidence and the probe's `RULESET` used the three-action subset |

Notes:
- Adoption: 5 of 5. Verdict kinds: one wrong rationale in the design (K5), one silent operator-configured bypass (K1), three single-row spec corrections. K1 is the only finding on the previous round's fix, and it was not a chain: the paragraph was new, the finding completed it.
- Severity re-rank vs Codex: K5 (badged P2) above K1 (badged P1). K1's P1 is category-driven; K5 changes what gets built.
- Convergence: this was the owner's agreed final round ("answer J1, one confirming round, then stop"). No further round is requested. The merge decision is the owner's.
- Cost: 5 rounds, about 5 hours across the PR; this round 1 rater, 25 min.

**Status: closed** — fixes pushed, PR handed to the owner for merge.

## 2026-11-18 — PR #207 round 1 (Codex on `d4e55f3`, `docs/design/remote-agent-gateway.md`)

**Chain detector:** 11 findings over 1 round, no chain (first round).
**Arrival check:** 11 inline comments, one review (`d4e55f3`), read with `--paginate`. The security review did not run ("insufficient available credits"); the code review did. The branch was ten commits ahead of the reviewed one (owner-review edits: voice removed, roles, lazy attachments, liveness, onboarding), so one finding (F616) was already answered by the tree.
**Control finding:** `#181 r9 F1` (`settled: implemented`) was sent as F99, presented as a code finding among doc findings. Rater 2 traced the *current* code and returned `cannot-occur`, correctly: PR #186 moved the base to `COOP_HOME`, so the premise no longer holds in the tree. A finding settled by implementation cannot serve as a control against the live tree, because the right answer is now "fixed", not the ledger's verdict. Rater 1 did not score it (author of the ledger row). **No usable control this round; agreement is uncorroborated.** A usable control needs a DROP or FILE whose premise persists and whose outcome is observed (`settled: bit` or `held`); the ledger has none yet.
**Rater 2:** blind, 12 findings, method, constants, the three documents, no verdicts, no log. Verdicts agreed on all 11 real findings (10 FIX as document edits, F616 no change). Two additions from rater 2 were taken: F74 is stated in five places, not one (the diagram, the roomsim mapping, the API table, §3.5, direction §2 row 3); F645 is contradicted by §8, which called `AGENTS.md` "confirmed" while the adapter table said `[unverified]`. Ranking differed in the middle (rater 1: F58, F80, F626, F63; rater 2: F58, F645, F74, F609), all far from a band edge since every verdict is FIX.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **F58** utility sends `done` with the wake's hwm on exit 0 without an agent `done`, so the cursor advances over posts the agent never read (P1) | true, `silent` (no log distinguishes a fabricated `done`), `cheap`. Rank 1: the FIX anchor's pattern | same, cites MAE 527 (catch-up starts after the cursor) and MAE 765 (the failure notice makes the skip visible). Rank 1 | **FIX** — any exit without `done` is `done --failed "no done from agent"`; the duplicate-reply case is named as the visible side of the trade |
| **F63** `done` carries an agent-supplied `hwm` the gateway saves as the cursor (P1) | true, `cheap`: the lease already holds `hwm`, so the fix removes the field | same, adds: own token → own cursor, correctness not security (ADR-0001); a `done` with no live lease must then move nothing | **FIX** — `hwm` dropped from `done`; the gateway saves the lease's mark; a `done` naming a wake with no live lease is logged and moves nothing |
| **F74** API table says the posting endpoint is enforce-only, §3.3 says both modes (P2) | true, one-line contradiction | true in five places (L78, L139, L167, L331, direction §2); "all five, or it survives" | **FIX** — all five |
| **F80** a turn running past `lease_expires_at` keeps sending while the gateway issues a new wake; late `done` accepted (P1) | true by the doc's own sentence. Fix without a renewal endpoint: utility kills at expiry, gateway ignores a late `done` | same shape, scored 5–30 hits/yr (P(turn > lease) 0.5–3%, a guess), payback ≪ 1 yr. Adds: utility takes no new wake while a command runs; the skill forbids `send` after expiry for own-harness agents | **FIX** — §3.4 kill at expiry + `done --failed "lease expired"`; no new wake during a command; own-harness late sends are unsolicited posts, best effort. Renewal deferred |
| **F88** agent-owned cron posts through `coop send` with no wake, so enforce-mode validation against the hint is undefined (P1) | true, unreachable until `enforce` is on; defining the wake-less `send` is the fix and is needed for proactive posts anyway | same, 1–5 hits/yr, adds that measure-mode counts (the enforce baseline) are equally undefined | **FIX** — §3.5 unsolicited post: mute, rate, closed threads, `to` only, both modes, counted separately |
| **F94** `send` syntax makes `--to` and TEXT mandatory, so a control-only item is unrepresentable (P2) | true, `cheap` | same | **FIX** — both optional, text needs `--to`, one of text or `--control` |
| **F609** inferring `answered` from observed posts misses a `must-reply` answered by `done` alone (P1) | true, it answers the doc's own open decision | same; a wrong rationale that would produce repeated wakes if built | **FIX** — `answered` in both modes, open decision closed |
| **F616** migration deletes `gateway/` while voice's disposition is open (P2) | already answered by the owner (direction §3.8, `599d393`): voice is removed and its code goes with the package | not true in the tree: DROP, reversal = a config referencing the voice endpoint before step 6 | **no change** — thread answered and resolved |
| **F626** a bare LAN listener with a bearer token (P1) | true, reachable in the stated first deployment; security by reachability | same, `cheap` as deployment guidance; proposed TLS by proxy only | **FIX** — owner's decision (2026-11-18): the utility connects over TLS, HTTP only for loopback, tunnel and test with a utility warning; `gateway.tls` with a self-signed certificate the utility pins, or a proxy. Open decision closed |
| **F637** `git mv` of the connectors leaves the frozen daemon unable to import them mid-migration (P1) | true inside the layout proposal; loud (ImportError on restart). Rank low-mid | same, 1–3 hits in the migration window | **FIX** — copy until §7 step 6, not move |
| **F645** the utility writes `AGENTS.md` into the owner's working directory (P2) | true; already `[unverified]`; state the constraint | same, `silent` (a repo's constraints vanish, no log); §8 "confirmed" contradicts §3.4 | **FIX** — the utility never writes a file the working directory already holds; carrier `[unverified]`; §8 reconciled |

Notes:
- Adoption: 10 of 11 (F616 answered by an earlier commit). High, as on every first round of a design document (#198 r1, #204 r1, #205 r1): the findings are on text written once, not on fixes.
- Severity re-rank vs Codex: F637 (P1) down to low-mid, a migration-plan defect that fails loudly; F74 (P2) up, a contradiction in the spec; F58 first regardless of badge.
- Method: the control failed as administered, not as judged. Recorded above so the next round does not resend a `settled: implemented` row as a control.
- Cost: 1 round, 2 raters, about 50 minutes.

**Status: open** — fixes pushed, re-review requested. The merge decision is the owner's.

## 2026-11-19 — PR #207 round 2 (Codex on `2c23210`, `docs/design/remote-agent-gateway.md`)

**Chain detector:** 22 findings over 2 rounds, round 2 `10 finding(s) on our own last fix, streak 1`, no chain (once is noise). Read: six of the eleven land on round-1 text by `git blame`, five of them *extend* a fix (G275 and G342 complete the unsolicited-post paragraph F88 added, G285 completes F626's TLS rule, G293 is the liveness rule meeting F80's "no new wake during a command", G321 is the lease-at-pickup sentence). None replaces a fix. Watched for round 3: a second streak on the same paragraphs means stop and re-read the increment, not another patch.
**Arrival check:** 11 inline comments, review on `2c23210`, read with `--paginate` and filtered on the review's timestamp. The security review did not run again (credits).
**Control finding:** none. The ledger still has no `settled: bit`/`held` row, and round 1 showed a `settled: implemented` row cannot serve against the live tree. Agreement this round is uncorroborated. Rater 2 disclosed that `git show 2c23210` printed round 1's log entry in the diff (F findings only, no G material).
**Rater 2:** blind, 11 findings, method, constants, the three documents and the simulator, no verdicts. Verdicts agreed on all 11 (10 FIX, G337 DROP). Rater 2's fix shape was taken over rater 1's on G321 (freeze the mark at issue, citing spec §5.6 "fix a high-water mark at turn start", instead of rebuilding the payload at pickup) and on G346/G305 (one restructure, a per-agent block, answers both). Rankings differed at the top (rater 1: G321 first; rater 2: G285, G293, G321); every verdict is FIX, so no band edge is near.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **G321** a pending wake picked up with the *current* hwm while its hint describes the original trigger: a later addressed post is read, then skipped by the saved cursor (P1) | true, `silent`, rank 1; fix: rebuild hint and hwm at pickup | true, `silent`, traced through sim L296/L646; cheaper fix: freeze the hwm at issue, the backlog sentence already covers the rest; spec §5.6 fixes the mark at turn start | **FIX** — hwm frozen at issue; later posts wait for the next sweep |
| **G285** the utility pins a fingerprint nobody gave it (P1) | true, completes F626 | true; built as written it is TOFU on the path the token travels; security by reachability on the LAN deployment | **FIX** — `config add agent` prints token, address and fingerprint; utility refuses a mismatch; §3.6 step 1 updated |
| **G312** direction says classifier is fail-open, design says addressed-only (P2) | true, contradiction | true, one site, design is authoritative; wrong rationale in the direction doc | **FIX** — direction says `addressed-only` on error, spec §5.1 |
| **G275** `send` names only a thread: no top-level unsolicited post, no room for a multi-room agent (P1) | true; `--room` for a new post | true; gate call stated: `cheap` because `--room` already exists on `read`; scored FIX anyway | **FIX** — `[--room R \| --thread T]`, §3.5 names the destination |
| **G346** bio stored per room, pushed before any room (P1) | true; bio is agent-level | true, loud at build, silent degradation if built per room | **FIX** — per-agent block in the state example |
| **G342** unsolicited `--control` skips §5.9 authority (P2) | true, one clause | partly: the simulator ignores an unauthorised control, so the consequence is a misleading visible line and an uncounted violation, not an applied mute | **FIX** — authority and target checks added, folded into the §3.5 rewrite with G275 |
| **G293** a turn longer than 2×`wait` with no calls is marked offline (P2) | true, noise | true, loud, fires on ordinary long turns and posts into the room | **FIX** — offline only without a live lease |
| **G300** typing at issue, pending wake never clears it (P2) | true, cosmetic | true on Rocket.Chat (explicit clear), auto-clears on Mattermost | **FIX** — typing on pickup, both documents |
| **G331** attachment counter sees trigger posts only (P2) | true | true, a measurement gap | **FIX** — counts the delivered range |
| **G305** duplicate `agents` key in the state example (P2) | true, from the liveness commit | true | **FIX** — same restructure as G346 |
| **G337** `send` retry after a lost response posts twice: add an idempotency key (P2) | true mechanism, not worth a new field plus retained results for a visible duplicate the contract allows; one sentence instead | scored: 0.03–9 hits/yr × 0.05 h, fix 3.5 h, tax 0.5 h/yr → net ≤ 0 across the range; DROP, optional one-liner | **DROP** the key; owner agreed (2026-11-19). Reason: duplicates are inside §14.4 and `net_annual_saving` ≤ 0. Reversal: a duplicate reply in a live room whose gateway log shows the post but no ack. Revisit 2027-11-19. The one-liner is in: the CLI does not retry, and the error names the cause and tells the agent to `read` before posting again |

Notes:
- Adoption: 10 of 11. All ten are bounded edits, no new mechanism; the one new mechanism proposed (G337) was the one dropped.
- Severity re-rank vs Codex: G321 (P1) first; G275 (P1) and G346 (P1) mid, loud at build; G312 (P2) up, a direction-doc rationale.
- Pattern (rater 2): four findings (G275, G321, G342, G346) are "a thing the design names has no stated home or lifecycle" (room on `send`, the mark at pickup, authority on an unsolicited control, the bio before a room). The sweep for round 3 is one pass over every noun in §3.1–3.5 asking where it is stored and when it is fixed.
- Cost: 1 round, 2 raters, about 45 minutes.

**Status: open** — fixes pushed, re-review requested. The merge decision is the owner's.

## 2026-11-19 — PR #207 round 3 (final round, Codex on `2e03eda`)

**Chain detector:** 30 findings over 3 rounds, round 3 `3 finding(s) on our own last fix, streak 2 — STOP`. The three (H-274, H-283, H-317) all propose extending the `send` grammar and the §3.5 unsolicited-post paragraph a third time (F88 → G275/G342 → these). Response per the method: not another patch. Re-read of the increment: the unsolicited post exists only because the scheduler moved agent-side (direction §3.2), and the hint check that makes its turn-membership matter bites only in enforce mode, off by default. All three are declined, which ends the chain. The five fixes land on text neither fix commit wrote.
**Owner's rule for this round (2026-11-19):** last round; only a major gap is fixed (silent loss of a post or a required turn, a security-boundary hole, a contradiction between the documents, a block on the one-at-a-time migration). No re-review is requested after it.
**Arrival check:** 8 inline comments, review on `2e03eda`, `--paginate`, filtered on the review's timestamp. Security review did not run (credits), third time.
**Control finding:** none; agreement uncorroborated.
**Rater 2:** blind, 8 findings, method, constants, the documents, the simulator and both connectors, no verdicts. Verdicts agreed on the five fixes. Rater 1 conceded on three, each on checkable evidence: H-283 (spec §5.4 lines 426-437: only an owner human sets an orchestrator, so a bot's control in a thread it just opened fails authority by composition), H-274 (design L124/L270: cursor and `answered` are untouched by a mislabelled send; the refusal is loud and enforce-only), H-290's shape (`envelope.py:135-137`, `gateway.py:977`: the simulator already counts a missing must-reply item in measure mode and fails the turn in enforce mode, so the port keeps §6 step 2 replay parity instead of inventing an unfinished-set rule). Rater 2's sweep was taken: H-324, H-333 and H-308 are one pattern, "today's path has a property that contradicts the new contract", written as one sentence group in §3.3 rather than three patches.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **H-324** the copied history filter drops unlisted senders before they can be labelled `guest` (P1) | true, `silent`, `mattermost/connector.py:1002`; major | true, both connectors, and the live path (`sender_allowed`) too; rank 1 | **FIX** — filters deliver an unlisted sender as `guest`; tests change with them |
| **H-333** one history page, `done` advances past the rest (P1) | true, `rest.py:408-411`; major | true, spec §5.6 names pagination as a requirement; rank 2 | **FIX** — `read` pages until the range is assembled, fails otherwise, cursor stays |
| **H-308** multiline text forges the header line (P1) | true, `history_context.py:22,130`; security boundary | true, the new renderer replaces the formatter that held the invariant; rank 3 | **FIX** — newlines collapsed to one space |
| **H-290** `done` without covering a must-reply thread advances the cursor (P1) | true, `silent` (spec L136-137: an open unanswered thread is in no set); fix: into the unfinished set | true with a mode split: measure loses the counter, enforce loses the turn; the simulator already does both; fix by naming that | **FIX** — the 5.9 row names the simulator's behaviour in both modes |
| **H-302** roster and locally hosted agents conflated (P1) | true, `roomsim/gateway.py:165-186` splits them; blocks §7 | partly: pending leases and offline noise for unmigrated bots, not a double wake; not major, FIX anyway as one sentence | **FIX** — a roster name without an `agents:` entry gets no cursor, lease, token or wake |
| **H-274** no `wake_id` on `send`; a cron post during a live lease is validated against the wrong hint (P1) | true; proposed `--wake` on `send` to end the chain structurally | true, consequence small: loud refusal in enforce, a mislabelled counter in measure, cursor and `answered` untouched; scored 0–0.4 hits/yr, tax 0.2 h/yr → DROP band; FILE against the enforce rollout | **FILE** — against §6 step 4 (enforce on), decay 2027-11-19. Reversal: a refused unsolicited post inside a lease window in the gateway log. Issue to be opened once the owner agrees |
| **H-317** `send` grammar omits `--file`, stdin, positional room, `--attach` (P2) | partly true; keep file/stdin, mark `--attach` out | the direction row already says text, file and stdin are kept; room and `--attach` were never promised; the grammar line is the chain line | **DROP as converged** — direction §4 governs; outbound upload is the attachment increment's. Reversal: a remote agent asking how to post a file |
| **H-283** `--room` with `--control` has no thread to authorise against (P2) | true; require `--thread` | `cannot-occur`: spec §5.4, only an owner human sets an orchestrator, so a bot's control in a fresh thread fails authority by composition | **DROP** — defined by composition. Reversal: a bot-issued top-level control the gateway applies |

Notes:
- Adoption: 5 of 8 fixed, 1 filed, 2 dropped. The three declined are the chain.
- Severity re-rank vs Codex: all eight badged P1/P2; the three P1s on the chain (H-274 and two P2s) rank last. H-324 first: daily, silent, on the default path.
- Convergence: this is the owner's agreed final round. No further review is requested. The merge decision is the owner's.
- Cost: 3 rounds, 2 raters each, about 2.5 hours across the PR; this round about 50 minutes.

**Status: closed** — fixes pushed, PR handed to the owner for merge.

Addendum (2026-11-19, after the round): the owner revisited H-274 and spec §5.9 as a design question rather than a finding. Settled: a `send` always states `--wake W` or `--unsolicited` (omitting both is a usage error), the wake id is attribution only (the wake's ledger, checked for must-reply coverage at `done`), every `send` passes the same room rules at posting time, and the specification's "reply only in a delivered or hinted thread" becomes an instruction and a counter, since no mechanism depends on it. H-274 therefore moves from FILE to **incorporated**: the misclassification it described cannot occur when nothing is classified. No issue is opened. The thread is answered and resolved.

## 2026-11-19 — PR #207 round 4 (owner-requested extra round, Codex on `7052c80`)

**Why a fourth round:** round 3 was declared final, then the owner reopened one finding (H-274) as a design question and settled it (`7052c80`: a `send` states its wake or `--unsolicited`, the wake id is attribution only, every send passes the same room rules). Because rounds 1–3 had each found a silent-loss defect, the owner asked for one more round on the settled text, to be wrapped up unless it found a major gap.
**Chain detector:** 34 findings over 4 rounds, round 4 `4 finding(s) on our own last fix, streak 3`. By blame only one of the four (K-719, line 272) lands on fix text (round 3's `read` paragraph); the other three are on pre-round text. K-719 is taken despite the streak: it replaces round 3's sentence rather than extending it, because round 3 put the concern at the wrong layer (an instruction to the agent) when the gateway serves the read and owns the cursor.
**Arrival check:** 4 inline comments, review on `7052c80`. Security review did not run (credits), fourth time.
**Control finding:** none; agreement uncorroborated.
**Rater 2:** blind, 4 findings, method, constants, the documents, the connectors' attachment code, no verdicts. Verdicts agreed on all four. Rater 2's shapes taken on K-719 (the empty-read case for unfinished-thread turns) and K-728 (the real omission is the token's room scope, which the design never stated; one sentence closes it and the attachment question together).

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **K-719** a normal `done` after a failed or skipped `read` saves the mark and skips the posts (P1) | true, `silent`, the F58 class; the gateway knows whether it served the read | same; round 3's sentence was an instruction, not a mechanism; `silent` for optional and catch-up posts | **FIX** — the lease records a complete read; a normal `done` without it is `done --failed "no read"` |
| **K-725** pause kept by the direction, absent from the design's state and wake path (P1) | true, two documents disagree | true, and `requirements.md:89` promises pause persists across restarts; today it does | **FIX** — `paused` persisted; a paused bot is held like a muted one for every thread |
| **K-723** `heartbeat: agent` has no API to open its own catch-up turn (P1) | true, an option the API cannot honour; remove it from this increment | mechanism true, consequence not a loss (the next wake redelivers from the cursor); cheapest by removal; not major | **FIX by removal** — `heartbeat: gateway` is the only value here; `agent` is defined with the transports that carry one |
| **K-728** any attachment id is fetched with the bot's credential; bind ids to delivered posts (P1) | mechanism true, reach near zero (random ids, the bot's own account); one cheap sentence | DROP the handle scheme (inside one agent's own account, ADR-0001); the gap is the token's room scope, unstated for `posts?room` too | **DROP** the scheme. The scope sentence written: the API answers only for rooms whose roster lists the agent, and `{id}` is the handle `read` printed. Reversal for the drop: a fetch logged for an id outside any range delivered to that agent. Revisit 2027-11-19 |

Notes:
- Adoption: 3 of 4 fixed (one by removal), 1 dropped with a one-sentence scope statement.
- Severity re-rank vs Codex: all four badged P1; K-719 first (silent), K-725 second (contradiction), K-723 and K-728 not major.
- Convergence: the round found one silent-loss defect (K-719), the same class as rounds 1–3 (F58, G321, H-290, H-324, H-333). Each was on a different sentence. No further round: the owner's rule, and the remaining findings were below the bar.
- Cost: 4 rounds, 2 raters each, about 3.5 hours across the PR; this round about 35 minutes.

**Status: closed** — fixes pushed, PR handed to the owner for merge.

## 2026-11-20 — PR #207 round 5 (unintended extra round, Codex on `2449ea8`)

**Why:** the owner triggered the review by accident after round 4's wrap-up and asked for a look under the same bar: fix a major gap, decline the rest.
**Chain detector:** 39 findings over 5 rounds, round 5 `--`: none of the five lands on the previous round's fix by the detector. By pickaxe two touch round-4 sentences (M-821 the read record, M-825 the §3.5 rewrite); both are taken because each found that a round-4 sentence was incomplete (the read record had no API to satisfy it) or wrong (posting-time checks contradicted the spec the row claims to port unchanged), not because of their proposed shape.
**Arrival check:** 5 new inline comments, review on `2449ea8`. Security review did not run (credits), fifth time.
**Control finding:** none; agreement uncorroborated.
**Rater 2:** blind, 5 findings, method, constants, the documents and the simulator, no verdicts. Verdicts agreed on four. **One disagreement, escalated to the owner** (M-825): both raters saw the contradiction, rater 1 read the fix as the spec's turn-start snapshot (A), rater 2 as keeping posting-time checks with an error telling the agent to close the thread (B). The owner chose A and refined it: an unsolicited `send` is itself a turn start, so one check at turn start covers both kinds of turn, and a refused unsolicited send goes back to the caller with the reason and a `retry_after`. He also ruled out B's "close with `done --answered`" on principle: the rate limit exists to slow a bot so humans keep up, never to drop a post or end a conversation. That sentence was missing from spec §5.3 and is added there.

| | rater 1 (author) | rater 2 (blind) | outcome |
|---|---|---|---|
| **M-825** enforce rechecks mutable rules at `send`, refusing a reply valid at turn start; must-reply then fails and is redelivered (P1) | true, a contradiction with spec §5.3 L29 and §5.9 L699 that rounds 3–4 introduced; A | true as a contradiction; consequence smaller (an agent can close with `done --answered`); B, to keep round 4's wording | **FIX, A as the owner refined it** — §3.5 rewritten: two kinds of turn, one check at turn start; a wake send validated against its hint; an unsolicited send is a turn start checked then, refused to the caller with reason and `retry_after`; the gateway drops nothing and never closes a thread for the agent. 5.3 and 5.9 rows and spec §5.3 updated |
| **M-821** `GET /posts` needs `after` the agent does not know and cannot be tied to the lease the read record requires (P1) | true, a round-4 consistency gap | true; built as written a REST agent either skips silently or fails every turn | **FIX** — `GET /posts?wake=W`, bounds derived and the read recorded; `room&after&through` for wake-less reads, recording nothing |
| **M-813** a `reset` between a closed thread's delivery and its unfinished wake leaves the new session with nothing to read (P2) | true, narrow; inclined to decline | true, `silent` (a blind `done --answered` looks normal); one sentence is the fix and the detection | **FIX** — a wake with `reset: true` re-serves the unfinished threads' posts and records the read complete |
| **M-823** roster-only bots have no bio storage; classifier input incomplete (P2) | true; proposed `bio_source: platform` for them | storage gap true, consequence not: the classifier asks per bot, shows `no profile`, and a foreign bot's decision is status only (spec §5.1) | **FIX, one sentence** — a roster-only name has no bio; the classifier sees `no profile` and records its decision for status only |
| **M-816** `--unsolicited --room` opens a new thread per post, so the per-thread rate window never fills; a runaway scheduler floods an enforced room (P1) | true if the window is per thread; one sentence | per thread confirmed (spec §5.3, sim `_count_for_rate`); loud, bounded by `coop pause`, not a boundary; scored 0.02–0.75 hits/yr against a new per-room window concept → payback ≥ 5 yr; FILE | **DROP** as converged, no issue. Reason: loud, the brake exists, and the owner's rule that the limit slows rather than stops makes a flood a pause-and-fix event. Reversal: one bot's top-level unsolicited posts flooding a room. Revisit 2027-11-20 |

Notes:
- Adoption: 4 of 5. The escalation produced a rule (the rate limit's intent, now in the spec) worth more than the finding.
- Severity re-rank vs Codex: M-825 and M-821 first (a contradiction with the ported spec, and a failure loop for REST agents); M-816 (P1) last.
- Convergence: closed. No further round. The merge decision is the owner's.
- Cost: this round about 45 minutes, 2 raters plus the owner on one escalation.

**Status: closed** — fixes pushed, PR handed to the owner for merge.

## 2026-11-20 — PR #207 round 6 (owner-requested, Codex on `b43d086`; reviewed jointly with the owner)

**Chain detector:** 50 findings over 6 rounds, round 6 `4 finding(s) on our own last fix, streak 1`. Noise level; the four are on round-5 text and none extends a fix in the same direction.
**Arrival check:** 11 inline comments, review `5990305028` on `b43d086`, unanchored (no line numbers). Security review did not run (credits), sixth time.
**Control finding:** none; agreement uncorroborated.
**Rater 2:** blind, 11 findings, method, constants, the documents, the simulator and both connectors' history paths, no verdicts. Verdicts agreed on 8 of 11. Rater 2 moved rater 1 on three with checkable evidence: N-454 (sim L925-942: without a degraded trip a bot that keeps missing a must-reply is re-woken every sweep, a loop; raised from "small" to FIX), N-497 (§7 step 3 adds to the new daemon before removing from the old, an overlap not a gap; the residual defect is the unstated initial cursor), N-508 (a normal cron flips the agent online and offline with a notice each time, loud flapping, not a masked outage). Rater 1 held on N-442 (its own round-2 recovery text cannot work as written) and on N-467 (an implementer cannot build "page until the range is assembled" without a representation), which rater 2 would file to the lab.
**Owner review (2026-11-20):** the owner read every finding with the scenario, recommendation and reason laid out, and chose: fix N-463, N-454, N-442, N-508, N-486, N-436, N-478; N-523 removed to `pushed` after confirming the gateway stores the pushed bio and that `platform`/`card` serve a later kit increment; N-497 with the ruling that migration is not seamless and claims nothing, the cursor starts at the latest post and an operator asks the agent to read history; N-520 to the lab, plus the owner's addition that every gateway-authored post begins with `SYSTEM:` so the classifier and agents can read it without metadata. **N-467 is still open with the owner** (the review's one unresolved item: write the `(timestamp, post id)` representation now, or leave it to the lab).

| | rater 1 | rater 2 | outcome |
|---|---|---|---|
| **N-463** wake-side mute/rate holds in measure mode contradict spec §8.1 and §6.2's parity claim (P1) | true, spec contradiction | true, and it breaks the document's own parity promise; rank 2 | **FIX** — measure mode holds nothing an agent could obey itself; pause, backoff, degraded, one-turn always hold |
| **N-454** no degraded trip for CLI-era invalid turns (P1) | small, one sentence | loop in enforce mode (sim trace); rank 1 | **FIX** — refused send or uncovered must-reply at `done` = one invalid turn; `consecutive_invalid_limit` unchanged |
| **N-523** `bio_source` advertises two unbuilt values (P1) | remove, as `heartbeat: agent` | same; silent misconfiguration | **FIX by removal** |
| **N-467** cursor/hwm as orderable post ids on platforms whose ids are opaque (P1) | FIX, one representation sentence | FILE to the §8.1 lab; or the same sentence | **open with the owner** |
| **N-442** lost-send recovery reads below the hwm (P2) | true, own round-2 error | decline, visible duplicate; the one-liner if touching | **FIX** — `read --room` alone prints cursor through latest |
| **N-497** migration cursor handoff (P1) | blocks migration | overlap, not a gap; initial cursor unstated | **FIX, one sentence + owner's ruling** — cursor at latest post; migration is not seamless by design |
| **N-508** `last_seen` on any request keeps a dead poller online (P2) | two phrases | loud flapping, not masking; fold in | **FIX** — `last_seen` is the last wake poll |
| **N-486** pattern rooms absent from §4 (P2) | half true, one sentence | omission, not contradiction; the sentence | **FIX, two comments** — keys are patterns; a discovered room takes the first match |
| **N-436** "never refused" reads unconditional (P1) | reword | DROP, the qualifier is there | **reworded** — "on the ground of its wake id" |
| **N-478** `trigger` required for sweep wakes (P2) | nullable | DROP, example not schema | **comment added** — null for catch-up and unfinished |
| **N-520** failure-notice metadata carrier undecided (P1) | decline, spec's own lab item | FILE with N-467 to the lab | **lab (§6)** + owner's `SYSTEM:` prefix |

Notes:
- Adoption: 9 fixed (two by one-line clarification), 1 to the lab, 1 open with the owner.
- Severity re-rank vs Codex: N-454 and N-463 first; N-436, N-478 (both badged) last.
- Method: the owner asked for findings to be presented as scenario, recommendation, reason, one per finding. Saved as a standing rule.
- Cost: this round about 70 minutes including the owner's review.

**Status: closed** — N-467 settled by the owner (2026-11-20): the `(timestamp, post id)` representation is written into the 5.6 row, the lab verifies it. All 50 threads resolved. No further review requested.

## 2026-11-20 — PR #208 round 1 (Codex on `2659d46`, `coop/room/core.py`)

**Increment:** the room core from `tools/roomsim` runs per room against an abstract platform and clock, with wakes, leases, `send` and `done` in place of the envelope (stage S1).
**Chain detector:** 5 findings over 1 round, no chain.
**Arrival check:** review `2659d46`, 5 inline threads, all on `core.py`. Security review did not run (credits).
**Internal review first:** a code-reviewer agent read the port against the simulator before Codex; its six true findings (platform-refused reply counted against the agent, backend-failure sends uncounted, `hwm=None` crash ending the periodic sweep, pause not covering a pending wake, `enforce` default, off-hint send checked against rate) were fixed before the PR opened and are in its body, not here.
**Control finding:** #181 r9 F1 was sent as F6 — and it was a bad control: it is `settled: implemented`, so at HEAD the defect no longer exists, and rater 2 correctly DROPped it as unreachable (`config_base_dir()` is a constant since #186). A control must be posed at the commit where it was open, or be a verdict whose reversal was observed. **Agreement in this round is uncorroborated.**
**Rater 2:** blind (general-purpose agent), six findings, method, constants, the design, the simulator source; no verdicts. Verdicts agreed on 2 of 5 real findings outright (F2, F4); the three splits were all on the `cheap` gate, as the skill predicts, and all three converged on rater 2's evidence: F1 is `silent` and rater 1 had not priced MAKE IT LOUD (skill Step 1); F3's two-line guard is not correct alone (`_due_threads` re-selects the archived thread every sweep) and spec 5.9 names the archived-thread refusal as a normal turn end; F5's one-line seed from `post.time` drops a live mute when traced (a mute at −8 d with a post at −5 d: the second `_thread()` call resets on wall-clock TTL).

| | rater 1 | rater 2 | outcome |
|---|---|---|---|
| **F1** removed bot still woken and reads (P1) | FILE → S3 (membership is the roster's layer; inexpressible in S1's platform) | true and wider (sweeps, `read_room`); `silent`; LOUD 2 lines; hold is S3's | **MAKE IT LOUD** (`status.hosted_not_members`, warning per wake) + **FILE → S3**; re-ranked P2 (misconfiguration spec 5.14 says to report loudly; not a cross-agent leak under ADR 0001) |
| **F2** restart leaves classifier work in flight (P2) | DROP (process boundary; a late pick is for an undelivered post) | DROP (`cannot-occur`: no production caller) | **DROP** |
| **F3** archived thread's pending work (P2) | FIX, cheap guard | DROP (net ≤ 0; spec 5.9's designed path; platforms archive rooms) | **DROP** — rater 1 conceded on the spec clause and the traced sweep loop |
| **F4** `done` after `expires_at` (P2) | DROP (outcome acceptable) | DROP, flagged as a `cheap`-gate split | **DROP** |
| **F5** replayed thread's `last_activity` (P2) | FIX, 2 lines | DROP (the 2 lines are wrong when traced; correct fix 10–15 lines + roomsim mirror; 0–0.3/yr) | **DROP** — rater 1 conceded on the trace; S3 note: a replay window under `control_ttl_days` makes it `cannot-occur` |

Notes:
- Adoption: 1 made loud, 1 filed, 4 dropped. Severity re-rank vs Codex: F1 P1 → P2; the rest below P2.
- Method: the `cheap` gate split 3 of 5, every time with the first rater holding the cheaper-looking fix and the second rater showing it incomplete or wrong when traced. Rater 1's lesson recorded: trace the proposed one-liner through every call site before calling it cheap.
- Cost: ~35 min rater 2, ~25 min rater 1, one round.

**Status: closed** — the confirming pass (round 2) found new ground only; see the next entry.

## 2026-11-20 — PR #208 round 2 (Codex on `85e8807`, `coop/room/core.py`, `coop/room/prompt.py`)

**Chain detector:** 11 findings over 2 rounds, no chain: round 2 is new ground, none on the round-1 fix.
**Arrival check:** review on `85e8807`, 6 inline threads. Security review did not run (credits).
**Control finding:** none available (round 1's was mis-posed); agreement uncorroborated.
**Rater 2:** blind (general-purpose agent), six findings, method, constants, spec, simulator; no verdicts. It reproduced every finding by running the port and applied every candidate fix to a scratchpad overlay against the 176 tests before calling it cheap. Verdicts agreed outright on G1, G2, G4, G6 (all FIX); rater 2 refined two of rater 1's fixes with traced evidence (G1: a control-only DM post must not become an addressed candidate — an owner's `limit:` in a DM would start a spurious turn at the next sweep; G6: reuse `control_ignored` and report a typo rather than store it). Two splits: G5 (rater 1 FIX by eviction; rater 2: not cheap — a position on `CachedDecision` and a new invariant against the dedupe and replay guards — and net ≤ 0 in an increment with no long-lived core; MAKE IT LOUD now) and G3 (rater 1 DROP as designed; rater 2 FILE: design §3.3 and spec 5.4 disagree on own posts, the unsolicited post is the case the simulator never had, and the proposed fix breaks 20 of 22 parity scenarios — the rule must be written before any fix). Rater 1 conceded both on the cited lines.

| | rater 1 | rater 2 | outcome |
|---|---|---|---|
| **G1** a DM post during a turn is dropped by the sweep, delivered 30 min later as catch-up (P1) | FIX, predicate | FIX, predicate with a control-only guard; rank 1 | **FIX** — `_addresses(post, bot)` at the five sites |
| **G2** hint prints `None posts` when one limit is disabled (P2) | FIX | FIX | **FIX** — only enabled limits are rendered |
| **G3** own posts omitted from `[cursor, hwm]` (P2) | DROP (own output is not input; `read --room` shows it) | FILE: the documents disagree; the fix as proposed breaks parity; band-edge 0.05–7 yr | **FILE → owner** (PR body) |
| **G4** live classifier timeout counted twice (P2) | FIX, 1 line | FIX, 1 line; not parity (the port added `wait_for`) | **FIX** + real-clock test |
| **G5** decision cache never evicted (P2) | FIX, prune by position | not cheap, net ≤ 0 in S1; LOUD | **MAKE IT LOUD** (`status()["decisions"]`); eviction to S3 |
| **G6** a guest can be designated orchestrator and then mute (P2) | FIX | FIX, `control_ignored`; rank 2 | **FIX** — the orchestrator must be a bot in the room |

Notes:
- Adoption: 4 fixed, 1 made loud, 1 filed to the owner. Severity re-rank vs Codex: G1 first (agreed P1), G6 second, G2 last.
- Method: rater 2 built every candidate fix in an overlay and ran the suite before judging `cheap`; two of rater 1's one-liners were incomplete when so tested. Rater 1's lesson from round 1 held: trace, then call it cheap.
- Cost: ~20 min rater 2 (plus its overlays), ~20 min rater 1, one round.

**Status: closed** — the confirming pass (round 3) found new ground only; G3 still waits on the owner (see the next entry).

## 2026-11-21 — PR #208 round 3 (Codex on `5074eb8`, `coop/room/{gateway,core,classifier}.py`)

**Chain detector:** 15 findings over 3 rounds, no chain in any round.
**Arrival check:** review on `5074eb8`, 4 inline threads (3 × P2, 1 × P3). Security review did not run (credits).
**Control finding:** none; agreement uncorroborated.
**Rater 2:** blind (general-purpose agent), four findings, method, constants, design, code; no verdicts. Every finding reproduced by running the port; every cheap fix built in an overlay and run against the suite. Agreed on H2, H3, H4 (FIX). **Split on H1**: rater 1 FIX (the persisted document is this increment's — the approved plan lists bio under `store.py`, design §3.1 persists bios); rater 2 FILE → S3 (no process restart exists in S1, the store is called only by tests, the verdict turns on scope). Rater 2 lacked the plan, so under the Step 4 table this is an **escalation**, not a concession: H1 stays FILE and goes to the owner in the PR body. On H2 rater 2's shape (the wake id already encodes its room; keep the check in the core as `minted()`) replaced rater 1's (the core remembers each bot's last wake id, which `restart()` forgets and an exempt mention overwrites — traced).

| | rater 1 | rater 2 | outcome |
|---|---|---|---|
| **H1** bios not persisted (P2) | FIX, `dump_agents`/`load_agents` in `store.py` | FILE → S3 (scope); payback FIX-band once S3 exists | **FILE, escalated to the owner** |
| **H2** a late send without `room` raises instead of landing unsolicited (P2) | FIX, core remembers the last wake id | FIX, the id names its room; `RoomCore.minted()` | **FIX** — `Gateway.send` falls back to the room that minted the id |
| **H3** trigger role from the platform, not the roster (P2) | FIX, 1 line | FIX, 1 line; rank 1 (a trust label handed to the agent) | **FIX** — `agent` iff the author is in `self.agents` |
| **H4** `@name` loses its reason (P3) | FIX, 1 line | FIX, 1 line | **FIX** |

Notes:
- Adoption: 3 fixed, 1 escalated. Severity re-rank vs Codex: H3 first (silent, reachable now); H2 last of the P2s (loud, no caller reaches it yet).
- Rater 2's side notes for S2/S3, carried to the plan: `_wake_seq` restarts per process so a stale id can equal a live one; after expiry the gateway no longer knows a wake's starting thread, so the CLI must carry `--thread` on a late send or the gateway keep ended wakes.
- Convergence: three rounds, no chain, round 3 all cheap or scope; no further Codex pass requested (the owner's rule from #207: stop when a round is edge cases).
- Cost: ~45 min rater 2, ~15 min rater 1.

**Status: closed** — H1 decided and fixed in round 4; round 2's G3 decided by the owner on 2026-11-21 (an own post is context, never cause) and implemented in PR #209; #208 merged as `1db22c5`.

## 2026-11-21 — PR #208 round 4 (Codex on `52bd273`, scoped by the owner to `store.py`'s agent document)

**Chain detector:** 16 findings over 4 rounds, no chain in any round.
**Arrival check:** review on `52bd273`, 1 inline thread (P2). Security review did not run (credits), third time.
**Control finding:** none settled yet (H1 is fixed, not observed); agreement uncorroborated.
**Rater 2:** blind (general-purpose agent), the finding, method, constants, design, code; no verdict given.

| | rater 1 | rater 2 | outcome |
|---|---|---|---|
| **I1** `load_agents` recreates an agent the gateway no longer hosts and installs its stale bio for the classifier; status lists it as local (P2) | true; `cheap` → FIX as proposed: filter the document against `gateway.agents`, document that `load_agents` runs after `add_room` | true, but the proposed fix is wrong: design §3.1 says a bio is pushed before any room exists (§3.6), so filtering on `gateway.agents` (filled by `add_room`) silently drops a legitimate bio on a restart that loads before the rooms, and adds a call-order invariant. `cheap` on the layer that owns "is this name hosted here": one line in `RoomCore._with_bio` (bio only for `self.hosted`), which also closes `Gateway.bio()` accepting any name. Status half: visible, needs the configured agent set the gateway does not model yet → FILE to the config increment | **FIX in `_with_bio`** (rater 1 conceded on design lines 96 and 115–117: the classifier sees a roster-only name without the gateway's bio, and a bio precedes rooms) + **FILE** the status half → S3 |

Notes:
- Convergence by concession with checkable evidence (design clauses), not escalation. The disagreement was again on the `cheap` gate's *shape*: the finding's `file:line` set the layer for rater 1, and the cheapest correct fix sat one layer up. Fourth time on this PR; the pattern is the skill's own warning ("the proposed one is systematically the expensive one"), here the proposed one was the wrong one.
- Severity vs Codex: P2 holds for the classifier half (silent: a wrong bio, no error); the status half is visible and lower.
- Cost: ~3 min rater 2 (agent), ~10 min rater 1.

**Status: closed** — fix pushed; the status half is on S3's list.

## 2026-11-21 — PR #209 round 1 (Codex on `92af6b7`, `coop/room/core.py`)

**Chain detector:** 1 finding over 1 round, no chain.
**Arrival check:** review on `92af6b7`, 1 inline thread (P2). Security review did not run (credits).
**Control finding:** none settled with an observed outcome (#208 r2 G3 settled by decision, not observation); agreement uncorroborated.
**Rater 2:** blind (general-purpose agent), the finding, method, constants, design, code; no verdict given.

| | rater 1 | rater 2 | outcome |
|---|---|---|---|
| **J1** `read_complete` is initialised from `delivered`, so an unfinished wake whose only new posts are the bot's own forgives a `done` without `read` and commits the cursor past them (P2) | true, reproduced (mute-expiry unfinished wake after an own unsolicited post: `turn_end normal`, cursor past it, `read --room` empty); `cheap` → FIX as proposed: `not payload`, the design's "nothing to deliver" is what `read` returns | true, reproduced with a narrower trigger: no unsolicited send needed, the bot's own reply of the previous turn lands after that wake's hwm, so every unfinished wake after a turn with a reply is this case; traced every wake source: only `unfinished` can issue empty `delivered` with non-empty `payload`. `cheap` → FIX as proposed, same layer (the lease owns "read complete"). Found the suite cannot tell the versions apart (186 pass both ways): the fix needs a pinning test | **FIX** (`not payload`, the field comment, one test: own reply in range + skipped read → `backend_failure "no read"`, cursor unmoved, the next wake reads it; the test fails on the old code) |

Notes:
- Verdicts agreed; rater 2's reproduction is the stronger one (the ordinary case, not the unsolicited one) and became the test.
- Scope: the increment's own — main had both `read` and the shortcut on `delivered`; the PR moved one and not the other.
- Severity vs Codex: P2 holds — silent (`turn_end normal`, no trace, no notice) and reached by an ordinary client that skips `read` on an unfinished wake, which §3.3 already treats as a defect; the loss is the exact promise of the 5.6 row.
- Cost: ~7 min rater 2 (agent), ~8 min rater 1.

**Status: closed** — fix pushed; the confirming Codex pass on `d8b1ee8` found nothing (👍). One round, one finding, no chain.

## 2026-11-21 — PR #210 round 1 (Codex on `2d82801`, `coop/room/gateway.py`, `coop/doorbell/api.py`, `coop/agent/client.py`)

Increment: S2, "a remote agent reaches the room core over HTTP with a bearer token … and `coop read / send / done / bio / attachment` are thin clients of that API". `chain-check.py`: one round, no chain.

**Control finding:** none settled with an observed outcome; agreement uncorroborated.

| # | finding | rater 1 | rater 2 (blind) | verdict | evidence |
|---|---|---|---|---|---|
| F1 | attachment handles bound to the room, not to the agent a read printed them to | FIX (cheap: record recipients per handle) | FILE under plan S3 #5 | **FILE** — rater 1 conceded on `core.py:892` (`read_room` already hands a hosted agent every post after its cursor, handles included): a per-agent record changes nothing an agent can reach today; it is S3's delivered-vs-fetched counters that need it. hits/yr 0.03–0.3 (rater 2), hours/hit ≈ 0. Reversal: counters show fetched > delivered; revisit 2027-11-21 |
| F2 | stale wake id + thread reaches a room whose roster does not list the agent → `persisted[agent]` KeyError → 500 | FIX | FIX | **FIX** `room_for` scans `minted` only over rooms hosting the agent (1 line, C901 8); test `test_a_late_send_on_another_rooms_wake_is_refused_not_500` |
| F3 | attachment fetch buffers the whole file, no size limit | FILE (S3: the limit is at the connector fetch, §3.3) | FILE, add to S3 | **FILE** — plan S3 #5 now names the size limit and streaming; unreachable until a connector implements `fetch_attachment` (only `sim.py` does). Reversal: a connector fetch lands uncapped; revisit 2027-11-21 |
| F4 | `control: {"mute": [[]]}` → TypeError in `_check_name_list` → 500 | FIX (API body typing) | FIX, 2 lines | **FIX** `_control_typed` beside `_typed` (C901 of `send` 7); three mistyped cases tested |
| F5 | certificate preflight has no timeout | FIX | FIX, 1 line | **FIX** `ssl.get_server_certificate(..., timeout=self.timeout)`; test pins the timeout reaches it |
| F6 | measure-mode violations not returned to the client | FILE (owner-deferred to S3) | FILE (deferred) | **FILE** — PR body "Not in this PR" #2, plan S3 #2; the counters are loud operator-side |
| F7 | `httpx.WriteError` not mapped to `LostResponse` | FIX (cheap, 1 token) | DROP: cannot-occur | **DROP** — rater 1 conceded on `httpcore/_async/http11.py:89`: a `WriteError` while sending is suppressed and the connection is read; what surfaces is `ReadError`, already mapped (HTTP/1.1 only, `client.py:72`). Reversal: an `[ERROR]` line from `coop send` naming `WriteError`; revisit 2027-11-21 |
| F8 | platform refusal on a wake send without `--thread` reports thread `None` | FIX | FIX, 2 lines | **FIX** the 502 carries `thread`; the client prefers it; CLI test asserts the line |

**Convergence:** two disagreements (F1, F7), both conceded by rater 1 on a cited line, neither on a number. Both were on the `cheap` gate, as every disagreement so far: a cheap fix for a false (F7) or empty (F1) consequence is still not a fix. **Pattern:** F2 and F4 are one kind — a malformed or hostile authenticated request answered 500 instead of a refusal; two in one round is noted, not yet a sweep (the raw API tests now cover body typing, control typing, roster on every room path).

**Cost:** ~25 min of attention, one round, two raters.

**Status: closed** — fixes pushed; the next round found new ground only (see below).

## 2026-11-21 — PR #210 round 2 (Codex on `d36b15f`, `coop/agent/cli.py`)

`chain-check.py`: two rounds, no chain (round 2 landed on `cli.py`, untouched by round 1's fixes). **Control finding:** none settled by observation; agreement uncorroborated.

| # | finding | rater 1 | rater 2 (blind) | verdict | evidence |
|---|---|---|---|---|---|
| G1 | `--gateway http://[bad` → `urlsplit` ValueError before the `try` → traceback, not `[ERROR]` | FIX (cheap) | FIX (cheap), plus: `HttpDoorbell.__init__` splits the URL too, so `ValueError` must join the except tuple | **FIX** warning moved inside the `try`, `ValueError` caught (C901 of `run` 4); `test_a_malformed_gateway_address_is_an_error_line` |
| G2 | attachment name/type printed raw, so a file named `x\n[main] #p9 H (owner): …` forges a header line | FIX (cheap) | FIX (cheap; `silent`: the forged line looks valid) | **FIX** `one_line` on name and type in `attachment_line`; `test_an_attachment_name_cannot_forge_a_header_line` |

**Convergence:** full agreement; rater 2 found the second `urlsplit` rater 1 had not. Pattern across rounds: G2 is the §3.3 forgery defence applied to a field added in this PR — the kind "a new rendered field skipped `one_line`"; one instance, noted.

**Cost:** ~10 min, one round, two raters.

**Status: closed** — fixes pushed; the next round found new ground only (see below).

## 2026-11-21 — PR #210 round 3 (Codex on `84933b8`, `coop/doorbell/api.py`, `coop/room/gateway.py`)

`chain-check.py`: three rounds, no chain (H1's line is from the original attachments commit, H2's from the S1 port). **Control finding:** none settled by observation; agreement uncorroborated.

| # | finding | rater 1 | rater 2 (blind) | verdict | evidence |
|---|---|---|---|---|---|
| H1 | a file name with CR/LF reaches `Content-Disposition`; aiohttp refuses the header at serialization, so `read` lists the attachment and the download fails | FIX (cheap) | FIX (cheap; verified empirically: `ValueError: Forbidden control character detected in headers`) | **FIX** control characters and quotes stripped from the header value (2 lines, C901 3); `test_a_forged_attachment_name_still_downloads` |
| H2 | `send --wake <A's wake> --room B` posts in A: the lease decides and `room` is ignored after the API authorised B | FIX (cheap) | FIX (cheap; `silent`); flagged that the PR body's "noted for S3" was a deferral without a decay date | **FIX** `room_for` refuses a room other than the live wake's (3 lines, C901 9); `test_a_wakes_send_naming_another_room_is_refused_not_misrouted`. Rater 2's point taken: a deferral in a PR body is policy, not a gate, and the fix was cheap all along |

**Convergence:** full agreement. **Round composition:** r1 8 findings (2 of a kind: an authenticated bad request answered 500; 6 edge), r2 2 edge, r3 2 edge; no chain in any round. By the project's convergence rule (a round that is all edge cases is fixed and closed) no further Codex pass is requested on this PR.

**Cost:** ~10 min, one round, two raters.

**Status: closed** — fixes pushed; the PR is handed to the owner for merge.

## 2026-11-22 — PR S3-1 (`feat/coop-s3-mm`) internal round (reviewer agent on `56bc505`, five lenses, before Codex)

Lenses: authenticated bad request → 500; platform text into a rendered line, header or path; silent loss on the event path; reload leaves state inconsistent; persistence gaps. Lens 2 came back clean (state files keyed by server name and platform-assigned room id; the header strips control characters; `post_line` goes through `one_line`). `chain-check.py` does not apply (no Codex review yet). **Second rater:** none for this round — the internal round is one rater plus the author; every verdict below is uncorroborated and the Codex round that follows is the independent read.

| # | finding | gate | verdict | evidence |
|---|---|---|---|---|
| I1 | a post arriving between `watch` and the room's registration in `_by_id` is dropped with no log line; the next commit moves the cursor past it | cheap, `silent` | **FIX** the room is registered before the first await; the worker starts in `_ensure_started`, after the replay, so a queued post is admitted once and after the saved cursor | `test_a_post_arriving_while_the_room_is_being_opened_is_not_lost` (a restart with a saved cursor; a new bot's cursor is the latest post by design §3.6, so for it the post is before its start) |
| I2 | the connector keeps the `Server` it was built with: a bot added by `coop config add agent` + `coop reload` raises `KeyError` in `add_bot`, swallowed as `[ERROR] … not connected` and then reported "connected"; `Names` stale too | cheap, `silent` half (the lie in the notes) | **FIX** `Connector.configure(server)` called before `_reload_bots`; `_connect_bot` returns whether it succeeded; `_reload_room` refreshes `log.names`; `_host` refreshes members when a bot joins a running room (found while testing: `members()["c"]` was missing) | `test_a_reload_gives_the_connector_the_new_bots_usernames`; the `FakeConnector` now takes its names from `configure`, as the real one does |
| I3 | resync after a reconnect fetches one page (60); the rest is never emitted and the cursor passes it | cheap, `silent` | **FIX** loop until an empty page | `test_resync_pages_through_everything_the_socket_missed` (130 posts) |
| I4 | `_backfill` takes a page of nothing but system posts for the end of history; the restart replay then starts below the log's floor | cheap, `silent` | **FIX** the connector's `history` pages past such a page itself; the protocol now says an empty page means the platform has nothing more | `test_history_pages_past_a_page_of_system_posts` |
| I5 | a bot that leaves and rejoins gets its old cursor back from `run.saved`; woken for everything said while absent | cheap | **FIX** `_unhost` pops the bot from `run.saved` | `test_a_bot_that_leaves_and_returns_starts_at_the_latest_post` |
| I6 | a DM handle equal to a channel slug (`mm:glin`) raises `ConfigError` out of `_host`: fatal at start, escapes `_on_event`, leaves a reload half applied | cheap (loud) | **FIX** caught in `_host`, noted `[ERROR]`, the second room is not hosted | `test_two_rooms_with_one_handle_are_noted_and_the_daemon_still_starts` |
| I7 | transport errors from Mattermost surface as aiohttp's HTML 500 on `send`, `attachment` and `done`; on `done` the expiry is cancelled before `_backend_failure`, whose notice raises, so the lease stays with no expiry | cheap, the `done` half `silent` | **FIX** `PlatformError` at the connector boundary (4xx stays `RefusedPost`, 403/404 on a file is `KeyError`), 502 with `{"error", "thread"}` on the three handlers, `_notice` swallows it, `_post_reply` uncounts (whether it landed is unknown; the agent reads the room, design §3.3) | `test_a_platform_that_does_not_answer_is_502_and_a_turn_still_ends`, `test_transport_failures_are_platform_errors_and_a_missing_file_a_key_error` |
| I8 | a reload that turns `sweep_interval_seconds` or `classifier_enabled` on changes nothing until a restart; the operator is told "applied" | cheap, `silent` | **FIX** `update()` takes the classifier and starts the sweep timers when the interval was off | `test_a_reload_that_turns_sweeps_and_the_classifier_on_starts_them_in_place` |
| I9 | `reset_pending` is cleared when the wake is issued; a restart before the poll loses it | cheap | **FIX** cleared at the poll, when the agent has it | `test_reset_stays_pending_until_the_agent_polls_the_wake` |
| I10 | a state file without `format` is a `KeyError` at start | cheap (loud) | **FIX** treated like an unreadable file: `[ERROR]` note, the room starts fresh | `test_a_state_file_of_another_format_is_noted_and_the_room_starts_fresh` |

**Composition:** 10 findings, 10 fixed, 0 dropped — an adoption rate of 100%, which the method flags as "triage not running"; the honest reading is that an internal round on fresh code finds the defects the author would have fixed anyway, and the gate that mattered was `cheap` on every one (the largest fix, I1/I2, is ~25 lines across two files). Pattern: 6 of 10 were on the event/reload path of `service.py`, all of the kind "a step before the state was registered or refreshed". The sweep for the pattern is the five service tests above; a Codex finding of the same kind on `service.py` is the signal to look for a missing registration step rather than patch the finding.

**Cost:** reviewer agent ~13 min; triage and fixes ~40 min; one rater.

**Status: open** — Codex round next.

## 2026-11-22 — PR #211 round 1 (Codex on `4ef8675`, `coop/{service,config,daemon}.py`, `coop/connectors/mattermost/{connector,rest}.py`, `coop/doorbell/api.py`, `coop/room/core.py`)

`chain-check.py`: one round, no chain (the detector has nothing to chain yet; rater 2 notes by metadata that F4's branch was written in the internal round's fix c2ce8dd — the first Codex round reviewed that round's fixes, which is expected, not a chain). **Control finding:** none in the batch; rater 2 points out the ledger now holds two rows marked settled (#181 r9 F1 "implemented", #208 r2 G3 "decided") — neither is an observed-outcome settlement of a drop or a rate, so neither is a control. *Agreement in this round is not evidence of independence.*

| # | finding | rater 1 | rater 2 (blind) | verdict | evidence |
|---|---|---|---|---|---|
| F1 | `RoomLog.hosted` never filled by the daemon: every real attachment fetch is a 404 | FIX (cheap, silent) | FIX (cheap) | **FIX** `log.hosted = core.hosted` (1 line) | `test_an_attachment_is_fetched_through_a_hosted_bot` |
| F2 | a connected bot whose credentials change in the file is not reconnected by `reload` | FIX (store token+username per connection, ~10 lines) | FILE (scored 0.0–0.6 h/yr net, payback 3–∞ y; the cheapest form needs `_connected` to become a dict = a named concept, so not `cheap`); a 4-line "restart to apply" note is cheap | **MAKE IT LOUD + FILE** rater 1 conceded on checkable grounds: the fix adds state the connection does not keep today, which is the concept the gate counts. The warning covers the detectable case (username / `token_file` path); a rotated token behind the same path is filed. Reversal: a bot whose sends 504 after a reload that reported success, "reconnect failed" in the log. Revisit 2027-11-22 | `test_a_reload_that_changes_a_connected_bots_credentials_says_a_restart_is_needed` |
| F3 | resync starts from nothing when no live post came since start: one page, the rest lost | FIX (cheap, silent) | FIX (cheap, silent) | **FIX** `history()` seeds the mark from its newest page (3 lines) | `test_resync_starts_after_the_startup_history_when_no_live_post_came_yet` |
| F4 | `PlatformError` and `RefusedPost` both 502; the client says "refused" for an unknown outcome | FIX (cheap) | FIX (cheap); "not just indistinguishable — misattributed" | **FIX** 504 ↔ `LostResponse` (5 lines) | `test_a_platform_that_does_not_answer_is_504_and_a_turn_still_ends`, `test_a_504_is_a_lost_response_the_agent_reads_the_room_for` |
| F5 | a reset wake lacks the unfinished threads' posts, which design §3.4 promises | FIX (cheap) | FIX (cheap); ranked second: in enforce mode the hint says must-reply and the read has nothing | **FIX** payload ∪ `thread_posts` of each unfinished thread (5 lines) | `test_a_reset_wake_serves_the_unfinished_threads_posts_again` |
| F6 | two bots' DMs with one person share `<server>:<counterpart>`; the second is refused at every start | FIX (cheap: `dm:<agent>:<counterpart>`) | FILE (scored 2–6 h/yr, payback 1–3 y; "a naming decision, not a line"; decide before handles are public) | 🙋 **escalated to the owner** — the handle format is operator-visible and no document settles it; while open the finding is FILE. Proposal: `<server>:dm:<agent>:<counterpart>` | thread left open |
| F7 | room parameters not type-checked; `enforce: "false"` is enforce mode | FIX (cheap, silent) | FIX (cheap, silent) | **FIX** annotation check in `_room` (9 lines) | `test_room_parameters_are_type_checked` |
| F8 | channel members: one page of 200 | FIX (cheap) | FIX (cheap) | **FIX** page loop, stopping on a page that adds nothing (8 lines) | `test_members_are_paged_past_two_hundred` |
| F9 | two websockets deliver out of order; the cursor passes a post | MAKE IT LOUD (rate ≈ 0: a whole turn inside ms of skew) | MAKE IT LOUD + FILE; **the stated mechanism cannot occur** (`websocket.py` awaits each frame in turn, so every connection emits P1 before P2 and the FIFO queue admits some P1 first); two other orderings are real and silent (own REST post ahead of an in-flight human post; a resync task interleaving with live frames), window unbounded in the code | **MAKE IT LOUD + FILE** converged: rater 2's trace is checkable and rater 1 was wrong about the mechanism, right about the verdict. `_warn_if_late` logs a post below a cursor or below an issued wake's mark; the fix (a wake that absorbs late arrivals, or a cursor that stops at the lowest undelivered position) is a core invariant, filed with the warning as its measurement. Revisit 2027-11-22 | `test_a_post_arriving_below_a_cursor_or_a_wakes_mark_is_logged` |
| F10 | the pid file is not a lock; two `coop start` in one instant both run, the loser unlinks the winner's file | FIX (cheap) | FIX (gate only; would DROP on economics: 0.3 h × 0.01–0.1/yr) | **FIX** `O_CREAT|O_EXCL`, release only by the holder (20 lines) | `test_the_pid_file_is_taken_once_and_released_only_by_its_holder` |
| F11 | malformed nested state raises past `_load_room_doc` and fails the start | FIX (cheap) | FIX (gate only; low value); the owning layer is the store | **FIX** `_restore` wraps both `load_room` calls, notes, clears the partial load (10 lines); the store-level `check_room_doc` rater 2 names is the clean shape and exceeds the gate — noted, not done | `test_malformed_saved_threads_start_the_room_fresh_loudly` |
| F12 | bad YAML is a traceback from `coop start` | FIX (cheap) | FIX (cheap, in `load()` itself) | **FIX** `load()` raises `ConfigError` (3 lines) | `test_an_int_serves_a_float_parameter_and_a_bad_yaml_file_is_a_config_error` |

**Convergence:** 9 agreements; F9 converged on checkable evidence (rater 1 conceded the mechanism); F2 converged on checkable evidence (rater 1 conceded the gate: the fix needs new connection state); F6 escalated. **Ranking vs Codex:** both raters demote F9 (the cited trigger is unreachable) and F6 (loud, a design gap) from P1; F3 and F7 are silent and rank above their size; Codex's P2 on F2 is right. **Round composition:** 12 findings — 5 of a kind "a state the daemon holds is not kept in step" (F1 log.hosted, F2 credentials, F3 mark, F11 partial load, plus the internal round's six), 3 silent, 4 edge/loud. Not all edge: a second Codex round is requested.

**Cost:** ~1 h 20 min (two raters ~14 min each in parallel, fixes and 14 tests ~45 min, replies ~10 min).

**Status: open** — round 2 requested on `c4f2ebc`; F6 awaits the owner.

## 2026-11-22 — PR #211 round 2 (Codex on `5ba2fef`, `coop/{service,daemon,cli,config}.py`, `coop/room/core.py`, `coop/connectors/normalize.py`)

`chain-check.py`: round 2 has one finding on the previous round's fix in `coop/service.py` and one in `coop/daemon.py` (streak 1 each; no chain). Rater 2 corrected the attribution by blame: the `service.py` line is `old, self.config = …`, touched only to keep `old` — the listener defect (G8) is the original reload's; the `daemon.py` one (G2) is genuinely on round 1's F10. **Control:** none; agreement is not evidence of independence.

| # | finding | rater 1 | rater 2 (blind) | verdict | evidence |
|---|---|---|---|---|---|
| G1 | two agents' token files with one value: the later agent silently wins; one client acts as the other | FIX (cheap, silent) | FIX (cheap; cross-agent identity is a security defect by ADR 0001, reachability not rate) | **FIX** refused in `_set_tokens`, before a reload switches | `test_two_agents_with_one_token_refuse_the_configuration` |
| G2 | the pid file's stale replacement unlinks by pathname; the empty-file window after `O_EXCL`; two starts both pass | FIX (tmp + `os.link`, content-checked unlink, ~8 lines) | DROP the race (≤ 1e-3/yr, ≤ 1e-2 correlated; `hours_per_hit` 1–3; the fix is `flock` with daemon-life state, net ≤ 0) + FIX the docstring, which claims what the code does not do; **the site is wrong**: `is_running()` → `release_lock()` unlinks by pathname before `acquire_lock` is reached, so a patch at `:42` leaves the same window and is a chain's second link | **DROP + docstring** converged: rater 1 conceded on the checkable site (`daemon.py` `is_running`/`release_lock`). Reversal: two `coop daemon running (pid=…)` lines with different pids and no `stopping` between; revisit 2027-11-22. If the owner wants the lock real: flock in one reshape, never a third patch here | docstring in 5ed1dd5 |
| G3 | a classifier result in flight is accepted after a reload disabled or changed the classifier | FIX (capture identity, drop the stale result, ~6 lines) | DROP (0.01–0.5/yr, harm ≈ 0, not silent — `classifier_retry` trace; **a result merely ignored leaves `decisions[post.id]` pending and the post held**, so the fix needs a generation that also resolves it) | **DROP** converged: rater 1's fix shape was wrong in a checkable way. Reversal: a `classifier_decision` trace for a post after an `update` that disabled the classifier; revisit 2027-11-22 | — |
| G4 | a malformed `agents.json` (`[]`, `agents: []`) aborts the start | FIX (cheap) | FIX (cheap; round 1 F11's sibling the sweep missed; the store-level check is the clean layer) | **FIX** one tuple widened | `test_malformed_agents_state_is_noted_and_bios_start_empty` |
| G5 | a saved `cursor: null` makes the floor `None` → one page; posts before it lost silently | FIX (cheap, silent) | FIX (cheap, silent; the only lost-delivery in the batch; Codex's P1 right by consequence, high by rate 0.02–0.3/yr) | **FIX** `exhaust` when any saved cursor is null: page to the start or the cap | `test_a_saved_null_cursor_backfills_to_the_start_of_history` |
| G6 | `add_hosted` then `start()` schedule two periodic timers per bot in every daemon room | FIX (cheap) | FIX, knowingly against the gate's letter (a `started` flag is a named concept; scored it is FILE band) because filing costs more than the 3-line fix and the tax is nil; Codex's "second catch-up right after a turn" does not follow | **FIX** `started` flag | `test_a_bot_hosted_before_start_gets_one_periodic_timer_not_two` |
| G7 | `config tls` replaces the live certificate before validating | FIX (temp files + swap) | FIX (cheaper: validate first, the loader never reads the files; 0 new concepts) | **FIX** reordered, per rater 2 | manual: the existing tls test covers the success path |
| G8 | reload accepts `gateway.listen`/`tls` changes it cannot apply and says "applied"; design line says only `servers` needs a restart | FIX (refuse, like servers) | FIX as a warning note: a refusal would fail every reload after `config tls` until a restart, including `config add agent`'s; **the wrong design sentence outranks the bug** | **FIX** warning + design §4 sentence, per rater 2 | `test_a_reload_that_changes_the_listener_says_a_restart_is_needed` |
| G9 | a person whose username is an agent's name (bot username differs) is taken for the bot: posts never wake it, listed as a bot | FIX (rename `<username>~` in `Names`) | MAKE IT LOUD + FILE (0.06–0.3/yr; the full fix is distinct namespaces end to end ≥ 1 day; the rename is a naming decision like round 1 F6) | **MAKE IT LOUD + FILE** `Names.collides`, warned from `set_members`; the configuration refuses the half it sees. The namespace decision goes to the owner with F6. **Settled 2026-11-22:** the owner chose the platform username as the room's one namespace; PR #212 removes `Names` and the warning, binds agent to username per room (`Gateway.bind`), and replaces the three tests with the crossed cases | `test_a_person_named_like_an_agent_is_a_person_the_bot_is_its_username`, `test_crossed_usernames_wake_by_binding_not_by_name`, `test_the_binding_is_per_room_and_strict_an_unbound_bot_of_an_agents_name_is_not_the_agents` (PR #212) |

**Convergence:** 6 agreements; G2 and G3 converged with rater 1 conceding on checkable evidence (a second unlink site; a fix that would hold a post forever); G9 converged on the gate (the rename is a namespace decision). **Pattern (rater 2):** three findings across two rounds are "reload neither applies X nor says so" (r1 F2 credentials, r2 G3 classifier, G8 listener) — a test enumerating every configuration section against applied / refused / warned on reload would close the class; noted for S3-2. G4 is r1 F11's sibling the sweep missed: a store-level `check_room_doc` / `check_agents_doc` is the layer that owns both. **Ranking vs Codex:** G5 and G1 top (silence; identity), G8's doc half next; G2 and G3 bottom, below their P2. **Round composition:** 9 findings, 2 silent (G1, G5), 2 dropped, 1 loud, the rest cheap loud fixes; a third round is requested, with the owner's convergence rule in view (an all-edge round ends it).

**Cost:** ~1 h (two raters ~10 min in parallel, fixes and 9 tests ~35 min, replies ~10 min).

**Status: open** — round 3 requested on `5ed1dd5`; F6 (DM handle) and the namespace split await the owner.

## 2026-11-22 — PR #211 round 3 (Codex on `710b87f`, `coop/{service,config,cli}.py`, `coop/connectors/mattermost/connector.py`)

`chain-check.py`: round 3 lands on no previous fix (no chain). **Control:** none; agreement is not evidence of independence.

| # | finding | rater 1 | rater 2 (blind) | verdict | evidence |
|---|---|---|---|---|---|
| H1 | one room failing to open (`members()`/`history()`) reports the whole bot not connected; its other rooms unhosted | FIX (cheap: per-room try) | FIX (cheap); **mechanism corrected**: at start the error refused the whole daemon (§14.3.1 breached); at reload it did retry from `_rooms_of`, so "until restart" was false; `members()` is the one REST call left outside the `PlatformError` boundary (sibling of internal I6/I7) | **FIX** `members()` wrapped; `_host` catches `(ConfigError, PlatformError)` and notes `[ERROR] <handle>: not opened` | `test_one_room_that_cannot_open_is_noted_and_the_bots_other_rooms_are_hosted` |
| H2 | `listen: host:not-a-port` passes load, fails inside the daemon | FIX (cheap) | FIX (cheap, edge, lowest value; keep 0 valid) | **FIX** | `test_bad_ports_and_falsy_sections_are_refused` |
| H3 | `config add agent` writes the token before validation | FIX (cheap, reorder) | FIX (cheap, edge; the "overwritten orphan token" half was dead: nothing reads a token without an `agents` entry) | **FIX** reorder | `test_config_add_agent_leaves_no_token_behind_when_the_configuration_is_refused` |
| H4 | a newly hosted bot's cursor is in memory only until the first commit; an unclean stop loses everything since start | FIX (cheap, **silent**) | FIX (cheap, silent, top of the batch by consequence; rate bounded by unclean stops 0.3–3/yr × 0.3–0.8) | **FIX** `_save_room` after `add_hosted` | `test_a_newly_hosted_bots_cursor_is_on_disk_before_any_turn` |
| H5 | a group message (`G`) as `is_dm=True`: every post addresses every hosted bot, the classifier skipped | FIX (cheap) | FIX (cheap; a documented rule — design and the frozen `watcher_rule.py` say 1:1 only — so the one-liner restores it rather than decides; independent of the F6 handle escalation) | **FIX** `is_dm` only for `D` | `test_a_group_message_is_a_room_not_a_one_to_one_dm` |
| H6 | `rooms: []` / `agents: false` silently read as `{}` | FIX (cheap, silent) | FIX (cheap; the `gateway`/`classifier` siblings have the same form) | **FIX** `_mapping` substitutes `{}` for absent/null only, siblings included | `test_bad_ports_and_falsy_sections_are_refused`, `test_port_zero_and_null_sections_are_fine` |
| H7 | a leave from an unhosted room leaves `_rooms_of` stale; a later rule hosts a room the bot left | FIX (cheap) | FIX (cheap); **worse than stated**: the stale entry's `members()` escaped through H1's hole and wedged every later reload until restart | **FIX** `_rooms_of` maintained on every leave; with H1 the residual is a note | `test_a_leave_from_an_unhosted_room_is_forgotten_so_a_later_rule_does_not_host_it` |
| H8 | a reload adding a roster-only bot: members normalised under the old names, the bot reads as absent | FIX (cheap) | FIX (borderline cheap: the refetch must be guarded or it recreates H1; FILE defensible since it self-heals on the next membership event) | **FIX** guarded refetch when the name mapping changed | `test_a_reload_that_adds_a_roster_only_bot_renormalises_the_members` |

**Convergence:** full agreement on all eight; rater 2 corrected the consequence of H1 (half) and H7 (worse), both checkable, and both raters named H1/H7/H8 as one kind — an exception or stale state on the `_host`/reload path, the class the round-1 and round-2 logs told the next rater to look for — fixed as one sweep. **The pattern's sweep:** `test_every_configuration_section_is_applied_refused_or_said_on_reload` walks the sections (room agents, agent token, server url, bot credentials, listener, classifier, guests, the rule itself) against applied / refused / warned; the next reload finding of this kind is a missing row there, not a patch. **One more, outside the eight (rater 2):** `dump_room` writes only `core.persisted`, so a save while a bot is not hosted (it failed to connect, or is hosted later in the same start) drops that bot's saved cursor from disk until it is hosted again; a crash in that window loses it. FILE: `dump_room` could carry the saved bots not in `persisted`. Reversal: a restart note "no saved cursor" for a bot the previous run had saved; revisit 2027-11-22. **Composition:** 8 findings — 1 silent (H4), 3 reachable-now with a real consequence (H1, H5, H7), 4 edge. Not an all-edge round: a confirming round is requested on the fix commit; by the owner's rule it closes the PR if it comes back all edge.

**Cost:** ~55 min (two raters ~10 min in parallel, fixes and 13 tests ~35 min, replies ~8 min).

**Status: open** — round 4 requested on `c5f0f1c`; F6 (DM handle) and the namespace split await the owner.

## 2026-11-22 — PR #211 round 4 (Codex on `539c2ee`, `coop/service.py`, `coop/doorbell/api.py`, `coop/connectors/mattermost/connector.py`)

`chain-check.py`: no chain (one line at `service.py:539 @ 5ba2fef` could not be blamed — moved since — so absence is unproven for it). **Control:** none; agreement is not evidence of independence.

| # | finding | rater 1 | rater 2 (blind) | verdict | evidence |
|---|---|---|---|---|---|
| J1 | archived rooms are hosted | DROP: probed on the docker Mattermost 11.x — `GET /users/{id}/channels` lists no archived channel without `include_deleted`; a post into one is 400 → `RefusedPost`, traced | FIX (cheap, 2-line guard at the one seam; the trigger unverified from the tree; the finding's `RoomLog.archived` half is wrong — a set of thread ids) | **FIX** the guard, with the probe recorded: at connect the case cannot occur; the field now has a reader. A channel archived while hosted is a lifecycle event outside this increment | `test_an_archived_room_is_not_hosted` |
| J2 | a room send without the `thread` key → 500 | FIX (cheap; the "authenticated bad request → 500" lens) | FIX (cheap; in the decoder, which owns "absent = null", not the API) | **FIX** `decode_reply` `.get` | `test_a_room_send_without_the_thread_key_is_a_new_top_level_post_not_500` |
| J3 | `typing` has no call site | deferred (plan S3-2) | DROP for this PR: the PR body's deferral list is the only record in reach (the plan is outside the tree); reversal: S3-2 merges with `typing` uncalled | **deferred** — thread left open as the pointer | — |
| J4 | a room reopened in one daemon reuses wake ids; a late `done`/`send` can land on the new lease | FIX (cheap, per-open seed) | FIX (cheap; carry the closed core's counter forward — strictly monotonic, where a per-open time seed is only probably unique); **silent, reachability unresolved** (the tree ships no poller; design §3.4 serial vs §3.2 one request always open); the PR body's "its `done` then finds no lease, by design" was false after a reopen | **FIX** per rater 2's shape; PR body corrected | `test_a_room_reopened_in_the_same_daemon_never_reuses_a_wake_id` |

**Convergence:** J2–J4 agreed; J1 converged on rater 2's shape with rater 1's probe as the evidence the tree lacked. **Composition:** J1 edge (cannot occur at connect), J2 edge + loud, J3 deferred, J4 silent — by the letter not an all-edge round; **both raters close the PR anyway**: every fix ≤ 3 lines in a function no earlier round touched, no chain in four rounds, and a fifth round would review five lines. **Whole PR:** internal 10 + Codex 12/9/8/4 = 43 findings; 36 fixed, 4 loud + filed (r1 F2 rotated token, r1 F9 late post delivery, r2 G9 namespace, r3 dump_room), 3 dropped (r2 G2 pid race, r2 G3 stale classifier result, — ), 1 deferred (typing); 2 escalated to the owner (r1 F6 DM handle; the namespace split). Adoption 84%. Pattern across rounds: "a state the daemon holds is not kept in step on a hosting/reload path" — 12 of 43; closed by the reload enumeration test in round 3. Rater disagreements: 6 of 33 scored twice, every one on `cheap`'s shape or on the mechanism, none on a number; rater 1 conceded 5 of 6 on checkable evidence, 1 escalated.

**Cost:** ~40 min this round; ~4 h 40 min across the five rounds (reviewer/rater agents ~1 h in parallel, fixes and ~50 tests ~3 h, replies and logs ~40 min).

**Status: closed** — fixes pushed (`8430821`); no further Codex pass; the PR is handed to the owner for merge with two open threads awaiting his decision.

## 2026-11-22 — PR #212 internal round (reviewer agent on `2679209`, lenses: other-namespace lookups, binding consistency, reload, persistence, deleted checks, weakened tests)

Increment: the room core works in the platform's namespace; the agent name is bound to the bot's username per room. Chain-check: not applicable (no Codex round yet). No blind second rater: every finding passed the `cheap` gate, so nothing was scored; recorded as *uncorroborated*.

| # | finding | gate | verdict | fix / test |
|---|---|---|---|---|
| N1 | a second `_host` for a room still inside `_open_room` hosts the bot before `add_room`, so `bind` raises, `add_room` then self-binds the username to a phantom agent, and every later `coop reload` raises `KeyError` after `self.config` was swapped (half-applied) | `cheap`, silent (the bot is hosted but never woken) | **FIX** | `add_room` right after the room is registered, before the first await; `_unhost` resolves the username through the binding. The pre-existing half (a second `_host` taking its cursor from a half-backfilled log) is **FILE** → plan S3-2, decay 2027-02-23, reversal: a `"cursor": null` saved for a bot in a room with posts |
| N2 | the room document keyed `bots` by username, so the restart the username-change refusal asks for silently dropped the renamed bot's cursor, `paused` and unfinished set; the increment definition and design §2 say the cursor files are the agent's | `cheap`, silent | **FIX** | `_save_room` re-keys `bots` by agent name; `_host`/`_unhost`/`_backfill` read by agent; thread controls stay by username (the room's names). `test_a_renamed_bot_keeps_its_cursor_and_pause_across_the_restart_the_refusal_asks_for` |
| N3 | one `FORMAT` constant for both documents: the bump for rooms also refused `agents.json`, whose schema did not change | `cheap` | **FIX** | `AGENTS_FORMAT = 1` apart from the room `FORMAT = 2` |
| N4 | `rules.md` changed but `rules_version` stayed 1, so a running agent never refetches; its old `--to <agent>` goes out as a dead mention | `cheap`, silent for deployed agents (none yet) | **FIX + MAKE IT LOUD** | `RULES_VERSION = 2` beside the document; `test_rules_version_is_bumped_with_the_rules_document` pins (version, sha256 of rules.md) so the next edit fails locally unless both move |
| N5 | `coop status` prints usernames per room while `pause` takes agent names; the gateway's `done_stale` trace carries the agent name where every other `bot=` is a username | `cheap` | **FIX** status (`agent (username)` per room line); **DROP** the trace: no room owns a stale wake, so no username exists to name; a comment says so. Reversal: an operator or a transcript reader misreads `done_stale`'s `bot` as a username; revisit 2027-02-22 |

Test gaps the reviewer named: the bio path with agent ≠ username (added: `test_a_bio_reaches_each_room_under_the_bots_username_and_a_restart_restores_it_there`), the original bug's join path (added: `test_a_person_whose_username_is_an_agents_key_joining_a_room_hosts_nothing`), the `coop status` prefix match (tightened to `a (a_bot)`). Composition: 5 findings, 3 silent, all cheap; 4 fixed, 1 half dropped. Cost: one reviewer agent (~16 min), fixes ~40 minutes.

**Status: closed** — Codex on `2c11882`: "Didn't find any major issues" (👍, no review, no threads). Whole PR: 5 internal findings, 4 fixed, 1 half dropped; no Codex finding. The reload-enumeration row `username: b_bot → b2_bot` and the rename restart test are the new fences.

## 2026-11-23 — PR #212 round 1 (Codex on `e930f34`, `coop/service.py`, `coop/room/gateway.py`, `coop/doorbell/api.py`, `coop/connectors/base.py`)

Chain-check: no chain (first Codex round with findings; the 👍 on `2c11882` is not a round). Blind rater 2 scored from the code without the author's verdicts; no control (no settled entry with an observed outcome yet): agreement uncorroborated.

| # | finding | rater 1 | rater 2 | verdict | fix / test |
|---|---|---|---|---|---|
| K1 | a second join handler binds and starts a room still inside `_open_room`: the bot's cursor comes from the half-fetched log, its first read delivers the whole page (P1) | FIX (the start half is this PR's fix) | FILE: pre-existing on `9c02aab` (same pre-await registration, same `_ensure_started` from `_on_event`); room lifecycle, not namespace; silent; payback ~1.1 yr (0.15–6), P1 inflated (one `reset` recovers) | **FILE** — rater 1 conceded on the `git show 9c02aab` evidence; plan S3-2 entry carries the reversal observation (a `"cursor": null` in a room with posts; a first read older than the join); decay 2027-02-23 (re-score or close with S3-2) . The race test's comment says it pins the binding only |
| K2 | a reload renaming a roster-only bot leaves `members()` under the old name; the bot drops out of the classifier's offer (P2) | FIX, cheap, silent (the refresh I deleted) | FIX, cheap, silent; roster-driven not rename-driven (roles were read under the old roster) | **FIX** `_reload_room` refreshes members when the roster changed | `test_a_reload_renaming_a_roster_only_bot_refreshes_the_members`; the roster-only add test loses its hand-fed refresh |
| K3 | `bind` of an already-bound agent under another username leaves the old side (P2) | FIX, cheap (reject) | FIX as a guard; unreachable today (the daemon unbinds first, `_connected` fixes the username); P2 → P3 | **FIX** `bind` refuses a rebind; the same-username rebind stays idempotent | `test_an_agent_already_bound_in_a_room_is_not_rebound_under_another_username` |
| K4 | standalone `build_app` + default `Gateway` serves the v2 rules as v1 (P2) | FIX, cheap (constant to `coop/rules.py`) | FIX, cheap: the constant to `coop/room/gateway.py` as the `Gateway` default, no new module; no caller today, P2 → P3 | **FIX** rater 2's shape (the default lives where the default is) | the pin test imports it from the gateway |
| K5 | `connectors/base.py` module docstring still describes the agent-name contract (P3) | FIX | FIX; outranks K3/K4 (a wrong rationale the next connector author reads first) | **FIX** docstring | — |

**Convergence:** K2–K5 agreed on verdict and gate; K1 split on "the increment's job" and converged on checkable evidence (`git show 9c02aab:coop/service.py`), rater 1 conceding. **Ranking vs Codex:** K1's P1 inflated (recoverable, pre-existing); K5 above K3/K4. **Composition:** 5 findings, 2 silent (K1 filed, K2 fixed), 3 edge; not an all-edge round. Cost: rater 2 ~12 min, fixes ~25 min.

**Status: closed** — Codex on `4f2b972`: "Didn't find any major issues" (👍, no new threads). Whole PR: internal 5 + Codex 5 = 10 findings; 8 fixed, 1 half dropped (the trace field), 1 filed (K1, room lifecycle, plan S3-2). No chain. Adoption 80%.

## 2026-11-23 — S3-1c internal round (reviewer agent on `0441f0f`, lenses: persisted state, reload, the bot-to-bot filter's cases, `_username` at `_host`, positional `kind`, group `info.name`, coverage)

Increment: a room's handle is built in one place from its kind, every segment a platform name (`<server>:<channel>`, `<server>:dm:<bot username>:<person>`, `<server>:gdm:<digest>`); a direct channel with a bot the server's `bots:` lists is not hosted. Chain-check: not applicable (no PR yet). Blind second rater: yes (agent, findings only). No control finding (no entry settled by observation yet): agreement is *uncorroborated*.

| # | finding | rater 1 | rater 2 | verdict | fix / test |
|---|---|---|---|---|---|
| M1 | `coop reload` after the operator lists a DM's counterpart under `bots:` leaves the open DM hosted; a restart closes it (`_host` filters, `_reload_room` does not) | FIX, cheap, silent | FIX, cheap; not silent (`list` shows it); the consequence half "the counterpart's posts become agent posts" is untrue — `kind_of` already read the platform's `is_bot` | **FIX** rater 2's shape: `_bot_to_bot` predicate, `_reload_room` treats it as no rule ("no rule matches now; closed") | `test_a_direct_channel_between_two_bots_is_not_hosted` gains the unlisted-bot DM `d3` hosted at start and closed by the reload that lists `y_bot` |
| M2 | a group room is only its digest in `list` and in the "no rule hosts" log; a rule for one group means computing sha256 by hand | FIX, cheap | (a) log line FIX, cheap; (b) `name` in `list`: `control.py` is outside the diff, scored payback 1–6 y → FILE by the gate's letter, "would not object to pulling it in; say which" | **FIX both** — (b) pulled in by the author: two lines on the surface the increment names ("what `coop list` shows"); a scope call, not a number call, recorded as such | `control.py` `name`; `cmd_list` prints it after a digest handle; `test_list_prints_a_group_messages_platform_name_after_its_digest_handle` |
| M3 | the new connector test feeds `user_added` for a D channel; Mattermost announces a new direct channel with `direct_added` (rater 2 verified in `channel.go`/`websocket_message.go` on master: channel-scoped, no `user_id`; `group_added` per member), which the connector does not handle, and a `posted` in an unwatched room is dropped without a log — a person's DM opened after start is never answered until a restart | FILE: #211's path, only the test's premise is new | FILE on question (b) only; cheap by the letter (two names in the existing arm) and FIX band by score (payback 0.07–5 y); silent; MAKE IT LOUD costs the same edit as the fix; "make the test honest" | **FILE** → plan S3-2 with the late-post item (decide between the event names and delivery-as-membership; a debug line on the drop at minimum); decay 2027-02-23; reversal: a lab DM opened after start that never answers, or a `direct_added` frame on a hosted bot's socket. The test docstring now names `user_added` as the arm under test and `direct_added` as the platform's frame | — |
| M4 | three operator-facing strings still teach `<server>:<room name>` (`pause -h`, `service.py` docstring, `rule_for`) | FIX, cheap | FIX, cheap; rank `cli.py` first | **FIX** | — |
| M5 | a `rooms:` key in the pre-branch DM form `<server>:<person>` stops matching (INFO names the new handle) | PR body line | DROP as code (unreleased, population 1, loud with the remedy in the line, §14.2); the PR body sentence | **DROP** + PR body line; reversal: a lab config with a key equal to a person's username; revisit 2027-11-23 | — |

**Convergence:** M1/M4 agreed; M2(b) and M3 split on scope only (both raters: not a number call), M2(b) resolved by the author pulling it in, M3 by filing with the test made honest. Rater 2 corrected rater 1's M1 consequence (checkable: `roster.py` `kind_of`). **Composition:** 5 findings, 1 silent (M3, filed), none edge. Cost: reviewer ~10 min, rater 2 ~9 min, fixes ~20 min. Adoption 3.5/5.

**Status: closed** — PR #213, Codex on `af5e972`: "Didn't find any major issues" (👍, no review, no threads). Whole PR: 5 internal findings, 3 fixed (M1, M2 with `list` pulled in, M4), 1 filed (M3, runtime DM discovery, plan S3-2), 1 dropped (M5). No chain. Adoption 60%.

## 2026-11-23 — S3-2a internal round (reviewer agent on `f25f702`, lenses: the `created` null-cursor paths, `RoomRun.ready` waiters and failure, saved null cursors, the posted warning, `user_added` window, reload, coverage; findings probed with the repo's fakes)

Increment: a room the platform creates around a bot is discovered from the platform's own announcement (`direct_added` / `group_added`) and its first post is delivered; a second join during an open waits for the open; a join the platform cannot describe is an error line; a post in a room the connection was never told it joined is a warning. Chain-check: not applicable (no PR yet). Blind second rater: yes (agent, findings only; C901 measured on scratch copies: `_host` 10, `_on_event` 10, `_frame_handler.handle` 8). No control (no settled entry fits): agreement is *uncorroborated*.

| # | finding | rater 1 | rater 2 | verdict | fix / test |
|---|---|---|---|---|---|
| R1 | a `created` join on a DM that already has history and no saved cursor hosts with a null cursor; the page of old posts becomes triggers (a listed DM whose open failed, then reopened — Mattermost re-sends `direct_added`; or a `direct_added` between `add_bot` and the host loop). Silent. The comment "never gets here" was a wrong rationale | FIX, not cheap (new `_listed` map per agent in the service) | FIX, **cheap at the layer that owns it**: `Membership.created` is the connector's promise and `bot.rooms` already knows whether the account was in the room before the frame; `_listed` is a second copy at the wrong layer. Counter-case priced (a new DM whose first open failed loses its first post on the reopen: the `[ERROR] not opened` names the room, §14.4) | **FIX** rater 2's shape — rater 1 conceded on the layer (checkable: `base.py` `Membership.created` docstring, Step 3) | `created = event in ("direct_added", "group_added") and room_id not in bot.rooms` before the `.add`; the connector test feeds the reopen and asserts `created=False` |
| R2 | one `if` on `run.ready`: with two waiters and a failed first open, the second finds the reopened run with `ready` clear and hosts on its empty log (cursor None, the sweep then wakes it on posts older than its join) | FIX, cheap | FIX, cheap (`_host` stays 10) | **FIX** `while` | the race test: three joins, the first open fails and the next stalls, nothing hosted until it completes; fails on `if` (mutation-checked) |
| R3 | the posted warning fires per post in a channel the bot belongs to on another team (`bot.rooms` from the team-filtered list; `my_channels` is cross-team; the fake's `elsewhere`). Loud, wrong, unbounded | FIX, cheap | FIX, cheap; side effects checked (`_any_bot` may pick a member bot for an other-team id: more correct) | **FIX** `bot.rooms` from the unfiltered listing | the warning test posts in `c02` (team2) and asserts one line only |
| R4 | second bot of a group the platform created around two hosted bots gets the first post one sweep (30 s) late: A's `_ensure_started` replays it to A only, B's live copy is a duplicate | FIX, cheap-ish (sweep on a created join into a started room) | **DROP**: not cheap (`_host` → 11 measured; the no-branch shape is one line in `core.add_hosted` but `core.py` is outside the diff); scored 0.3–16 hits/yr × 0.02 h × 1.0, fix 0.5–1 h, tax 0.1/yr → net −0.09 to +0.2 h/yr, midpoint net < 0 | **DROP** — rater 1 conceded on the measurement and the score (a delay, not a loss; nobody remediates). Reversal: a second bot in a new group answering its first mention only after ~30 s, or a trace where a created room's second `hosted_added` is followed by that bot's first `turn_start` from a sweep. Revisit 2027-11-23. If reopened: `core.add_hosted`'s `if self.started:` requests an immediate sweep, not a `_host` branch | — |
| R5 | after `[ERROR] … joined not described`, a join the platform never re-announces (`user_added`, `group_added`) leaves the room's later posts dropped quietly; a reload does not retry (`_rooms_of` not appended); only a restart hosts it. Outcome pre-existing (main: the same aftermath after the websocket's `logger.exception`) | FIX the note ("until a restart"); retry FILE | FILE: not silent (the error line is the MAKE IT LOUD, paid); scored 0.01–1/yr × 0.25 h, fix 1–2 h (a retried set, new state) → payback 20 yr+; lifted from DROP to FILE by §14.3(3) ("retry a degraded section on a later reload rather than requiring a restart"), a SHALL this path misses | **FILE** + the note says "until a restart" → plan S3-2 (a join whose `room()` failed is retried on reload); decay 2027-11-23; reversal: an `[ERROR] … joined not described` line followed by an operator restart to host that room | — |
| R6 | `_ensure_started` and `core.add_hosted` docstrings false for created rooms | FIX | FIX; `core.py` outside the diff ruled in (a parenthetical on the function whose input this diff changed, citing the section it rewrote) | **FIX** | — |
| R7 | `_rooms_of` grows a duplicate `RoomInfo` per DM reopen; `_host_newly_ruled` walks them (harmless) | FIX, cheap | FIX, cheap (one line, `_on_event` stays 10; alone DROP by economics, the gate runs first) | **FIX** replace-by-id on append | — |

Rater 2's addition: the `ready` wait runs on the joining bot's frame task, so that bot's other rooms queue on the socket for the length of the open — one comment line in `_host`, not a fix. **Convergence:** R2/R3/R6/R7 agreed; R1 split on the fix's layer and R4 on the gate/score, both converged on checkable evidence (Step 3; a C901 measurement and a scored range), rater 1 conceding both; R5 agreed on FILE, rater 2's §14.3(3) citation lifts it from DROP. **Composition:** 7 findings, 3 silent (R1, R2 fixed; R4 dropped), none edge. Cost: reviewer ~22 min, rater 2 ~13 min, fixes ~25 min. Adoption 5/7.

## 2026-11-23 — PR #214 round 1 (Codex on `99a7d9a`, `coop/service.py`)

Chain-check: no chain (first Codex round). One finding, the internal round's R4 with a stronger consequence. No second rater: the gate decides, not a score.

| # | finding | rater 1 | verdict | fix / test |
|---|---|---|---|---|
| C1 | the second bot of a group created around two hosted bots is added with a null cursor after the room started; `_ensure_started` returns, its `posted` frame is a duplicate of the backfill, so "an initial post mentioning the later bot may never wake it" (P1) | **R4 reopened on new evidence**: the DROP priced a ≤30 s delay from the periodic sweep, but `_schedule_periodic_sweep` does nothing when `sweep_interval_seconds <= 0` (`core.py:286`), a setting the e2e and test configurations use — there the post is never delivered until another one arrives. Silent. With `core.py` already in the diff (R6), the one-line fix at the owning layer is `cheap`: `add_hosted` into a started room requests a sweep, which also catches a re-hosted bot up from a restored cursor instead of waiting for the periodic one. Ranking vs Codex: P1 → P2 (a delay by default, a loss only with the periodic sweep off) | **FIX** | `core.add_hosted`: `_request_sweep(bot, "hosted")` when started; `test_a_group_created_around_two_bots_delivers_its_first_post_to_both_at_once` (sweeps off; fails without the request, mutation-checked). R4's DROP entry is **settled: bit** — the reversal observation ("the second bot's first `turn_start` from a sweep rather than `addressed`") was reached by reading, not by a report, and the dropped rate missed the sweeps-off configuration |
