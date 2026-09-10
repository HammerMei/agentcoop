"""ConfigToolApp — the config TUI's root Textual application.

Reached via `coop config` (gateway/cli.py's `_run_config`).
See docs/design/config-tool.md for the full M1–M3 design; this is Phase 1:
read-only overview + detail screens, plus the $EDITOR escape hatch.
"""

from __future__ import annotations

import subprocess

from textual import work
from textual.app import App

from ..config_validate import ValidationResult, validate_config
from .editor import resolve_editor_command
from .modals import ConfirmModal
from .model import EditableConfig
from .screens.overview import OverviewScreen


class ConfigToolApp(App):
    """Root app. Owns the single EditableConfig instance every screen reads
    (`self.editable_config`), or `self.load_error` if the file doesn't
    currently parse."""

    TITLE = "coop config"

    def __init__(self, config_path: str, lint: bool = False):
        super().__init__()
        self.config_path = str(config_path)
        self.lint = lint
        self.editable_config: EditableConfig | None = None
        self.load_error: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            self.editable_config = EditableConfig.load(self.config_path)
            self.load_error = None
        except (ValueError, FileNotFoundError) as exc:
            self.editable_config = None
            self.load_error = str(exc)

    def on_mount(self) -> None:
        self.push_screen(OverviewScreen())

    @work
    async def action_quit(self) -> None:
        """Overrides App's default action_quit (bound to ctrl+q, and to the
        visible 'q' binding on OverviewScreen) to gate on unsaved changes.
        No Phase 2/3 edit screen exists yet to ever set `dirty`, but this is
        the mechanism they'll all rely on — built now, alongside save()/
        dirty tracking, rather than bolted on once the first edit screen
        needs it.

        `@work` is required here, not optional: `push_screen_wait()` calls
        `get_current_worker()` internally and raises `NoActiveWorker` if this
        method runs as a plain action coroutine instead of inside a Textual
        worker task (confirmed empirically — every keybinding-triggered
        action normally runs as a bare coroutine, not a worker)."""
        if self.editable_config is not None and self.editable_config.dirty:
            discard = await self.push_screen_wait(
                ConfirmModal("Discard unsaved changes and quit?", confirm_label="Discard")
            )
            if not discard:
                return
        self.exit()

    def notify(  # type: ignore[override]
        self,
        message: str,
        *,
        title: str = "",
        severity="information",
        timeout: float | None = None,
        markup: bool = False,
    ) -> None:
        """Notifications are PLAIN TEXT here — `markup` is FORCED off.

        Textual parses notification text as Rich markup by default, and every
        notification this app raises names operator-authored data (a rule,
        connector, agent, template or preset name, or an exception message
        quoting a config value). Two failures follow, both verified: a name
        like `[ab]` was swallowed whole (`Deleted rule ''.`), and one
        spelling a closing tag (`[/]`) raised `MarkupError` from inside the
        toast, so the notification that was supposed to report an outcome
        killed the render instead.

        Fixed here rather than at ~12 call sites: `Widget.notify()` delegates
        to `App.notify()`, so one place covers every screen and modal in the
        package, and a call site added later inherits it. Nothing here ever
        wants markup in a toast — the styling comes from `severity`.

        FORCED, not defaulted (Codex review of #129, round 9, catching round
        8's own fix): `Widget.notify()` declares its own `markup: bool = True`
        and forwards it EXPLICITLY, so a default here was overridden for every
        `self.notify(...)` call from a screen — which is nearly all of them.
        Only direct `app.notify(...)` calls were ever protected, and round 8's
        test happened to use exactly that path, so it passed while the real
        doors stayed open. The incoming value is therefore ignored on purpose;
        the parameter is kept only so the signature stays substitutable.
        """
        del markup  # see above: never honoured, deliberately
        super().notify(
            message, title=title, severity=severity, timeout=timeout, markup=False
        )

    # ── Shared, non-action helpers (called from OverviewScreen's actions) ───

    def run_validate(self) -> ValidationResult:
        """Run the exact same check `coop config validate` uses — single
        source of truth for what "valid" means, per docs/design/config-tool.md."""
        return validate_config(self.config_path, lint=self.lint)

    def reload_config(self) -> None:
        """Re-read config.yaml from disk (e.g. after the $EDITOR round-trip,
        or a manual 'refresh' action) and repaint the active screen."""
        self._load()
        if isinstance(self.screen, OverviewScreen):
            self.screen.repaint_from_memory()

    def open_editor_and_reload(self) -> None:
        """Suspend the TUI, open $EDITOR on config.yaml, resume + reload.

        Only reachable from OverviewScreen (see docs/design/config-tool.md,
        Q6) — restricting it there means it can never race with an in-progress
        edit on some other screen holding unsaved in-memory state.
        """
        try:
            # resolve_editor_command must stay INSIDE this try — it calls
            # shlex.split() on $EDITOR/$VISUAL, which raises ValueError on
            # unbalanced quoting (e.g. EDITOR="vim '"). That's exactly the
            # kind of editor-launch failure this method exists to catch and
            # notify on, not crash on.
            editor_argv = resolve_editor_command(self.config_path)
            with self.suspend():
                subprocess.call(editor_argv)  # pragma: no cover — needs a real terminal
        except Exception as exc:
            self.notify(f"Could not open editor: {exc}", severity="error")
            return
        self.reload_config()
