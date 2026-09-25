"""Unit tests for gateway/configtool/__init__.py's run_app() — the pre-launch
tty guard.

`ConfigToolApp.run()` is mocked out (it would otherwise try to take over a
real terminal); these tests only cover what happens before it is constructed.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from gateway.configtool import run_app


class TestRunAppTtyGuard(unittest.TestCase):
    def test_interactive_terminal_launches_the_app(self):
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("gateway.configtool.app.ConfigToolApp") as mock_app_cls,
        ):
            code = run_app("some/config.yaml", lint=True)

        self.assertEqual(code, 0)
        mock_app_cls.assert_called_once_with(config_path="some/config.yaml", lint=True)
        mock_app_cls.return_value.run.assert_called_once()

    def test_non_interactive_terminal_is_rejected_before_the_app_is_built(self):
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch("sys.stdout.isatty", return_value=True),
            patch("gateway.configtool.app.ConfigToolApp") as mock_app_cls,
        ):
            code = run_app("some/config.yaml")

        self.assertEqual(code, 1)
        mock_app_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
