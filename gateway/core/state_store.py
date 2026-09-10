"""StateStore: thin wrapper around WatcherState persistence.

Encapsulates load/save logic and watermark pulling from the connector,
keeping SessionManager free of persistence details.
"""

from __future__ import annotations

import logging

from .connector import Connector
from .state import WatcherState, load_state, save_state

logger = logging.getLogger("coop.core.state_store")


class StateStore:
    """Loads, saves, and manages WatcherState records on disk.

    Pulls live watermarks from the connector before serializing so that
    the persisted timestamps are always up-to-date.
    """

    def __init__(self, state_name: str, connector: Connector) -> None:
        self._state_name = state_name
        self._connector = connector

    @property
    def state_name(self) -> str:
        """The connector entry these records belong to (``state.<name>.json``)."""
        return self._state_name

    def load(self) -> dict[str, WatcherState]:
        """Load persisted state records, keyed by watcher_name."""
        return {ws.watcher_name: ws for ws in load_state(self._state_name)}

    def merged_view(
        self, states: dict[str, WatcherState]
    ) -> dict[str, WatcherState]:
        """Return the persisted records overlaid by ``states``, which wins.

        The one statement of "what records exist right now": everything on disk,
        plus whatever the caller holds in memory, with the in-memory copy
        authoritative because it is the newer of the two.

        ``save`` writes this minus whatever it was explicitly told to prune;
        ``list_watchers`` reads it and then applies the caller's state filter.
        Neither is the identity, so this is **not** a promise that what an
        operator sees is what survives a restart — it is the narrower and
        load-bearing one that both start from the same set.
        Spelling the merge a second time at either call site is how the two
        drift into disagreeing about whether a watcher exists — a record the
        caller never touched (skipped agent, failed start) is on disk and not in
        memory, which is precisely the case the merge exists for.
        """
        merged = self.load()
        merged.update(states)
        return merged

    def save(
        self, states: dict[str, WatcherState], *, prune: set[str] | None = None
    ) -> None:
        """Merge ``states`` into the persisted records and write the result.

        **This merges rather than replaces.**  ``save_state`` rewrites the whole
        file, so passing it only the caller's dict silently discards every
        record the caller does not happen to hold — and callers routinely hold a
        subset.  ``sync_watchers`` seeds its map only from *configured* watchers,
        so a watcher skipped because its agent was unavailable (a fail-closed
        guard, not a removal) or one whose start raised would have its session
        id, watermark and paused flag erased.  Both are silent, and the session
        id is unrecoverable.

        Merging inverts that failure mode: the worst case becomes a record that
        outlives its config, which ``config_validate`` already warns about,
        instead of a session that cannot be resumed.

        Removal is therefore explicit.  Pass ``prune`` to drop records
        deliberately — currently only ``sync_watchers``, for watchers no longer
        in ``config.yaml``.

        Read-modify-write is safe here because this method is synchronous: it
        contains no ``await``, so no other coroutine can interleave between the
        read and the write.  Keep it that way.

        Watermark reads are best-effort — if the connector is partially torn
        down (e.g. during shutdown), a failure for one room is logged and
        skipped rather than aborting the save.  Only the live records are
        polled; a record read back from disk has no connector-side room state to
        consult.
        """
        for ws in states.values():
            if ws.room_id:
                try:
                    live_ts = self._connector.get_last_processed_ts(ws.room_id)
                    # `is not None`, so a connector that has *cleared* its watermark can
                    # say so — the same distinction `_stop_processor` makes, and it has to
                    # be made in both places or the one that runs first wins. `None` means
                    # "no opinion, this room saw no activity in this run"; an empty string
                    # is an opinion, written by a connector that learned this account is no
                    # longer in the room. Skipping it here leaves the pre-removal mark on
                    # disk for any process that exits without a clean stop.
                    if live_ts is not None:
                        ws.last_processed_ts = live_ts
                except Exception as e:
                    logger.warning(
                        "Could not read live watermark for room '%s': %s — "
                        "persisting last known value instead",
                        ws.room_id, e,
                    )

        # Start from disk so records this process never touched survive.  A missing
        # or corrupt file still loads as empty (see load_state), degrading to the old
        # replace behaviour rather than raising — but a *legacy-format* file now
        # raises LegacyStateError instead, deliberately: it holds real records this
        # merge would otherwise drop on the floor while looking successful.  Nothing
        # catches it here, because a save that silently discarded persisted sessions
        # is the outcome the refusal exists to prevent.
        merged = self.merged_view(states)

        for name in prune or ():
            if merged.pop(name, None) is not None:
                logger.info("Pruned persisted state for watcher '%s'", name)

        save_state(self._state_name, list(merged.values()))
