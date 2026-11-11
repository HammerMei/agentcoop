"""roomsim: a deterministic simulator of the multi-agent room design.

It implements section 5 of ``docs/design/multi-agent-enhancement.md`` outside the
gateway runtime (section 8.1): a server-ordered :class:`~tools.roomsim.room.Room`
stands in for the chat platform, one :class:`~tools.roomsim.gateway.SimGateway`
per gateway applies the mechanisms to the bots it hosts, and a
:class:`~tools.roomsim.scenario.Simulation` drives both from scripted human
events on a simulated clock. Bot turns go through a
:class:`~tools.roomsim.bots.BotRunner`: scripted envelopes for mechanism tests
and replays, or the gateway's ``AgentBackend`` adapters for live runs.

The simulator reuses nothing else from the gateway, so the mechanism layer is
written from the specification alone. Where the two disagree, one of them is
wrong (section 8.1).
"""
