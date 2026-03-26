"""Narrow factual Chat synthesis (RO-2 style)."""
from __future__ import annotations

import json
import unittest

from apps.cli.runtime.narrow_factual_chat import (
    content_defines_symbol,
    detect_narrow_factual_code_question,
    evidence_sufficient_for_narrow_factual,
    factual_explore_iterations_remaining,
    read_file_texts_from_history,
)


class TestNarrowFactualChat(unittest.TestCase):
    def test_ro2_prompt_detects_symbol(self) -> None:
        t = (
            "/chat ¿Qué hace la función seal_task_contract_after_intake y en qué archivo está? "
            "Responde en 5 frases máximo. No uses herramientas si ya sabes la respuesta; "
            "si no, lee solo ese archivo."
        )
        s = detect_narrow_factual_code_question(t)
        self.assertIsNotNone(s)
        assert s is not None
        self.assertIn("seal_task_contract_after_intake", s.symbols)

    def test_implement_prompt_rejected(self) -> None:
        t = "Implementa la función foo en bar.py con tests"
        self.assertIsNone(detect_narrow_factual_code_question(t))

    def test_evidence_from_read_file_history(self) -> None:
        src = "def seal_task_contract_after_intake():\n    pass\n"
        hist = [
            {"role": "user", "content": "q"},
            {
                "role": "tool",
                "name": "read_file",
                "content": json.dumps({"content": src}),
            },
        ]
        scope = detect_narrow_factual_code_question(
            "¿Qué hace la función seal_task_contract_after_intake?"
        )
        self.assertIsNotNone(scope)
        assert scope is not None
        self.assertTrue(evidence_sufficient_for_narrow_factual(scope, hist))

    def test_read_file_texts_skips_errors(self) -> None:
        hist = [
            {
                "role": "tool",
                "name": "read_file",
                "content": json.dumps({"error": "nf"}),
            },
            {
                "role": "tool",
                "name": "read_file",
                "content": json.dumps({"content": "ok"}),
            },
        ]
        self.assertEqual(read_file_texts_from_history(hist), ["ok"])

    def test_factual_iterations_remaining(self) -> None:
        self.assertEqual(factual_explore_iterations_remaining(14, 15), 1)
        self.assertEqual(factual_explore_iterations_remaining(15, 15), 0)

    def test_content_defines_symbol(self) -> None:
        self.assertTrue(content_defines_symbol("@x\ndef foo():\n  pass", "foo"))
        self.assertTrue(content_defines_symbol("async def bar():\n  pass", "bar"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
