"""Repeated identical ls detection for small-scope shallow listing tasks."""
from __future__ import annotations

import json
import unittest

from apps.cli.runtime.exploration_redundancy import (
    iter_ls_events_chronological,
    redundant_ls_answer_now_enabled,
    shallow_scope_has_repeated_identical_ls,
)


class TestExplorationRedundancy(unittest.TestCase):
    def test_iter_ls_two_rounds(self) -> None:
        hist = [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "ls", "arguments": json.dumps({"path": "apps/foo"})},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "a", "name": "ls", "content": json.dumps({"files": ["a.py"]})},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "b",
                        "type": "function",
                        "function": {"name": "ls", "arguments": json.dumps({"path": "apps/foo"})},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "b", "name": "ls", "content": json.dumps({"files": ["a.py"]})},
        ]
        ev = iter_ls_events_chronological(hist)
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0][1], ev[1][1])

    def test_repeated_identical_on_scope(self) -> None:
        hist = [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "ls", "arguments": json.dumps({"path": "apps/cli/runtime/adapters"})},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "a",
                "name": "ls",
                "content": json.dumps({"files": ["x.md"]}),
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "b",
                        "type": "function",
                        "function": {"name": "ls", "arguments": json.dumps({"path": "apps/cli/runtime/adapters"})},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "b",
                "name": "ls",
                "content": json.dumps({"files": ["x.md"]}),
            },
        ]
        self.assertTrue(
            shallow_scope_has_repeated_identical_ls(hist, "apps/cli/runtime/adapters"),
        )

    def test_different_second_listing_not_redundant(self) -> None:
        hist = [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "ls", "arguments": json.dumps({"path": "apps/foo"})},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "a", "name": "ls", "content": json.dumps({"files": ["a.py"]})},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "b",
                        "type": "function",
                        "function": {"name": "ls", "arguments": json.dumps({"path": "apps/foo"})},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "b", "name": "ls", "content": json.dumps({"files": ["b.py"]})},
        ]
        self.assertFalse(shallow_scope_has_repeated_identical_ls(hist, "apps/foo"))

    def test_redundant_policy_enabled_default(self) -> None:
        self.assertTrue(redundant_ls_answer_now_enabled())


if __name__ == "__main__":
    unittest.main(verbosity=2)
