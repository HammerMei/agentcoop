"""The agent's side of the doorbell (design §3.3): the HTTP client and the ``coop
read / send / done / bio / attachment`` commands.

It is kept apart from the gateway so it can one day install on its own: of the
gateway's code it imports only the data model (``coop.room.model``), the platform
protocol's refusal (``coop.room.platform``), the hint renderer (``coop.room.prompt``),
the wire format (``coop.wire``), the errors and the fingerprint helpers; never the
room core, the gateway, the API or aiohttp. ``tests/coop/test_agent_fence.py`` holds
that line.
"""
