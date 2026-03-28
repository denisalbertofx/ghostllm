import unittest

from apps.cli.runtime.task_contract import infer_work_task_type, route_intake_intent


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


if __name__ == "__main__":
    unittest.main()
