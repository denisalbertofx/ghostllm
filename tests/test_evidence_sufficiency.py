"""Centralized evidence sufficiency for bounded read-only tasks (RO-2 / RO-3)."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from apps.cli.runtime.evidence_sufficiency import (
    evaluate_evidence_sufficiency_for_task,
    evidence_sufficient_immediate_synthesis_enabled,
    should_run_immediate_synthesis_after_sufficiency,
    sufficiency_result_to_answer_now_tuple,
    summarize_gathered_evidence,
)


class TestEvidenceSufficiency(unittest.TestCase):
    def test_summarize_ls_and_read(self) -> None:
        hist = [
            {"role": "tool", "name": "ls", "content": json.dumps({"files": ["a.py"]})},
            {"role": "tool", "name": "read_file", "content": json.dumps({"content": "x" * 10})},
        ]
        s = summarize_gathered_evidence(hist)
        self.assertEqual(s.ls_success_count, 1)
        self.assertEqual(s.read_file_success_count, 1)
        self.assertGreaterEqual(s.read_file_approx_chars, 10)

    def test_tuple_none_when_insufficient(self) -> None:
        r = evaluate_evidence_sufficiency_for_task(
            task_text="Redesign the entire codebase architecture",
            history=[],
            contract_spec=None,
            discovery_action_count=0,
            explore_act_blocked=False,
        )
        self.assertFalse(r.sufficient)
        self.assertIsNone(sufficiency_result_to_answer_now_tuple(r))

    def test_shallow_list_sufficient_ro3_style(self) -> None:
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
        r = evaluate_evidence_sufficiency_for_task(
            task_text=task,
            history=hist,
            contract_spec=None,
            discovery_action_count=1,
            explore_act_blocked=False,
        )
        self.assertTrue(r.sufficient)
        self.assertEqual(r.task_family, "shallow_list")
        t = sufficiency_result_to_answer_now_tuple(r)
        self.assertIsNotNone(t)
        assert t is not None
        self.assertEqual(t[0], "shallow_list")
        self.assertEqual(t[2], "readonly")

    def test_narrow_factual_sufficient_ro2_style(self) -> None:
        task = "¿Qué hace la función seal_task_contract_after_intake?"
        src = "def seal_task_contract_after_intake():\n    '''doc'''\n    return\n"
        hist = [
            {"role": "user", "content": task},
            {"role": "tool", "name": "read_file", "content": json.dumps({"content": src})},
        ]
        r = evaluate_evidence_sufficiency_for_task(
            task_text=task,
            history=hist,
            contract_spec=None,
            discovery_action_count=1,
            explore_act_blocked=False,
        )
        self.assertTrue(r.sufficient)
        self.assertEqual(r.task_family, "narrow_factual")
        self.assertEqual(r.synthesis_context, "factual")

    def test_no_discovery_yet_insufficient(self) -> None:
        task = "¿Qué hace la función seal_task_contract_after_intake?"
        hist = [{"role": "user", "content": task}]
        r = evaluate_evidence_sufficiency_for_task(
            task_text=task,
            history=hist,
            contract_spec=None,
            discovery_action_count=0,
            explore_act_blocked=False,
        )
        self.assertFalse(r.sufficient)
        self.assertEqual(r.reason, "no_discovery_yet")

    def test_should_run_respects_micro_task_gate(self) -> None:
        class _Micro:
            micro_task_kind = "shallow_list"

        class _NoMicro:
            micro_task_kind = None

        with patch.dict(
            os.environ,
            {
                "GHOST_EVIDENCE_SUFFICIENT_IMMEDIATE_SYNTHESIS": "1",
                "GHOST_MICRO_TASK_IMMEDIATE_SYNTHESIS": "0",
            },
        ):
            self.assertFalse(should_run_immediate_synthesis_after_sufficiency(_Micro()))
            self.assertTrue(should_run_immediate_synthesis_after_sufficiency(_NoMicro()))
        with patch.dict(os.environ, {"GHOST_EVIDENCE_SUFFICIENT_IMMEDIATE_SYNTHESIS": "0"}):
            self.assertFalse(should_run_immediate_synthesis_after_sufficiency(_Micro()))

    def test_immediate_synthesis_env_on(self) -> None:
        with patch.dict(os.environ, {"GHOST_EVIDENCE_SUFFICIENT_IMMEDIATE_SYNTHESIS": "1"}):
            self.assertTrue(evidence_sufficient_immediate_synthesis_enabled())

    def test_repo_overview_sufficient_after_summary(self) -> None:
        task = "/plan listame todo los archivos de esta app y dime el stack tecnologico cual es"
        hist = [
            {"role": "user", "content": task},
            {"role": "tool", "name": "summarize_repo", "content": json.dumps({"summary": "Runtime: polyglot"})},
        ]
        r = evaluate_evidence_sufficiency_for_task(
            task_text=task,
            history=hist,
            contract_spec=None,
            discovery_action_count=1,
            explore_act_blocked=True,
        )
        self.assertTrue(r.sufficient)
        self.assertEqual(r.task_family, "repo_overview")
        self.assertEqual(r.synthesis_context, "readonly")


if __name__ == "__main__":
    unittest.main(verbosity=2)
