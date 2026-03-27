import unittest

from apps.cli.runtime.iteration_budget import (
    compute_iteration_budget_plan,
    plan_iteration_hard_cap,
)
from apps.cli.runtime.task_contract import Intent


class _Session:
    micro_task_kind = None
    task_intent = "analysis"
    change_expectation = "should_not_write"


class TestIterationBudget(unittest.TestCase):
    def test_broad_plan_prompt_uses_plan_readonly_budget(self):
        prompt = "/plan encuentra los bugs mas importantes"
        plan = compute_iteration_budget_plan(
            task_text=prompt,
            intent=Intent(mode="Plan", task=prompt, is_slash_command=True),
            session=_Session(),
            global_cap=15,
        )
        self.assertEqual(plan.category, "plan_readonly_broad")
        self.assertGreaterEqual(plan.effective_max, 14)

    def test_plan_iteration_hard_cap_never_below_global(self):
        self.assertGreaterEqual(plan_iteration_hard_cap(15), 15)


if __name__ == "__main__":
    unittest.main()
