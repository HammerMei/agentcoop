"""coop: the remote-agent gateway (``docs/design/remote-agent-gateway.md``).

The daemon watches chat rooms, decides who should speak, and wakes remote agents
through a one-way doorbell. ``coop.room`` is the per-room core ported from
``tools/roomsim``; the doorbell, connectors and CLI arrive in later increments.
The frozen ``gateway`` package is the previous daemon, kept until cutover (§7).
"""
