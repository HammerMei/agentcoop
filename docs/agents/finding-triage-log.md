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
| **F1** a non-default COOP_HOME another local user pre-created, links or can write into lets them replace `repo/.venv/bin/coop`, which `~/.local/bin/coop` runs (security review) | true, silent; not promise-contradicting (SECURITY.md lists local co-tenants in neither column); split: owner/symlink/others-writable refusal is `cheap` (~8 lines, installer only); the parent walk is not | same split, three ways: (i-a) owner + symlink refusal `cheap`, **FIX**; (i-b) others-writable + 0700 **DROP** — `stat` is not portable, secrets are already 0600, 0700 would change the default dir's mode; (ii) parent walk **FILE** 6 months; scored (ii): hits 1e-5–0.12/yr, hours_per_hit 20–40, tax 0.5 → net ≤ 0 at the midpoint | **FIX** (i-a) and the others-writable half of (i-b): `runtime_dir_is_ours` refuses a symlink, a non-directory, a directory not owned by the invoker, or one writable by others — `find -maxdepth 0 -perm -o+w`, which BSD and GNU find both take, so the portability objection does not hold (concession on checkable evidence, in rater 2's direction on 0700 and rater 1's on the mode check). 0700-on-create **DROP** per rater 2. Parent walk: **DROP** — owner's ruling (2026-09-25): a shared parent without a sticky bit is the host's flaw, and a directory the operator chose under one is the operator's; nothing special is done there. Recorded as the boundary in SECURITY.md (see round 4) |

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
