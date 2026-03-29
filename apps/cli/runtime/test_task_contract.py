import unittest

from apps.cli.runtime.task_contract import (
    infer_work_task_type,
    normalize_cli_entry_text,
    route_intake_intent,
)


class TestTaskContractGreenfieldScaffold(unittest.TestCase):
    def test_infer_work_task_type_prefers_scaffold_for_bootstrap_prompt(self):
        task_type = infer_work_task_type(
            "crea desde cero una CLI de tareas en Python con SQLite, tests y README con estructura limpia"
        )
        self.assertEqual(task_type, "scaffold")

    def test_route_intake_intent_marks_bootstrap_scaffold(self):
        intent = route_intake_intent(
            "/do crea desde cero una CLI de tareas en Python con SQLite"
        )
        self.assertEqual(intent.mode, "Execute")
        self.assertEqual(intent.task_type, "scaffold")
        self.assertEqual(intent.scaffold_type, "bootstrap")

    def test_route_intake_intent_marks_scaffold_continuation(self):
        intent = route_intake_intent(
            "continua este proyecto y terminalo: crea una CLI de tareas en Python con SQLite y pytest"
        )
        self.assertEqual(intent.task_type, "scaffold")
        self.assertEqual(intent.scaffold_type, "extend")

    def test_infer_work_task_type_prefers_scaffold_for_continuation_prompt(self):
        task_type = infer_work_task_type(
            "continua este proyecto y terminalo: crea una CLI de tareas en Python con SQLite y README"
        )
        self.assertEqual(task_type, "scaffold")

    def test_normalize_cli_entry_text_promotes_bare_plan_to_slash(self):
        self.assertEqual(
            normalize_cli_entry_text('plan "audita este backend y dime los bugs"'),
            '/plan "audita este backend y dime los bugs"',
        )

    def test_route_intake_intent_treats_bare_fix_as_slash_command(self):
        intent = route_intake_intent("fix arregla el bug del login")
        self.assertTrue(intent.is_slash_command)
        self.assertEqual(intent.mode, "Fix")
        self.assertEqual(intent.task, "arregla el bug del login")

    def test_infer_work_task_type_respects_bare_plan_alias(self):
        self.assertEqual(
            infer_work_task_type('plan "audita este backend y dime los bugs"'),
            "plan",
        )


if __name__ == "__main__":
    unittest.main()
