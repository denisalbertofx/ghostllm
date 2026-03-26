"""Windows / legacy console: UTF-8 stdio bootstrap and ASCII UI flag."""
from __future__ import annotations

import importlib
import os
import sys
import unittest


class TestTerminalCapability(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("GHOST_ASCII_UI", None)
        mod = sys.modules.get("apps.cli.runtime.terminal_capability")
        if mod is not None:
            mod._CONFIGURED = False
            mod._ASCII_UI = None
            mod._NOTICE_EMITTED = False

    def test_ghost_ascii_ui_forced_by_env(self) -> None:
        os.environ["GHOST_ASCII_UI"] = "1"
        tc = importlib.import_module("apps.cli.runtime.terminal_capability")
        tc._CONFIGURED = False
        tc._ASCII_UI = None
        tc._NOTICE_EMITTED = False
        tc.configure_cli_stdio_early()
        self.assertTrue(tc.ghost_ui_uses_ascii())

    def test_configure_is_idempotent(self) -> None:
        tc = importlib.import_module("apps.cli.runtime.terminal_capability")
        tc._CONFIGURED = False
        tc._ASCII_UI = None
        tc._NOTICE_EMITTED = False
        tc.configure_cli_stdio_early()
        first = tc._ASCII_UI
        tc.configure_cli_stdio_early()
        self.assertEqual(tc._ASCII_UI, first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
