"""Claude hook settings file lifecycle and backend wiring.

Extracted from ClaudePermissionBroker so the broker stays focused on
approval transport and role-based tool filtering.  This adapter owns:

  - Writing the temporary Claude settings JSON with the hook URL.
  - Patching ``backend.settings_path`` so ``claude -p`` picks up the hook
    automatically via ``--settings``.
  - Cleaning up (removing the temp file, restoring the original path).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapter import ClaudeBackend

logger = logging.getLogger("coop.agents.claude.settings")

_HOST = "127.0.0.1"


class ClaudeSettingsAdapter:
    """Manages Claude hook settings file lifecycle and backend wiring.

    Usage::

        adapter = ClaudeSettingsAdapter(backend=my_backend)
        path = adapter.write_hook_config(port=12345, timeout_seconds=300)
        adapter.patch_backend()

        # ... broker runs ...

        adapter.cleanup()
    """

    # Match all tools so the hook sees every call.
    _APPROVAL_MATCHER = ".*"

    def __init__(self, backend: "ClaudeBackend | None" = None) -> None:
        self._backend = backend
        self._settings_path: str = ""
        self._original_backend_settings_path: str = ""

    @property
    def settings_path(self) -> str:
        """Absolute path to the generated Claude settings JSON file.

        Empty string if ``write_hook_config()`` has not been called yet.
        """
        return self._settings_path

    def write_hook_config(self, port: int, timeout_seconds: int) -> str:
        """Write the hook settings JSON and return the absolute file path.

        Creates a temporary file with restricted permissions (0600).
        """
        settings = {
            "hooks": {
                "PreToolUse": [{
                    "matcher": self._APPROVAL_MATCHER,
                    "hooks": [{
                        "type": "http",
                        "url": f"http://{_HOST}:{port}/hook",
                        "timeout": timeout_seconds + 10,
                    }],
                }]
            }
        }
        fd, path = tempfile.mkstemp(suffix=".json", prefix="acg-claude-settings-")
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f)
        logger.info("Claude settings file written: %s", path)
        self._settings_path = path
        return path

    def patch_backend(self) -> None:
        """Patch ``backend.settings_path`` so ``claude -p`` invocations pick up the hook.

        No-op if no backend was provided.  Saves the original value for
        restoration in ``cleanup()``.
        """
        if self._backend is None:
            return
        self._original_backend_settings_path = getattr(
            self._backend, "settings_path", ""
        )
        self._backend.settings_path = self._settings_path  # type: ignore[attr-defined]
        logger.debug(
            "Patched ClaudeBackend.settings_path → %s", self._settings_path
        )

    def cleanup(self) -> None:
        """Remove the settings file and restore the original backend path.

        Idempotent — safe to call multiple times.
        """
        if self._settings_path:
            Path(self._settings_path).unlink(missing_ok=True)
            logger.debug("Removed Claude settings file: %s", self._settings_path)
        # Restore original settings_path
        if self._backend is not None:
            self._backend.settings_path = self._original_backend_settings_path  # type: ignore[attr-defined]
        self._settings_path = ""
