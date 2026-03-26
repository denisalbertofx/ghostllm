"""runtime_health: deterministic summary from artifact fields (healthy / degraded / failed)."""
from __future__ import annotations

import copy
import unittest

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.explore_promotion import REASON_READ_ONLY_STAGNATION_THRESHOLD
from apps.cli.runtime.repair_specialist import REPAIR_OUTCOME_NO_EFFECTIVE_PATCH
from apps.cli.runtime.runtime_health import compute_runtime_health
from apps.cli.runtime.session_metrics import OPERATIONAL_METRICS_DEFAULTS, refresh_operational_metrics_snapshot
from apps.cli.runtime.session_phase import LOOP_ABORT_PROVIDER, LOOP_ABORT_STAGNATION


def _base_om() -> dict:
    return dict(OPERATIONAL_METRICS_DEFAULTS)


class TestRuntimeHealthHealthy(unittest.TestCase):
    def test_done_passed_verify_no_flags(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.task_outcome = "implemented"
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "success", "steps_executed_count": 2}
        om = _base_om()
        om["verification_pass_count"] = 1
        s.operational_metrics = om
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertEqual(h["health_status"], "healthy")
        self.assertEqual(h["terminal_status"], "done")
        self.assertEqual(h["verify_status"], "passed")
        self.assertEqual(h["repair_status"], "not_needed")
        self.assertEqual(h["exploration_status"], "normal")
        self.assertEqual(h["main_risk_flags"], [])

    def test_skipped_verify_no_diff_still_healthy(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.diff_summary = []
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "skipped", "steps_executed_count": 0}
        s.operational_metrics = _base_om()
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertEqual(h["verify_status"], "skipped")
        self.assertEqual(h["health_status"], "healthy")

    def test_deterministic_twice(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "success", "steps_executed_count": 1}
        s.operational_metrics = _base_om()
        om = refresh_operational_metrics_snapshot(s)
        a = compute_runtime_health(s, operational_metrics=om)
        b = compute_runtime_health(s, operational_metrics=copy.deepcopy(om))
        self.assertEqual(a, b)


class TestRuntimeHealthDegraded(unittest.TestCase):
    def test_verify_reused(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "success", "steps_executed_count": 1}
        om = _base_om()
        om["verification_reuse_count"] = 1
        s.operational_metrics = om
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertEqual(h["verify_status"], "reused")
        self.assertEqual(h["health_status"], "degraded")
        self.assertIn("reuse_in_finalize", h["main_risk_flags"])

    def test_repair_attempted_failed(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.repair_attempt_count = 1
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "success", "steps_executed_count": 1}
        om = _base_om()
        om["repair_attempts"] = 1
        om["repair_success_count"] = 0
        s.operational_metrics = om
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertEqual(h["repair_status"], "attempted_failed")
        self.assertEqual(h["health_status"], "degraded")

    def test_long_explore(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "success", "steps_executed_count": 1}
        s.phase_transitions = [{"to": "EXPLORE", "from": ""}] * 5
        s.operational_metrics = _base_om()
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertEqual(h["exploration_status"], "prolonged")
        self.assertEqual(h["health_status"], "degraded")
        self.assertIn("long_explore", h["main_risk_flags"])

    def test_forced_promote_stagnation(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "success", "steps_executed_count": 1}
        s.promotion_decisions = [
            {
                "kind": "explore_to_act",
                "reason_code": REASON_READ_ONLY_STAGNATION_THRESHOLD,
                "from_phase": "EXPLORE",
                "to_phase": "ACT",
            }
        ]
        s.operational_metrics = _base_om()
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertEqual(h["exploration_status"], "forced_promote")
        self.assertEqual(h["health_status"], "degraded")

    def test_prompt_bloat(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "success", "steps_executed_count": 1}
        om = _base_om()
        om["prompt_chars_sent"] = 500_000
        s.operational_metrics = om
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertIn("prompt_bloat", h["main_risk_flags"])

    def test_provider_retry_heavy_snapshot(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "success", "steps_executed_count": 1}
        s.provider_response_snapshot = {"retry_attempts": 4}
        s.operational_metrics = _base_om()
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertIn("provider_retry_heavy", h["main_risk_flags"])

    def test_no_effective_patch(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.repair_specialist_outcome = REPAIR_OUTCOME_NO_EFFECTIVE_PATCH
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "success", "steps_executed_count": 1}
        s.operational_metrics = _base_om()
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertIn("no_effective_patch", h["main_risk_flags"])


class TestRuntimeHealthFailed(unittest.TestCase):
    def test_aborted_terminal(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "ABORTED"
        s.task_outcome = "no_op"
        s.terminal_resolution_record = {
            "terminal_phase": "ABORTED",
            "outcome": "no_op",
            "loop_abort_reason": LOOP_ABORT_STAGNATION,
            "resolution_sources": {"loop_abort_reason_raw": LOOP_ABORT_STAGNATION},
        }
        s.verification = {"status": "skipped", "steps_executed_count": 0}
        om = _base_om()
        om["final_aborted_reason"] = LOOP_ABORT_STAGNATION
        s.operational_metrics = om
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertEqual(h["health_status"], "failed")
        self.assertEqual(h["terminal_status"], "aborted")
        self.assertIn("aborted_stagnation", h["main_risk_flags"])

    def test_aborted_provider_flag(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "ABORTED"
        s.terminal_resolution_record = {
            "terminal_phase": "ABORTED",
            "outcome": "no_op",
            "loop_abort_reason": LOOP_ABORT_PROVIDER,
            "resolution_sources": {
                "provider_status": "fatal_error",
                "loop_abort_reason_raw": LOOP_ABORT_PROVIDER,
            },
        }
        om = _base_om()
        om["final_aborted_reason"] = LOOP_ABORT_PROVIDER
        s.operational_metrics = om
        s.verification = {"status": "skipped", "steps_executed_count": 0}
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertEqual(h["health_status"], "failed")
        self.assertIn("aborted_provider", h["main_risk_flags"])


class TestRuntimeHealthRiskFlags(unittest.TestCase):
    def test_verification_skipped_with_diff(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.diff_summary = [{"file": "a.py", "type": "edit"}]
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "skipped", "steps_executed_count": 0}
        s.operational_metrics = _base_om()
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertIn("verification_skipped", h["main_risk_flags"])
        self.assertEqual(h["health_status"], "degraded")

    def test_repeated_repair_failures_flag(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.repair_attempt_count = 2
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "success", "steps_executed_count": 1}
        om = _base_om()
        om["repair_attempts"] = 2
        om["repair_success_count"] = 0
        s.operational_metrics = om
        h = compute_runtime_health(s, operational_metrics=refresh_operational_metrics_snapshot(s))
        self.assertIn("repeated_repair_failures", h["main_risk_flags"])


class TestRuntimeHealthArtifactJson(unittest.TestCase):
    def test_to_dict_includes_runtime_health(self) -> None:
        s = ArtifactSession("s1", "t")
        s.phase = "DONE"
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        s.verification = {"status": "success", "steps_executed_count": 1}
        d = s.to_dict()
        self.assertIn("runtime_health", d)
        self.assertEqual(d["runtime_health"]["schema_version"], 1)
        self.assertIn("health_status", d["runtime_health"])


if __name__ == "__main__":
    unittest.main()
