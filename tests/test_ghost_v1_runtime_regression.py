"""
Ghost v1 runtime regression pack (release gate).

Builds **synthetic completed sessions** that match real CodexAssistant artifact shape, then asserts:

- Final phase + terminal_resolution_record coherence
- ``compute_runtime_health`` summary (status + risk flags)
- Key ``operational_metrics`` thresholds
- Forensic fields (``promotion_decisions``, ``repair_forensic_summary``)
- No illegal REPAIR transitions, coherent phase chains, VERIFY obligation when diffs exist

These tests do **not** invoke the LLM; they guard the state machine and export contract.
"""
from __future__ import annotations

import unittest

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.explore_promotion import REASON_DISCOVERY_CAP_REACHED
from apps.cli.runtime.outcome_engine import OUTCOME_READ_ONLY
from tests.ghost_v1_regression_fixtures import (
    GhostV1RegressionExpectation,
    RuntimeHealthExpectation,
    replay_phase_transitions,
    terminal_record_aborted,
    terminal_record_done,
    validate_ghost_v1_regression_session,
)
from apps.cli.runtime.repair_specialist import REPAIR_OUTCOME_NO_EFFECTIVE_PATCH
from apps.cli.runtime.session_phase import LOOP_ABORT_POLICY, SessionPhase
from apps.cli.runtime.task_contract import Intent, ensure_task_contract_foundation


def _base_session(sid: str = "reg", task: str = "t") -> ArtifactSession:
    s = ArtifactSession(sid, task)
    ensure_task_contract_foundation(
        s,
        Intent(mode="Chat", task=task, original_text=task),
    )
    return s


class TestGhostV1ReadOnlyInvestigation(unittest.TestCase):
    """Scenario 1: read-only investigation — no diff, verify skipped, DONE."""

    def test_final_state(self) -> None:
        s = _base_session(task="explain how routing works")
        path = [
            ("", "INTAKE"),
            ("INTAKE", "EXPLORE"),
            ("EXPLORE", "CLOSING"),
            ("CLOSING", "DONE"),
        ]
        replay_phase_transitions(s, path)
        s.diff_summary = []
        s.verification = {"status": "skipped", "steps_executed_count": 0, "checks": []}
        s.terminal_resolution_record = terminal_record_done(outcome=OUTCOME_READ_ONLY)
        s.task_outcome = OUTCOME_READ_ONLY
        s.operational_metrics.update(
            {
                "total_read_tool_calls": 5,
                "total_write_tool_calls": 0,
                "total_tool_calls": 5,
                "total_loop_iterations": 3,
            }
        )

        exp = GhostV1RegressionExpectation(
            final_phase=SessionPhase.DONE.value,
            task_outcome=OUTCOME_READ_ONLY,
            health=RuntimeHealthExpectation(
                health_status="healthy",
                terminal_status="done",
                verify_status="skipped",
                repair_status="not_needed",
                exploration_status="normal",
            ),
            min_operational_metrics={
                "total_read_tool_calls": 5,
                "total_write_tool_calls": 0,
            },
            assert_diff_requires_verify=True,
        )
        rh = validate_ghost_v1_regression_session(s, exp)
        self.assertEqual(rh["main_risk_flags"], [])


class TestGhostV1SingleFileVerifyPass(unittest.TestCase):
    """Scenario 2: small implementation, integrity verify passes."""

    def test_final_state(self) -> None:
        s = _base_session(task="add constant to lib/foo.py")
        path = [
            ("", "INTAKE"),
            ("INTAKE", "EXPLORE"),
            ("EXPLORE", "ACT"),
            ("ACT", "VERIFY"),
            ("VERIFY", "CLOSING"),
            ("CLOSING", "DONE"),
        ]
        replay_phase_transitions(s, path)
        s.diff_summary = [{"file": "lib/foo.py", "type": "edit", "content_sha256": "a" * 64}]
        s.verification = {"status": "success", "steps_executed_count": 2, "checks": []}
        s.terminal_resolution_record = terminal_record_done(outcome="implemented")
        s.task_outcome = "implemented"
        s.operational_metrics.update(
            {
                "verification_pass_count": 1,
                "total_act_phases": 1,
                "total_verify_phases": 1,
            }
        )

        exp = GhostV1RegressionExpectation(
            final_phase=SessionPhase.DONE.value,
            task_outcome="implemented",
            health=RuntimeHealthExpectation(
                health_status="healthy",
                terminal_status="done",
                verify_status="passed",
                repair_status="not_needed",
                exploration_status="normal",
            ),
            min_operational_metrics={
                "verification_pass_count": 1,
                "total_act_phases": 1,
                "total_verify_phases": 1,
            },
        )
        validate_ghost_v1_regression_session(s, exp)


class TestGhostV1VerifyFailThenRepairSuccess(unittest.TestCase):
    """Scenario 3: first verify fails, REPAIR → ACT → verify succeeds."""

    def test_final_state(self) -> None:
        s = _base_session(task="fix API handler")
        path = [
            ("", "INTAKE"),
            ("INTAKE", "EXPLORE"),
            ("EXPLORE", "ACT"),
            ("ACT", "VERIFY"),
            ("VERIFY", "REPAIR"),
            ("REPAIR", "ACT"),
            ("ACT", "VERIFY"),
            ("VERIFY", "CLOSING"),
            ("CLOSING", "DONE"),
        ]
        replay_phase_transitions(s, path)
        s.diff_summary = [{"file": "apps/server/api.py", "type": "edit", "content_sha256": "b" * 64}]
        s.verification = {"status": "success", "steps_executed_count": 2, "checks": []}
        s.repair_attempt_count = 1
        s.repair_summary = "Adjusted types for handler"
        s.repair_specialist_outcome = "applied_patch"
        s.repair_outcome_log = [{"outcome": "applied_patch", "at": "t0"}]
        s.last_failed_check = "TypeCheck"
        s.terminal_resolution_record = terminal_record_done(outcome="implemented")
        s.task_outcome = "implemented"
        s.append_promotion_decision(
            kind="repair_to_act",
            reason_code="repair_specialist_applied_patch",
            from_phase="REPAIR",
            to_phase="ACT",
        )
        s.operational_metrics.update(
            {
                "verification_fail_count": 1,
                "verification_pass_count": 1,
                "repair_attempts": 1,
                "repair_success_count": 1,
                "total_act_phases": 2,
                "total_verify_phases": 2,
                "total_repair_phases": 1,
            }
        )

        exp = GhostV1RegressionExpectation(
            final_phase=SessionPhase.DONE.value,
            task_outcome="implemented",
            health=RuntimeHealthExpectation(
                health_status="healthy",
                terminal_status="done",
                verify_status="passed",
                repair_status="attempted_succeeded",
                exploration_status="normal",
            ),
            min_operational_metrics={
                "verification_fail_count": 1,
                "verification_pass_count": 1,
                "repair_success_count": 1,
                "total_repair_phases": 1,
            },
            min_promotion_decisions=1,
            min_repair_forensic_keys=2,
        )
        validate_ghost_v1_regression_session(s, exp)


class TestGhostV1RepairNoEffectivePatch(unittest.TestCase):
    """Scenario 4: specialist produced no effective patch — forensic flag."""

    def test_degraded_with_flag(self) -> None:
        s = _base_session()
        path = [
            ("", "INTAKE"),
            ("INTAKE", "ACT"),
            ("ACT", "VERIFY"),
            ("VERIFY", "REPAIR"),
            ("REPAIR", "ACT"),
            ("ACT", "VERIFY"),
            ("VERIFY", "CLOSING"),
            ("CLOSING", "DONE"),
        ]
        replay_phase_transitions(s, path)
        s.diff_summary = [{"file": "x.py", "type": "edit"}]
        s.verification = {"status": "success", "steps_executed_count": 1, "checks": []}
        s.repair_specialist_outcome = REPAIR_OUTCOME_NO_EFFECTIVE_PATCH
        s.repair_attempt_count = 1
        s.repair_outcome_log = [{"outcome": REPAIR_OUTCOME_NO_EFFECTIVE_PATCH}]
        s.terminal_resolution_record = terminal_record_done(outcome="implemented")
        s.task_outcome = "implemented"
        s.operational_metrics.update({"repair_success_count": 1, "repair_attempts": 1})

        exp = GhostV1RegressionExpectation(
            final_phase=SessionPhase.DONE.value,
            task_outcome="implemented",
            health=RuntimeHealthExpectation(
                health_status="degraded",
                terminal_status="done",
                verify_status="passed",
                repair_status="attempted_succeeded",
                exploration_status="normal",
            ),
            require_risk_flags=("no_effective_patch",),
        )
        validate_ghost_v1_regression_session(s, exp)


class TestGhostV1ProviderRetryHeavy(unittest.TestCase):
    """Scenario 5: provider retried before eventual success — risk flag."""

    def test_degraded_with_retry_flag(self) -> None:
        s = _base_session()
        path = [
            ("", "INTAKE"),
            ("INTAKE", "EXPLORE"),
            ("EXPLORE", "ACT"),
            ("ACT", "VERIFY"),
            ("VERIFY", "CLOSING"),
            ("CLOSING", "DONE"),
        ]
        replay_phase_transitions(s, path)
        s.diff_summary = []
        s.verification = {"status": "success", "steps_executed_count": 1, "checks": []}
        s.provider_response_snapshot = {"retry_attempts": 4}
        s.terminal_resolution_record = terminal_record_done()
        s.task_outcome = "implemented"

        exp = GhostV1RegressionExpectation(
            final_phase=SessionPhase.DONE.value,
            task_outcome="implemented",
            health=RuntimeHealthExpectation(
                health_status="degraded",
                terminal_status="done",
                verify_status="passed",
                repair_status="not_needed",
                exploration_status="normal",
            ),
            require_risk_flags=("provider_retry_heavy",),
        )
        validate_ghost_v1_regression_session(s, exp)


class TestGhostV1ProlongedExploreThenAct(unittest.TestCase):
    """Scenario 6: many EXPLORE entries then promotion to ACT and success."""

    def test_long_explore_and_promotion(self) -> None:
        s = _base_session(task="map the codebase before editing")
        pairs = [("", "INTAKE"), ("INTAKE", "EXPLORE")]
        for _ in range(4):
            pairs.extend([("EXPLORE", "ACT"), ("ACT", "EXPLORE")])
        pairs.extend(
            [
                ("EXPLORE", "ACT"),
                ("ACT", "VERIFY"),
                ("VERIFY", "CLOSING"),
                ("CLOSING", "DONE"),
            ]
        )
        replay_phase_transitions(s, pairs)
        s.diff_summary = [{"file": "src/a.ts", "type": "edit", "content_sha256": "c" * 64}]
        s.verification = {"status": "success", "steps_executed_count": 1, "checks": []}
        s.terminal_resolution_record = terminal_record_done()
        s.task_outcome = "implemented"
        s.append_promotion_decision(
            kind="explore_to_act",
            reason_code=REASON_DISCOVERY_CAP_REACHED,
            from_phase="EXPLORE",
            to_phase="ACT",
        )

        exp = GhostV1RegressionExpectation(
            final_phase=SessionPhase.DONE.value,
            task_outcome="implemented",
            health=RuntimeHealthExpectation(
                health_status="degraded",
                terminal_status="done",
                verify_status="passed",
                repair_status="not_needed",
                exploration_status="prolonged",
            ),
            require_risk_flags=("long_explore",),
            min_promotion_decisions=1,
        )
        validate_ghost_v1_regression_session(s, exp)


class TestGhostV1SkipVerifyPolicy(unittest.TestCase):
    """Scenario 7: contract skip_verify — VERIFY phase may be omitted with diff."""

    def test_skip_verify_path(self) -> None:
        s = _base_session(task="docs-only tweak")
        assert isinstance(s.task_contract, dict)
        s.task_contract["skip_verify"] = True

        path = [
            ("", "INTAKE"),
            ("INTAKE", "ACT"),
            ("ACT", "CLOSING"),
            ("CLOSING", "DONE"),
        ]
        replay_phase_transitions(s, path)
        s.diff_summary = [{"file": "README.md", "type": "edit"}]
        s.verification = {"status": "skipped", "steps_executed_count": 0, "checks": []}
        s.terminal_resolution_record = terminal_record_done(outcome="implemented")
        s.task_outcome = "implemented"

        exp = GhostV1RegressionExpectation(
            final_phase=SessionPhase.DONE.value,
            task_outcome="implemented",
            health=RuntimeHealthExpectation(
                health_status="degraded",
                terminal_status="done",
                verify_status="skipped",
                repair_status="not_needed",
                exploration_status="normal",
            ),
            require_risk_flags=("verification_skipped",),
            assert_diff_requires_verify=False,
        )
        validate_ghost_v1_regression_session(s, exp)


class TestGhostV1AbortBudgetExhausted(unittest.TestCase):
    """Scenario 8: autonomy budget exhausted → ABORTED."""

    def test_aborted_budget(self) -> None:
        s = _base_session()
        path = [
            ("", "INTAKE"),
            ("INTAKE", "EXPLORE"),
            ("EXPLORE", "ACT"),
            ("ACT", "CLOSING"),
            ("CLOSING", "ABORTED"),
        ]
        replay_phase_transitions(s, path)
        s.diff_summary = []
        s.verification = {"status": "skipped", "steps_executed_count": 0, "checks": []}
        s.terminal_resolution_record = terminal_record_aborted(
            outcome="no_op",
            loop_abort_reason="budget_exhausted",
            resolution_extra={"budget_exhausted": True, "budget_status": "exhausted"},
        )
        s.task_outcome = "no_op"
        s.operational_metrics["final_aborted_reason"] = "budget_exhausted"

        exp = GhostV1RegressionExpectation(
            final_phase=SessionPhase.ABORTED.value,
            task_outcome="no_op",
            health=RuntimeHealthExpectation(
                health_status="failed",
                terminal_status="aborted",
                verify_status="skipped",
                repair_status="not_needed",
                exploration_status="normal",
            ),
            require_risk_flags=("aborted_budget",),
            assert_diff_requires_verify=True,
        )
        validate_ghost_v1_regression_session(s, exp)


class TestGhostV1AbortPolicyBlock(unittest.TestCase):
    """Scenario 9: policy / terminal stop → ABORTED."""

    def test_aborted_policy(self) -> None:
        s = _base_session()
        path = [
            ("", "INTAKE"),
            ("INTAKE", "EXPLORE"),
            ("EXPLORE", "CLOSING"),
            ("CLOSING", "ABORTED"),
        ]
        replay_phase_transitions(s, path)
        s.diff_summary = []
        s.verification = {"status": "skipped", "steps_executed_count": 0, "checks": []}
        s.terminal_resolution_record = terminal_record_aborted(
            outcome="no_op",
            loop_abort_reason=LOOP_ABORT_POLICY,
            resolution_extra={"policy_status": "policy_block"},
        )
        s.task_outcome = "no_op"
        s.operational_metrics["final_aborted_reason"] = LOOP_ABORT_POLICY

        exp = GhostV1RegressionExpectation(
            final_phase=SessionPhase.ABORTED.value,
            task_outcome="no_op",
            health=RuntimeHealthExpectation(
                health_status="failed",
                terminal_status="aborted",
                verify_status="skipped",
                repair_status="not_needed",
                exploration_status="normal",
            ),
            require_risk_flags=("aborted_policy",),
        )
        validate_ghost_v1_regression_session(s, exp)


class TestGhostV1IllegalTransitionGuard(unittest.TestCase):
    """Guard: REPAIR → terminal still forbidden (existing invariant)."""

    def test_repair_to_done_rejected(self) -> None:
        from apps.cli.runtime.phase_invariants import (
            assert_repair_outgoing_edges_valid,
            phase_edges_from_transitions,
            simulate_phase_path,
        )

        rows = simulate_phase_path([("VERIFY", "REPAIR"), ("REPAIR", "DONE")])
        with self.assertRaises(AssertionError):
            assert_repair_outgoing_edges_valid(phase_edges_from_transitions(rows))


if __name__ == "__main__":
    unittest.main()
