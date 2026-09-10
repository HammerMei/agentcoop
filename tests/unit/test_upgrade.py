"""Unit tests for gateway.upgrade."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.upgrade import (
    _POST_UPGRADE_BOOTSTRAP,
    _ensure_local_bin_symlinks,
    _run_post_upgrade_hook,
    do_git_upgrade,
    load_install_meta,
    run_migrations,
    run_post_upgrade,
)

# ---------------------------------------------------------------------------
# load_install_meta
# ---------------------------------------------------------------------------

class TestLoadInstallMeta:
    def test_load_install_meta_missing(self, tmp_path: Path):
        """Returns {} when file does not exist."""
        meta_file = tmp_path / "nonexistent.json"
        result = load_install_meta(meta_file)
        assert result == {}

    def test_load_install_meta_git(self, tmp_path: Path):
        """Reads git method correctly."""
        meta_file = tmp_path / "install_meta.json"
        expected = {
            "method": "git",
            "repo_path": "/home/user/agentcoop",
            "version": "0.1.0",
            "installed_at": "2026-03-27",
        }
        meta_file.write_text(json.dumps(expected))

        result = load_install_meta(meta_file)

        assert result["method"] == "git"
        assert result["repo_path"] == "/home/user/agentcoop"
        assert result["version"] == "0.1.0"
        assert result["installed_at"] == "2026-03-27"

    def test_load_install_meta_malformed(self, tmp_path: Path):
        """Returns {} on JSON decode error."""
        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text("{ not valid json }")
        result = load_install_meta(meta_file)
        assert result == {}

    def test_load_install_meta_empty(self, tmp_path: Path):
        """Returns {} for an empty file."""
        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text("")
        result = load_install_meta(meta_file)
        assert result == {}

    def test_load_install_meta_brew(self, tmp_path: Path):
        """Reads brew method correctly."""
        meta_file = tmp_path / "install_meta.json"
        data = {"method": "brew", "repo_path": None, "version": "0.2.0", "installed_at": "2026-01-01"}
        meta_file.write_text(json.dumps(data))

        result = load_install_meta(meta_file)

        assert result["method"] == "brew"
        assert result["repo_path"] is None


# ---------------------------------------------------------------------------
# run_migrations
# ---------------------------------------------------------------------------

class TestRunMigrations:
    def test_run_migrations_noop(self):
        """run_migrations is a no-op and does not raise."""
        # Should not raise for any version string
        run_migrations("0.1.0")
        run_migrations("0.0.0")
        run_migrations("unknown")
        run_migrations("")

    def test_run_migrations_returns_none(self):
        """run_migrations returns None."""
        result = run_migrations("0.1.0")
        assert result is None


# ---------------------------------------------------------------------------
# run_upgrade
# ---------------------------------------------------------------------------

class TestRunUpgrade:
    """Tests for run_upgrade — all file I/O and subprocess calls are mocked."""

    def test_run_upgrade_unknown_method(self, tmp_path: Path):
        """Exits with error for an unknown install method."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({"method": "snap", "version": "0.1.0"}))

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1

    def test_run_upgrade_brew_is_an_unknown_method(self, tmp_path: Path):
        """There has never been a Homebrew tap; the branch that ran `brew upgrade`
        was dead code and is gone (#154). A meta file that says brew now gets the
        unknown-method error, and nothing is executed."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({"method": "brew", "repo_path": None, "version": "0.1.0"}))

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("subprocess.run") as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1
        mock_run.assert_not_called()

    def test_run_upgrade_git_missing_repo(self, tmp_path: Path):
        """Exits with error when repo_path in meta does not exist."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "install_meta.json"
        missing_repo = tmp_path / "nonexistent-repo"
        meta_file.write_text(json.dumps({
            "method": "git",
            "repo_path": str(missing_repo),
            "version": "0.1.0",
        }))

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1

    def test_run_upgrade_git_no_repo_path(self, tmp_path: Path):
        """Exits with error when repo_path is missing from meta."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({"method": "git", "version": "0.1.0"}))

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1

    def test_run_upgrade_git_success(self, tmp_path: Path):
        """Happy path: git pull + uv sync called, meta version updated."""
        from gateway.upgrade import run_upgrade

        repo = tmp_path / "repo"
        repo.mkdir()
        # Write a fake pyproject.toml so _read_current_version works
        (repo / "pyproject.toml").write_text('version = "0.2.0"\n')

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({
            "method": "git",
            "repo_path": str(repo),
            "version": "0.1.0",
            "installed_at": "2026-01-01",
        }))

        ok_result = MagicMock()
        ok_result.returncode = 0

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("gateway.upgrade.is_running", return_value=(False, None)),
            patch("gateway.upgrade._find_uv", return_value="uv"),
            patch("subprocess.run", return_value=ok_result) as mock_run,
        ):
            run_upgrade()

        # git pull and uv sync should have been called
        calls = mock_run.call_args_list
        commands = [c.args[0] for c in calls]
        assert ["git", "-C", str(repo), "pull"] in commands
        assert ["uv", "sync"] in commands

        # Meta file should be updated with new version
        updated = json.loads(meta_file.read_text())
        assert updated["version"] == "0.2.0"

    def test_run_upgrade_forwards_the_pre_upgrade_version(self, tmp_path: Path):
        """The version handed to do_git_upgrade must be the one from BEFORE the bump.

        This is the link 5ca8795 rests on and nothing covered it. Two mutations
        survived the whole suite: dropping the argument (version-aware post-upgrade
        steps then always receive ""), and moving the install_meta.json bump above
        the call so the child receives the NEW version — the exact shape of the
        permanently-missed-migration bug the hook exists to prevent.
        """
        from gateway.upgrade import run_upgrade

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pyproject.toml").write_text('version = "0.2.0"\n')
        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({
            "method": "git", "repo_path": str(repo), "version": "0.1.0",
        }))

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("gateway.upgrade.is_running", return_value=(False, None)),
            patch("gateway.upgrade.do_git_upgrade") as do_upgrade,
        ):
            run_upgrade()

        do_upgrade.assert_called_once_with(repo, "0.1.0")
        # And the bump really did happen afterwards, so "0.1.0" was the pre-upgrade
        # value rather than simply a version that was never written.
        assert json.loads(meta_file.read_text())["version"] == "0.2.0"

    def test_run_upgrade_forwards_unknown_when_meta_has_no_version(self, tmp_path: Path):
        """Pins the sentinel the caller actually sends.

        run_post_upgrade's contract documents this: the unknown case arrives as the
        literal "unknown", never as "", so a version-aware step must not test for
        falsiness.
        """
        from gateway.upgrade import run_upgrade

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pyproject.toml").write_text('version = "0.2.0"\n')
        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({"method": "git", "repo_path": str(repo)}))

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("gateway.upgrade.is_running", return_value=(False, None)),
            patch("gateway.upgrade.do_git_upgrade") as do_upgrade,
        ):
            run_upgrade()

        do_upgrade.assert_called_once_with(repo, "unknown")

    def test_run_upgrade_git_success_daemon_running(self, tmp_path: Path):
        """When daemon is running, it is stopped then restarted."""
        from gateway.upgrade import run_upgrade

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pyproject.toml").write_text('version = "0.2.0"\n')

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({
            "method": "git",
            "repo_path": str(repo),
            "version": "0.1.0",
            "installed_at": "2026-01-01",
        }))

        ok_result = MagicMock()
        ok_result.returncode = 0

        stop_mock = MagicMock()
        start_mock = MagicMock()

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("gateway.upgrade.is_running", return_value=(True, 12345)),
            patch("gateway.upgrade.stop_daemon", stop_mock),
            patch("gateway.upgrade.start_daemon", start_mock),
            patch("gateway.upgrade._find_uv", return_value="uv"),
            patch("subprocess.run", return_value=ok_result) as mock_run,
        ):
            run_upgrade()

        stop_mock.assert_called_once()
        # NOT in-process. `start_daemon()` forks, and the fork inherits this
        # process's already-imported (pre-upgrade) modules — the daemon it
        # produced ran the old release with a command line that still said
        # `upgrade`. The restart has to be a fresh interpreter.
        start_mock.assert_not_called()
        starts = [c for c in mock_run.call_args_list if c.args and c.args[0][-1] == "start"]
        assert len(starts) == 1, mock_run.call_args_list
        cmd = starts[0].args[0]
        assert cmd[0] == sys.executable, "a NEW interpreter, not a fork of this one"
        assert cmd[1:] == ["-m", "gateway.cli", "start"]
        # Ordering: pull and sync before the restart, so the new interpreter
        # sees the pulled code and its dependencies.
        order = [c.args[0][-1] if c.args else None for c in mock_run.call_args_list]
        assert order.index("pull") < order.index("start")
        assert order.index("sync") < order.index("start")

    def test_run_upgrade_reports_a_daemon_that_failed_to_start(self, tmp_path: Path):
        """The fresh `start`'s parent exits non-zero when the daemon does not come
        up. That has to reach the operator as an error with the recovery step, not
        be swallowed under "Upgrade complete!" — the code IS upgraded, only the
        restart failed, and the message must say exactly that."""
        from gateway.upgrade import run_upgrade

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.2.0"\n')
        (repo / "gateway").mkdir()
        (repo / "gateway" / "contexts").mkdir()
        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({
            "method": "git", "repo_path": str(repo),
            "version": "0.1.0", "installed_at": "2026-01-01",
        }))

        def _run(cmd, *a, **kw):
            r = MagicMock()
            r.returncode = 3 if cmd[-1] == "start" else 0
            return r

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("gateway.upgrade.is_running", return_value=(True, 12345)),
            patch("gateway.upgrade.stop_daemon", MagicMock()),
            patch("gateway.upgrade.start_daemon", MagicMock()),
            patch("gateway.upgrade._find_uv", return_value="uv"),
            patch("subprocess.run", side_effect=_run),
        ):
            with pytest.raises(SystemExit) as excinfo:
                run_upgrade()

        assert excinfo.value.code == 3

    def test_run_upgrade_git_pull_failure(self, tmp_path: Path):
        """Exits with error when git pull fails."""
        from gateway.upgrade import run_upgrade

        repo = tmp_path / "repo"
        repo.mkdir()

        meta_file = tmp_path / "install_meta.json"
        meta_file.write_text(json.dumps({
            "method": "git",
            "repo_path": str(repo),
            "version": "0.1.0",
        }))

        fail_result = MagicMock()
        fail_result.returncode = 1

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("gateway.upgrade.is_running", return_value=(False, None)),
            patch("gateway.upgrade._find_uv", return_value="uv"),
            patch("subprocess.run", return_value=fail_result),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1

    def test_run_upgrade_missing_meta_exits(self, tmp_path: Path):
        """No install_meta.json → error and exit 1. There is no pip fallback:
        AgentCoop is not on PyPI, and the `agentcoop` name there is someone
        else's package, so `pip install --upgrade` was removed with the
        publishing workflow (#154)."""
        from gateway.upgrade import run_upgrade

        meta_file = tmp_path / "nonexistent_meta.json"

        with (
            patch("gateway.upgrade.META_FILE", meta_file),
            patch("subprocess.run") as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            run_upgrade()

        assert exc_info.value.code == 1
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _file_hash
# ---------------------------------------------------------------------------

class TestFileHash:
    """Tests for _file_hash helper."""

    def test_returns_sha256_for_existing_file(self, tmp_path: Path):
        """Returns a hex SHA256 digest for a file that exists."""
        import hashlib

        from gateway.upgrade import _file_hash

        f = tmp_path / "test.txt"
        f.write_bytes(b"hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert _file_hash(f) == expected

    def test_returns_none_for_missing_file(self, tmp_path: Path):
        """Returns None when the file does not exist."""
        from gateway.upgrade import _file_hash

        assert _file_hash(tmp_path / "nonexistent.txt") is None

    def test_different_contents_yield_different_hashes(self, tmp_path: Path):
        """Two files with different contents produce different hashes."""
        from gateway.upgrade import _file_hash

        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_bytes(b"content-a")
        b.write_bytes(b"content-b")
        assert _file_hash(a) != _file_hash(b)

    def test_same_contents_yield_same_hash(self, tmp_path: Path):
        """Two files with identical contents produce the same hash."""
        from gateway.upgrade import _file_hash

        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_bytes(b"identical")
        b.write_bytes(b"identical")
        assert _file_hash(a) == _file_hash(b)


# ---------------------------------------------------------------------------
# _snapshot_context_hashes
# ---------------------------------------------------------------------------

class TestSnapshotContextHashes:
    """Tests for _snapshot_context_hashes."""

    def test_returns_empty_when_no_contexts_dir(self, tmp_path: Path):
        """Returns {} when repo has no contexts/ directory."""
        from gateway.upgrade import _snapshot_context_hashes

        assert _snapshot_context_hashes(tmp_path) == {}

    def test_returns_hashes_for_all_files(self, tmp_path: Path):
        """Returns a filename→hash dict for every file in contexts/."""
        import hashlib

        from gateway.upgrade import _snapshot_context_hashes

        ctx = tmp_path / "contexts"
        ctx.mkdir()
        (ctx / "a.md").write_bytes(b"content-a")
        (ctx / "b.md").write_bytes(b"content-b")

        result = _snapshot_context_hashes(tmp_path)
        assert set(result.keys()) == {"a.md", "b.md"}
        assert result["a.md"] == hashlib.sha256(b"content-a").hexdigest()
        assert result["b.md"] == hashlib.sha256(b"content-b").hexdigest()

    def test_ignores_subdirectories(self, tmp_path: Path):
        """Subdirectories inside contexts/ are not included."""
        from gateway.upgrade import _snapshot_context_hashes

        ctx = tmp_path / "contexts"
        ctx.mkdir()
        (ctx / "file.md").write_bytes(b"content")
        (ctx / "subdir").mkdir()

        result = _snapshot_context_hashes(tmp_path)
        assert set(result.keys()) == {"file.md"}

    def test_empty_contexts_dir_returns_empty(self, tmp_path: Path):
        """Returns {} when contexts/ directory exists but is empty."""
        from gateway.upgrade import _snapshot_context_hashes

        (tmp_path / "contexts").mkdir()
        assert _snapshot_context_hashes(tmp_path) == {}


# ---------------------------------------------------------------------------
# _sync_context_files
# ---------------------------------------------------------------------------

class TestSyncContextFiles:
    """Tests for the smart context file sync decision table."""

    def _make_repo(self, tmp_path: Path, files: dict[str, bytes]) -> Path:
        """Create a fake repo with contexts/ files and return the repo path."""
        repo = tmp_path / "repo"
        ctx = repo / "contexts"
        ctx.mkdir(parents=True)
        for name, content in files.items():
            (ctx / name).write_bytes(content)
        return repo

    def _make_runtime(self, tmp_path: Path, files: dict[str, bytes]) -> Path:
        """Create a fake runtime dir with contexts/ files and return it."""
        runtime = tmp_path / "runtime"
        ctx = runtime / "contexts"
        ctx.mkdir(parents=True)
        for name, content in files.items():
            (ctx / name).write_bytes(content)
        return runtime

    def test_brand_new_file_is_copied(self, tmp_path: Path):
        """A file in repo but absent from pre_pull_hashes is copied unconditionally."""
        from gateway.upgrade import _sync_context_files

        repo = self._make_repo(tmp_path, {"new.md": b"new content"})
        runtime = self._make_runtime(tmp_path, {})

        _sync_context_files(repo, runtime, pre_pull_hashes={})

        assert (runtime / "contexts" / "new.md").read_bytes() == b"new content"

    def test_missing_user_file_is_copied(self, tmp_path: Path):
        """A file that exists in the repo but not in runtime is copied (first-upgrade case)."""
        import hashlib

        from gateway.upgrade import _sync_context_files

        content = b"existing content"
        repo = self._make_repo(tmp_path, {"existing.md": content})
        # Runtime has no contexts dir at all — simulate old install
        runtime = tmp_path / "runtime"
        runtime.mkdir()

        pre_pull_hashes = {"existing.md": hashlib.sha256(content).hexdigest()}

        _sync_context_files(repo, runtime, pre_pull_hashes=pre_pull_hashes)

        assert (runtime / "contexts" / "existing.md").read_bytes() == content

    def test_unchanged_repo_file_is_skipped(self, tmp_path: Path):
        """When repo file hasn't changed (hash matches pre-pull), user copy is not touched."""
        import hashlib

        from gateway.upgrade import _sync_context_files

        content = b"unchanged content"
        repo = self._make_repo(tmp_path, {"ctx.md": content})
        runtime = self._make_runtime(tmp_path, {"ctx.md": b"user modified version"})

        pre_pull_hashes = {"ctx.md": hashlib.sha256(content).hexdigest()}

        _sync_context_files(repo, runtime, pre_pull_hashes=pre_pull_hashes)

        # User's copy should be untouched
        assert (runtime / "contexts" / "ctx.md").read_bytes() == b"user modified version"

    def test_changed_file_unmodified_by_user_is_overwritten(self, tmp_path: Path):
        """Repo file changed + user copy still matches old repo → overwrite with new version."""
        import hashlib

        from gateway.upgrade import _sync_context_files

        old_content = b"old repo content"
        new_content = b"new repo content"
        repo = self._make_repo(tmp_path, {"ctx.md": new_content})
        # User copy matches the OLD repo version (unmodified)
        runtime = self._make_runtime(tmp_path, {"ctx.md": old_content})

        pre_pull_hashes = {"ctx.md": hashlib.sha256(old_content).hexdigest()}

        _sync_context_files(repo, runtime, pre_pull_hashes=pre_pull_hashes)

        assert (runtime / "contexts" / "ctx.md").read_bytes() == new_content

    def test_changed_file_modified_by_user_saves_default(self, tmp_path: Path):
        """Repo file changed + user copy diverged → save new version as .default, warn."""
        import hashlib

        from gateway.upgrade import _sync_context_files

        old_content = b"old repo content"
        new_content = b"new repo content"
        user_content = b"user customized content"

        repo = self._make_repo(tmp_path, {"ctx.md": new_content})
        runtime = self._make_runtime(tmp_path, {"ctx.md": user_content})

        pre_pull_hashes = {"ctx.md": hashlib.sha256(old_content).hexdigest()}

        _sync_context_files(repo, runtime, pre_pull_hashes=pre_pull_hashes)

        # Original user copy must be untouched
        assert (runtime / "contexts" / "ctx.md").read_bytes() == user_content
        # New repo version saved as .default
        assert (runtime / "contexts" / "ctx.md.default").read_bytes() == new_content

    def test_creates_contexts_dir_if_absent(self, tmp_path: Path):
        """Creates runtime/contexts/ if it does not exist yet."""
        from gateway.upgrade import _sync_context_files

        repo = self._make_repo(tmp_path, {"new.md": b"content"})
        runtime = tmp_path / "runtime"
        runtime.mkdir()  # No contexts/ subdir

        _sync_context_files(repo, runtime, pre_pull_hashes={})

        assert (runtime / "contexts" / "new.md").exists()

    def test_no_contexts_dir_in_repo_is_noop(self, tmp_path: Path):
        """Does nothing when repo has no contexts/ directory."""
        from gateway.upgrade import _sync_context_files

        repo = tmp_path / "repo"
        repo.mkdir()
        runtime = self._make_runtime(tmp_path, {})

        # Should not raise
        _sync_context_files(repo, runtime, pre_pull_hashes={})

    def test_multiple_files_handled_independently(self, tmp_path: Path):
        """Each file follows its own decision path independently."""
        import hashlib

        from gateway.upgrade import _sync_context_files

        old_a = b"old-a"
        new_a = b"new-a"
        unchanged_b = b"b-content"

        repo = self._make_repo(tmp_path, {"a.md": new_a, "b.md": unchanged_b})
        runtime = self._make_runtime(tmp_path, {"a.md": old_a, "b.md": unchanged_b})

        pre_pull_hashes = {
            "a.md": hashlib.sha256(old_a).hexdigest(),
            "b.md": hashlib.sha256(unchanged_b).hexdigest(),
        }

        _sync_context_files(repo, runtime, pre_pull_hashes=pre_pull_hashes)

        # a.md was updated in repo and user had old version → overwrite
        assert (runtime / "contexts" / "a.md").read_bytes() == new_a
        # b.md unchanged in repo → untouched
        assert (runtime / "contexts" / "b.md").read_bytes() == unchanged_b


# ---------------------------------------------------------------------------
# _find_uv
# ---------------------------------------------------------------------------

class TestFindUv:
    """Tests for _find_uv path resolution with fallbacks."""

    def test_returns_shutil_which_result_when_on_path(self):
        """Returns the path from shutil.which when uv is on PATH."""
        from gateway.upgrade import _find_uv

        with patch("shutil.which", return_value="/usr/local/bin/uv"):
            assert _find_uv() == "/usr/local/bin/uv"

    def test_falls_back_to_local_bin(self, tmp_path: Path):
        """Falls back to ~/.local/bin/uv when shutil.which returns None."""
        from gateway.upgrade import _find_uv

        fake_uv = tmp_path / ".local" / "bin" / "uv"
        fake_uv.parent.mkdir(parents=True)
        fake_uv.touch()

        with (
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            assert _find_uv() == str(fake_uv)

    def test_falls_back_to_cargo_bin(self, tmp_path: Path):
        """Falls back to ~/.cargo/bin/uv when ~/.local/bin/uv is absent."""
        from gateway.upgrade import _find_uv

        fake_uv = tmp_path / ".cargo" / "bin" / "uv"
        fake_uv.parent.mkdir(parents=True)
        fake_uv.touch()

        with (
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            assert _find_uv() == str(fake_uv)

    def test_exits_when_uv_not_found_anywhere(self, tmp_path: Path):
        """Calls sys.exit(1) when uv cannot be located by any method."""
        from gateway.upgrade import _find_uv

        with (
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.home", return_value=tmp_path),
            pytest.raises(SystemExit) as exc_info,
        ):
            _find_uv()

        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _ensure_local_bin_symlinks
# ---------------------------------------------------------------------------


class TestEnsureLocalBinSymlinks:
    """do_git_upgrade only runs `git pull` + `uv sync`, so a console script added
    in a later release lands in .venv/bin and never reaches the PATH install.sh
    configured. Every test here redirects Path.home() — this function writes to
    ~/.local/bin, and must never touch the real one."""

    def _setup(self, tmp_path: Path, *, installed: bool, scripts=("coop",)):
        home = tmp_path / "home"
        local_bin = home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        repo = tmp_path / "repo"
        venv_bin = repo / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        for name in scripts:
            (venv_bin / name).write_text("#!/bin/sh\n")
        if installed:
            # install.sh's fingerprint: the primary entrypoint is already linked.
            (local_bin / "coop").symlink_to(venv_bin / "coop")
        return home, local_bin, repo, venv_bin

    def test_links_a_script_added_after_install(self, tmp_path: Path):
        home, local_bin, repo, venv_bin = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )
        assert not (local_bin / "coop-provision").exists()

        with patch("gateway.upgrade.Path.home", return_value=home):
            _ensure_local_bin_symlinks(repo)

        link = local_bin / "coop-provision"
        assert link.is_symlink()
        assert link.resolve() == (venv_bin / "coop-provision").resolve()

    def test_does_nothing_when_not_an_installer_managed_layout(self, tmp_path: Path):
        # No ~/.local/bin/coop => pipx/distro/manual install.
        # Must not inject symlinks the user never asked for.
        home, local_bin, repo, _ = self._setup(
            tmp_path, installed=False, scripts=("coop", "coop-provision")
        )

        with patch("gateway.upgrade.Path.home", return_value=home):
            _ensure_local_bin_symlinks(repo)

        assert list(local_bin.iterdir()) == []

    def test_repoints_a_stale_symlink(self, tmp_path: Path):
        home, local_bin, repo, venv_bin = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )
        stale = tmp_path / "old-repo" / ".venv" / "bin" / "coop-provision"
        stale.parent.mkdir(parents=True)
        stale.write_text("#!/bin/sh\n")
        (local_bin / "coop-provision").symlink_to(stale)

        with patch("gateway.upgrade.Path.home", return_value=home):
            _ensure_local_bin_symlinks(repo)

        assert (local_bin / "coop-provision").resolve() == (venv_bin / "coop-provision").resolve()

    def test_repairs_a_dangling_installer_symlink(self, tmp_path: Path):
        """A repo/venv move leaves the fingerprint symlink dangling — still repair it.

        Path.exists() FOLLOWS symlinks, so a symlink to a target that has gone
        away reads as False. Gating the whole function on exists() alone made it
        bail out in precisely the situation it exists to fix: the primary command
        stays dangling AND no other script gets linked, even though the upgrade
        against the corrected repo path succeeded.
        """
        home, local_bin, repo, venv_bin = self._setup(
            tmp_path, installed=False, scripts=("coop", "coop-provision")
        )
        # The old repo path was never created => this symlink dangles.
        gone = tmp_path / "old-repo" / ".venv" / "bin" / "coop"
        (local_bin / "coop").symlink_to(gone)
        assert (local_bin / "coop").is_symlink()
        assert not (local_bin / "coop").exists()  # the trap

        with patch("gateway.upgrade.Path.home", return_value=home):
            _ensure_local_bin_symlinks(repo)

        # Both are now linked into the current venv, and both actually resolve.
        for name in ("coop", "coop-provision"):
            link = local_bin / name
            assert link.is_symlink(), f"{name} was not linked"
            assert link.exists(), f"{name} still dangles"
            assert link.resolve() == (venv_bin / name).resolve()

    def test_is_idempotent(self, tmp_path: Path):
        home, local_bin, repo, venv_bin = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )
        with patch("gateway.upgrade.Path.home", return_value=home):
            _ensure_local_bin_symlinks(repo)
            first = (local_bin / "coop-provision").resolve()
            _ensure_local_bin_symlinks(repo)

        assert (local_bin / "coop-provision").resolve() == first

    def test_backs_up_a_users_own_regular_file_then_links(self, tmp_path: Path):
        """A real file the user made is preserved, but does not block the link.

        Observed in the wild: a hand-written wrapper that sets PYTHONPATH and pins
        a specific interpreter. An earlier version skipped it silently on the
        grounds that the command was already reachable — but that leaves a worse
        state than it looks, because install_meta.json points `upgrade` at
        repo_path while PATH runs the occupant, so the tool manages a repo whose
        code never executes. Moving it aside keeps the managed command working
        AND loses nothing, which is why replacing beats both skipping (stale
        command) and deleting (destructive).
        """
        home, local_bin, repo, venv_bin = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )
        own = local_bin / "coop-provision"
        own.write_text("# my own wrapper\n")

        with patch("gateway.upgrade.Path.home", return_value=home):
            _ensure_local_bin_symlinks(repo)

        # The managed link now exists and works.
        assert own.is_symlink()
        assert own.resolve() == (venv_bin / "coop-provision").resolve()
        # And the user's file survived, byte-for-byte, under a timestamped name.
        backups = list(local_bin.glob("coop-provision.*.bak"))
        assert len(backups) == 1, f"expected exactly one backup, got {backups}"
        assert backups[0].read_text() == "# my own wrapper\n"
        assert not backups[0].is_symlink()

    def test_restores_the_backup_when_the_new_symlink_cannot_be_made(self, tmp_path: Path):
        """A failure after the backup must not cost the user their command.

        The destination is cleared before the new link is created, so if
        symlink_to() fails (no inodes, a filesystem without symlink support, a
        race) the user is left with LESS than they started with: a working command
        moved into a .bak and nothing on PATH. install.sh's caller then aborts the
        whole install for the entrypoint, so the rollback is what keeps a failure
        from being worse than never having run.
        """
        home, local_bin, repo, _ = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )
        own = local_bin / "coop-provision"
        own.write_text("# my own wrapper\n")

        with patch("gateway.upgrade.Path.home", return_value=home), \
             patch("gateway.upgrade.Path.symlink_to", side_effect=OSError("no inodes")):
            _ensure_local_bin_symlinks(repo)  # must not raise

        # The user's command is back where it was, byte-for-byte...
        assert own.exists(), "the wrapper was not restored"
        assert not own.is_symlink()
        assert own.read_text() == "# my own wrapper\n"
        # ...and the backup was consumed rather than left as a duplicate.
        assert list(local_bin.glob("coop-provision.*.bak")) == []

    def test_restores_a_displaced_foreign_symlink_when_relinking_fails(self, tmp_path: Path):
        """Same guarantee for the symlink branch, which keeps no backup file."""
        home, local_bin, repo, _ = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )
        foreign = tmp_path / "mine" / "my-provisioner.sh"
        foreign.parent.mkdir()
        foreign.write_text("#!/bin/sh\n")
        link = local_bin / "coop-provision"
        link.symlink_to(foreign)

        real_symlink_to = Path.symlink_to
        calls = {"n": 0}

        def fail_first(self, target, target_is_directory=False):
            # Fail the attempt to install OUR link, but let the rollback's own
            # symlink_to succeed — otherwise the test cannot tell a restored link
            # from one that was never removed.
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("no inodes")
            return real_symlink_to(self, target)

        with patch("gateway.upgrade.Path.home", return_value=home), \
             patch("gateway.upgrade.Path.symlink_to", new=fail_first):
            _ensure_local_bin_symlinks(repo)  # must not raise

        assert link.is_symlink()
        assert link.readlink() == foreign
        assert foreign.read_text() == "#!/bin/sh\n"

    def test_a_broken_pipe_cannot_cost_the_user_their_command(self, tmp_path: Path):
        """A dead stdout reader may cost the MESSAGE, never the state.

        rich's Console.print() raises SystemExit(1) when stdout is a broken pipe.
        SystemExit is a BaseException, so `except (OSError, RuntimeError)` does not
        catch it and the rollback does not run. With a print between rename() and
        symlink_to(), `coop upgrade | head -4` left the user's own
        wrapper in a .bak with nothing on PATH — silently, because SystemExit(1)
        prints nothing. Hence all filesystem work happens before any output.

        Asserts state, not ordering, so it stays true if the messages are reworded:
        whatever the print does, the link must exist and the backup must survive.
        """
        home, local_bin, repo, venv_bin = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )
        own = local_bin / "coop-provision"
        own.write_text("# my own wrapper\n")

        with patch("gateway.upgrade.Path.home", return_value=home), \
             patch("gateway.upgrade.console.print", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            _ensure_local_bin_symlinks(repo)

        assert own.is_symlink(), "link was not created before the failing print"
        assert own.resolve() == (venv_bin / "coop-provision").resolve()
        backups = list(local_bin.glob("coop-provision.*.bak"))
        assert len(backups) == 1, f"expected the backup to survive, got {backups}"
        assert backups[0].read_text() == "# my own wrapper\n"

    def test_rollback_completes_before_the_failure_is_reported(self, tmp_path: Path):
        """The failure path carried the same hazard the success path was fixed for.

        Both conditions together, and each is independently plausible: symlink_to()
        fails after the rename (read-only fs, no inodes), and the warning print then
        hits a broken pipe. SystemExit is a BaseException, so reporting before
        restoring skipped the rollback and left the command only in the .bak — the
        identical data loss, reached through the error handler instead of the happy
        path. State first, message second, on both paths.
        """
        home, local_bin, repo, _ = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )
        own = local_bin / "coop-provision"
        own.write_text("# my own wrapper\n")

        with patch("gateway.upgrade.Path.home", return_value=home), \
             patch("gateway.upgrade.Path.symlink_to", side_effect=OSError("read-only fs")), \
             patch("gateway.upgrade.console.print", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            _ensure_local_bin_symlinks(repo)

        assert own.exists(), "the wrapper was not restored before the failing print"
        assert not own.is_symlink()
        assert own.read_text() == "# my own wrapper\n"
        assert list(local_bin.glob("coop-provision.*.bak")) == [], "backup left behind"

    def test_says_where_the_backup_is_when_the_rollback_also_fails(
        self, tmp_path: Path, capsys
    ):
        """The worst reachable state must not also be a silent one.

        Link creation fails, and putting the file back fails too (another process
        took the path, permissions changed). The user then has no command on PATH
        and their original under a timestamped name they never chose — so the name
        is the one thing that has to be printed. Swallowing the rollback error is
        right (it must not replace the real one); swallowing the location is not.
        """
        home, local_bin, repo, _ = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )
        own = local_bin / "coop-provision"
        own.write_text("# my own wrapper\n")

        real_rename = Path.rename

        def rename_then_fail(self, target):
            # Let the move-aside succeed, then fail the rollback's move-back.
            if str(self).endswith(".bak"):
                raise OSError("rollback blocked")
            return real_rename(self, target)

        with patch("gateway.upgrade.Path.home", return_value=home), \
             patch("gateway.upgrade.Path.symlink_to", side_effect=OSError("read-only fs")), \
             patch("gateway.upgrade.Path.rename", new=rename_then_fail):
            _ensure_local_bin_symlinks(repo)  # must not raise

        # ALL whitespace removed, not collapsed: rich wraps at the console width, so
        # a long path arrives split across lines and a naive `name in out` fails on
        # the newline rather than on a missing message.
        flat = "".join(capsys.readouterr().out.split())
        backups = list(local_bin.glob("coop-provision.*.bak"))
        assert len(backups) == 1, "the file should still be in its backup"
        assert backups[0].read_text() == "# my own wrapper\n"
        assert backups[0].name in flat, (
            f"the backup location must be reported; got: {flat}"
        )

    def test_backups_do_not_clobber_each_other(self, tmp_path: Path):
        """Two runs must not have the second backup overwrite the first.

        Path.rename() overwrites silently on POSIX, so a fixed `.bak` name would
        destroy the earlier backup — the exact data loss the backup exists to
        prevent. Runs inside the same second are resolved by a numeric suffix, so
        this holds regardless of how fast the two calls land.
        """
        home, local_bin, repo, _ = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )
        own = local_bin / "coop-provision"

        own.write_text("first\n")
        with patch("gateway.upgrade.Path.home", return_value=home):
            _ensure_local_bin_symlinks(repo)
        # Simulate the user putting a second wrapper back afterwards.
        own.unlink()
        own.write_text("second\n")
        with patch("gateway.upgrade.Path.home", return_value=home):
            _ensure_local_bin_symlinks(repo)

        backups = sorted(p.read_text() for p in local_bin.glob("coop-provision.*.bak"))
        assert backups == ["first\n", "second\n"], backups

    def test_reports_the_old_target_when_repointing_a_foreign_symlink(
        self, tmp_path: Path, capsys
    ):
        """A symlink that is not ours is replaced, but never silently.

        No backup for this case on purpose: the file it points at is untouched, so
        a `.bak` symlink preserves nothing and just accumulates. The reported
        problem was that the replacement was invisible, so the old target is
        printed instead.
        """
        home, local_bin, repo, venv_bin = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )
        foreign_target = tmp_path / "mine" / "my-provisioner.sh"
        foreign_target.parent.mkdir()
        foreign_target.write_text("#!/bin/sh\n")
        (local_bin / "coop-provision").symlink_to(foreign_target)

        with patch("gateway.upgrade.Path.home", return_value=home):
            _ensure_local_bin_symlinks(repo)

        out = capsys.readouterr().out
        assert "my-provisioner.sh" in out.replace("\n", "")
        assert (local_bin / "coop-provision").resolve() == (venv_bin / "coop-provision").resolve()
        # The file it used to point at is untouched, and no litter was created.
        assert foreign_target.read_text() == "#!/bin/sh\n"
        assert list(local_bin.glob("coop-provision.*.bak")) == []

    def test_skips_scripts_absent_from_this_release(self, tmp_path: Path):
        # An older release where coop-provision does not exist in .venv/bin.
        home, local_bin, repo, _ = self._setup(
            tmp_path, installed=True, scripts=("coop",)
        )

        with patch("gateway.upgrade.Path.home", return_value=home):
            _ensure_local_bin_symlinks(repo)

        assert not (local_bin / "coop-provision").exists()

    def test_symlink_failure_is_not_fatal(self, tmp_path: Path, capsys):
        # A failure here must not invalidate an upgrade that already succeeded —
        # but it must SAY so, or PATH is silently left unfixed.
        home, _, repo, _ = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )

        with patch("gateway.upgrade.Path.home", return_value=home), \
             patch("gateway.upgrade.Path.symlink_to", side_effect=OSError("read-only fs")):
            _ensure_local_bin_symlinks(repo)  # must not raise

        out = capsys.readouterr().out
        assert "Warning" in out
        assert "read-only fs" in out, "the cause must reach the user, not just a generic warning"

    def test_a_symlink_cycle_at_the_destination_is_not_fatal(self, tmp_path: Path):
        """A cyclic symlink must not escape as an exception.

        Path.resolve() is version-dependent here: on Python 3.12 a cycle raises
        RuntimeError("Symlink loop from ..."), which is NOT an OSError subclass;
        on 3.13 it returns the path without raising. This project supports both,
        so the only assertion that is meaningful on both legs is the contract
        itself: the call returns instead of propagating. Deliberately does NOT
        assert on console output or on the resulting link state — those legitimately
        differ between 3.12 (warns, leaves the cycle) and 3.13 (falls through and
        relinks), and pinning either would red one CI leg for no defect.

        Why it matters beyond a stray traceback: this runs inside do_git_upgrade(),
        which run_upgrade() calls between stop_daemon() and start_daemon() with no
        try/finally — so anything escaping here leaves the daemon stopped after a
        pull that already succeeded.
        """
        home, local_bin, repo, _ = self._setup(
            tmp_path, installed=True, scripts=("coop", "coop-provision")
        )
        # Two symlinks pointing at each other => resolving either one loops.
        a = local_bin / "coop-provision"
        b = local_bin / "coop-provision-cycle"
        a.symlink_to(b)
        b.symlink_to(a)
        assert a.is_symlink()

        with patch("gateway.upgrade.Path.home", return_value=home):
            _ensure_local_bin_symlinks(repo)  # must not raise on any Python we support


# ---------------------------------------------------------------------------
# run_post_upgrade / _run_post_upgrade_hook
# ---------------------------------------------------------------------------


class TestPostUpgradeHook:
    """The hook exists so post-upgrade logic added in a LATER release can run
    during the upgrade that delivers it.

    `gateway.upgrade` is imported into the upgrading process before `git pull`, so
    the module in memory is the OLD release's, and rewriting the file on disk
    cannot change it — only a subprocess re-imports from disk. Verified end to end
    outside the suite (a parent holding the old module invoked the hook and the
    child executed a step written after import); these tests pin the contract.
    """

    def _repo(self, tmp_path: Path, *, with_python: bool = True) -> Path:
        repo = tmp_path / "repo"
        venv_bin = repo / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        if with_python:
            (venv_bin / "python").write_text("#!/bin/sh\n")
        return repo

    def test_invokes_the_pulled_tree_with_its_own_interpreter(self, tmp_path: Path):
        repo = self._repo(tmp_path)

        with patch("gateway.upgrade.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            _run_post_upgrade_hook(repo, "0.5.1")

        run.assert_called_once()
        argv = run.call_args[0][0]
        assert argv[0] == str(repo / ".venv" / "bin" / "python"), (
            "must use the pulled tree's interpreter, not the running one"
        )
        # from_version must reach the child: version-aware work belongs in
        # run_post_upgrade, and run_migrations cannot do it (frozen parent).
        assert argv[1:] == ["-c", _POST_UPGRADE_BOOTSTRAP, str(repo), "0.5.1"]
        kwargs = run.call_args[1]
        # cwd so `import gateway` resolves from the source tree even if the
        # editable install's .pth is stale.
        assert kwargs["cwd"] == str(repo)
        # A hang here happens while the daemon is stopped, so it must be bounded.
        assert kwargs["timeout"] > 0
        assert kwargs["check"] is False

    def test_bootstrap_is_a_no_op_when_the_pulled_release_predates_it(self, tmp_path: Path):
        """An older ref has no post-upgrade steps, which is not an error.

        Runs the literal string we ship against a real package that lacks the
        entry point. A bare attribute access exits non-zero here and prints a full
        AttributeError traceback, which reads like a failed upgrade immediately
        after a successful one.
        """
        pkg = tmp_path / "gateway"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "upgrade.py").write_text("# a release predating run_post_upgrade\n")

        result = subprocess.run(
            [sys.executable, "-c", _POST_UPGRADE_BOOTSTRAP, str(tmp_path), "0.5.1"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr
        assert "Traceback" not in result.stderr
        assert result.stdout == ""

    def test_bootstrap_passes_repo_and_from_version_through(self, tmp_path: Path):
        """The literal string we ship must deliver BOTH arguments.

        The bootstrap is frozen in the release that runs it, so if it dropped
        from_version a future version-aware step could never receive one, and the
        breakage would only show up a release later.
        """
        pkg = tmp_path / "gateway"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "upgrade.py").write_text(
            "def run_post_upgrade(repo_path, from_version=''):\n"
            "    print(f'GOT {repo_path} | {from_version}')\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", _POST_UPGRADE_BOOTSTRAP, str(tmp_path), "0.4.2"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"GOT {tmp_path} | 0.4.2"

    def test_missing_interpreter_warns_and_does_not_spawn(self, tmp_path: Path, capsys):
        repo = self._repo(tmp_path, with_python=False)

        with patch("gateway.upgrade.subprocess.run") as run:
            _run_post_upgrade_hook(repo)

        run.assert_not_called()
        # The warning is the whole user-visible contract of this branch: without it
        # the upgrade prints a clean "Upgrade complete!" while the post-upgrade
        # steps never ran and ~/.local/bin was never fixed.
        assert "Warning" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "outcome",
        [
            {"return_value": MagicMock(returncode=1)},
            {"side_effect": subprocess.TimeoutExpired(cmd="python", timeout=1)},
            {"side_effect": OSError("exec format error")},
        ],
        ids=["nonzero-exit", "timeout", "oserror"],
    )
    def test_failures_are_never_fatal(self, tmp_path: Path, outcome: dict, capsys):
        """A pull + sync that already succeeded must not be reported as a failure,
        and nothing may escape: this runs between stop_daemon() and start_daemon()
        in run_upgrade(), which has no try/finally (issue #83), so an exception
        here would strand a stopped daemon.

        Also asserts the warning, not only the absence of an exception. Silently
        swallowing these produces the worst outcome available: "Upgrade complete!"
        with the post-upgrade steps never having run.
        """
        repo = self._repo(tmp_path)

        with patch("gateway.upgrade.subprocess.run", **outcome):
            _run_post_upgrade_hook(repo)  # must not raise

        assert "Warning" in capsys.readouterr().out

    def test_run_post_upgrade_ensures_the_console_script_links(self, tmp_path: Path):
        repo = tmp_path / "repo"
        with patch("gateway.upgrade._ensure_local_bin_symlinks") as ensure:
            run_post_upgrade(repo)
        ensure.assert_called_once_with(repo)

    def test_run_post_upgrade_accepts_from_version(self, tmp_path: Path):
        """Signature guard. The `python -c` line that calls this lives in the
        PREVIOUS release, so parameters may gain defaults but must never be
        removed or reordered — otherwise an in-the-wild bootstrap breaks."""
        repo = tmp_path / "repo"
        with patch("gateway.upgrade._ensure_local_bin_symlinks"):
            run_post_upgrade(repo)  # positional-only, as an older bootstrap sends
            run_post_upgrade(repo, "0.5.1")  # as the current bootstrap sends

    def test_do_git_upgrade_goes_through_the_hook(self, tmp_path: Path):
        """Regression guard for the whole mechanism.

        Calling _ensure_local_bin_symlinks() directly from do_git_upgrade looks
        equivalent and is silently broken: it runs the OLD release's logic, which
        is the bug the hook exists to fix. Nothing else would catch that, because
        both spellings behave identically whenever the code happens to be
        unchanged — which is every test that does not simulate a pull.
        """
        repo = tmp_path / "repo"
        repo.mkdir()

        with patch("gateway.upgrade.subprocess.run") as run, \
             patch("gateway.upgrade._find_uv", return_value="uv"), \
             patch("gateway.upgrade._snapshot_context_hashes", return_value={}), \
             patch("gateway.upgrade._sync_context_files"), \
             patch("gateway.upgrade._ensure_local_bin_symlinks") as ensure, \
             patch("gateway.upgrade._run_post_upgrade_hook") as hook:
            run.return_value = MagicMock(returncode=0)
            do_git_upgrade(repo, "0.5.1")

        hook.assert_called_once_with(repo, "0.5.1")
        ensure.assert_not_called()

    def test_the_hook_runs_after_uv_sync_not_before(self, tmp_path: Path):
        """Position is part of the mechanism, so pinning the call alone is not enough.

        The child links whatever console scripts exist in .venv/bin. Run it before
        `uv sync` and the pulled release's newly added scripts are not built yet, so
        `if not target.exists(): continue` links nothing — and silently, since the
        function warns only on failure. Verified that the previous assertions did not
        catch this: moving the call above `_find_uv()` left the suite fully green.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        events: list[str] = []

        def record_run(argv, **kwargs):
            events.append(f"run:{argv[0]}")
            return MagicMock(returncode=0)

        with patch("gateway.upgrade.subprocess.run", side_effect=record_run), \
             patch("gateway.upgrade._find_uv", return_value="uv"), \
             patch("gateway.upgrade._snapshot_context_hashes", return_value={}), \
             patch("gateway.upgrade._sync_context_files"), \
             patch(
                 "gateway.upgrade._run_post_upgrade_hook",
                 side_effect=lambda *a, **k: events.append("hook"),
             ):
            do_git_upgrade(repo, "0.5.1")

        assert events == ["run:git", "run:uv", "hook"], (
            f"the hook must run after both git pull and uv sync, got {events}"
        )
