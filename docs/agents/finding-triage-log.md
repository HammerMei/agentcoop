# Finding triage — log

Step 6 of the `finding-triage` skill: the five quantities and the verdict for every
finding decided, the one least sure of as a range, and every control result,
convergence and escalation. Entries are marked **settled** once their outcome is
observed — the drop that bit or did not, the fix that saved what it promised or did not.
Settled entries are the only legitimate source of a control finding for a blind second
rater; until one exists, every round runs without a control and says so.

Constants and anchors: `finding-triage.md` in this directory.

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
