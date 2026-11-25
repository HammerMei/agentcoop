"""The ``coop`` binary routes on its first word (coop/entry.py): the operator's
commands, the agent's, and the frozen gateway's whole tree under ``legacy``."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from coop import entry
from coop.entry import AGENT_COMMANDS, main


def test_the_console_script_is_the_dispatcher():
    import tomllib
    from pathlib import Path

    scripts = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["coop"] == "coop.entry:main"


def test_no_arguments_or_help_prints_the_three_families(capsys):
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "coop legacy <command>" in err and "read send done bio attachment" in err and "start stop status" in err
    assert main(["--help"]) == 0 and "coop legacy" in capsys.readouterr().out


def test_an_agent_command_goes_to_the_agent_cli_with_its_arguments():
    with patch("coop.agent.cli.main", return_value=7) as agent_main:
        assert main(["read", "--room", "team", "--gateway", "http://x", "--token", "t"]) == 7
    agent_main.assert_called_once_with(["read", "--room", "team", "--gateway", "http://x", "--token", "t"])
    assert set(AGENT_COMMANDS) == {"read", "send", "done", "bio", "attachment"}


def test_an_operator_command_goes_to_the_operator_cli():
    with patch("coop.cli.main", return_value=3) as operator_main:
        assert main(["status"]) == 3
    operator_main.assert_called_once_with(["status"])


def test_legacy_runs_the_old_tree_with_the_arguments_where_it_looks_and_its_exit_as_the_return(capsys):
    assert main(["legacy", "--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: coop legacy") and "schedule" in out and "fetch-history" in out
    assert sys.argv[0] == "coop legacy"
    assert main(["legacy"]) == 1  # no command: the old main prints help and exits 1
    assert main(["legacy", "no-such-command"]) == 2  # argparse's usage error


def _old_commands() -> list[str]:
    from gateway.cli import _build_parser

    return sorted(_build_parser()._subparsers._group_actions[0].choices)


@pytest.mark.parametrize("command", _old_commands())
def test_every_old_subcommand_is_reachable_through_legacy(command, capsys):
    """``coop legacy <old command> --help`` reaches the old parser's own help: the
    tree is whole, and its usage names the new form."""
    assert main(["legacy", command, "--help"]) == 0
    assert capsys.readouterr().out.startswith(f"usage: coop legacy {command}")


def test_the_agent_side_imports_nothing_of_the_daemon_or_the_old_package():
    """``coop read`` pays for its own client only (the agent CLI is kept apart from
    the operator's on purpose)."""
    import subprocess

    code = ("import sys; import coop.entry; from coop.agent import cli; "
            "print(sorted(m for m in sys.modules if m.startswith(('gateway', 'coop.cli', 'coop.service', 'coop.config'))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]", out


def test_legacy_is_not_a_name_the_other_families_use():
    assert entry.LEGACY not in AGENT_COMMANDS
    from coop.cli import parser

    assert entry.LEGACY not in parser()._subparsers._group_actions[0].choices
