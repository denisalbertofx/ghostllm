import unittest

from apps.cli.runtime.outcome_engine import OUTCOME_READ_ONLY
from apps.cli.runtime.session_phase import LOOP_ABORT_STAGNATION, SessionPhase
from apps.cli.runtime.terminal_resolution import terminal_phase_from_signals


class TestTerminalResolution(unittest.TestCase):
    def test_readonly_stagnation_with_final_answer_soft_closes(self) -> None:
        phase = terminal_phase_from_signals(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_READ_ONLY,
            loop_abort_reason=LOOP_ABORT_STAGNATION,
            has_final_nl_response=True,
            evidence_score=0,
        )
        self.assertEqual(phase, SessionPhase.DONE)

    def test_readonly_stagnation_without_final_answer_stays_aborted(self) -> None:
        phase = terminal_phase_from_signals(
            budget_exhausted=False,
            session_failed=False,
            outcome=OUTCOME_READ_ONLY,
            loop_abort_reason=LOOP_ABORT_STAGNATION,
            has_final_nl_response=False,
            evidence_score=0,
        )
        self.assertEqual(phase, SessionPhase.ABORTED)


if __name__ == "__main__":
    unittest.main()
