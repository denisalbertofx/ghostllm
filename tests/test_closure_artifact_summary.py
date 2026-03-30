"""Operator closure summary (artifact JSON + markdown helpers)."""
from __future__ import annotations

import unittest

from apps.cli.runtime.closure_artifact_summary import (
    apply_tier_language_guard_es,
    build_closure_operator_view,
    closure_reason_operator_alias_en,
    derive_completion_style,
    primary_headline_es,
)
from apps.cli.runtime.session_phase import LOOP_ABORT_MAX_ITERATIONS


class _Sess:
    def __init__(self) -> None:
        self.phase = ""
        self.closure_reason = ""
        self.closure_reason_summary_es = ""
        self.closure_posture_es = ""
        self.task_outcome = ""
        self.task_confidence_level = "medium"
        self.task_completion_confidence = 0.7
        self.outcome_confidence = 0.75
        self.terminal_resolution_record: dict = {}
        self.task_confidence_signals: dict = {}


class TestTierLanguageGuard(unittest.TestCase):
    def test_suspected_tier_blocks_confirmed_bug_wording(self) -> None:
        raw = "Bug confirmado en apps/server/main.py: fallará en producción."
        out = apply_tier_language_guard_es(raw, "suspected")
        self.assertIn("hipótesis", out.lower())
        self.assertNotIn("main.py", out)

    def test_suspected_tier_blocks_bloquea_servicio(self) -> None:
        raw = "Esto bloquea todo el servicio en producción."
        out = apply_tier_language_guard_es(raw, "unverified")
        self.assertIn("hipótesis", out.lower())

    def test_confirmed_tier_passes_through(self) -> None:
        raw = "Bug confirmado tras tests ejecutados."
        self.assertEqual(apply_tier_language_guard_es(raw, "confirmed"), raw)

    def test_unknown_tier_is_treated_as_non_confirmed(self) -> None:
        raw = "Impacto crítico: bloqueará todo el servicio."
        out = apply_tier_language_guard_es(raw, "future_new_tier")
        self.assertIn("hip", out.lower())


class TestClosureArtifactSummary(unittest.TestCase):
    def test_alias_en_maps_primary_verify(self) -> None:
        self.assertEqual(
            closure_reason_operator_alias_en("primary_verification_pass"),
            "verified_write",
        )

    def test_alias_en_informative_readonly(self) -> None:
        self.assertEqual(
            closure_reason_operator_alias_en("informative_missing_path_readonly"),
            "read_only_missing_path_in_workspace",
        )
        self.assertEqual(
            closure_reason_operator_alias_en("repo_mismatch_graceful_closure"),
            "read_only_wrong_repo_mismatch",
        )

    def test_soft_done_readonly_style(self) -> None:
        s = _Sess()
        s.phase = "DONE"
        s.closure_reason = "fallback_max_iterations_with_readonly_confidence"
        s.terminal_resolution_record = {
            "loop_stop_signal_raw": LOOP_ABORT_MAX_ITERATIONS,
            "iteration_limit_assessment": {
                "kind": "iteration_cap_soft_done_answered",
                "summary_es": "x",
            },
        }
        style, expl = derive_completion_style(s)
        self.assertEqual(style, "soft")
        self.assertIn("intencional", expl)

    def test_firm_verify_pass(self) -> None:
        s = _Sess()
        s.phase = "DONE"
        s.closure_reason = "primary_verification_pass"
        style, _ = derive_completion_style(s)
        self.assertEqual(style, "firm")

    def test_headline_informative_missing_path(self) -> None:
        s = _Sess()
        s.phase = "DONE"
        s.closure_reason = "informative_missing_path_readonly"
        h = primary_headline_es(s)
        self.assertIn("no están", h.lower())

    def test_headline_evidence_synthesis(self) -> None:
        s = _Sess()
        s.phase = "DONE"
        s.closure_reason = "primary_read_only_evidence"
        s.task_confidence_signals = {"had_evidence_sufficient_immediate_synthesis": True}
        h = primary_headline_es(s)
        self.assertIn("evidencia suficiente", h.lower())

    def test_operator_view_has_iteration_secondary_flag(self) -> None:
        s = _Sess()
        s.phase = "DONE"
        s.closure_reason = "fallback_max_iterations_with_readonly_confidence"
        s.task_outcome = "read_only"
        s.terminal_resolution_record = {
            "loop_stop_signal_raw": LOOP_ABORT_MAX_ITERATIONS,
            "iteration_limit_assessment": {"kind": "iteration_cap_soft_done_answered"},
        }
        v = build_closure_operator_view(s)
        self.assertTrue((v.get("iteration_limit_context") or {}).get("narrative_is_secondary"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
