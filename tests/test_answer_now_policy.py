"""Unified answer-now policy for bounded code questions."""
from __future__ import annotations

import json
import unittest

from apps.cli.runtime.answer_now_policy import (
    answer_now_policy_enabled,
    bounded_code_task_kind,
    decide_answer_now_after_tools,
    is_high_ambiguity_or_broad_task,
)


class TestAnswerNowPolicy(unittest.TestCase):
    def test_broad_task_blocks(self) -> None:
        self.assertTrue(
            is_high_ambiguity_or_broad_task("Redesign the entire codebase architecture for microservices")
        )
        self.assertFalse(is_high_ambiguity_or_broad_task("List .py files in apps/cli (top level only)"))

    def test_bounded_kind_lists_factual(self) -> None:
        self.assertEqual(
            bounded_code_task_kind(
                "Lista los .py directamente dentro de apps/cli/runtime/adapters (solo primer nivel)."
            ),
            "shallow_list",
        )
        self.assertEqual(
            bounded_code_task_kind("¿Qué hace la función foo y en qué archivo está?"),
            "narrow_factual",
        )

    def test_decide_shallow_list_chat(self) -> None:
        task = (
            "Lista los .py directamente dentro de apps/cli/runtime/adapters "
            "(solo primer nivel, sin subcarpetas)."
        )
        hist = [
            {"role": "user", "content": task},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "ls", "arguments": json.dumps({"path": "apps/cli/runtime/adapters"})},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "ls",
                "content": json.dumps({"files": ["a.py"]}),
            },
        ]
        dec = decide_answer_now_after_tools(
            task_text=task,
            history=hist,
            contract_spec=None,
            discovery_action_count=1,
            explore_act_blocked=False,
        )
        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertEqual(dec.kind, "shallow_list")

    def test_decide_factual(self) -> None:
        task = "¿Qué hace la función seal_task_contract_after_intake?"
        src = "def seal_task_contract_after_intake():\n    '''doc'''\n    return\n"
        hist = [
            {"role": "user", "content": task},
            {
                "role": "tool",
                "name": "read_file",
                "content": json.dumps({"content": src}),
            },
        ]
        dec = decide_answer_now_after_tools(
            task_text=task,
            history=hist,
            contract_spec=None,
            discovery_action_count=1,
            explore_act_blocked=False,
        )
        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertEqual(dec.kind, "narrow_factual")

    def test_policy_enabled_default(self) -> None:
        self.assertTrue(answer_now_policy_enabled())

    def test_redundant_identical_ls_triggers_without_extension_match(self) -> None:
        """Two identical ls on scope path: no new info even if .py filter not satisfied."""
        task = (
            "Lista los .py directamente dentro de apps/cli/runtime/adapters "
            "(solo primer nivel, sin subcarpetas)."
        )
        hist = [
            {"role": "user", "content": task},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "ls", "arguments": json.dumps({"path": "apps/cli/runtime/adapters"})},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "ls",
                "content": json.dumps({"files": ["readme.md"]}),
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "ls", "arguments": json.dumps({"path": "apps/cli/runtime/adapters"})},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c2",
                "name": "ls",
                "content": json.dumps({"files": ["readme.md"]}),
            },
        ]
        dec = decide_answer_now_after_tools(
            task_text=task,
            history=hist,
            contract_spec=None,
            discovery_action_count=2,
            explore_act_blocked=False,
        )
        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertEqual(dec.kind, "shallow_list")
        self.assertEqual(dec.evidence_reason, "repeated_identical_ls_no_delta")

    def test_bounded_kind_repo_overview(self) -> None:
        self.assertEqual(
            bounded_code_task_kind("/plan listame todo los archivos de esta app y dime el stack tecnologico cual es"),
            "repo_overview",
        )

    def test_decide_repo_overview_after_summary(self) -> None:
        task = "/plan listame todo los archivos de esta app y dime el stack tecnologico cual es"
        hist = [
            {"role": "user", "content": task},
            {"role": "tool", "name": "summarize_repo", "content": json.dumps({"summary": "Runtime: polyglot"})},
        ]
        dec = decide_answer_now_after_tools(
            task_text=task,
            history=hist,
            contract_spec=None,
            discovery_action_count=1,
            explore_act_blocked=True,
        )
        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertEqual(dec.kind, "repo_overview")


if __name__ == "__main__":
    unittest.main(verbosity=2)
