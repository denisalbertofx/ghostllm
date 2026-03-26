"""Tests for unified SessionPhase terminal resolution."""
import unittest

from apps.cli.runtime.outcome_engine import (
    OUTCOME_BLOCKED,
    OUTCOME_IMPLEMENTED,
    OUTCOME_NO_OP,
    OUTCOME_PARTIALLY_IMPLEMENTED,
    OUTCOME_READ_ONLY,
    OUTCOME_VERIFICATION_FAILED,
)
from apps.cli.runtime.session_phase import (
    LOOP_ABORT_MAX_ITERATIONS,
    LOOP_ABORT_POLICY,
    LOOP_ABORT_PROVIDER,
    LOOP_ABORT_STAGNATION,
    SessionPhase,
    TERMINAL_PHASES,
    compute_terminal_session_phase,
)


class TestTerminalSessionPhase(unittest.TestCase):
    def test_budget_exhausted_aborted(self):
        p = compute_terminal_session_phase(
            budget_exhausted=True,
            session_failed=False,
            outcome=OUTCOME_VERIFICATION_FAILED,
            loop_abort_reason=None,
        )
        self.assertEqual(p, SessionPhase.ABORTED)
        self.assertIn(p, TERMINAL_PHASES)

    def test_verification_failed_aborted(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_VERIFICATION_FAILED,
            loop_abort_reason=None,
        )
        self.assertEqual(p, SessionPhase.ABORTED)

    def test_session_failed_aborted(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=True,
            outcome=OUTCOME_IMPLEMENTED,
            loop_abort_reason=None,
        )
        self.assertEqual(p, SessionPhase.ABORTED)

    def test_success_done(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_IMPLEMENTED,
            loop_abort_reason=None,
        )
        self.assertEqual(p, SessionPhase.DONE)
        self.assertIn(p, TERMINAL_PHASES)

    def test_stagnation_abort_overrides_success_outcome(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_IMPLEMENTED,
            loop_abort_reason=LOOP_ABORT_STAGNATION,
        )
        self.assertEqual(p, SessionPhase.ABORTED)

    def test_policy_abort(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_IMPLEMENTED,
            loop_abort_reason=LOOP_ABORT_POLICY,
        )
        self.assertEqual(p, SessionPhase.ABORTED)

    def test_provider_abort_reason(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_IMPLEMENTED,
            loop_abort_reason=LOOP_ABORT_PROVIDER,
        )
        self.assertEqual(p, SessionPhase.ABORTED)

    def test_max_iterations_abort_reason(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_IMPLEMENTED,
            loop_abort_reason=LOOP_ABORT_MAX_ITERATIONS,
        )
        self.assertEqual(p, SessionPhase.ABORTED)

    def test_max_iterations_read_only_soft_done_with_evidence(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_READ_ONLY,
            loop_abort_reason=LOOP_ABORT_MAX_ITERATIONS,
            evidence_score=10,
            has_final_nl_response=False,
        )
        self.assertEqual(p, SessionPhase.DONE)

    def test_max_iterations_read_only_with_final_nl_done(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_READ_ONLY,
            loop_abort_reason=LOOP_ABORT_MAX_ITERATIONS,
            evidence_score=0,
            has_final_nl_response=True,
        )
        self.assertEqual(p, SessionPhase.DONE)

    def test_max_iterations_read_only_low_evidence_aborts(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_READ_ONLY,
            loop_abort_reason=LOOP_ABORT_MAX_ITERATIONS,
            evidence_score=0,
            has_final_nl_response=False,
        )
        self.assertEqual(p, SessionPhase.ABORTED)

    def test_no_op_aborted(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_NO_OP,
            loop_abort_reason=None,
        )
        self.assertEqual(p, SessionPhase.ABORTED)

    def test_partially_implemented_aborted(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_PARTIALLY_IMPLEMENTED,
            loop_abort_reason=None,
        )
        self.assertEqual(p, SessionPhase.ABORTED)

    def test_blocked_aborted(self):
        p = compute_terminal_session_phase(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_BLOCKED,
            loop_abort_reason=None,
        )
        self.assertEqual(p, SessionPhase.ABORTED)


if __name__ == "__main__":
    unittest.main()
