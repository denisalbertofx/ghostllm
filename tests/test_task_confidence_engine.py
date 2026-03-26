"""Task confidence snapshot and iteration-limit merge."""
from __future__ import annotations

import unittest

from apps.cli.runtime.outcome_engine import (
    INFORMATIVE_KIND_EXPLORE_CHURN,
    INFORMATIVE_KIND_MISSING_PATH_TOOLS,
    INFORMATIVE_KIND_MISSING_SYMBOL_HINT,
    OUTCOME_IMPLEMENTED,
    OUTCOME_READ_ONLY,
    TaskOutcomeResult,
)
from apps.cli.runtime.session_phase import LOOP_ABORT_MAX_ITERATIONS, SessionPhase
from apps.cli.runtime.task_confidence_engine import (
    compute_task_confidence_closure,
    merge_iteration_limit_summary_with_confidence,
)


class _Sess:
    def __init__(self) -> None:
        self.diff_summary = []
        self.events: list = []
        self.micro_task_kind = None
        self.iteration_budget: dict = {}


class TestTaskConfidenceEngine(unittest.TestCase):
    def test_implemented_verify_high(self) -> None:
        s = _Sess()
        s.diff_summary = [{"file": "a.py"}]
        tor = TaskOutcomeResult(
            outcome=OUTCOME_IMPLEMENTED,
            confidence=0.95,
            evidence_score=20,
            summary="ok",
            evidence_lines=[],
            recommended_next_action="",
        )
        snap = compute_task_confidence_closure(
            session=s,
            outcome=OUTCOME_IMPLEMENTED,
            outcome_result=tor,
            terminal_phase=SessionPhase.DONE,
            loop_abort_reason=None,
            has_final_nl=True,
            verification={
                "status": "success",
                "checks": [{"provenance": "executed", "status": "passed", "name": "t"}],
                "steps_executed_count": 1,
            },
        )
        self.assertEqual(snap.task_confidence_level, "high")
        self.assertGreaterEqual(snap.task_completion_confidence, 0.9)
        self.assertEqual(snap.closure_reason_code, "primary_verification_pass")

    def test_informative_missing_path_closure_reason(self) -> None:
        s = _Sess()
        tor = TaskOutcomeResult(
            outcome=OUTCOME_READ_ONLY,
            confidence=0.76,
            evidence_score=8,
            summary="paths",
            evidence_lines=[],
            recommended_next_action="",
            informative_readonly_kind=INFORMATIVE_KIND_MISSING_PATH_TOOLS,
        )
        snap = compute_task_confidence_closure(
            session=s,
            outcome=OUTCOME_READ_ONLY,
            outcome_result=tor,
            terminal_phase=SessionPhase.DONE,
            loop_abort_reason=None,
            has_final_nl=True,
            verification={},
        )
        self.assertEqual(snap.closure_reason_code, "informative_missing_path_readonly")
        self.assertIn("informativo", snap.closure_reason_summary_es.lower())

    def test_informative_missing_symbol_closure_reason(self) -> None:
        s = _Sess()
        tor = TaskOutcomeResult(
            outcome=OUTCOME_READ_ONLY,
            confidence=0.78,
            evidence_score=10,
            summary="sym",
            evidence_lines=[],
            recommended_next_action="",
            informative_readonly_kind=INFORMATIVE_KIND_MISSING_SYMBOL_HINT,
        )
        snap = compute_task_confidence_closure(
            session=s,
            outcome=OUTCOME_READ_ONLY,
            outcome_result=tor,
            terminal_phase=SessionPhase.DONE,
            loop_abort_reason=None,
            has_final_nl=True,
            verification={},
        )
        self.assertEqual(snap.closure_reason_code, "informative_missing_symbol_readonly")

    def test_informative_churn_closure_reason(self) -> None:
        s = _Sess()
        s.events.append({"event": "explore_v2_churn_abort", "reason": "exploration_churn: ls"})
        tor = TaskOutcomeResult(
            outcome=OUTCOME_READ_ONLY,
            confidence=0.78,
            evidence_score=10,
            summary="churn",
            evidence_lines=[],
            recommended_next_action="",
            informative_readonly_kind=INFORMATIVE_KIND_EXPLORE_CHURN,
        )
        snap = compute_task_confidence_closure(
            session=s,
            outcome=OUTCOME_READ_ONLY,
            outcome_result=tor,
            terminal_phase=SessionPhase.DONE,
            loop_abort_reason=None,
            has_final_nl=True,
            verification={},
        )
        self.assertEqual(snap.closure_reason_code, "informative_explore_churn_readonly")
        self.assertIn("churn", snap.closure_reason_summary_es.lower())

    def test_read_only_iteration_fallback(self) -> None:
        s = _Sess()
        s.events.append({"event": "explore_answer_now_nudge"})
        tor = TaskOutcomeResult(
            outcome=OUTCOME_READ_ONLY,
            confidence=0.85,
            evidence_score=10,
            summary="ro",
            evidence_lines=["a"],
            recommended_next_action="",
        )
        snap = compute_task_confidence_closure(
            session=s,
            outcome=OUTCOME_READ_ONLY,
            outcome_result=tor,
            terminal_phase=SessionPhase.DONE,
            loop_abort_reason=LOOP_ABORT_MAX_ITERATIONS,
            has_final_nl=True,
            verification={"status": "skipped", "checks": []},
        )
        self.assertIn("fallback_max_iterations", snap.closure_reason_code)
        self.assertIn("secundario", snap.closure_reason_summary_es.lower())
        self.assertIn("solo lectura", snap.closure_reason_summary_es.lower())

    def test_merge_iteration_assessment(self) -> None:
        ila = {
            "kind": "iteration_cap_soft_done_answered",
            "summary_es": "Texto original.",
        }
        out = merge_iteration_limit_summary_with_confidence(
            ila,
            task_confidence_level="medium",
            task_completion_confidence=0.75,
            closure_reason_summary_es="Narrativa confianza.",
        )
        self.assertIn("Confianza de tarea", out["summary_es"])
        self.assertIn("Texto original.", out["summary_es"])
        self.assertEqual(out["task_confidence_level"], "medium")


if __name__ == "__main__":
    unittest.main(verbosity=2)
