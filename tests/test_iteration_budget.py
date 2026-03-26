"""Dynamic iteration budget classification and caps."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from apps.cli.runtime.iteration_budget import (
    adapt_synthesis_reserve_iterations,
    apply_iteration_budget_to_session,
    classify_iteration_budget_category,
    compute_iteration_budget_plan,
    global_iteration_cap,
)
from apps.cli.runtime.task_contract import Intent


class _Sess:
    def __init__(self) -> None:
        self.micro_task_kind = None
        self.task_intent = ""
        self.change_expectation = ""
        self.events: list = []


class TestIterationBudget(unittest.TestCase):
    def test_micro_task_category(self) -> None:
        s = _Sess()
        s.micro_task_kind = "shallow_list"
        cat, _ = classify_iteration_budget_category(
            task_text="x",
            intent=Intent(mode="Chat", task="x"),
            micro_task_kind="shallow_list",
            task_intent="analysis",
            change_expectation="should_not_write",
        )
        self.assertEqual(cat, "micro_task")

    def test_complex_overrides_read_only_intent(self) -> None:
        cat, _ = classify_iteration_budget_category(
            task_text="Redesign the entire codebase architecture for microservices",
            intent=Intent(mode="Plan", task=""),
            micro_task_kind=None,
            task_intent="analysis",
            change_expectation="should_not_write",
        )
        self.assertEqual(cat, "complex_multi_file")

    def test_bounded_factual_without_micro(self) -> None:
        task = "¿Qué hace la función seal_task_contract_after_intake?"
        cat, _ = classify_iteration_budget_category(
            task_text=task,
            intent=Intent(mode="Chat", task=task),
            micro_task_kind=None,
            task_intent="ask",
            change_expectation="may_write",
        )
        self.assertEqual(cat, "factual_code_question")

    def test_plan_small_read_only(self) -> None:
        task = "Summarize dependencies in package.json only"
        cat, _ = classify_iteration_budget_category(
            task_text=task,
            intent=Intent(mode="Plan", task=task),
            micro_task_kind=None,
            task_intent="review",
            change_expectation="should_not_write",
        )
        self.assertEqual(cat, "small_read_only")

    def test_effective_max_clamped_to_global(self) -> None:
        task = "¿Qué hace foo?"
        intent = Intent(mode="Chat", task=task)
        s = _Sess()
        s.task_intent = "ask"
        s.change_expectation = "may_write"
        with patch.dict("os.environ", {"GHOST_MAX_ITERATIONS": "8"}, clear=False):
            plan = compute_iteration_budget_plan(task_text=task, intent=intent, session=s, global_cap=8)
        self.assertEqual(plan.global_cap, 8)
        self.assertLessEqual(plan.effective_max, 8)
        self.assertGreaterEqual(plan.effective_max, 4)

    def test_complex_uses_full_global(self) -> None:
        task = "Roadmap and plan de sprint for the monolith"
        intent = Intent(mode="Chat", task=task)
        s = _Sess()
        with patch.dict("os.environ", {"GHOST_MAX_ITERATIONS": "20"}, clear=False):
            plan = compute_iteration_budget_plan(task_text=task, intent=intent, session=s, global_cap=20)
        self.assertEqual(plan.category, "complex_multi_file")
        self.assertEqual(plan.effective_max, 20)

    def test_strategy_disabled_full_cap(self) -> None:
        task = "List py files in apps/cli"
        intent = Intent(mode="Plan", task=task)
        s = _Sess()
        with patch.dict("os.environ", {"GHOST_ITERATION_BUDGET_STRATEGY": "0"}, clear=False):
            plan = compute_iteration_budget_plan(task_text=task, intent=intent, session=s, global_cap=15)
        self.assertEqual(plan.effective_max, 15)
        self.assertEqual(plan.category, "strategy_disabled")

    def test_apply_sets_session_blob(self) -> None:
        task = "¿Qué hace foo?"
        intent = Intent(mode="Chat", task=task)
        s = _Sess()
        with patch.dict("os.environ", {"GHOST_MAX_ITERATIONS": "15"}, clear=False):
            apply_iteration_budget_to_session(s, task_text=task, intent=intent)
        self.assertIn("effective_max", s.iteration_budget)
        self.assertTrue(any(e.get("event") == "iteration_budget_applied" for e in s.events))

    def test_adapt_synthesis_reserve(self) -> None:
        self.assertEqual(adapt_synthesis_reserve_iterations(6, 2), 1)
        self.assertEqual(adapt_synthesis_reserve_iterations(9, 2), 2)
        self.assertEqual(adapt_synthesis_reserve_iterations(20, 3), 3)

    def test_global_iteration_cap_reads_env(self) -> None:
        with patch.dict("os.environ", {"GHOST_MAX_ITERATIONS": "22"}, clear=False):
            self.assertEqual(global_iteration_cap(), 22)

    def test_simple_write_category_when_flag_on(self) -> None:
        task = "Create a new file docs/note.md with hello"
        intent = Intent(mode="Chat", task=task)
        s = _Sess()
        with patch.dict("os.environ", {"GHOST_FAST_SIMPLE_WRITE_BUDGET": "1"}, clear=False):
            cat, _ = classify_iteration_budget_category(
                task_text=task,
                intent=intent,
                micro_task_kind=None,
                task_intent="implementation",
                change_expectation="must_write",
            )
        self.assertEqual(cat, "simple_write")

    def test_simple_write_flag_off_stays_write_verify(self) -> None:
        task = "Create a new file docs/note.md"
        intent = Intent(mode="Chat", task=task)
        with patch.dict("os.environ", {"GHOST_FAST_SIMPLE_WRITE_BUDGET": "0"}, clear=False):
            cat, _ = classify_iteration_budget_category(
                task_text=task,
                intent=intent,
                micro_task_kind=None,
                task_intent="implementation",
                change_expectation="must_write",
            )
        self.assertEqual(cat, "write_verify")

    def test_explicit_file_edit_defaults_to_simple_write(self) -> None:
        task = "Edit apps/server/main.py to improve the health response"
        intent = Intent(mode="Chat", task=task)
        with patch.dict("os.environ", {}, clear=False):
            cat, _ = classify_iteration_budget_category(
                task_text=task,
                intent=intent,
                micro_task_kind=None,
                task_intent="modification",
                change_expectation="must_write",
            )
        self.assertEqual(cat, "simple_write")


if __name__ == "__main__":
    unittest.main(verbosity=2)
