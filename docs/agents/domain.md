# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring
the codebase. AgentCoop is a single-context repo: one `CONTEXT.md` at the root, ADRs under
`docs/adr/`.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root: the glossary. It settles the product's own naming
  (AgentCoop / Coop / gateway) and the domain terms the code and docs use.
- **`docs/adr/`**: the ADRs touching the area you are about to work in. Created lazily —
  if the directory does not exist yet, proceed silently. Do not flag its absence or
  suggest creating it upfront; `/domain-modeling` creates it when the first decision
  actually needs recording.

## Use the glossary's vocabulary

When your output names a domain concept — an issue title, a refactor proposal, a
hypothesis, a test name — use the term as defined in `CONTEXT.md`, and avoid the synonyms
it lists under _Avoid_.

If the concept you need is not in the glossary yet, that is a signal: either you are
inventing language the project does not use (reconsider), or there is a real gap (note it
for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently
overriding:

> _Contradicts ADR-000N (<its title>), but worth reopening because…_
