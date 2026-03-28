"""Regresión: `/do` no puede quedar con contrato analysis + should_not_write."""
from __future__ import annotations

import unittest

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.slash_execute_contract import apply_slash_execute_write_overrides
from apps.cli.runtime.task_contract import (
    Intent,
    ensure_task_contract_foundation,
    update_task_contract_after_spec_apply,
)


class TestSlashExecuteContract(unittest.TestCase):
    def test_do_overrides_analysis_should_not_write(self) -> None:
        s = ArtifactSession("sid_x", "corrige un bug pequeño en login", task_id="task_1")
        ensure_task_contract_foundation(s, Intent(mode="Chat", task="x", original_text="x"))
        spec = {
            "intent": "analysis",
            "change_expectation": "should_not_write",
            "scope": ["api"],
            "target_files": [],
            "verification_policy": {},
            "budget_policy": {},
            "forbidden_layers": [],
        }
        update_task_contract_after_spec_apply(s, spec)
        s.task_intent = spec["intent"]
        s.change_expectation = spec["change_expectation"]
        slash = Intent(
            mode="Execute",
            task="corrige un bug pequeño en login",
            is_slash_command=True,
            original_text="/do corrige un bug pequeño en login",
        )
        self.assertTrue(apply_slash_execute_write_overrides(s, slash))
        self.assertEqual(s.change_expectation, "may_write")
        self.assertEqual(s.task_intent, "bugfix")
        spec_after = s.task_contract.get("spec") or {}
        budget = spec_after.get("budget_policy") or {}
        self.assertGreaterEqual(int(budget.get("max_shell_calls") or 0), 4)
        self.assertGreaterEqual(int(budget.get("reserved_write_tool_calls") or 0), 4)

    def test_plan_slash_not_overridden(self) -> None:
        s = ArtifactSession("sid_y", "revisa arquitectura", task_id="task_2")
        ensure_task_contract_foundation(s, Intent(mode="Chat", task="x", original_text="x"))
        spec = {
            "intent": "analysis",
            "change_expectation": "should_not_write",
            "scope": ["api"],
            "target_files": [],
            "verification_policy": {},
            "budget_policy": {},
            "forbidden_layers": [],
        }
        update_task_contract_after_spec_apply(s, spec)
        s.task_intent = spec["intent"]
        s.change_expectation = spec["change_expectation"]
        plan = Intent(
            mode="Plan",
            task="revisa arquitectura",
            is_slash_command=True,
            original_text="/plan revisa arquitectura",
        )
        self.assertFalse(apply_slash_execute_write_overrides(s, plan))
        self.assertEqual(s.change_expectation, "should_not_write")

    def test_fix_top_level_mode_also_overrides_readonly_spec(self) -> None:
        s = ArtifactSession("sid_z", "corrige el proyecto actual", task_id="task_3")
        ensure_task_contract_foundation(s, Intent(mode="Chat", task="x", original_text="x"))
        spec = {
            "intent": "analysis",
            "change_expectation": "should_not_write",
            "scope": [],
            "target_files": [],
            "verification_policy": {},
            "budget_policy": {},
            "forbidden_layers": [],
        }
        update_task_contract_after_spec_apply(s, spec)
        s.task_intent = spec["intent"]
        s.change_expectation = spec["change_expectation"]
        fix = Intent(
            mode="Fix",
            task="corrige el proyecto actual",
            is_slash_command=False,
            original_text="corrige el proyecto actual",
        )
        self.assertTrue(apply_slash_execute_write_overrides(s, fix))
        self.assertEqual(s.task_intent, "bugfix")
        self.assertEqual(s.change_expectation, "may_write")


if __name__ == "__main__":
    unittest.main()
