"""AttachmentWorkspace: per-watcher symlink management for cached attachments.

Extracted from WatcherLifecycle to keep filesystem preparation separate from
watcher orchestration.  The workspace creates a symlink inside the agent's
working directory that points to the connector's global attachment cache, so
the agent sees attachment files as cwd-local paths (avoiding out-of-project
permission prompts from Claude Code).

Layout::

    {working_directory}/.acg-attachments/{path_key}
        → {global_cache}/{connector_name}/{room_id}/

where path_key is a digest of (connector, room_id) — see gateway/core/paths.py for
why the display name is not used here.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .connector import Attachment, Connector
from .paths import resolve_under

logger = logging.getLogger("coop.core.attachment_workspace")


def localize_attachment_paths(
    attachments: list[Attachment],
    local_base: str | None = None,
) -> list[str]:
    """Remap attachment paths through a per-watcher symlink directory.

    If ``local_base`` is set (e.g. ``{cwd}/.acg-attachments/{path_key}``
    → global cache dir), each attachment's filename is resolved under the
    symlink so the agent sees a cwd-local path.  This avoids out-of-project
    permission prompts from Claude Code.

    Falls back to the original absolute path when no symlink is configured
    or when the remapped path does not exist (download may have been skipped).
    """
    if not local_base:
        return [att.local_path for att in attachments]

    base = Path(local_base)
    result: list[str] = []
    for att in attachments:
        local = base / Path(att.local_path).name
        if local.exists():
            result.append(str(local))
        else:
            result.append(att.local_path)
    return result


class AttachmentWorkspace:
    """Manages per-room attachment symlinks inside agent working directories.

    Usage::

        from gateway.core.paths import room_path_key

        workspace = AttachmentWorkspace(connector)
        local_base = workspace.setup(
            room_path_key(connector_name, room_id), room_id, working_directory
        )
        # local_base is either a str path or None if attachments are unsupported

    The example passes the key explicitly because passing a watcher name instead would
    *work* — it satisfies `resolve_under` — and silently restore per-watcher links, so the
    misuse produces no error. See gateway/core/paths.py for which key belongs where.
    """

    def __init__(self, connector: Connector) -> None:
        self._connector = connector

    def setup(
        self,
        path_key: str,
        room_id: str,
        working_directory: str,
    ) -> str | None:
        """Create or update a per-room symlink for cached attachments.

        ``path_key`` identifies the ROOM — pass `room_path_key(...)`. The watcher's
        display name used to be this path component, which made it load-bearing: renaming
        a room orphaned the old link, and a collision pointed one room's attachment path
        at another room's files (§2.3). Which key belongs to which artifact is documented
        once, in gateway/core/paths.py; this docstring deliberately does not restate it.
        The cost is that a directory listing no longer reads as room names, which `list`
        offsets by showing both.

        Returns:
            Absolute path to the symlink directory (str) if the connector
            supports attachment caching, or ``None`` otherwise.
        """
        cache_dir = self._connector.attachment_cache_dir(room_id)
        if not cache_dir:
            return None

        acg_dir = Path(working_directory) / ".acg-attachments"
        acg_dir.mkdir(parents=True, exist_ok=True)

        # Containment is checked on the link's *location*, never by resolving through
        # it: this link points outside working_directory on purpose, at the global
        # cache, so demanding that its target stay inside would always fail (§2.3).
        link = resolve_under(acg_dir, path_key)
        cache_path = Path(cache_dir)
        cache_path.mkdir(parents=True, exist_ok=True)

        # Deliberately a plain check-then-act, with no synchronisation.
        #
        # Two watchers share this link only when they share a connector AND a room — the
        # same bot account in the same room, which CLAUDE.md records as a degenerate case
        # with no practical use beyond framework-level testing. In that configuration two
        # concurrent starts can race here and one raises, failing that watcher's startup
        # loudly.
        #
        # That is accepted rather than fixed. `impl/uniqueness` (design §4.1) makes one
        # watcher per room the rule, and `impl/manager` moves the lifecycle lock to
        # `(connector, room_id)` — the same granularity as this link — so the race
        # disappears with its precondition rather than needing a guard. Tolerating the
        # collision now would mean code whose only job is to survive a state that is about
        # to become impossible, and which someone would then have to remember to remove.
        if link.is_symlink():
            if link.resolve() != cache_path.resolve():
                link.unlink()
                link.symlink_to(cache_path)
                logger.info("Updated attachment symlink: %s → %s", link, cache_path)
        elif link.exists():
            logger.warning(
                "Attachment path %s exists but is not a symlink — skipping", link
            )
            return None
        else:
            link.symlink_to(cache_path)
            logger.info("Created attachment symlink: %s → %s", link, cache_path)

        return str(link)

    def reclaim(self, path_key: str, room_id: str, working_directory: str) -> None:
        """Remove the per-room symlink and the cached attachment directory.

        Expiry's half of `setup` (§2.5, "expiry reclaims everything"). Symlink
        handling is defined *before* deletion, per the design's own requirement:

        * The link is **unlinked, never followed** — `unlink` on the link
          itself; its target is not resolved for the removal.
        * The cache directory is removed only when it is a real directory. A
          cache path that is itself a symlink is unlinked, not descended into:
          `rmtree` through a link would delete whatever tree the link points
          at, which is exactly the escape the design forbids. Links *inside*
          the tree are removed as links by `rmtree`'s own contract.

        Best-effort and idempotent: a missing link or directory is success,
        and a filesystem error logs rather than blocking the expiry — the
        record's reclamation must not be held hostage by a stale mount.
        """
        try:
            acg_dir = Path(working_directory) / ".acg-attachments"
            link = resolve_under(acg_dir, path_key)
            if link.is_symlink():
                link.unlink(missing_ok=True)
                logger.info("Removed attachment symlink %s", link)
        except Exception as e:
            logger.warning("Could not remove attachment symlink for %s: %s",
                           path_key, e)

        cache_dir = self._connector.attachment_cache_dir(room_id)
        if not cache_dir:
            return
        try:
            cache_path = Path(cache_dir)
            if cache_path.is_symlink():
                cache_path.unlink(missing_ok=True)
            elif cache_path.is_dir():
                import shutil

                shutil.rmtree(cache_path)
                logger.info("Removed attachment cache %s", cache_path)
        except Exception as e:
            logger.warning("Could not remove attachment cache for room %s: %s",
                           room_id, e)
