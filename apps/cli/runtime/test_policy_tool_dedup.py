"""Tests for canonical tool invocation keys (policy denial dedup)."""
from __future__ import annotations

import unittest

from apps.cli.runtime.policy_tool_dedup import canonical_tool_invocation_key


class TestPolicyToolDedup(unittest.TestCase):
    def test_read_file_path_and_key_order_irrelevant(self):
        a = canonical_tool_invocation_key(
            "read_file",
            {"path": "./src/Foo.py", "max_lines": 50, "start_line": 1},
        )
        b = canonical_tool_invocation_key(
            "read_file",
            {"start_line": 1, "path": "src/foo.py", "max_lines": 50},
        )
        self.assertEqual(a, b)

    def test_read_file_start_line_one_full_read_equivalence(self):
        a = canonical_tool_invocation_key("read_file", {"path": "x.py", "start_line": 1})
        b = canonical_tool_invocation_key("read_file", {"path": "x.py"})
        self.assertEqual(a, b)

    def test_run_shell_whitespace_normalized(self):
        a = canonical_tool_invocation_key("run_shell", {"command": "pytest  -q"})
        b = canonical_tool_invocation_key("run_shell", {"command": "pytest -q"})
        self.assertEqual(a, b)

    def test_generic_omits_empty_defaults(self):
        a = canonical_tool_invocation_key(
            "edit_file",
            {"path": "./a.ts", "old_str": "", "hint": "x"},
        )
        b = canonical_tool_invocation_key(
            "edit_file",
            {"path": "a.ts", "old_str": None, "hint": "x"},
        )
        self.assertEqual(a, b)
