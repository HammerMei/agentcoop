"""`coop upgrade` brings ~/.agentcoop/agents/builtin/coop-keeper/ up to the shipped
release by manifest, and touches nothing else (coop-keeper design §3.1 as amended:
a forever-growing list of owned paths, `in-use` overwritten, `obsolete` removed,
everything unlisted left alone).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.upgrade import (
    DEFAULT_RUNTIME_DIR,
    KEEPER_DST_REL,
    KEEPER_IN_USE,
    KEEPER_OBSOLETE,
    KEEPER_SRC_REL,
    _read_keeper_manifest,
    keeper_text_for,
    run_post_upgrade,
    sync_keeper_dir,
)
from tests.helpers import assert_tree_copied

REPO = Path(__file__).resolve().parents[2]


class _Asserter:
    """assert_tree_copied wants a TestCase-like assertEqual; this suite is pytest-style."""

    def assertEqual(self, a, b, msg=None):
        assert a == b, msg


def _make_repo(tmp_path: Path, files: dict[str, str], manifest: str) -> Path:
    """Local on purpose: it builds a *shipped keeper tree* (files under
    agents/coop-keeper plus a manifest), which no other suite needs — the
    gateway-config builders in tests/helpers.py model a different thing."""
    repo = tmp_path / "repo"
    src = repo / KEEPER_SRC_REL
    for rel, content in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (src / "manifest.yaml").write_text(manifest)
    return repo


MANIFEST = f"""\
AGENTS.md: {KEEPER_IN_USE}
manifest.yaml: {KEEPER_IN_USE}
.claude/settings.json: {KEEPER_IN_USE}
.claude/skills/coop-add-bot: {KEEPER_IN_USE}
.claude/skills/old-skill: {KEEPER_OBSOLETE}
retired.md: {KEEPER_OBSOLETE}
"""

FILES = {
    "AGENTS.md": "new agents\n",
    ".claude/settings.json": "{}\n",
    ".claude/skills/coop-add-bot/SKILL.md": "new skill\n",
}


class TestSyncKeeperDir:
    def test_creates_the_directory_on_an_install_that_has_none(self, tmp_path: Path):
        """v1.0.0 installs have no agents/ at all."""
        repo = _make_repo(tmp_path, FILES, MANIFEST)
        runtime = tmp_path / "runtime"
        runtime.mkdir()

        sync_keeper_dir(repo, runtime)

        dst = runtime / KEEPER_DST_REL
        assert (dst / "AGENTS.md").read_text() == "new agents\n"
        assert (dst / ".claude" / "skills" / "coop-add-bot" / "SKILL.md").exists()
        assert (dst / "manifest.yaml").exists()
        assert (runtime / "agents" / "user").is_dir()

    def test_in_use_file_is_overwritten(self, tmp_path: Path):
        repo = _make_repo(tmp_path, FILES, MANIFEST)
        dst = tmp_path / "runtime" / KEEPER_DST_REL
        dst.mkdir(parents=True)
        (dst / "AGENTS.md").write_text("old agents\n")

        sync_keeper_dir(repo, tmp_path / "runtime")

        assert (dst / "AGENTS.md").read_text() == "new agents\n"

    def test_in_use_directory_is_replaced_as_a_unit(self, tmp_path: Path):
        """A file a release dropped from inside a shipped skill does not linger."""
        repo = _make_repo(tmp_path, FILES, MANIFEST)
        dst = tmp_path / "runtime" / KEEPER_DST_REL
        skill = dst / ".claude" / "skills" / "coop-add-bot"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("old skill\n")
        (skill / "stale-helper.md").write_text("gone in this release\n")

        sync_keeper_dir(repo, tmp_path / "runtime")

        assert (skill / "SKILL.md").read_text() == "new skill\n"
        assert not (skill / "stale-helper.md").exists()

    def test_obsolete_paths_are_removed_file_and_directory(self, tmp_path: Path):
        repo = _make_repo(tmp_path, FILES, MANIFEST)
        dst = tmp_path / "runtime" / KEEPER_DST_REL
        old = dst / ".claude" / "skills" / "old-skill"
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text("obsolete\n")
        (dst / "retired.md").write_text("obsolete\n")

        sync_keeper_dir(repo, tmp_path / "runtime")

        assert not old.exists()
        assert not (dst / "retired.md").exists()

    def test_obsolete_path_that_is_already_gone_is_fine(self, tmp_path: Path):
        repo = _make_repo(tmp_path, FILES, MANIFEST)
        sync_keeper_dir(repo, tmp_path / "runtime")  # nothing to remove; must not raise

    def test_unlisted_paths_are_never_touched(self, tmp_path: Path):
        """settings.local.json, an operator's notes, an operator-added skill —
        none named in the manifest, all survive without being named."""
        repo = _make_repo(tmp_path, FILES, MANIFEST)
        dst = tmp_path / "runtime" / KEEPER_DST_REL
        (dst / ".claude" / "skills" / "my-own-skill").mkdir(parents=True)
        (dst / ".claude" / "skills" / "my-own-skill" / "SKILL.md").write_text("mine\n")
        (dst / ".claude" / "settings.local.json").write_text('{"mine": true}\n')
        (dst / "notes.md").write_text("operator notes\n")

        sync_keeper_dir(repo, tmp_path / "runtime")

        assert (dst / ".claude" / "skills" / "my-own-skill" / "SKILL.md").read_text() == "mine\n"
        assert (dst / ".claude" / "settings.local.json").read_text() == '{"mine": true}\n'
        assert (dst / "notes.md").read_text() == "operator notes\n"

    def test_is_idempotent(self, tmp_path: Path):
        repo = _make_repo(tmp_path, FILES, MANIFEST)
        sync_keeper_dir(repo, tmp_path / "runtime")
        before = sorted(p.relative_to(tmp_path) for p in (tmp_path / "runtime").rglob("*"))
        sync_keeper_dir(repo, tmp_path / "runtime")
        after = sorted(p.relative_to(tmp_path) for p in (tmp_path / "runtime").rglob("*"))
        assert before == after

    def test_no_shipped_keeper_dir_is_a_noop(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        sync_keeper_dir(repo, tmp_path / "runtime")
        assert not (tmp_path / "runtime").exists()

    def test_in_use_entry_the_release_does_not_ship_is_skipped_with_a_warning(
        self, tmp_path: Path, capsys
    ):
        repo = _make_repo(tmp_path, FILES, MANIFEST + "missing.md: in-use\n")
        sync_keeper_dir(repo, tmp_path / "runtime")
        assert "missing.md" in capsys.readouterr().out
        assert not (tmp_path / "runtime" / KEEPER_DST_REL / "missing.md").exists()


class TestReadKeeperManifest:
    @pytest.mark.parametrize(
        "bad",
        [
            "- a\n- b\n",                       # a list, not a mapping
            "AGENTS.md: current\n",             # unknown status
            "/etc/passwd: obsolete\n",          # absolute path
            "../outside: obsolete\n",           # escapes the directory
        ],
    )
    def test_rejects_a_manifest_it_cannot_trust(self, tmp_path: Path, bad: str):
        src = tmp_path / "src"
        src.mkdir()
        (src / "manifest.yaml").write_text(bad)
        with pytest.raises(ValueError):
            _read_keeper_manifest(src)

    def test_missing_manifest_raises(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        with pytest.raises(OSError):
            _read_keeper_manifest(src)

    def test_the_shipped_manifest_is_readable(self):
        manifest = _read_keeper_manifest(REPO / KEEPER_SRC_REL)
        assert manifest["manifest.yaml"] == "in-use"


class TestRunPostUpgradeSyncsTheKeeper:
    def test_runs_after_the_symlink_step_against_runtime_dir(self, tmp_path: Path):
        repo = tmp_path / "repo"
        with patch("gateway.upgrade._ensure_local_bin_symlinks"), \
             patch("gateway.upgrade.sync_keeper_dir") as sync, \
             patch("gateway.upgrade.RUNTIME_DIR", tmp_path / "rt"):
            run_post_upgrade(repo, from_version="1.0.0")
        sync.assert_called_once_with(repo, tmp_path / "rt")

    def test_a_manifest_problem_is_a_warning_not_a_crash(self, tmp_path: Path, capsys):
        """Contract of run_post_upgrade: skippable, never fatal."""
        repo = tmp_path / "repo"
        with patch("gateway.upgrade._ensure_local_bin_symlinks"), \
             patch("gateway.upgrade.sync_keeper_dir", side_effect=ValueError("bad manifest")):
            run_post_upgrade(repo, from_version="1.0.0")
        assert "bad manifest" in capsys.readouterr().out

    def test_a_v0_install_does_not_sync(self, tmp_path: Path):
        repo = tmp_path / "repo"
        with patch("gateway.upgrade._ensure_local_bin_symlinks"), \
             patch("gateway.upgrade.sync_keeper_dir") as sync:
            run_post_upgrade(repo, from_version="0.5.1")
        sync.assert_not_called()

    def test_the_shipped_tree_round_trips_through_the_sync(self, tmp_path: Path):
        """The real agents/coop-keeper/ syncs into an empty runtime dir and
        every shipped file arrives — byte for byte when the runtime dir is the
        default one, which is what `DEFAULT_RUNTIME_DIR` patched to `runtime`
        makes it here."""
        runtime = tmp_path / "runtime"
        with patch("gateway.upgrade.DEFAULT_RUNTIME_DIR", runtime):
            sync_keeper_dir(REPO, runtime)
        assert_tree_copied(_Asserter(), REPO / KEEPER_SRC_REL, runtime / KEEPER_DST_REL)


class TestKeeperFilesNameTheRuntimeDir:
    """The shipped keeper files spell the default runtime directory —
    `~/.agentcoop/...` in Claude permission rules and the skills' prose,
    `*/.agentcoop/...` in opencode's globs. Under a non-default `COOP_HOME` the
    allow globs would miss every path the keeper touches (each edit prompts)
    and the deny globs would miss the credential files (the guardrail is
    silently off). The sync rewrites them to the installation's directory
    (#182, gap C); the default installation keeps the shipped bytes."""

    def test_a_non_default_runtime_dir_is_written_into_the_permission_files(self, tmp_path: Path):
        runtime = tmp_path / "coop"
        sync_keeper_dir(REPO, runtime)
        dst = runtime / KEEPER_DST_REL
        settings = (dst / ".claude" / "settings.json").read_text()
        # `//` — a single leading `/` would be relative to the settings source.
        assert f"Edit(/{runtime}/agents/**)" in settings
        assert f"Read(/{runtime}/config.yaml)" in settings
        assert "~/.agentcoop" not in settings
        opencode = (dst / "opencode.json").read_text()
        assert f'"{runtime}/agents/*": "allow"' in opencode
        # The read/edit globs keep their `*/` shape; only the directory name moves.
        assert '"*/coop/config.yaml": "deny"' in opencode
        assert '"*/coop/agents/.bot-password.*": "deny"' in opencode
        assert ".agentcoop" not in opencode

    def test_the_skills_prose_names_the_same_directory(self, tmp_path: Path):
        runtime = tmp_path / "coop"
        sync_keeper_dir(REPO, runtime)
        dst = runtime / KEEPER_DST_REL
        for rel in ("AGENTS.md", ".claude/skills/coop-add-bot/SKILL.md",
                    ".claude/skills/coop-apply-config/SKILL.md"):
            text = (dst / rel).read_text()
            assert "~/.agentcoop" not in text, rel
            assert f"{runtime}/agents" in text, rel

    def test_the_default_runtime_dir_keeps_the_shipped_bytes(self, tmp_path: Path):
        for rel in (".claude/settings.json", "opencode.json", "AGENTS.md"):
            assert keeper_text_for("~/.agentcoop/x */.agentcoop/y", DEFAULT_RUNTIME_DIR, rel) == \
                "~/.agentcoop/x */.agentcoop/y"

    def test_keeper_text_for_writes_the_form_each_reader_understands(self, tmp_path: Path):
        base = tmp_path / "coop"
        assert keeper_text_for("Read(~/.agentcoop/x)", base, ".claude/settings.json") == f"Read(/{base}/x)"
        assert keeper_text_for('"~/.agentcoop/agents/*" "*/.agentcoop/y"', base, "opencode.json") == \
            f'"{base}/agents/*" "*/coop/y"'
        assert keeper_text_for("see ~/.agentcoop/agents", base, ".claude/skills/coop-add-bot/SKILL.md") == \
            f"see {base}/agents"

    def test_a_manifest_directory_is_rewritten_file_by_file(self, tmp_path: Path):
        repo = _make_repo(tmp_path, {
            "AGENTS.md": "see ~/.agentcoop/agents/user/\n",
            ".claude/settings.json": "{}\n",
            ".claude/skills/coop-add-bot/SKILL.md": "mkdir -p ~/.agentcoop/agents/user/x\n",
        }, MANIFEST)
        runtime = tmp_path / "coop"
        sync_keeper_dir(repo, runtime)
        dst = runtime / KEEPER_DST_REL
        assert (dst / "AGENTS.md").read_text() == f"see {runtime}/agents/user/\n"
        assert (dst / ".claude/skills/coop-add-bot/SKILL.md").read_text() == f"mkdir -p {runtime}/agents/user/x\n"
