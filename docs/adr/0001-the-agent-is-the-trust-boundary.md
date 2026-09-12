# 1. The agent is the trust boundary, not the session

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

AgentCoop puts agents into chat rooms as teammates. A watcher binds a room to an
agent, and one agent may hold many sessions across many rooms and connectors.

Routing defects therefore come in two shapes, and until now the project had no
written rule for telling them apart:

1. A message reaches a session of the agent it was meant for, but not the
   session it was meant for.
2. A message reaches a **different agent** than the one the rules resolve to.

Without a stated boundary, both get argued as "a message leaked", and severity
ends up decided by whoever is in the room. That is the wrong way to rank a
defect, and it also drives design: a boundary we have not named is a boundary
we cannot build to.

## Decision

**An agent is an entity, in the same sense a person is. The trust boundary is
drawn around the agent, not around the session.**

Concretely:

- A message reaching the **wrong session of the same agent** is a correctness
  defect. It is **not** a confidentiality violation.
- A message reaching a **different agent** is a confidentiality violation, and
  is ranked as a security defect.

## Rationale

There is no session-level protection to violate. An agent can write anything it
sees into its own memory at any moment, and carry it into any later session of
its own. Nothing in the gateway can prevent that, and nothing should pretend to:
persistent memory is a feature of the agents AgentCoop hosts, not a leak in it.

Claiming confidentiality between sessions of one agent would therefore be
claiming a protection the system cannot enforce — worse than claiming none,
because someone would design against it.

Between two agents the situation is different in kind. They are separate
entities with separate accounts, separate memory and separate permission
grants. A message crossing that line reaches a party that was never entitled to
it, and no later configuration change can recall it.

## Consequences

- **Severity ranking.** A cross-session routing defect within one agent is
  ranked on its operational cost like any other correctness bug. A cross-agent
  one is ranked as a security defect, where reachability rather than likelihood
  sets its probability.

- **Design.** Do not build session-level isolation machinery: it cannot hold,
  and its presence would imply a guarantee this document denies. Effort belongs
  at the agent boundary — the identity barrier, DM claims, per-agent accounts
  and the permission broker — which is where a violation is actually possible.

- **The identity barrier earns its keep here.** Its job is to stop two
  *different* agents from acquiring an overlapping claim on the same
  conversation. That is the line this ADR says is real.

- **A shared bot account is judged by where its rules point.** Two connectors
  sharing one platform account are not by themselves a violation. If their
  watcher rules resolve to the same agent, an overlap is a correctness defect;
  if they resolve to different agents, the same overlap is a security defect.
  The configuration, not the account, decides which.

- **`docs/requirements.md` makes no session-confidentiality promise**, and must
  not acquire one.
