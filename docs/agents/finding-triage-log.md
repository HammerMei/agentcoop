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
