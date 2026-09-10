"""Filesystem keys and containment checks for per-room artifacts.

Two paths used to be named after a watcher's *display* name —
`RUNTIME_DIR/system-prompts/<name>.md` and
`{working_directory}/.acg-attachments/<name>` — which made that name load-bearing.
Every change to it was destructive: the old file and symlink were orphaned, and a
collision repointed one room's attachment path at another room's files (design §2.3).

**The two artifacts key on different things, because they identify different things:**

* `room_path_key(connector, room_id)` — the attachment workspace. The cache it links to
  is per room and shared by definition, so a rename cannot orphan it.
* `watcher_prompt_key(connector, room_id)` — the durable-instructions
  file. Its contents come from the agent and that watcher's own context files, and two
  watchers may bind different agents to one room, so a room-only key would let the
  second silently overwrite the first. A rename therefore *does* still orphan a prompt
  file; what the digest removes is the collision between rooms. Once watchers are
  created per room the name is derived from the room, and this key becomes
  room-determined too.

Both are **derived** rather than the raw `room_id`, which matters because `room_id` is
external connector data and nothing constrains it to one safe path segment: today's
platforms emit opaque alphanumerics, but a future connector — or a corrupted state file —
could supply `/`, `..`, a leading dash, or something absurdly long, any of which escapes
or collides inside these roots. A fixed-width digest is uniform and safe by
construction; the raw id stays in state and in `list` output.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger("coop.core.paths")

# Hex characters kept from the digest. 32 is 128 bits — far past what collision
# resistance needs for a per-installation room count, and short enough to stay readable
# in a directory listing and well inside every filesystem's component limit.
_KEY_WIDTH = 32


def room_path_key(connector: str, room_id: str) -> str:
    """Return the filesystem key for one room on one connector.

    **A stable digest, not `hash()`.** Python's builtin `hash()` is salted per process,
    so a key derived from it would change on every restart — and nothing would appear
    broken, because the prompt file is rewritten and the symlink re-created on each
    watcher start. It would simply orphan one file and one symlink per boot, forever:
    a silent leak, with green tests. This is precisely the mass-orphaning §2.3 exists to
    stop, so the derivation is a fixed digest and is pinned by a golden-vector test.

    The input is JSON-encoded rather than concatenated, because concatenation is
    ambiguous: `("ab", "c")` and `("a", "bc")` would produce the same bytes and
    therefore the same key for two different rooms.
    """
    canonical = json.dumps([connector, room_id], separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()[:_KEY_WIDTH]


def watcher_prompt_key(connector: str, room_id: str) -> str:
    """Return the filesystem key for one watcher's durable-instructions file.

    Keyed by `(connector, room_id)` — the watcher's identity (§2.3) — and NOT by
    the display handle. It used to take the handle as well, a deviation written
    for the static era, when two watchers with different agents could share one
    room and a room-only key let the later one overwrite the first one's
    identity file. One room now has exactly one watcher (§4.1), and the handle
    follows the room's name (`WatcherLifecycle.observe_room_name`): a key that
    included it moved on every rename, orphaning the file the running session
    was still being given and leaving `reclaim_durable_instructions` to delete a
    file that was never written under that key (owner, 2026-09-02).

    Distinct from `room_path_key` only so the two artifacts cannot be confused
    for one another; the input is the same pair, digested with a different tag.
    """
    canonical = json.dumps(
        ["prompt", connector, room_id], separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()[:_KEY_WIDTH]


def resolve_under(root: Path | str, *parts: str) -> Path:
    """Join `parts` under `root` and assert the result stays inside it.

    The containment check §2.3 asks for after constructing any of these paths. It is
    deliberately performed on the *parent* rather than by resolving the final component:
    the attachment symlink legitimately points outside the working directory, at the
    global cache, so resolving through it and demanding containment would always fail.

    Raises:
        ValueError: If the joined path would land outside `root`.
    """
    root_path = Path(root).resolve()

    # Each part must be one ordinary path component. Checking this first rather than
    # relying on the containment test below, because the containment test alone lets a
    # trailing '..' through: `(root / "..").parents` still contains `root`, so the
    # comparison succeeds even though the path means the directory above. Found by the
    # test for it, which is the argument for having written that test.
    for part in parts:
        if not part or part in (".", ".."):
            raise ValueError(f"Refusing {part!r} as a path component under '{root_path}'")
        if "/" in part or "\\" in part or part != Path(part).name:
            raise ValueError(
                f"Refusing a path component containing a separator: {part!r}"
            )

    # Verified against the resolved form, returned in the caller's own form. Returning
    # the resolved path would canonicalise it — on macOS `/var/...` comes back as
    # `/private/var/...` — which changes what callers log and compare for no benefit,
    # since the check has already established where the path really lands.
    candidate = Path(root).joinpath(*parts)
    resolved = root_path.joinpath(*parts)
    # Resolve the parent (conceptually — it need not exist yet) and re-attach the final
    # name, so a symlinked leaf is judged by where it lives rather than by its target.
    final = resolved.parent.resolve() / resolved.name
    if final != root_path and root_path not in final.parents:
        raise ValueError(
            f"Refusing a path outside its root: {'/'.join(parts)!r} resolves to "
            f"'{final}', which is not under '{root_path}'"
        )
    return candidate


def remove_workspace_link(path: Path | str) -> None:
    """Delete a per-room artifact without following a symlink out of the tree.

    "Define symlink handling before deletion" (§2.3). Only `impl/expiry` consumes this,
    but the definition belongs with the paths it protects: the attachment workspace is
    *made of* symlinks pointing at the global cache, so a reclamation that recursed
    through them would delete the cache — or, if an entry were replaced by a link aimed
    somewhere else, whatever it pointed at.

    A symlink is unlinked (never followed). A real directory is removed with its
    contents, and any symlink *inside* it is unlinked rather than descended into, which
    is what `shutil.rmtree` already does for links it encounters. A missing path is not
    an error.
    """
    p = Path(path)
    if p.is_symlink():
        # is_symlink() before exists(): a dangling link exists() as False but must
        # still be removed, and unlink() must never be replaced by rmtree() here.
        p.unlink()
        logger.debug("Removed attachment symlink %s", p)
        return
    if not p.exists():
        return
    if p.is_dir():
        shutil.rmtree(p)
        logger.debug("Removed attachment directory %s", p)
        return
    p.unlink()
    logger.debug("Removed attachment file %s", p)
