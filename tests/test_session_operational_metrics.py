"""Operational metrics: accumulation, stable keys, verification reuse does not double-count pass/fail."""
from __future__ import annotations

import unittest

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.session_metrics import (
    OPERATIONAL_METRICS_DEFAULTS,
    record_main_model_turn_ok,
    record_repair_success,
    record_tool_execution_metrics,
    record_tool_output_archived,
    record_verification_metrics,
    refresh_operational_metrics_snapshot,
    set_final_aborted_reason,
    set_total_loop_iterations,
)


class TestOperationalMetricsAccumulation(unittest.TestCase):
    def test_artifact_starts_with_all_default_keys(self) -> None:
        s = ArtifactSession("sid", "task")
        refresh_operational_metrics_snapshot(s)
        d = s.to_dict()
        self.assertIn("operational_metrics", d)
        for k in OPERATIONAL_METRICS_DEFAULTS:
            self.assertIn(k, d["operational_metrics"])

    def test_phase_bumps_via_record_phase_transition(self) -> None:
        s = ArtifactSession("sid", "task")
        s.record_phase_transition("ACT", detail="t", from_phase="EXPLORE")
        s.record_phase_transition("VERIFY", detail="v", from_phase="ACT")
        s.record_phase_transition("REPAIR", detail="r", from_phase="VERIFY")
        m = refresh_operational_metrics_snapshot(s)
        self.assertEqual(m["total_act_phases"], 1)
        self.assertEqual(m["total_verify_phases"], 1)
        self.assertEqual(m["total_repair_phases"], 1)

    def test_model_turn_and_prompt_chars(self) -> None:
        s = ArtifactSession("sid", "task")
        record_main_model_turn_ok(s, prompt_chars=1200)
        record_main_model_turn_ok(s, prompt_chars=800)
        m = refresh_operational_metrics_snapshot(s)
        self.assertEqual(m["total_model_turns"], 2)
        self.assertEqual(m["prompt_chars_sent"], 2000)

    def test_tool_buckets(self) -> None:
        s = ArtifactSession("sid", "task")
        record_tool_execution_metrics(s, "read_file")
        record_tool_execution_metrics(s, "ls")
        record_tool_execution_metrics(s, "edit_file")
        record_tool_execution_metrics(s, "run_shell")
        m = refresh_operational_metrics_snapshot(s)
        self.assertEqual(m["total_tool_calls"], 4)
        self.assertEqual(m["total_read_tool_calls"], 2)
        self.assertEqual(m["total_write_tool_calls"], 1)

    def test_tool_archive_increments(self) -> None:
        s = ArtifactSession("sid", "task")
        record_tool_output_archived(s)
        record_tool_output_archived(s)
        m = refresh_operational_metrics_snapshot(s)
        self.assertEqual(m["tool_output_archived_count"], 2)

    def test_loop_iterations_and_repair_snapshot(self) -> None:
        s = ArtifactSession("sid", "task")
        set_total_loop_iterations(s, 7)
        s.repair_attempt_count = 2
        s.repair_specialist_prompt_chars = 500
        m = refresh_operational_metrics_snapshot(s)
        self.assertEqual(m["total_loop_iterations"], 7)
        self.assertEqual(m["repair_attempts"], 2)
        self.assertEqual(m["repair_prompt_chars_sent"], 500)

    def test_repair_success(self) -> None:
        s = ArtifactSession("sid", "task")
        record_repair_success(s)
        record_repair_success(s)
        m = refresh_operational_metrics_snapshot(s)
        self.assertEqual(m["repair_success_count"], 2)

    def test_final_abort_reason(self) -> None:
        s = ArtifactSession("sid", "task")
        set_final_aborted_reason(s, "stagnation")
        m = refresh_operational_metrics_snapshot(s)
        self.assertEqual(m["final_aborted_reason"], "stagnation")


class TestVerificationMetricsNoDoubleCount(unittest.TestCase):
    def test_reuse_only_increments_reuse(self) -> None:
        s = ArtifactSession("sid", "task")
        record_verification_metrics(
            s,
            {"status": "success", "steps_executed_count": 3},
            reused=False,
        )
        record_verification_metrics(
            s,
            {"status": "success", "steps_executed_count": 3},
            reused=True,
        )
        m = refresh_operational_metrics_snapshot(s)
        self.assertEqual(m["verification_pass_count"], 1)
        self.assertEqual(m["verification_fail_count"], 0)
        self.assertEqual(m["verification_reuse_count"], 1)

    def test_skipped_verification_no_pass_fail(self) -> None:
        s = ArtifactSession("sid", "task")
        record_verification_metrics(
            s,
            {"status": "skipped", "steps_executed_count": 0},
            reused=False,
        )
        m = refresh_operational_metrics_snapshot(s)
        self.assertEqual(m["verification_pass_count"], 0)
        self.assertEqual(m["verification_fail_count"], 0)


if __name__ == "__main__":
    unittest.main()
