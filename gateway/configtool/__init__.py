"""coop config TUI.

Reached via ``coop config`` (no subcommand) — see
gateway/cli.py's ``_run_config``. ``coop config validate``
stays a separate, scriptable command backed by gateway/config_validate.py.

Not to be confused with gateway/tools/tui.py, an unrelated interactive REPL
for chatting with agent backends directly (naming collision only — see
docs/design/config-tool.md).
"""

from __future__ import annotations

import sys


def run_app(config_path: str, lint: bool = False) -> int:
    """Launch the config TUI. Returns a process exit code.

    Guards against a piped/non-interactive invocation (e.g.
    ``coop config | cat`` or ``ssh host cmd </dev/null``) —
    Textual's driver needs a real terminal; without one this would otherwise
    hang rather than fail fast.

    Runs the same one-time `.env` -> config.yaml migration
    (``gateway/config_migrate.py``) that ``coop start`` runs
    automatically — the config TUI was, until this was added, the one
    remaining entry point that could show/edit a pre-migration config
    (secrets still behind `${VAR}` and `.env`), which is what used to
    justify keeping a whole secret-resolution-for-display code path in the
    TUI itself. Triggering the migration here instead removes the need for
    that entirely: by the time `ConfigToolApp` is constructed, config.yaml
    always holds real literal values, same as the daemon sees.

    A missing config.yaml is intentionally NOT fatal here (unlike
    ``start_daemon()``, which requires an actually-loadable config to run
    the gateway service) — the TUI already has its own graceful handling
    for that case (an empty-tables view with a "does not currently load"
    banner), so `migrate_env_to_config()`'s `FileNotFoundError` is simply
    let through to that same path rather than blocking the TUI from
    opening at all.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "Error: 'coop config' requires an interactive terminal.\n"
            "Use 'coop config validate' for a non-interactive check.",
            file=sys.stderr,
        )
        return 1

    from ..config_migrate import migrate_env_to_config

    try:
        migration = migrate_env_to_config(config_path)
    except FileNotFoundError:
        pass  # let ConfigToolApp's own "does not currently load" banner handle it
    except (ValueError, OSError) as exc:
        print(f"Error: could not migrate .env into config.yaml: {exc}", file=sys.stderr)
        return 1
    else:
        if migration.migrated:
            print(
                f"Migrated {migration.ref_count} secret reference(s) from .env "
                f"into config.yaml; .env moved to {migration.env_backup_path}."
            )

    from .app import ConfigToolApp

    ConfigToolApp(config_path=config_path, lint=lint).run()
    return 0
