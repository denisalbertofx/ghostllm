import unittest

from apps.cli.runtime.intent_classifier import (
    EXPECT_MAY_WRITE,
    EXPECT_MUST_WRITE,
    EXPECT_SHOULD_NOT_WRITE,
    INTENT_ANALYSIS,
    INTENT_BUGFIX,
    INTENT_IMPLEMENTATION,
    SCOPE_API,
    SCOPE_DATA,
    SCOPE_UNKNOWN,
    classify_task_intent,
)


class TestIntentClassifier(unittest.TestCase):
    def test_implementation_add_filtering_api(self):
        r = classify_task_intent(
            "Add support for filtering issues by status in GET /api/issues. Do not touch UI."
        )
        self.assertEqual(r.intent, INTENT_IMPLEMENTATION)
        self.assertEqual(r.scope, SCOPE_API)
        self.assertEqual(r.change_expectation, EXPECT_MUST_WRITE)

    def test_bugfix_put_api(self):
        r = classify_task_intent("Fix broken PUT /api/issues/[id]")
        self.assertEqual(r.intent, INTENT_BUGFIX)
        self.assertEqual(r.scope, SCOPE_API)
        self.assertEqual(r.change_expectation, EXPECT_MUST_WRITE)

    def test_implementation_data_api_only_spanish(self):
        r = classify_task_intent(
            "trabaja solo en la capa de datos y API del proyecto actual. No toques la UI. "
            "Agrega soporte para filtrar issues por status en el endpoint GET /api/issues. "
            "Si el valor es invalido, responde 400 con error claro."
        )
        self.assertEqual(r.intent, INTENT_IMPLEMENTATION)
        self.assertNotEqual(r.scope, "fullstack")
        self.assertIn(r.scope, (SCOPE_API, SCOPE_DATA))

    def test_scope_exclusion_no_fullstack(self):
        r = classify_task_intent("Add feature X. Only data and API. Do not touch UI.")
        self.assertNotEqual(r.scope, "fullstack")
        self.assertEqual(r.intent, INTENT_IMPLEMENTATION)

    def test_intent_softening_make_only_necessary_changes(self):
        r = classify_task_intent("Add feature X. Make only necessary changes.")
        self.assertEqual(r.intent, INTENT_IMPLEMENTATION)
        self.assertEqual(r.change_expectation, EXPECT_MAY_WRITE)

    def test_intent_softening_if_already_present(self):
        r = classify_task_intent("Implement X, but if already present, explain with evidence.")
        self.assertEqual(r.intent, INTENT_IMPLEMENTATION)
        self.assertEqual(r.change_expectation, EXPECT_MAY_WRITE)

    def test_intent_softening_spanish_report_verified(self):
        r = classify_task_intent(
            "Agrega validacion en GET /api/issues. Haz solo los cambios necesarios. "
            "Despues concluye con total precision que verificaste de verdad y que no ejecutaste."
        )
        self.assertEqual(r.intent, INTENT_IMPLEMENTATION)
        self.assertEqual(r.change_expectation, EXPECT_MAY_WRITE)

    def test_strategy_plan_prompt_is_analysis_not_ui(self):
        r = classify_task_intent(
            "/plan creame un plan para mejorar la arquitectura de esta app"
        )
        self.assertEqual(r.intent, INTENT_ANALYSIS)
        self.assertEqual(r.scope, SCOPE_UNKNOWN)
        self.assertEqual(r.change_expectation, EXPECT_SHOULD_NOT_WRITE)


if __name__ == "__main__":
    unittest.main()
