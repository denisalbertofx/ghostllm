"""Programmatic read-only grounding: ledger + snippet/claim enforcement."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from apps.cli.runtime.analysis_grounding import (
    enforce_readonly_assistant_message,
    readonly_analysis_grounding_enabled,
    record_read_file_ledger,
    snippet_is_grounded,
)


def _sess_analysis() -> SimpleNamespace:
    return SimpleNamespace(
        task_contract={"spec": {"intent": "analysis", "change_expectation": "should_not_write"}},
        task_intent="analysis",
        change_expectation="should_not_write",
        read_grounding_ledger=[],
        analysis_grounding_digest="",
        events=[],
    )


def _sess_execute() -> SimpleNamespace:
    return SimpleNamespace(
        task_contract={"spec": {"intent": "implementation", "change_expectation": "must_write"}},
        task_intent="implementation",
        change_expectation="must_write",
        read_grounding_ledger=[],
        events=[],
    )


class TestReadonlyGroundingGate(unittest.TestCase):
    def test_disabled_for_execute_mode(self) -> None:
        self.assertFalse(readonly_analysis_grounding_enabled(_sess_execute(), "Execute"))

    def test_enabled_for_plan_mode(self) -> None:
        s = _sess_execute()
        self.assertTrue(readonly_analysis_grounding_enabled(s, "Plan"))

    def test_enabled_for_analysis_intent_chat(self) -> None:
        self.assertTrue(readonly_analysis_grounding_enabled(_sess_analysis(), "Chat"))


class TestLedgerAndSnippets(unittest.TestCase):
    def test_snippet_allowed_when_literal_in_ledger(self) -> None:
        s = _sess_analysis()
        body = "def foo():\n    return 42\n"
        record_read_file_ledger(s, "a.py", body)
        self.assertTrue(snippet_is_grounded("def foo():\n    return 42", _ledger_corpus(s)))

    def test_fenced_block_removed_when_not_in_ledger(self) -> None:
        s = _sess_analysis()
        text = "Aquí va:\n```python\nfake_typo_fn()\n# pad to length\n```\nFin."
        r = enforce_readonly_assistant_message(text, s)
        self.assertIn("No cito el fragmento", r.text)
        self.assertNotIn("Fragmento de código omitido", r.text)
        self.assertNotIn("```", r.text)
        self.assertGreaterEqual(r.fenced_replaced, 1)
        self.assertTrue(any(e.get("action") == "fenced_snippet_ungrounded" for e in r.events))

    def test_fenced_block_kept_when_matches_read(self) -> None:
        s = _sess_analysis()
        code = "def bar():\n    pass\n" + ("# x\n" * 30)
        record_read_file_ledger(s, "b.py", code)
        quoted = "def bar():\n    pass\n"
        text = f"Ok:\n```\n{quoted}```"
        r = enforce_readonly_assistant_message(text, s)
        self.assertIn("def bar():", r.text)
        self.assertEqual(r.fenced_replaced, 0)

    def test_language_fenced_block_kept_when_matches_read(self) -> None:
        s = _sess_analysis()
        code = "def baz():\n    return 7\n" + ("# y\n" * 30)
        record_read_file_ledger(s, "c.py", code)
        text = "Ok:\n```python\ndef baz():\n    return 7\n```\n"
        r = enforce_readonly_assistant_message(text, s)
        self.assertIn("def baz():", r.text)
        self.assertEqual(r.fenced_replaced, 0)

    def test_inline_code_snippet_removed_when_not_in_ledger(self) -> None:
        s = _sess_analysis()
        text = "Evidence: `cursor.execute(\"SELECT * FROM tasks WHERE completed = 1\")`"
        r = enforce_readonly_assistant_message(text, s)
        self.assertIn("cita no verificada", r.text)
        self.assertGreaterEqual(r.inline_replaced, 1)

    def test_inline_code_snippet_kept_when_matches_read(self) -> None:
        s = _sess_analysis()
        code = 'cursor.execute("SELECT * FROM tasks WHERE completed = 1")\n'
        record_read_file_ledger(s, "task_manager.py", code)
        text = "Evidence: `cursor.execute(\"SELECT * FROM tasks WHERE completed = 1\")`"
        r = enforce_readonly_assistant_message(text, s)
        self.assertIn("cursor.execute", r.text)
        self.assertEqual(r.inline_replaced, 0)

    def test_strong_claim_softened_when_evidence_weak(self) -> None:
        s = _sess_analysis()
        record_read_file_ledger(s, "tiny.py", "x\n")
        text = "Hay un bug crítico y el código fallará en producción."
        r = enforce_readonly_assistant_message(text, s)
        self.assertIn("posible problema grave", r.text)
        self.assertTrue(r.claims_softened)

    def test_sql_injection_claim_softened_without_literal_dynamic_sql(self) -> None:
        s = _sess_analysis()
        record_read_file_ledger(
            s,
            "task_manager.py",
            'cursor.execute("SELECT * FROM tasks WHERE completed = 1")\n',
        )
        text = "Hallazgo: posible inyección SQL en task_manager.py."
        r = enforce_readonly_assistant_message(text, s)
        self.assertIn("riesgo SQL no confirmado", r.text)

    def test_concurrency_claim_softened_without_literal_concurrency_signal(self) -> None:
        s = _sess_analysis()
        record_read_file_ledger(
            s,
            "task_manager.py",
            "def add_task(self, description):\n    conn = self.get_db_connection()\n",
        )
        text = "Hallazgo: posible problema de concurrencia en task_manager.py."
        r = enforce_readonly_assistant_message(text, s)
        self.assertIn("riesgo concurrente no confirmado", r.text)


class TestArtifactDigest(unittest.TestCase):
    def test_digest_set_when_events(self) -> None:
        s = _sess_analysis()
        enforce_readonly_assistant_message("```\nnot in ledger at all\n```", s)
        self.assertTrue(s.analysis_grounding_digest)


def _ledger_corpus(session: SimpleNamespace) -> str:
    parts = []
    for e in session.read_grounding_ledger:
        for w in e.get("windows", []):
            parts.append(w)
    return "\n".join(parts)


if __name__ == "__main__":
    unittest.main()
