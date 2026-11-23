"""The connector layer: one :class:`~coop.connectors.base.Connector` per chat server,
feeding :class:`~coop.connectors.roomlog.RoomLog`, the per-room ``Platform`` the room
core reads (design §3.1, §5)."""
