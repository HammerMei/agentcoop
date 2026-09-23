"""Editing `config.yaml` from the command line: `coop config add/remove/patch`,
`show --raw` and `backends` (coop-keeper design §3.10).

Everything here works on the RAW document — the file as written, templates and
`inherits:` intact — never on the resolved `GatewayConfig`: the keeper edits by
path and the file must come back the way the operator wrote it, one entry
changed. Resolution is validation's job; a merged document is dumped beside the
file and handed to `validate_config()` before anything is written, and the write
itself is the config TUI's atomic save (`EditableConfig.save`: one timestamped
backup under `.config-backups/`, one `os.replace`).

The file digest here is over the file's BYTES, not `config_diff.config_digest`
(which hashes the resolved config and is blind to a `description:` edit): a
write planned against one version of the file must be refused when *anything*
about the file changed, which is what `--if-digest` promises (§3.8).
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import yaml

from .config import _AGENT_TYPE_DEFAULT_COMMAND


class DocumentError(Exception):
    """The file cannot be read as a configuration document at all — missing,
    unreadable, not YAML, or not a mapping. Distinct from a document that reads
    fine and fails validation."""


def file_digest(data: bytes) -> str:
    """SHA-256 (hex) of the file's bytes — the `file_digest` the write commands
    report and `--if-digest` compares against."""
    return hashlib.sha256(data).hexdigest()


def read_document(path: str | Path) -> tuple[dict, str]:
    """The raw document and the digest of the bytes it was read from.

    An empty file is an empty document (an empty deployment is valid).
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DocumentError(f"{path}: could not read: {exc}") from exc
    try:
        document = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise DocumentError(f"{path}: invalid YAML: {exc}") from exc
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise DocumentError(
            f"{path}: must contain a YAML mapping at the top level, "
            f"got {type(document).__name__}"
        )
    return document, file_digest(data)


# ── Backends ──────────────────────────────────────────────────────────────────

def resolve_command(command: str) -> str | None:
    """Where `command` resolves on PATH, or None — the one lookup `add agent`
    refuses on and `config backends` reports with."""
    return shutil.which(command)


def backends() -> dict[str, dict]:
    """Each supported backend type with its default command and whether that
    command is on PATH, keyed by type."""
    out: dict[str, dict] = {}
    for backend_type, command in _AGENT_TYPE_DEFAULT_COMMAND.items():
        path = resolve_command(command)
        out[backend_type] = {"command": command, "found": path is not None, "path": path}
    return out
