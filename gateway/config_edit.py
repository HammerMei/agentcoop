"""Editing `config.yaml` from the command line: `coop config add/remove/patch`,
`show --raw` and `backends` (coop-keeper design §3.10).

Everything here works on the RAW document — the file as written, templates and
`inherits:` intact — never on the resolved `GatewayConfig`: coop-keeper edits by
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

import contextlib
import copy
import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .config import _AGENT_TYPE_DEFAULT_COMMAND
from .config_diff import REDACTED

if TYPE_CHECKING:
    from .config_validate import ValidationResult


class DocumentError(Exception):
    """The file cannot be read as a configuration document at all — missing,
    unreadable, not YAML, or not a mapping. Distinct from a document that reads
    fine and fails validation."""


def yaml_error_summary(exc: yaml.YAMLError) -> str:
    """What went wrong and where, WITHOUT the source snippet `str(exc)`
    carries — that snippet is the offending line, which in a configuration
    file may be `password: …`."""
    mark = getattr(exc, "problem_mark", None)
    where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
    problem = getattr(exc, "problem", None) or type(exc).__name__
    return f"{problem}{where}"


def file_digest(data: bytes) -> str:
    """SHA-256 (hex) of the file's bytes — the `file_digest` the write commands
    report and `--if-digest` compares against."""
    return hashlib.sha256(data).hexdigest()


def read_document(path: str | Path) -> tuple[dict, str]:
    """The raw document and the digest of the bytes it was read from.

    An empty file is an empty document, and so is a file that does not exist
    yet: both are the empty deployment, valid (§3.10) and the state a first
    bootstrap starts from (§3.2, §7 test 1). The digest of an absent file is
    the digest of no bytes, so a write planned against "nothing there" is
    refused once something is.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    except OSError as exc:
        raise DocumentError(f"{path}: could not read: {exc}") from exc
    try:
        document = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise DocumentError(f"{path}: invalid YAML: {yaml_error_summary(exc)}") from exc
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


# ── The patch ─────────────────────────────────────────────────────────────────
#
# JSON merge-patch (RFC 7386) over the raw document — a mapping merges into a
# mapping, `null` deletes, anything else replaces — with one addition for the
# two top-level blocks that are LISTS of named entries. A fragment's
# `connectors:`/`watcher_rules:` list is merged into the file's by `name`:
#
#     connectors:
#       - name: bob@mm          # absent -> appended; present -> merged into
#         type: mattermost
#       - name: old-bot          # `op: add` refuses an existing name
#         op: add
#         ...
#       - name: gone-bot         # `op: remove` deletes the entry, and only it
#         op: remove
#
# `op` is the fragment's word, stripped before anything reaches the file. A
# name is matched, never parsed. `agents:` and the template blocks are mappings
# keyed by name in the file itself, so plain merge-patch already addresses one
# entry there and `null` removes it.

NAMED_LISTS: dict[str, str] = {"connectors": "connector", "watcher_rules": "rule"}
OP_KEY = "op"
FROM_FILE_KEY = "from_file"

# `REDACTED` (imported above) is the one sentinel every masked view prints. A
# document holding it must never be written back, so it is refused wherever a
# value enters (§3.10).


class PatchError(Exception):
    """The change cannot be applied as asked — nothing is written. The message
    names the place and never echoes a value (a value may be a credential)."""


def _deepcopy(value: Any) -> Any:
    return copy.deepcopy(value)


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386: `patch` applied to `target`. Returns a new structure."""
    if not isinstance(patch, dict):
        return _deepcopy(patch)
    out = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def merge_named_list(existing: Any, items: list, block: str) -> list:
    """The file's `block` list with the fragment's `items` merged in by name."""
    if existing is None:
        existing = []
    if not isinstance(existing, list):
        raise PatchError(f"'{block}:' in the file is not a list — fix the file first")
    out = [_deepcopy(entry) for entry in existing]

    def index_of(name: str) -> int | None:
        for i, entry in enumerate(out):
            if isinstance(entry, dict) and entry.get("name") == name:
                return i
        return None

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise PatchError(f"{block}[{i}] in the fragment must be a mapping")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise PatchError(
                f"{block}[{i}] in the fragment needs a string 'name' — entries are matched by name"
            )
        op = item.get(OP_KEY)
        entry = {k: v for k, v in item.items() if k != OP_KEY}
        at = index_of(name)
        if op is None:
            if at is None:
                out.append(_deepcopy(entry))
            else:
                out[at] = merge_patch(out[at], entry)
        elif op == "add":
            if at is not None:
                raise PatchError(f"{block}: '{name}' already exists (op: add refuses to replace it)")
            out.append(_deepcopy(entry))
        elif op == "remove":
            if set(entry) != {"name"}:
                raise PatchError(
                    f"{block}: '{name}' with op: remove carries other fields — "
                    "a removal names the entry and nothing else"
                )
            if at is None:
                raise PatchError(f"{block}: no entry named '{name}' to remove")
            del out[at]
        else:
            raise PatchError(f"{block}: '{name}' has unknown op {op!r} — use 'add' or 'remove'")
    return out


def apply_fragment(document: dict, fragment: Any) -> dict:
    """`fragment` (a `--file` document, or one built from `--set`/`--unset`
    or by an `add` command) applied to the raw `document`. Neither input is
    modified. The `***` sentinel is refused here, at the one place every
    write passes, so no command can skip the check (`prepare_fragment` also
    refuses it earlier, before `from_file` values are read)."""
    if not isinstance(fragment, dict):
        raise PatchError(
            f"the fragment must be a YAML mapping at the top level, "
            f"got {type(fragment).__name__}"
        )
    hit = find_sentinel(fragment)
    if hit:
        raise PatchError(f"{hit} is the masked value {REDACTED!r} — a masked view is never written back")
    lists = {k: v for k, v in fragment.items() if k in NAMED_LISTS and isinstance(v, list)}
    merged = merge_patch(document, {k: v for k, v in fragment.items() if k not in lists})
    for block, items in lists.items():
        merged[block] = merge_named_list(document.get(block), items, block)
    return merged


# ── `--set` / `--unset` / `--entry` ───────────────────────────────────────────

def parse_entry(spec: str | None) -> tuple[str, str] | None:
    """`connector:<name>` or `rule:<name>` → (block, name). The name is
    everything after the first ':' and is not parsed further."""
    if spec is None:
        return None
    kind, sep, name = spec.partition(":")
    blocks = {kind_: block for block, kind_ in NAMED_LISTS.items()}
    if not sep or kind not in blocks or not name:
        kinds = " or ".join(f"{k}:<name>" for k in blocks)
        raise PatchError(f"--entry must be {kinds}, got {spec!r}")
    return blocks[kind], name


def parse_set(spec: str) -> tuple[str, object]:
    """`<path>=<value>`; the value is parsed as YAML, so `500` is an integer and
    `"500"` a string. The value is never echoed in an error."""
    path, sep, raw = spec.partition("=")
    if not sep or not path:
        # The argument is not echoed: with no '=' there is no telling which
        # half of it the operator meant as the value.
        raise PatchError("--set needs <path>=<value>")
    if raw.strip() == REDACTED:
        # Not valid YAML (`*` opens an alias), so it would otherwise be refused
        # for the wrong reason; the quoted form parses and is caught later.
        raise PatchError(f"{path} is the masked value {REDACTED!r} — a masked view is never written back")
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PatchError(f"--set {path}: the value is not valid YAML ({type(exc).__name__})") from exc
    return path, value


def _segments(path: str) -> list[str]:
    keys = path.split(".")
    if any(not k for k in keys):
        raise PatchError(f"path {path!r} has an empty segment")
    return keys


def fragment_from_paths(
    sets: list[tuple[str, object]], unsets: list[str], entry: tuple[str, str] | None,
    document: dict | None = None,
) -> dict:
    """One fragment carrying every `--set` and `--unset`, scoped to `--entry`'s
    list entry when given. Merging the paths into one mapping means a later
    `--set` on the same path wins, as a later key in a fragment would.

    `--entry` ADDRESSES an entry: with `document` given, a name that is not
    there is refused rather than appended as a new entry made of the paths —
    a connector born of `--set server.url=…` alone is never what was meant."""
    body: dict = {}
    for path, value in sets:
        # By hand, not via merge_patch: `--set path=null` is an explicit
        # deletion and merge_patch would drop the leaf before it reached the
        # document, reporting success for a change it never made.
        body = _nest_into(body, path, value)
    for path in unsets:
        body = _nest_into(body, path, None)
    if entry is None:
        return body
    block, name = entry
    if document is not None and name not in _raw_named(document, block):
        raise PatchError(f"--entry: no {NAMED_LISTS[block]} named '{name}'")
    return {block: [{"name": name, **body}]}


def _nest_into(body: dict, path: str, value: Any) -> dict:
    """`body` with `value` placed at `path` — including an explicit `None`,
    which merge_patch would treat as a delete and drop. A later path wins
    over an earlier one, as a later key in a fragment would."""
    keys = _segments(path)
    out = _deepcopy(body)
    cursor = out
    for key in keys[:-1]:
        nxt = cursor.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[key] = nxt
        cursor = nxt
    cursor[keys[-1]] = _deepcopy(value)
    return out


# ── Values that come from files, and values that must not come at all ─────────

def read_secret_file(path: str) -> str:
    """The content of a credential file, minus one trailing newline (the one
    `openssl rand -hex 24 > file` leaves). Empty is refused. Errors name the
    path, never the content."""
    try:
        text = Path(path).expanduser().read_text()
    except OSError as exc:
        raise PatchError(f"could not read credential file {path!r}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        # Not an OSError; the message would otherwise be a traceback. The
        # content is not echoed.
        raise PatchError(f"credential file {path!r} is not text ({exc.encoding})") from exc
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if not text:
        raise PatchError(f"credential file {path!r} is empty")
    return text


def resolve_from_file(value: Any, where: str = "") -> Any:
    """Every `{from_file: <path>}` mapping in `value` replaced by that file's
    content. A mapping carrying `from_file` and other keys is refused."""
    if isinstance(value, dict):
        if FROM_FILE_KEY in value:
            if set(value) != {FROM_FILE_KEY} or not isinstance(value[FROM_FILE_KEY], str):
                raise PatchError(
                    f"{where or 'value'}: a from_file mapping holds exactly one string "
                    f"key, {{{FROM_FILE_KEY}: <path>}}"
                )
            return read_secret_file(value[FROM_FILE_KEY])
        return {k: resolve_from_file(v, f"{where}.{k}" if where else k) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_from_file(v, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


def find_sentinel(value: Any, where: str = "") -> str | None:
    """The path of the first value equal to `REDACTED`, or None."""
    if isinstance(value, str):
        return where or "value" if value == REDACTED else None
    if isinstance(value, dict):
        for k, v in value.items():
            hit = find_sentinel(v, f"{where}.{k}" if where else str(k))
            if hit:
                return hit
    if isinstance(value, list):
        for i, v in enumerate(value):
            hit = find_sentinel(v, f"{where}[{i}]")
            if hit:
                return hit
    return None


def prepare_fragment(fragment: Any) -> dict:
    """A fragment as the operator gave it → the fragment to apply: `from_file`
    values read, and the masked sentinel refused wherever it appears (a masked
    view must never be written back)."""
    if not isinstance(fragment, dict):
        raise PatchError(
            f"the fragment must be a YAML mapping at the top level, "
            f"got {type(fragment).__name__}"
        )
    hit = find_sentinel(fragment)
    if hit:
        raise PatchError(f"{hit} is the masked value {REDACTED!r} — a masked view is never written back")
    resolved = resolve_from_file(fragment)
    hit = find_sentinel(resolved)
    if hit:
        raise PatchError(f"{hit} is the masked value {REDACTED!r} — a masked view is never written back")
    return resolved


def read_fragment_file(path: str) -> dict:
    """A `--file` fragment: read as bytes so PyYAML detects the encoding (a
    UTF-16 file from a Windows editor loads; undecodable bytes are a YAML
    error, not a UnicodeDecodeError out of a text read). Errors name the
    file and the position, never a line of it."""
    try:
        with open(path, "rb") as f:
            loaded = yaml.safe_load(f)
    except OSError as exc:
        raise PatchError(f"could not read fragment {path!r}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise PatchError(f"fragment {path!r} is not valid YAML: {yaml_error_summary(exc)}") from exc
    return {} if loaded is None else loaded


def json_safe_keys(value: Any) -> Any:
    """`value` with every non-string mapping key rendered as its string —
    YAML allows `2026-01-01:` or `1:` as a key, JSON does not, and
    `json.dumps(default=str)` converts values only."""
    if isinstance(value, dict):
        return {(k if isinstance(k, str) else str(k)): json_safe_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe_keys(v) for v in value]
    return value


# ── Plan and write ────────────────────────────────────────────────────────────

@dataclass
class EditOutcome:
    """What one write command reports — dry run or apply, accepted or refused.
    `config` is always the REDACTED merged document (or None when there is
    none to show); nothing here ever carries a value read from a file."""

    ok: bool
    dry_run: bool
    config_path: str
    file_digest: str | None = None
    config: dict | None = None
    findings: list[dict] = field(default_factory=list)
    error: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "config_path": self.config_path,
            "file_digest": self.file_digest,
            "config": self.config,
            "findings": self.findings,
        }
        if self.error:
            out["error"] = self.error
        out.update(self.extra)
        return out


def validate_document(document: dict, config_path: Path) -> "ValidationResult":
    """`validate_config()` over `document` dumped beside `config_path` — beside,
    because `working_directory` and `context_inject_files` resolve relative to
    the file's directory, and a temp file elsewhere would validate other paths."""
    from .config_validate import validate_config

    beside = config_path.parent
    if not beside.is_dir():
        # The file does not exist yet and neither does its directory (a first
        # bootstrap). A dry run must not create the directory, so validate in
        # a scratch one: a relative path in the document would resolve against
        # a directory that does not exist either way, and fail either way.
        scratch = tempfile.TemporaryDirectory()
        beside = Path(scratch.name)
    else:
        scratch = None
    handle = tempfile.NamedTemporaryFile(
        "w", dir=beside, prefix=f".{config_path.name}.", suffix=".dry-run", delete=False,
    )
    try:
        with handle:
            yaml.safe_dump(document, handle, sort_keys=False, allow_unicode=True)
        try:
            return validate_config(handle.name)
        except Exception as exc:  # noqa: BLE001 — a boundary: the validator's own crash
            # `validate_config` collects problems rather than raising, but a
            # value of the wrong type can still reach a connector parser that
            # was never written for it (`server.url: []` meets `.rstrip`).
            # Here that is a refusal of the edit, not a traceback.
            raise PatchError(
                f"the validator could not check the result ({type(exc).__name__}: {exc}) "
                "— nothing written"
            ) from exc
    finally:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        if scratch is not None:
            scratch.cleanup()


def _create_file(path: Path, document: dict) -> None:
    """The first write to a config.yaml that does not exist yet: the same
    temp-beside-then-replace as `EditableConfig.save`, minus the backup there
    is nothing to take, and 0600 because the file is about to hold secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        # 0600 from the first byte, exclusively created: the document holds
        # the secrets, and a chmod after the write would leave a window. A
        # stale `.tmp` from an interrupted earlier write is ours to clear.
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            yaml.dump(document, f, sort_keys=False, allow_unicode=True)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def edit_document(
    config_path: str,
    mutate: Callable[[dict], dict],
    *,
    dry_run: bool,
    if_digest: str | None = None,
    extra: Callable[[dict], dict] | None = None,
) -> EditOutcome:
    """The one path every write command takes (§3.10).

    Read the file and its digest; refuse if `if_digest` names another
    version; `mutate` the document; validate the result beside the file —
    refused with the findings when invalid; return it masked on a dry run;
    otherwise check the digest once more and write through the config TUI's
    atomic save (one backup, one replace). `extra(merged)` adds
    command-specific keys to the report (`add` puts its masked entry there).
    """
    from .config_diff import redact_raw_document
    from .config_validate import finding_to_dict
    from .configtool.model import EditableConfig

    path = Path(config_path)
    abspath = os.path.abspath(config_path)
    try:
        document, digest = read_document(path)
    except DocumentError as exc:
        return EditOutcome(ok=False, dry_run=dry_run, config_path=abspath, error=str(exc))
    if if_digest is not None and if_digest != digest:
        return EditOutcome(
            ok=False, dry_run=dry_run, config_path=abspath, file_digest=digest,
            error=f"{config_path} has changed since this edit was planned "
                  f"(file digest is now {digest}) — nothing written; plan it again",
        )
    try:
        merged = mutate(document)
    except PatchError as exc:
        return EditOutcome(ok=False, dry_run=dry_run, config_path=abspath,
                           file_digest=digest, error=str(exc))

    try:
        result = validate_document(merged, path)
    except PatchError as exc:
        return EditOutcome(ok=False, dry_run=dry_run, config_path=abspath,
                           file_digest=digest, error=str(exc))
    findings = [finding_to_dict(f) for f in result.findings if f.severity != "lint"]
    redacted = redact_raw_document(merged)
    more = extra(merged) if extra else {}
    if not result.ok:
        return EditOutcome(
            ok=False, dry_run=dry_run, config_path=abspath, file_digest=digest,
            config=redacted, findings=findings, extra=more,
            error=f"the result does not validate ({len(result.errors)} error(s)) — nothing written",
        )
    if dry_run:
        return EditOutcome(ok=True, dry_run=True, config_path=abspath, file_digest=digest,
                           config=redacted, findings=findings, extra=more)

    # The digest was checked against the operator's plan above; this second
    # look catches a change during this very run. What remains between here
    # and the replace is process-local and inside what §3.8 promises.
    try:
        _, now = read_document(path)
        if now != digest:
            return EditOutcome(
                ok=False, dry_run=False, config_path=abspath, file_digest=digest,
                error=f"{config_path} changed while this edit ran — nothing written; plan it again",
            )
        if path.exists():
            EditableConfig(document=merged, path=path).save()
        else:
            _create_file(path, merged)
        written = file_digest(path.read_bytes())
    except (OSError, ValueError) as exc:
        return EditOutcome(ok=False, dry_run=False, config_path=abspath, file_digest=digest,
                           error=f"could not write {config_path}: {exc}")
    return EditOutcome(ok=True, dry_run=False, config_path=abspath, file_digest=written,
                       config=redacted, findings=findings, extra=more)


# ── `add` and `remove`: fragments built from arguments ────────────────────────

# An agent name becomes a directory name under `agents/user/` (§3.5): one path
# component, lower case, no separators.
AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# The connector types `add connector` knows the server block of — the two
# chat platforms coop-keeper provisions accounts on.
ADDABLE_CONNECTOR_TYPES = ("rocketchat", "mattermost")


def _raw_connectors(document: dict) -> list[dict]:
    block = document.get("connectors") or []
    return [c for c in block if isinstance(c, dict)] if isinstance(block, list) else []


def _raw_named(document: dict, block: str) -> set[str]:
    """The names present in a block, list-shaped or mapping-shaped."""
    value = document.get(block) or {}
    if isinstance(value, list):
        return {e.get("name") for e in value if isinstance(e, dict) and isinstance(e.get("name"), str)}
    if isinstance(value, dict):
        return {k for k in value if isinstance(k, str)}
    return set()


def connector_fragment(
    document: dict, config_path: Path, *, name: str, connector_type: str, server_url: str,
    team: str | None, username: str | None, password_file: str | None, owners: list[str],
    inherits: str | None, credentials_from: str | None,
) -> dict:
    """`config add connector` as an `op: add` fragment. Credentials come from
    `--password-file` or are copied — `server.username` and the password or
    token — from an existing connector (`--credentials-from`, §3.10), never
    from the command line."""
    if name in _raw_named(document, "connectors"):
        raise PatchError(f"connector '{name}' already exists — an existing name is not replaced")
    if connector_type not in ADDABLE_CONNECTOR_TYPES:
        # The `server` block built below is the Rocket.Chat/Mattermost shape;
        # another type (voice, script) would validate with that block ignored.
        raise PatchError(
            f"unknown connector type {connector_type!r} — one of "
            f"{', '.join(ADDABLE_CONNECTOR_TYPES)} (other types are written with 'config patch')"
        )
    server: dict = {"url": server_url}
    if team:
        server["team"] = team
    if credentials_from is not None:
        if username is not None or password_file is not None:
            raise PatchError("--credentials-from copies the username and credential; "
                             "do not also pass --username or --password-file")
        server.update(_credentials_of(document, config_path, credentials_from))
    else:
        if username is None or password_file is None:
            raise PatchError("--username and --password-file are required "
                             "(or --credentials-from <connector>)")
        server["username"] = username
        server["password"] = read_secret_file(password_file)
    entry: dict = {"name": name, "type": connector_type}
    if inherits:
        entry["inherits"] = inherits
    entry["server"] = server
    if owners:
        entry["allowed_users"] = {"owners": list(owners)}
    return {"connectors": [{OP_KEY: "add", **entry}]}


def _credentials_of(document: dict, config_path: Path, source: str) -> dict:
    """`server.username` plus the password or token of connector `source`,
    read from its raw entry resolved against its own `inherits:` template."""
    from .configtool.model import EditableConfig

    raw = next((c for c in _raw_connectors(document) if c.get("name") == source), None)
    if raw is None:
        raise PatchError(f"--credentials-from: no connector named '{source}'")
    try:
        merged = EditableConfig(document=document, path=config_path).merged_entry("connector", raw)
    except (ValueError, FileNotFoundError) as exc:
        raise PatchError(f"--credentials-from: connector '{source}' cannot be resolved: {exc}") from exc
    server = merged.get("server")
    if not isinstance(server, dict):
        raise PatchError(f"--credentials-from: connector '{source}' has no 'server' block to copy")
    out: dict = {}
    if server.get("username"):
        out["username"] = server["username"]
    for key in ("password", "token"):
        if server.get(key):
            out[key] = server[key]
    # A token stands alone (MattermostConfig authenticates with it and no
    # username); a password needs the username it belongs to.
    if not ("token" in out or ("username" in out and "password" in out)):
        raise PatchError(
            f"--credentials-from: connector '{source}' carries neither a token nor a "
            "username with a password to copy"
        )
    return out


def agent_fragment(
    document: dict, *, name: str, agent_type: str, command: str, working_directory: str,
    inherits: str | None,
) -> dict:
    """`config add agent` as a merge-patch fragment. Refuses a name that is
    not one path component, a name already present, and a command that does
    not resolve on PATH — a machine-specific check that belongs at the moment
    of adding, not in `config validate` (§3.10)."""
    if not AGENT_NAME_RE.match(name):
        raise PatchError(
            f"agent name {name!r} is not a single lower-case path component "
            f"(pattern {AGENT_NAME_RE.pattern}) — it becomes a directory name"
        )
    if name in _raw_named(document, "agents"):
        raise PatchError(f"agent '{name}' already exists — an existing name is not replaced")
    if agent_type not in _AGENT_TYPE_DEFAULT_COMMAND:
        # The loader accepts any string here and the failure would come at
        # start, as "Unknown agent type" from backend construction.
        raise PatchError(
            f"unknown agent type {agent_type!r} — one of "
            f"{', '.join(sorted(_AGENT_TYPE_DEFAULT_COMMAND))} (see 'coop config backends')"
        )
    if resolve_command(command) is None:
        raise PatchError(
            f"command {command!r} was not found on PATH — install the backend, or see "
            "'coop config backends'"
        )
    entry: dict = {"type": agent_type}
    if inherits:
        entry["inherits"] = inherits
    entry["command"] = command
    entry["working_directory"] = working_directory
    return {"agents": {name: entry}}


def rule_fragment(
    document: dict, *, name: str, connector: str, agent: str, include: list[str], direct: bool,
    inherits: str | None,
) -> dict:
    """`config add rule` as an `op: add` fragment. `rooms:` is written only when
    an include or `--direct` was given, so a template's `rooms` can apply."""
    if name in _raw_named(document, "watcher_rules"):
        raise PatchError(f"rule '{name}' already exists — an existing name is not replaced")
    entry: dict = {"name": name}
    if inherits:
        entry["inherits"] = inherits
    entry["connector"] = connector
    entry["agent"] = agent
    if include or direct:
        rooms: dict = {}
        if include:
            rooms["include"] = list(include)
        if direct:
            rooms["direct"] = True
        entry["rooms"] = rooms
    return {"watcher_rules": [{OP_KEY: "add", **entry}]}


REMOVABLE = {"connector": "connectors", "agent": "agents", "rule": "watcher_rules"}


def remove_fragment(document: dict, kind: str, name: str) -> dict:
    """`config remove <kind> <name>`: the entry and only it. Whether anything
    still refers to it is validation's finding on the result (§3.10: refused
    while referenced; removal order is the operator's)."""
    block = REMOVABLE[kind]
    if name not in _raw_named(document, block):
        raise PatchError(f"no {kind} named '{name}'")
    if block in NAMED_LISTS:
        return {block: [{"name": name, OP_KEY: "remove"}]}
    return {block: {name: None}}
