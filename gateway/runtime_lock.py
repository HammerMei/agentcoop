"""PID-based runtime lock: ensures only one gateway instance runs at a time.

Writes the current process PID to the lock file on acquire() and removes it
on release().  The lock file path is shared with daemon.PID_FILE so the CLI's
``is_running()`` check continues to work without any additional state.

Why a separate module?
  ``daemon.py`` imports ``service.py``, which imports ``control.py``.
  ``control.py`` needs to check the lock on TimeoutError.  A direct import
  of ``daemon.py`` from ``control.py`` would create a circular dependency:
  control → daemon → service → control.  This module breaks the cycle.
"""

from __future__ import annotations

import os

from .paths import RUNTIME_DIR

LOCK_FILE = RUNTIME_DIR / "gateway.pid"


def acquire() -> None:
    """Write the current process PID to the lock file atomically.

    Uses a per-PID temp file + rename so a crash mid-write never leaves a
    partially written lock file that could be misread as a valid PID.
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    # Use a PID-unique temp name so concurrent callers don't collide on the
    # same .tmp file (e.g. two processes racing after is_running() returned False).
    tmp = LOCK_FILE.with_name(f"gateway.{os.getpid()}.pid.tmp")
    try:
        tmp.write_text(str(os.getpid()))
        tmp.replace(LOCK_FILE)  # atomic on POSIX when on the same filesystem
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def release() -> None:
    """Remove the lock file (idempotent — safe to call even if not acquired)."""
    LOCK_FILE.unlink(missing_ok=True)


def locked_pid() -> int | None:
    """Return the PID of the live process holding the lock, or None.

    Returns ``None`` when:
      - The lock file does not exist.
      - The lock file contains an invalid (non-integer) value.
      - The recorded PID refers to a process that no longer exists.

    Returns the PID when:
      - The file exists, contains a valid integer, and the process is alive
        (``os.kill(pid, 0)`` succeeds or raises ``PermissionError``).
    """
    if not LOCK_FILE.exists():
        return None
    try:
        pid = int(LOCK_FILE.read_text().strip())
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)  # signal 0: check liveness without actually signalling
        return pid
    except ProcessLookupError:
        return None  # process is gone — stale lock file
    except PermissionError:
        # Process exists but is owned by a different UID.  Still live.
        return pid
