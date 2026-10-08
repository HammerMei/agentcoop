# Finding triage — AgentCoop's constants

The *method* is not here. It lives in the `finding-triage` skill and is the same for
every project. This file holds only what is AgentCoop's: the discount ladder, the
decision bands, the anchors to score against, and which reviewer to read.

## Reviewer

`chatgpt-codex-connector` — Codex, requested explicitly with a `@codex review`
comment. See `CLAUDE.md` § *Code Review with Codex* for how to confirm a review
actually arrived.

## `discount` — how much the operator has already priced in

A multiplier on the harm, never below 0.3: it nudges a borderline call, it never
decides one.

**These three numbers are not derived from anything.** The requirements clauses each row
cites establish that an operation is best effort; they do not say whether that is worth
0.56 or 0.32. The ladder is a starting guess, to be calibrated once the triage log has
settled entries. Treat it accordingly: **a verdict that flips on `discount` alone is a
disagreement**, and belongs in the skill's Step 4 rather than in a conclusion.

| operation | `discount` | grounded in |
|---|---|---|
| message handling, `status`, `list`, `config show` | **1.0** | the promised path |
| `config reload`, `config migrate-env`, TUI save | **0.56** | §14.2 — a write carries its own risk; operators run it off-hours |
| `coop upgrade`, data migration, a forked or unsupported setup | **0.32** | §14.1 — upgrades are best effort, back up first |

Section numbers are `docs/requirements.md` § *Operational Commitments*. **Cite the
clause.** If no clause covers the case the discount is 1.0 — the absence of a promise
is not a discount.

## Complexity limit for the `cheap` gate

**McCabe complexity ≤ 10 on the touched function, after the fix, as ruff's `C901`
computes it.** Measure with

```bash
uv run ruff check --select C901 --ignore-noqa <file>
```

which prints each over-limit function with its number (`--ignore-noqa` so a function
that carries a ratchet marker is still reported). The number to compare is the
function's, not the file's. A fix that pushes a function past 10, or lands in one
already past it, is not `cheap` — score it, and charge the complexity as
`tax_hours_per_year`.

**One metric, on purpose.** `radon cc` counts boolean operators and comprehensions
as well as branches, so it reads higher than `C901` on the same function (the two
handlers below: radon 46/43, `C901` 32/29; a 12-term `and` passes `C901` at 10 and
radon counts it 12). A gate measured one way and a backstop enforcing the other would
let CI pass what the gate refuses, so the gate uses the backstop's metric. radon is a
fine second opinion when deciding whether a branch *or a condition* is the thing to
simplify; it is not the number the gate compares.

Why this limit and not a diff size: on 2026-10-07 the two connector message handlers
measured `_on_raw_ddp_message` 32 and `_on_posted_event` 29, and they are where the
`cheap` fixes of PR #121 (27 rounds) and PR #181 (9 rounds) landed. Each of those
fixes passed the gate as written then. The cheapest fix for PR #195's one finding
would have been one more `if` in the 32.

`ruff`'s `C901` (max-complexity 10) is the CI backstop, **ratcheted per function**:
the 85 functions in `gateway/` and `tests/` already over the limit when the rule was
enabled carry `# noqa: C901` on their def line (added with `ruff --add-noqa`), so the
rule bites on every function that is still under it — including a new one in a file
whose neighbours are marked. Remove a marker when its function is brought under 10;
never add one. `scripts/` is outside CI's lint scope (`ci.yml`, `Makefile`: `gateway/
tests/`) and is not ratcheted — probe scripts are not shipped code. What the backstop
cannot see is growth *inside* a marked function — the hundredth `if` in the 32 — and
that is exactly the case the gate exists for.

### Failing the complexity condition is not a DROP

The condition takes a fix out of the `cheap` lane; it does not decide the finding.
A bug in a function that is already over the limit is still a bug, and the functions
over the limit are the message handlers — the code where a dropped fix costs the most.
When the complexity condition is the only part of `cheap` that fails:

1. **Look for a fix that adds no complexity.** Extract the condition into a named
   predicate (`is_mention()` is the house pattern), reuse a helper that already
   exists, or make the check at the layer that owns the concern — which Step 3 asks
   for anyway. If such a shape exists, the fix is `cheap` again. Most are.
2. **Otherwise, the refactor is its own item.** Bringing the function under the limit
   is scored as a separate finding — the "pattern, not findings" sweep — with its own
   `fix_hours` and its own payback. **Its cost is never charged to the bug.** Decide
   the refactor first; then the fix: on a refactored function it is `cheap`; on one
   left as it is, score the fix on its own harm with the added branch as its tax.
   "The function is already a mess" is a reason to file the refactor, not to drop
   the fix.

## Revisit window — 12 months

Every DROP and FILE carries a **reversal observation** (one sentence, greppable: a log
line, an issue keyword, a file and line) and a **revisit date twelve months out**, kept
in the ledger at the top of `finding-triage-log.md`. At the date: observation seen →
`settled: bit`; not seen → `settled: held`. With this project's population
(§14.5, a handful of operators), a clean year is `hits_per_year ≤ 3/N` by the rule of
three — for three operators, at most one a year, which is the magnitude the DROP anchor
assumes. So `held` is a measurement, not a shrug. Entries flagged `silent` settle as
`held (unobservable)` and are not control material.

## Triage cost — recorded, not scored

Each log entry records **minutes, rounds and raters**. Tokens may be noted and are not
compared across entries: they vary with the model more than with the finding. None of
it enters the ROI: a round whose output is a *rule* — PR #186 round 4 wrote the host
co-tenant boundary into SECURITY.md and closed that class — is worth more than the
findings it dropped, and a per-finding cost ratio would have called it waste. The
record exists for the trend, and for the one question it can answer: whether a fast
lane is needed for findings whose harm and fix are both far below the cost of deciding.

## Decision bands

| `payback_years` | verdict |
|---|---|
| under 1 | **FIX** |
| 1 to 5 | **FILE**, with a decay date |
| over 5, or `net_annual_saving` ≤ 0 | **DROP**, with a written reason |

Five years is deliberately short. AgentCoop is young, and a fix that takes longer than
that to repay is being justified by a future the project has not earned yet.

## `hits_per_year` — the user base

AgentCoop is self-hosted and run by a small number of operators (`requirements.md`
§14.5), and `hits_per_year` counts across all of them. The same defect is worth far
less here than in software with thousands of installs, so the bar for fixing is higher
— documented, rather than lazy.

**The `.env` migration path has a population near zero.** v0 → v1 is a reinstall, not an
in-place migration (`docs/migration-v1.md`), and a v1 install never has a `.env` to
migrate — so `migrate_env_to_config()`, `has_pending_migration()` and the preflight's
handling of them can be reached only by an install that predates the rename. A finding
on that path is **declined unless it is `silent` and one line**; do not spend a review
round hardening it. The logic itself is a candidate for removal once the Phase 1
onboarding rework lands.

For a security finding, use **reachability** rather than likelihood. What counts as a
security finding is settled by `docs/adr/0001-the-agent-is-the-trust-boundary.md`: a
message reaching the wrong session of the *same* agent is a correctness defect; one
reaching a *different* agent is a security defect.

## Gate examples, from this repository

- **`cheap`** — *removed-agent processors are not drained before the backend stops.*
  The fix is `changed_agents | removed_agents`: one expression, no new concept. Fixed
  without scoring.
- **`cannot-occur` / unreachable** — a review claimed an apply could fail because
  "the first `replace_rules()` fails". Traced: `replace_rules()` is a list assignment
  in both layers (`session_manager.py`, `watcher_manager.py`) and cannot raise. The
  cited trigger does not exist. *Trace and record the chain, as here — never assume.*
- **`silent`** — *a digest collision reports an unapplied config as in sync.* No error,
  no log, `config show` looks right. It may still be dropped, but not quietly.

## Anchors — score by comparison, not in the abstract

Three cases reaching three verdicts by three different routes. Ask "is this worse or
lighter than the FILE anchor?" rather than "what is the rate?".

### FIX — driven by a large `hours_per_hit` and by silence

*A state file that cannot be read, with the daemon booting anyway* (the behaviour
before #143): a removed connector's state file is never opened again, so the daemon
starts successfully while silently abandoning every session in it.

`hours_per_hit 10` (notice it, work out which rooms lost sessions, reset each) ·
`hits_per_year 3` (connector rename or removal with state present — and silent, so it
accumulates) · `discount 1.0` (boot path) · `fix_hours 10` · `tax 0`
→ 30 h/year, **payback 0.3 years.**

### FILE — a race, decided by correlation rather than by the window

*Shutdown racing an in-flight reload*: a SIGTERM during the apply can tear a state
write. The independent estimate is negligible — a five-second window against a handful
of stops a year is once in millennia. But the events are **correlated**: an operator
who thinks a reload has hung presses Ctrl-C, and a deploy script may reload then
restart. The correlated path is what sets the rate.

`hours_per_hit 3` · `hits_per_year 1` (the correlated path) · `discount 0.56` ·
`fix_hours 3` · `tax 0.1`
→ 1.7 h/year, net 1.6, **payback 1.9 years.**

### DROP — killed by the economics, not by a gate

*A canonical digest collision between an integer key and a string key that spells its
own type tag.* YAML permits both, so it is expressible and does **not** fail
`cannot-occur`; it is scored, and the scoring kills it. It was the last link of a
chain that ran for six review rounds, and typed-canonicalisation is maintained forever.

`hours_per_hit 0.1` (`config show` shows a stale digest; reload still restarts the
connector) · `hits_per_year 0.01` · `discount 1.0` · `fix_hours 3` · `tax 0.5`
→ 0.001 h/year against a 0.5 h/year tax → **`net_annual_saving` is negative. No horizon
repays it.**

## What the anchors teach

- **FIX** — silence lets the rate accumulate; with a large per-hit cost it genuinely repays.
- **FILE** — for a race, ask what correlates the two events; that, not the window, sets the rate.
- **DROP** — a fix whose ongoing tax exceeds the harm is never worth doing, however real the defect.

## A note on using these in a blind test

These anchors name real findings and state their verdicts. Anyone — or any agent — who
reads this file before triaging those same findings has been told the answer. That is
the price of concrete anchors and it is worth paying; the fix is to choose *different*
findings when measuring agreement, not to blur the anchors.
