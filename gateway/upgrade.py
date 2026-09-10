"""Upgrade logic for agent-chat-gateway."""

import json
import subprocess
import sys
from pathlib import Path

from rich.console import Console

from .daemon import is_running, start_daemon, stop_daemon  # noqa: F401 (re-exported for patching)
from .paths import RUNTIME_DIR

META_FILE = RUNTIME_DIR / "install_meta.json"

console = Console()


def load_install_meta(meta_file: Path | None = None) -> dict:
    """Load ~/.agent-chat-gateway/install_meta.json. Returns {} if missing."""
    path = meta_file or META_FILE
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _file_hash(path: Path) -> str | None:
    """Return SHA256 hex digest of a file, or None if the file does not exist."""
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _snapshot_context_hashes(repo_path: Path) -> dict[str, str]:
    """Return {filename: sha256} for all files in repo/contexts/ before git pull.

    Called before git pull so we can later tell whether the repo file changed
    and whether the user's runtime copy still matches the old repo version.
    """
    ctx_dir = repo_path / "contexts"
    if not ctx_dir.is_dir():
        return {}
    return {
        f.name: h
        for f in ctx_dir.iterdir()
        if f.is_file() and (h := _file_hash(f)) is not None
    }


def _sync_context_files(
    repo_path: Path,
    runtime_dir: Path,
    pre_pull_hashes: dict[str, str],
) -> None:
    """Sync user-facing context files from repo to runtime dir after git pull.

    Built-in system context files (rc-gateway-context.md, scheduling-context.md)
    are bundled inside the gateway Python package (gateway/contexts/) and
    auto-injected at runtime — they are NOT synced here.

    Only user-editable example files in repo/contexts/ (e.g. rc-room-profiles.example.md)
    are synced to the runtime dir.

    Decision table for each file in repo/contexts/:
      - Repo file is brand-new (wasn't in pre-pull snapshot) → copy unconditionally.
      - User is missing the file (first upgrade from old install) → copy unconditionally.
      - Repo file unchanged after pull → skip (nothing new to deliver).
      - Repo file changed + user copy unmodified (hash matches old repo) → overwrite.
      - Repo file changed + user copy modified (hash differs from old repo) → save new
        version as <name>.default and warn the user to merge manually.
    """
    import shutil

    ctx_src = repo_path / "contexts"
    ctx_dst = runtime_dir / "contexts"

    if not ctx_src.is_dir():
        return

    ctx_dst.mkdir(parents=True, exist_ok=True)

    for src_file in sorted(ctx_src.iterdir()):
        if not src_file.is_file():
            continue

        name = src_file.name
        dst_file = ctx_dst / name
        new_hash = _file_hash(src_file)
        old_repo_hash = pre_pull_hashes.get(name)
        user_hash = _file_hash(dst_file)

        # Brand-new file added to repo, or user is missing the file entirely
        # (e.g. upgrading from an old install that predates context file copying).
        # Copy unconditionally in both cases.
        if old_repo_hash is None or user_hash is None:
            shutil.copy2(src_file, dst_file)
            console.print(f"  New context file: contexts/{name}")
            continue

        # Repo file unchanged after pull — nothing to do.
        if new_hash == old_repo_hash:
            continue

        # Repo file changed. User copy is "unmodified" when it still matches
        # the old repo version.
        user_modified = user_hash != old_repo_hash

        if not user_modified:
            shutil.copy2(src_file, dst_file)
            console.print(f"  Updated context file: contexts/{name}")
        else:
            default_file = ctx_dst / f"{name}.default"
            shutil.copy2(src_file, default_file)
            console.print(
                f"  [yellow]Warning:[/yellow] contexts/{name} has local changes.\n"
                f"    New version saved as contexts/{name}.default\n"
                f"    Review and merge manually — check release notes for details."
            )


def _find_uv() -> str:
    """Return the path to the uv executable.

    SSH sessions often have a minimal PATH that omits ~/.local/bin, so
    shutil.which() may fail even when uv is installed.  Fall back to the
    standard installation locations used by the official uv installer.
    """
    import shutil

    if path := shutil.which("uv"):
        return path
    for candidate in [
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".cargo" / "bin" / "uv",
    ]:
        if candidate.exists():
            return str(candidate)
    console.print("[red]Error:[/red] uv not found. Install from https://docs.astral.sh/uv/")
    sys.exit(1)


# Console scripts that install.sh symlinks into ~/.local/bin. Kept in sync with
# the symlink block in install.sh by hand — there is no clean way to share one
# list between bash and Python, so a script added there must be added here too.
_LOCAL_BIN_SCRIPTS = ("coop", "coop-provision")

# Post-upgrade steps are local filesystem work, so this is a hang guard rather
# than a work budget. It is kept short on purpose: the hook runs while the daemon
# is stopped, so waiting is an outage.
_POST_UPGRADE_TIMEOUT = 120


def _backup_path(link: Path) -> Path:
    """Return an unused `<link>.<timestamp>.bak` path next to `link`.

    Timestamped so repeated upgrades accumulate distinct backups rather than one
    clobbering the next. The collision suffix only matters for two runs inside
    the same second, but Path.rename() overwrites silently on POSIX, and losing
    an earlier backup is exactly the data loss the caller is trying to avoid.
    """
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    candidate = link.with_name(f"{link.name}.{stamp}.bak")
    n = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = link.with_name(f"{link.name}.{stamp}-{n}.bak")
        n += 1
    return candidate


def _ensure_local_bin_symlinks(repo_path: Path) -> None:
    """Ensure ~/.local/bin has a symlink for each console script.

    install.sh creates these at install time, but do_git_upgrade only runs
    `git pull` + `uv sync` — so a console script introduced in a LATER release
    lands in .venv/bin and never becomes reachable on the PATH the installer
    configured. That is not hypothetical: acg-provision shipped after install.sh
    already existed, so every user who installed before it and upgraded through
    this flow would end up with .venv/bin/acg-provision and no ~/.local/bin
    entry, leaving the command missing until they manually re-ran the installer.

    Gated on ~/.local/bin/agent-chat-gateway already being present, because that
    symlink is install.sh's own fingerprint: its presence means this machine
    opted into that layout. Without the guard, a pipx / distro-package /
    manual-venv install would suddenly acquire symlinks it never asked for.
    (Only the `git` upgrade method reaches this — brew and pip manage their own
    shims.) "Present" deliberately includes a *dangling* symlink; see the gate.

    What happens at an occupied destination, following the same policy as
    install.sh's link_console_script():

      a link that already RESOLVES  -> nothing. Note "resolves to the same file",
        to the same file                not "has the same target string": the check
                                        below compares Path.resolve(), so a
                                        relative or differently-spelled link to
                                        the same file is left alone here while
                                        install.sh — which compares readlink text
                                        — would rewrite it. Same outcome, one
                                        needless rewrite; the two are not
                                        byte-identical predicates.
      any other symlink             -> repointed, printing the old target (nothing
                                       is preserved by keeping it — the file it
                                       pointed at is untouched — but the change is
                                       reported rather than silent)
      a real file or directory      -> moved aside to <name>.<timestamp>.bak, then
                                       linked; that cannot be reconstructed from a
                                       printed path, so it is kept

    Replacing a real file rather than skipping it is deliberate. Skipping looks
    safer but leaves a worse state: install_meta.json points `upgrade` at
    repo_path while PATH runs the occupant, so the tool manages a repo whose code
    never executes. Backing up keeps the managed command working and still loses
    nothing. Nothing else cleans up the .bak files; INSTALL.md's uninstall
    section tells the user to look for them.

    Stale symlinks are re-pointed, which covers the repo-moved case — including
    when the move left the old symlink dangling rather than merely stale.

    Never fatal. A symlink that cannot be written does not invalidate the
    upgrade that just succeeded, so failures warn and continue.
    """
    local_bin = Path.home() / ".local" / "bin"
    fingerprint = local_bin / "agent-chat-gateway"
    # `or is_symlink()` is load-bearing: exists() FOLLOWS symlinks, so an
    # installer symlink whose target has gone away (the repo or venv was moved)
    # reads as False. Gating on exists() alone bailed out in exactly the case
    # this function is supposed to repair, leaving the primary command dangling
    # AND every other script unlinked. A dangling symlink is still install.sh's
    # fingerprint — arguably more so, since only a managed install creates it.
    if not (fingerprint.exists() or fingerprint.is_symlink()):
        return

    for script in _LOCAL_BIN_SCRIPTS:
        target = repo_path / ".venv" / "bin" / script
        link = local_bin / script
        if not target.exists():
            # Script not built in this release — nothing to link.
            continue
        # What was displaced, so a later failure can put it back. Exactly one of
        # these is ever set.
        displaced_backup: Path | None = None
        displaced_target: Path | None = None
        try:
            if link.is_symlink():
                if link.resolve() == target.resolve():
                    continue  # already correct
                # A symlink, but not ours. Replace it — nothing is preserved by
                # keeping it, since the file it points at is untouched and a
                # stale .bak symlink is pure litter — but REPORT what it was.
                # The objection to the old behaviour was the silence, not the
                # replacement. Path.readlink(), not resolve(), so this still
                # prints something useful for a dangling or looping link — and
                # needs no new import.
                displaced_target = link.readlink()
            elif link.exists():
                # A real file or directory the user made by hand (observed in
                # the wild: a wrapper setting PYTHONPATH and pinning a specific
                # interpreter). Unlike a symlink this cannot be reconstructed
                # from a printed path, so it is moved aside rather than deleted.
                #
                # Replacing rather than skipping is deliberate. Skipping leaves
                # a worse state than it looks: install_meta.json points `upgrade`
                # at repo_path while PATH runs the occupant, so the tool manages
                # a repo whose code never executes. Backing up keeps the managed
                # command working and still loses nothing.
                displaced_backup = _backup_path(link)
                link.rename(displaced_backup)
            link.unlink(missing_ok=True)
            link.symlink_to(target)

            # Everything above is filesystem work with NO stdout writes, and that
            # ordering is load-bearing rather than stylistic.
            #
            # rich's Console.print() raises SystemExit(1) when stdout is a broken
            # pipe (on_broken_pipe(), reached from _check_buffer's BrokenPipeError
            # handler). SystemExit is a BaseException, so `except (OSError,
            # RuntimeError)` below does NOT catch it and the rollback never runs.
            # With a print between rename() and symlink_to(), `agent-chat-gateway
            # upgrade | head -4` left the user's own wrapper in a .bak with NOTHING
            # on PATH, silently — SystemExit(1) prints nothing at all. Reproduced
            # end to end on 3.12 and 3.13.
            #
            # It is reachable rather than theoretical: real `uv sync` writes 0
            # bytes to stdout, so from "Running uv sync ..." until here there are
            # no stdout writes for seconds — ample time for a reader to go away
            # (`| less` then q, an ssh drop, a dying tee).
            #
            # Reporting after the fact means a broken pipe can only cost the
            # MESSAGE, never the state: by this point the link exists and the
            # backup is beside it.
            # markup=False on every line that interpolates a PATH. rich parses
            # [...] as a style tag and drops it, so a symlink to
            # /opt/tools[old]/bin/x was reported as /opt/tools/bin/x. That is not
            # cosmetic here: the justification for deleting a foreign symlink
            # instead of backing it up is that its target gets reported, and by
            # this point the link is already gone — the printed path is the user's
            # only record of what to restore. A plausible-looking wrong path is
            # worse than no path.
            if displaced_target is not None:
                console.print(
                    f"  ~/.local/bin/{script} pointed at {displaced_target} — repointed it",
                    markup=False,
                )
            if displaced_backup is not None:
                console.print(
                    "  ~/.local/bin/"
                    f"{script} was not a symlink — backed it up as {displaced_backup.name}",
                    markup=False,
                )
            console.print(f"  Linked ~/.local/bin/{script} -> {target}", markup=False)
        except (OSError, RuntimeError) as e:
            # RuntimeError is not redundant: on Python 3.12 a symlink cycle makes
            # Path.resolve() raise RuntimeError("Symlink loop from ...") — which is
            # NOT an OSError subclass, so `except OSError` alone lets it escape.
            # (Verified: 3.12 raises, 3.13 returns the path without raising.)
            #
            # This function now runs only in the post-upgrade CHILD, so an escape
            # here does NOT strand the daemon — it exits the child non-zero and the
            # parent warns. (An earlier version of this comment claimed otherwise;
            # it predated the hook and was left stale by it.) The reason to catch
            # is narrower but still real: the child is the only thing that links
            # the console scripts, so letting it die means PATH is silently not
            # fixed, and a partial rename would go unrolled-back. The
            # daemon-stranding guarantee belongs to _run_post_upgrade_hook() in the
            # parent; see issue #83 for the window it sits in.
            # ROLL BACK FIRST, REPORT SECOND — for exactly the reason the success
            # path above reports last. A print here can raise SystemExit on a broken
            # pipe, and SystemExit is a BaseException, so it would skip the rollback
            # entirely and leave the user's command in a .bak with nothing on PATH:
            # the same data loss, reached through the failure path instead of the
            # happy one. Restoring before printing means the worst a dead reader can
            # do is hide the explanation.
            #
            # Without this rollback at all, a failure between clearing the
            # destination and creating the link leaves the user with LESS than they
            # started with. Suppressed rather than raised because a failed rollback
            # must not turn a missing link into a lost file as well.
            restored = ""
            try:
                if displaced_backup is not None and displaced_backup.exists():
                    displaced_backup.rename(link)
                    restored = "file"
                elif displaced_target is not None and not link.is_symlink():
                    link.symlink_to(displaced_target)
                    restored = "link"
            except OSError:
                # Swallowed so a failed rollback cannot replace the original error —
                # but NOT silently. This is the worst reachable state (no command on
                # PATH, and the user's file under a timestamped name they never
                # chose), so the one thing they need is where it went. install.sh
                # already reports this; not doing so here was the two
                # implementations disagreeing again.
                restored = "failed"

            console.print(
                f"  [yellow]Warning:[/yellow] could not link ~/.local/bin/{script}: {e}"
            )
            if restored == "file":
                console.print(f"  Restored the previous ~/.local/bin/{script}")
            elif restored == "link":
                console.print(
                    f"  Restored ~/.local/bin/{script} -> {displaced_target}",
                    markup=False,
                )
            elif restored == "failed" and displaced_backup is not None:
                console.print(
                    f"  Could not put it back either — your original is at {displaced_backup}",
                    markup=False,
                )
            elif restored == "failed" and displaced_target is not None:
                console.print(
                    "  Could not restore the previous link, which pointed at "
                    f"{displaced_target}",
                    markup=False,
                )


def run_post_upgrade(repo_path: Path, from_version: str = "") -> None:
    """Steps that must run with the NEWLY PULLED release's own logic.

    This is the extension point for automatic upgrade steps. Anything added here
    executes from the release being upgraded *to*, not the one being upgraded
    *from* — which is the whole reason it exists, and the reason it is invoked in
    a fresh interpreter rather than called directly (see
    _run_post_upgrade_hook()).

    `from_version` is the version recorded in install_meta.json BEFORE this
    upgrade, so version-aware work belongs here too. It is passed in rather than
    read from install_meta.json to keep the child from depending on when the
    caller happens to rewrite that file.

    Two non-version values can arrive, and NEITHER is empty:
      "unknown" — install_meta.json had no `version` key (run_upgrade's own
                  default), so this is what the real caller sends today;
      ""        — the calling release's bootstrap omitted the argument, which only
                  the signature default below can produce.
    So compare against the versions you care about (`if from_version == "0.5.1"`)
    and never test for falsiness — `if not from_version:` misses the actual
    unknown case, which is the one such a guard is usually reaching for.

    Do NOT put version-aware work in run_migrations() instead: that runs in the
    pre-pull process, so a migration added to it never executes for the upgrade
    that delivers it, and by the next upgrade install_meta.json already records
    the newer version — so its `from_version` condition can be missed
    permanently. See run_migrations()'s own docstring.

    Contract for anything added here:
      * Idempotent. It may run again on the next upgrade, and re-running must be
        harmless. Do not rely on `from_version` alone to run something once —
        an interrupted upgrade can repeat the same transition.
      * SKIPPABLE, which is the sharp edge. A step that raises makes the child exit
        non-zero, the caller only WARNS, and run_upgrade then records the new
        version anyway — so on the next upgrade `from_version` is already the newer
        one and a step guarded on the older version never runs again. A failure here
        is therefore permanent, not deferred. Do not put anything here that must
        eventually happen exactly once; there is no retry, and adding one means
        changing when the version is recorded (issue #85).
      * Non-fatal in effect. Prefer warning over raising; the caller treats a
        non-zero exit as a warning, but an exception here still means the
        remaining steps are skipped.
      * No network, and fast. The caller runs inside the window where the daemon
        is stopped, and enforces a timeout.

    The signature is a frozen contract: the `python -c` line that calls it lives
    in the PREVIOUS release, so parameters can be added with defaults but never
    removed or reordered. Anything else the step needs, it should read from disk.
    """
    _ensure_local_bin_symlinks(repo_path)


# Executed by `python -c` in the pulled tree. Deliberately tiny: everything it
# does is resolved at subprocess start from the NEW code on disk, so keeping
# logic out of this string is what makes the mechanism work.
#
# getattr with a no-op default rather than a bare attribute access: a pulled
# release that has no run_post_upgrade — checking out an older ref, say — has no
# post-upgrade steps, which is not an error. A bare access printed a full
# AttributeError traceback in that case, which reads like a failed upgrade
# immediately after a successful one. Genuine failures INSIDE run_post_upgrade
# still surface, which is the part worth seeing.
_POST_UPGRADE_BOOTSTRAP = (
    "import pathlib, sys, gateway.upgrade as u; "
    "getattr(u, 'run_post_upgrade', lambda *_a: None)"
    "(pathlib.Path(sys.argv[1]), sys.argv[2])"
)


def _run_post_upgrade_hook(repo_path: Path, from_version: str = "") -> None:
    """Run the pulled release's run_post_upgrade() in a FRESH interpreter.

    `gateway.upgrade` is imported into this process before `git pull`, so the
    module object in memory belongs to the OLD release; rewriting the file on disk
    cannot change it. A subprocess re-imports from disk, so it executes the NEW
    code — this is the only way post-upgrade logic can run during the very upgrade
    that delivers it.

    The limit is worth stating exactly, because it is easy to expect too much: the
    *call below* is itself frozen in the old release. A release therefore benefits
    only if the release BEFORE it already contained this call. It does nothing for
    the release that introduces it, and works for every release after — which is
    the point. Anything that must reach users on the introducing release still
    needs a manual step or a documented note.

    Never fatal, on two counts. A `git pull` + `uv sync` that already succeeded
    must not be reported as a failure because a follow-up step could not run. And
    this executes between stop_daemon() and start_daemon() in run_upgrade(), which
    has no try/finally (see issue #83) — so an exception escaping here would leave
    the daemon stopped after a successful pull. Hence the timeout as well: a hung
    child in that window is an outage, not a delay.
    """
    python = repo_path / ".venv" / "bin" / "python"
    if not python.exists():
        console.print(
            f"  [yellow]Warning:[/yellow] no interpreter at {python} — "
            "skipping post-upgrade steps."
        )
        return

    try:
        result = subprocess.run(
            [str(python), "-c", _POST_UPGRADE_BOOTSTRAP, str(repo_path), from_version],
            # cwd so `import gateway` still resolves from the source tree even if
            # the editable install's .pth is missing or stale.
            cwd=str(repo_path),
            check=False,
            timeout=_POST_UPGRADE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        console.print(
            "  [yellow]Warning:[/yellow] post-upgrade steps timed out after "
            f"{_POST_UPGRADE_TIMEOUT}s — the upgrade itself succeeded."
        )
        return
    except OSError as e:
        console.print(
            f"  [yellow]Warning:[/yellow] could not run post-upgrade steps: {e}"
        )
        return

    if result.returncode != 0:
        # Includes the pulled release simply not having run_post_upgrade — e.g.
        # checking out an older ref — which surfaces as a non-zero exit from the
        # child's ImportError/AttributeError rather than as a crash here.
        console.print(
            "  [yellow]Warning:[/yellow] post-upgrade steps exited "
            f"{result.returncode} — the upgrade itself succeeded."
        )


def do_git_upgrade(repo_path: Path, from_version: str = "") -> None:
    """git pull + uv sync + context file sync in the given repo directory.

    `from_version` is only forwarded to the post-upgrade hook, so version-aware
    steps run with the pulled release's logic instead of this process's.
    """
    # Snapshot context file hashes BEFORE git pull so we can compare afterwards
    # to determine which files changed and whether users have local modifications.
    pre_pull_hashes = _snapshot_context_hashes(repo_path)

    console.print(f"  Running [bold]git pull[/bold] in {repo_path} ...")
    result = subprocess.run(
        ["git", "-C", str(repo_path), "pull"],
        check=False,
    )
    if result.returncode != 0:
        console.print("[red]Error:[/red] git pull failed.")
        sys.exit(1)

    uv = _find_uv()
    console.print("  Running [bold]uv sync[/bold] ...")
    result = subprocess.run(
        [uv, "sync"],
        cwd=str(repo_path),
        check=False,
    )
    if result.returncode != 0:
        console.print("[red]Error:[/red] uv sync failed.")
        sys.exit(1)

    _sync_context_files(repo_path, RUNTIME_DIR, pre_pull_hashes)

    # After uv sync, so the freshly pulled release's dependencies and console
    # scripts are in place before its own post-upgrade steps run.
    _run_post_upgrade_hook(repo_path, from_version)


def run_migrations(from_version: str) -> None:
    """No-op skeleton, kept for callers. DO NOT add migrations here.

    This runs in the PRE-PULL process, so it has the same frozen-module problem
    _run_post_upgrade_hook() exists to solve — but worse, because the failure is
    silent and permanent rather than one-release-late:

      * A migration added here in release N+1 does not run when a user upgrades
        from N to N+1: this process is still executing N's copy of this function,
        which does not contain it.
      * run_upgrade() then records N+1 in install_meta.json regardless.
      * On the next upgrade the migration finally exists in memory, but
        `from_version` is now N+1, so a condition written for N never matches.
        The migration is missed forever, with nothing reporting it.

    Put version-aware upgrade work in run_post_upgrade(repo_path, from_version)
    instead. It receives the same value and executes in the pulled release, which
    is where the code that needs to run actually lives.
    """
    # Intentionally empty — see the docstring before adding anything.


def _read_current_version(repo_path: Path) -> str:
    """Read version from pyproject.toml after upgrade."""
    try:
        toml = (repo_path / "pyproject.toml").read_text()
        for line in toml.splitlines():
            stripped = line.strip()
            if stripped.startswith("version"):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"


def _is_pip_installed() -> bool:
    """Return True if the package is installed as a regular pip/PyPI package."""
    try:
        import importlib.metadata

        importlib.metadata.version("agentcoop")
        # Confirm it's not a local editable install (editable installs have a direct_url.json
        # with "editable": true, or a .pth file pointing to a local path)
        dist = importlib.metadata.distribution("agentcoop")
        direct_url_text = None
        for f in dist.files or []:
            if f.name == "direct_url.json":
                try:
                    direct_url_text = f.read_text()
                except Exception:
                    pass
                break
        if direct_url_text:
            import json as _json

            info = _json.loads(direct_url_text)
            # Editable or local directory installs are not "pip from PyPI"
            if info.get("dir_info", {}).get("editable") or "url" not in info:
                return False
            if info["url"].startswith("file://"):
                return False
        return True
    except Exception:
        return False


def _do_pip_upgrade() -> None:
    """Upgrade via pip install --upgrade."""
    console.print("  Detected install method: [bold]pip (PyPI)[/bold]")
    console.print("  Running [bold]pip install --upgrade agentcoop[/bold] ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "agentcoop"],
        check=False,
    )
    if result.returncode != 0:
        console.print("[red]Error:[/red] pip upgrade failed.")
        sys.exit(1)
    console.print("[green]Upgrade complete![/green]")


def _restart_daemon_in_fresh_interpreter() -> int:
    """Start the daemon from a NEW interpreter, so it runs the code just pulled.

    Not `start_daemon()`. That double-forks, and a child of THIS process
    inherits every module this process has already imported — which is the
    PRE-upgrade release, because the CLI imports `.daemon` → `.service` → the
    whole runtime before `run_upgrade` ever runs. `git pull` changed the files
    on disk; nothing re-imported them. So `agent-chat-gateway upgrade` used to report success
    and restart a daemon running the old code, and it stayed old until someone
    happened to `stop`/`start` by hand. Measured on a live deployment: two
    consecutive upgrades, both restarted daemons whose command line still read
    `agent-chat-gateway upgrade` — the fork — and neither ran the pulled code.
    The mix is worse than "old": a module first imported lazily AFTER the fork
    loads from disk and is new, so one process ran two releases at once.

    A fresh `python -m gateway.cli start` imports from disk, picks up whatever
    `uv sync` changed, and its parent blocks until the daemon signals it is up
    — so the "Upgrade complete!" line below is printed only once the new
    daemon is actually running. (Before, `start_daemon()` never returned — its
    parent `sys.exit`s from `_wait_for_startup_signal` — so with a running
    daemon that line was never printed at all.)

    Returns the `start` command's exit status.
    """
    cmd = [sys.executable, "-m", "gateway.cli", "start"]
    return subprocess.run(cmd).returncode


def run_upgrade() -> None:
    """Entry point called by CLI."""
    console.print("[bold cyan]agent-chat-gateway upgrade[/bold cyan]")

    meta = load_install_meta()
    if not meta:
        # No install_meta.json — check if installed via pip before giving up
        if _is_pip_installed():
            _do_pip_upgrade()
            return
        console.print(
            "[yellow]Warning:[/yellow] install_meta.json not found.\n"
            "Cannot determine install method. Please upgrade manually:\n"
            "  cd <repo>  &&  git pull  &&  uv sync"
        )
        sys.exit(1)

    method = meta.get("method", "unknown")
    old_version = meta.get("version", "unknown")

    if method == "git":
        repo_path_str = meta.get("repo_path")
        if not repo_path_str:
            console.print("[red]Error:[/red] repo_path not set in install_meta.json.")
            sys.exit(1)
        repo_path = Path(repo_path_str)
        if not repo_path.exists():
            console.print(
                f"[red]Error:[/red] Repo path not found: {repo_path}\n"
                "Update install_meta.json with the correct repo path or upgrade manually."
            )
            sys.exit(1)

        # Check if daemon is running; stop it if so
        running, _pid = is_running()
        if running:
            console.print("  Daemon is running — stopping it before upgrade...")
            stop_daemon()

        # old_version is forwarded so the pulled release's run_post_upgrade() can
        # do version-aware work. It must be read BEFORE install_meta.json is
        # rewritten below, which it is (see old_version above).
        do_git_upgrade(repo_path, old_version)
        run_migrations(old_version)

        # Update version in install_meta
        new_version = _read_current_version(repo_path)
        meta["version"] = new_version
        META_FILE.write_text(json.dumps(meta, indent=2))
        console.print(f"  Updated install_meta.json: version={new_version}")

        if running:
            console.print("  Restarting daemon...")
            rc = _restart_daemon_in_fresh_interpreter()
            if rc != 0:
                console.print(
                    f"[red]Error:[/red] the daemon did not start after the upgrade "
                    f"(exit {rc}). The code is upgraded; start it with "
                    f"'agent-chat-gateway start' and check the log."
                )
                sys.exit(rc)

        console.print(
            f"\n[green]Upgrade complete![/green] {old_version} → {new_version}\n"
            "Changelog: https://github.com/HammerMei/agent-chat-gateway/releases"
        )
        return

    # Unknown method
    console.print(
        f"[red]Error:[/red] Unknown install method: [bold]{method}[/bold]\n\n"
        "Please upgrade manually:\n"
        "  cd <repo>  &&  git pull  &&  uv sync"
    )
    sys.exit(1)
