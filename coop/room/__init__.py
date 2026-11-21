"""The room core: section 5 of ``docs/design/multi-agent-enhancement.md`` as the
remote-agent gateway runs it (``docs/design/remote-agent-gateway.md`` §3.1).

One :class:`~coop.room.core.RoomCore` per room applies the mechanisms to the bots
it hosts, against a :class:`~coop.room.platform.Platform` (the room's posts and
members) and a :class:`~coop.room.clock.Clock`. A turn is a wake the core issues,
a lease that starts when the agent's poll picks it up, the agent's ``read``,
``send`` and ``done`` calls, and the cursor the core saves on a normal ``done``.
"""
