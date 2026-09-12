"""One-time migration: fold a `.env`-backed secret directly into config.yaml
as a literal value, then remove `.env`.

Context (docs/design/config-tool.md decision 6 revisited): `.env` was
originally the config tool's recommended place for connector secrets, with
`${VAR}`/`$VAR` references in config.yaml resolved against it at load time
(`gateway/config.py`'s `load_dotenv` + `_expand_env_vars`). On reflection,
splitting one deployment's state across two files bought less than it cost:
`config.yaml` gets the same `chmod 0600` treatment `.env` always had, so the
only real remaining benefit — a config.yaml that's safe to share/back up
without also handing over secrets — is better served by a dedicated export/
redaction path than by permanently maintaining two files as the norm.

Decision: enforce a real migration rather than supporting both forms
indefinitely (a nag people can ignore isn't enforcement). This module holds
the actual migration LOGIC as one function, callable from two TRIGGERS:
`gateway/daemon.py`'s `start_daemon()` (automatic, on every server start —
becomes a permanent no-op the moment `.env` is gone) and a standalone CLI
command (`coop config migrate-env`) for a manual/dry-run/
Docker-entrypoint invocation. Same function either way — no logic
duplicated between the two call sites.

Symlink safety (Docker bind-mount deployments): `EditableConfig.save()`
writes via `os.replace()`, which replaces a destination that is itself a
symlink rather than writing through it — relevant because the Docker
entrypoint's Mode 1 (`docker/entrypoint.coop.sh`) symlinks the runtime
`config.yaml`/`.env` to a bind-mounted host directory. Code-review finding:
`gateway/daemon.py`'s automatic trigger resolved `config_path` before
calling this function, so it was accidentally safe — but the standalone
`coop config migrate-env` CLI command (the one Docker users
are explicitly told to run manually) did NOT resolve first, so it would
silently "migrate" the container-local symlinks only, never touch the real
host files, and report false success. Fixed by resolving `config_path`
unconditionally as the very first step below, so every caller gets the
same guarantee regardless of whether it remembered to resolve first.

Safety model: backup -> migrate -> validate -> fail-closed. Reuses
`EditableConfig.load()`/`.save()` for the raw load and the validate-before-
write/backup/atomic-replace machinery (never reimplemented) — a migration
that would fail `validate_config()` never touches the real config.yaml, and
`.env` is only ever removed AFTER the migrated config.yaml has been saved
successfully. `gateway/config.py`'s own `load_dotenv`/`_expand_env_vars` are
reused for resolution too, so the literal values this migration writes are
exactly what the daemon would already have resolved at real startup — not a
reimplementation that could quietly diverge.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .config import ENV_VAR_REF_RE, _expand_env_vars
from .configtool.model import EditableConfig


@dataclass
class MigrationResult:
    """What `migrate_env_to_config()` actually did, for the caller (daemon
    startup / CLI command) to report to the user."""

    migrated: bool
    ref_count: int = 0
    env_backup_path: Path | None = None


def _count_env_refs(obj: object) -> int:
    """Total `$VAR`/`${VAR}` occurrences anywhere in the raw document's
    string values, recursively — used only to report "N secret(s) migrated"
    to the user; not load-bearing for the migration itself."""
    if isinstance(obj, str):
        return len(ENV_VAR_REF_RE.findall(obj))
    if isinstance(obj, dict):
        return sum(_count_env_refs(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_env_refs(item) for item in obj)
    return 0


def has_pending_migration(config_path: str | Path) -> bool:
    """Whether a `.env` still sits beside `config_path` — i.e. `migrate_env_to_config`
    would do work rather than no-op.

    The one predicate both the CLI preflight and this module's own migration
    answer with, so the two can never diverge: it resolves `config_path` first
    and looks beside the RESOLVED path, exactly as the migration does (a
    symlinked `config.yaml` otherwise puts the CLI's answer and the daemon's in
    different directories).

    Never raises. A missing `config_path` is "nothing pending" — reporting that
    file's absence belongs to whoever tries to read it, not to this question.
    """
    try:
        resolved = Path(config_path).resolve()
    except OSError:
        return False
    return (resolved.parent / ".env").exists()


def migrate_env_to_config(config_path: str | Path) -> MigrationResult:
    """If a `.env` file sits next to `config_path`, resolve every `$VAR`/
    `${VAR}` reference in the raw config document to its literal value,
    save the result (validated, backed up, atomically — see module
    docstring), then move `.env` into `.config-backups/` (created as part
    of that save) so nothing is silently deleted.

    No-op (`MigrationResult(migrated=False)`) if `.env` doesn't exist —
    this is what makes the migration permanently self-limiting: once it has
    run once for a given config directory, every later call (e.g. every
    subsequent daemon start) is a cheap no-op forever. Checked BEFORE
    loading/parsing config.yaml at all (a plain `Path.exists()` stat) —
    code-review finding: this used to load and fully YAML-parse config.yaml
    first and check `.env` second, meaning every daemon start double-parsed
    config.yaml (once here, once moments later via `GatewayConfig.
    from_file()`) for a check that resolves to "nothing to do" the
    overwhelming majority of the time.

    Raises `FileNotFoundError` if `config_path` itself doesn't exist —
    checked explicitly and unconditionally, REGARDLESS of whether `.env`
    exists (a second code-review finding, on the fix above: checking
    `.env` before confirming `config_path` exists let a missing config with
    no `.env` alongside it slip through as a silent, false "nothing to
    migrate" no-op instead of a clear error — misleading standalone via the
    CLI, `coop config migrate-env`, and worse via `gateway/
    daemon.py`'s automatic trigger, where it let the subsequent
    unconditional `_harden_config_permissions()` chmod call crash on a
    nonexistent file with an unhandled traceback instead of a clean
    diagnostic). Also raises `ValueError` if any reference can't be
    resolved (missing from both `.env` and the process environment — same
    check `_expand_env_vars` already performs for the real daemon load
    path). The caller must treat both as fatal: `.env` and config.yaml are
    left completely untouched, and starting the gateway with a half-
    migrated or still-referencing config would be worse than refusing to
    start.
    """
    # Resolved unconditionally, before anything else touches the path — see
    # module docstring's "Symlink safety" section. Cheap even in the (now
    # overwhelmingly common) no-.env case, unlike the YAML parse below.
    config_path = Path(config_path).resolve()
    if not config_path.exists():
        # Matches EditableConfig.load()'s / GatewayConfig.from_file()'s own
        # wording exactly, since this is effectively the same check, just
        # performed earlier (before the .env stat below) so it can never be
        # silently skipped by the "no .env" no-op path.
        raise FileNotFoundError(f"Config file not found: {config_path}")

    env_path = config_path.parent / ".env"
    # Same question as `has_pending_migration()`, asked on the already-resolved
    # path — the predicate is what the CLI preflight calls, so keep the two
    # reading the same location.
    if not has_pending_migration(config_path):
        return MigrationResult(migrated=False)

    cfg = EditableConfig.load(config_path)
    ref_count = _count_env_refs(cfg.document)

    # Merge .env's own values into THIS process's environment — the exact
    # same call gateway/config.py's GatewayConfig.from_file makes (doesn't
    # override a var already set in the process environment, so if the
    # daemon would actually have resolved a reference from the ambient
    # environment rather than .env, this migration produces the same
    # literal value the daemon has always been using, not a divergent one).
    load_dotenv(env_path)

    try:
        expanded = _expand_env_vars(cfg.document)
    except ValueError as exc:
        raise ValueError(f"Cannot migrate .env into config.yaml: {exc}") from exc

    cfg.document = expanded
    cfg.mark_dirty()
    cfg.save()  # validate + timestamped backup + atomic replace + chmod —
    # this ALSO creates and chmod(0o700)s .config-backups/, reused directly
    # below rather than re-created (code-review finding: this used to
    # mkdir+chmod it again here, redundant work on a directory save() just
    # built correctly moments earlier).

    # Only remove .env once the migrated config.yaml is durably saved —
    # moved (not deleted outright) into .config-backups/, so there's one
    # designated place for "anything historical," not a stray .env.migrated
    # left in the live config directory.
    backup_dir = cfg.path.parent / ".config-backups"
    backup_path = backup_dir / f".env.pre-migration.{int(time.time())}"
    env_path.rename(backup_path)
    backup_path.chmod(0o600)

    return MigrationResult(migrated=True, ref_count=ref_count, env_backup_path=backup_path)
