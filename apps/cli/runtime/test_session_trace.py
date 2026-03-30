"""Tests for structured CLI session tracing (local JSON telemetry)."""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.session_trace import (
    ApprovalTrace,
    ModelCallTrace,
    PhaseTrace,
    SessionTrace,
    SessionTraceManager,
    ToolCallTrace,
    TRACE_STATUS_COMPLETED,
    TRACE_STATUS_FAILED,
    build_phase_coverage,
    compute_trace_summary,
    finish_session_trace,
    format_compact_trace_summary,
    paths_outside_repo_from_task,
    persist_trace_file,
    record_policy_approval,
    summarize_approval_wait,
    summarize_model_time,
    summarize_tool_time,
    trace_manager_attached,
    trace_timestamp_iso,
    verification_trace_from_result,
    wrong_repo_note_from_task,
)
from apps.cli.ui.renderer import GhostRenderer
from rich.console import Console


def _minimal_trace(**kwargs: object) -> SessionTrace:
    base = dict(
        session_id="x",
        cwd="/tmp",
        repo_root="/tmp",
        started_at="2026-01-01T00:00:00+00:00",
        total_duration_ms=1000.0,
        phases=[],
        phase_coverage=[],
        model_calls=[],
        tool_calls=[],
        approvals=[],
        verification=[],
        repairs=[],
    )
    base.update(kwargs)
    return SessionTrace(**base)  # type: ignore[arg-type]


class SessionTracePersistenceTests(unittest.TestCase):
    def test_total_duration_populated_nonzero(self) -> None:
        mgr = SessionTraceManager("tid", os.getcwd(), "dev")
        time.sleep(0.02)
        t = mgr.build_session_trace()
        self.assertGreater(t.total_duration_ms, 0.001)

    def test_session_trace_file_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SessionTraceManager("task_test123", str(tmp), command_mode="dev")
            trace = mgr.build_session_trace()
            path = persist_trace_file(str(tmp), trace)
            self.assertTrue(Path(path).is_file())
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(data["session_id"], "task_test123")
            self.assertIn("summary", data)
            self.assertIn("phase_coverage", data)

    def test_session_trace_writes_ndjson_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_log_root = Path(tmp) / ".ghost" / "logs"
            with patch("apps.cli.runtime.session_trace._session_log_root", return_value=fake_log_root):
                mgr = SessionTraceManager("task_log123", str(tmp), command_mode="dev")
                mgr.start_phase("alpha")
                mgr.end_phase("alpha")
                mgr.record_tool_call(
                    ToolCallTrace(
                        tool_name="read_file",
                        target="demo.py",
                        started_at=trace_timestamp_iso(),
                        ended_at=trace_timestamp_iso(),
                        duration_ms=12.0,
                        ok=True,
                    )
                )
                mgr.finish_and_persist(str(tmp))
            lines = fake_log_root.joinpath(next(iter(os.listdir(fake_log_root)))).read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(lines), 4)
            payloads = [json.loads(line) for line in lines]
            self.assertEqual(payloads[0]["event_type"], "session_start")
            tool_events = [row for row in payloads if row["event_type"] == "tool_call"]
            self.assertTrue(tool_events)
            self.assertTrue(tool_events[0]["payload"]["correlation_id"].endswith(":tool:1"))

    def test_session_trace_records_runtime_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_log_root = Path(tmp) / ".ghost" / "logs"
            with patch("apps.cli.runtime.session_trace._session_log_root", return_value=fake_log_root):
                mgr = SessionTraceManager("task_runtime123", str(tmp), command_mode="plan")
                mgr.record_runtime_event(
                    "gateway_preflight",
                    {"ok": True, "actual_response_mode": "streaming"},
                )
                mgr.finish_and_persist(str(tmp))
            lines = fake_log_root.joinpath(next(iter(os.listdir(fake_log_root)))).read_text(encoding="utf-8").splitlines()
            payloads = [json.loads(line) for line in lines]
            runtime_events = [row for row in payloads if row["event_type"] == "gateway_preflight"]
            self.assertTrue(runtime_events)
            self.assertEqual(runtime_events[0]["payload"]["actual_response_mode"], "streaming")

    def test_phase_timing_recorded_success(self) -> None:
        mgr = SessionTraceManager("s1", os.getcwd(), "dev")
        mgr.start_phase("alpha")
        time.sleep(0.02)
        mgr.end_phase("alpha", status=TRACE_STATUS_COMPLETED)
        mgr.start_phase("beta")
        time.sleep(0.01)
        mgr.end_phase("beta")
        t = mgr.build_session_trace()
        self.assertEqual(len(t.phases), 2)
        self.assertGreater(t.phases[0].duration_ms, 0)
        self.assertEqual(t.phases[0].status, TRACE_STATUS_COMPLETED)

    def test_failed_tool_still_records_duration(self) -> None:
        mgr = SessionTraceManager("s2", os.getcwd(), "dev")
        t0 = trace_timestamp_iso()
        mgr.record_tool_call(
            ToolCallTrace(
                tool_name="read_file",
                target="missing.py",
                started_at=t0,
                ended_at=trace_timestamp_iso(),
                duration_ms=33.0,
                status=TRACE_STATUS_FAILED,
                ok=False,
                error_message="Path not found",
                result_summary="",
            )
        )
        t = mgr.build_session_trace()
        self.assertEqual(len(t.tool_calls), 1)
        self.assertEqual(t.tool_calls[0].duration_ms, 33.0)
        self.assertEqual(t.tool_calls[0].status, TRACE_STATUS_FAILED)

    def test_approval_wait_timing_recorded(self) -> None:
        mgr = SessionTraceManager("s3", os.getcwd(), "dev")
        with trace_manager_attached(mgr):
            record_policy_approval(
                "manual_shell_or_policy",
                "Execute Shell: echo hi",
                trace_timestamp_iso(),
                True,
                wait_duration_ms=42.5,
            )
        t = mgr.build_session_trace()
        self.assertEqual(len(t.approvals), 1)
        self.assertAlmostEqual(t.approvals[0].wait_duration_ms, 42.5, places=3)
        self.assertEqual(t.approvals[0].status, TRACE_STATUS_COMPLETED)

    def test_summary_bottleneck_phase(self) -> None:
        trace = _minimal_trace(
            total_duration_ms=600.0,
            phases=[
                PhaseTrace(
                    phase_name="fast",
                    started_at="2026-01-01T00:00:00+00:00",
                    ended_at="2026-01-01T00:00:01+00:00",
                    duration_ms=10.0,
                ),
                PhaseTrace(
                    phase_name="slow",
                    started_at="2026-01-01T00:00:01+00:00",
                    ended_at="2026-01-01T00:00:02+00:00",
                    duration_ms=500.0,
                ),
            ],
        )
        s = compute_trace_summary(trace)
        self.assertEqual(s.bottleneck_phase, "slow")

    def test_summary_approval_wait_bottleneck(self) -> None:
        trace = _minimal_trace(
            total_duration_ms=20000.0,
            approvals=[
                ApprovalTrace(
                    approval_type="shell",
                    command_or_action="x",
                    requested_at="2026-01-01T00:00:00+00:00",
                    resolved_at="2026-01-01T00:00:15+00:00",
                    wait_duration_ms=15000.0,
                    approved=True,
                    status=TRACE_STATUS_COMPLETED,
                )
            ],
            model_calls=[
                ModelCallTrace(
                    role="general",
                    model="m",
                    started_at="2026-01-01T00:00:00+00:00",
                    ended_at="2026-01-01T00:00:01+00:00",
                    duration_ms=500.0,
                    status=TRACE_STATUS_COMPLETED,
                    ok=True,
                )
            ],
        )
        s = compute_trace_summary(trace)
        self.assertGreater(s.total_approval_wait_ms, s.total_model_time_ms)
        self.assertIn("approval", s.bottleneck_reason.lower())

    def test_summary_slow_tool_bottleneck_summarize_repo(self) -> None:
        trace = _minimal_trace(
            total_duration_ms=9000.0,
            model_calls=[
                ModelCallTrace(
                    role="general",
                    model="m",
                    started_at="2026-01-01T00:00:00+00:00",
                    ended_at="2026-01-01T00:00:01+00:00",
                    duration_ms=800.0,
                    status=TRACE_STATUS_COMPLETED,
                    ok=True,
                )
            ],
            tool_calls=[
                ToolCallTrace(
                    tool_name="summarize_repo",
                    target="",
                    started_at="2026-01-01T00:00:00+00:00",
                    ended_at="2026-01-01T00:00:01+00:00",
                    duration_ms=5000.0,
                    status=TRACE_STATUS_COMPLETED,
                    ok=True,
                )
            ],
        )
        s = compute_trace_summary(trace)
        self.assertIn("summarize_repo", s.bottleneck_reason)

    def test_execution_loop_counters_recorded(self) -> None:
        mgr = SessionTraceManager("loop1", os.getcwd(), "dev")
        mgr.record_chat_model_turn()
        mgr.record_chat_model_turn()
        mgr.record_tool_round()
        t = mgr.build_session_trace()
        self.assertEqual(t.execution_loop_model_turns, 2)
        self.assertEqual(t.execution_loop_tool_rounds, 1)
        s = compute_trace_summary(t)
        self.assertEqual(s.execution_loop_model_turns, 2)
        self.assertEqual(s.execution_loop_tool_rounds, 1)
        short = format_compact_trace_summary("x.json", s, "/tmp", 1000.0)
        self.assertIn("loop_turns=2", short)
        self.assertIn("tool_rounds=1", short)

    def test_high_turn_count_hint_in_bottleneck_reason(self) -> None:
        trace = _minimal_trace(
            total_duration_ms=50000.0,
            execution_loop_model_turns=9,
            execution_loop_tool_rounds=4,
        )
        s = compute_trace_summary(trace)
        self.assertIn("turnos", s.bottleneck_reason.lower())

    def test_summary_slow_model_bottleneck(self) -> None:
        trace = _minimal_trace(
            total_duration_ms=12000.0,
            model_calls=[
                ModelCallTrace(
                    role="general",
                    model="big",
                    started_at="2026-01-01T00:00:00+00:00",
                    ended_at="2026-01-01T00:00:01+00:00",
                    duration_ms=9000.0,
                    status=TRACE_STATUS_COMPLETED,
                    ok=True,
                )
            ],
            tool_calls=[
                ToolCallTrace(
                    tool_name="ls",
                    target=".",
                    started_at="2026-01-01T00:00:00+00:00",
                    ended_at="2026-01-01T00:00:01+00:00",
                    duration_ms=50.0,
                    status=TRACE_STATUS_COMPLETED,
                    ok=True,
                )
            ],
            approvals=[
                ApprovalTrace(
                    approval_type="x",
                    command_or_action="",
                    requested_at="2026-01-01T00:00:00+00:00",
                    resolved_at="2026-01-01T00:00:01+00:00",
                    wait_duration_ms=100.0,
                    approved=True,
                    status=TRACE_STATUS_COMPLETED,
                )
            ],
        )
        s = compute_trace_summary(trace)
        self.assertIn("latency", s.bottleneck_reason.lower())

    def test_trace_persists_on_failure_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SessionTraceManager("task_fail", tmp, "dev")
            mgr.final_outcome = "blocked"
            mgr.final_state = "CONCLUSION"
            path, short, detail = mgr.finish_and_persist(tmp)
            self.assertIsNotNone(path)
            self.assertTrue(Path(path).exists())
            body = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(body["final_outcome"], "blocked")
            self.assertIsInstance(detail, str)

    def test_artifact_linkage_trace_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".ghost" / "artifacts").mkdir(parents=True)
            art_path = Path(tmp) / ".ghost" / "artifacts" / "task_xyz.json"
            art_path.write_text("{}", encoding="utf-8")
            sess = MagicMock()
            sess.session_id = "task_xyz"
            sess.runtime_contract_source = "taskspec"
            sess.task_outcome = "blocked"
            sess.phase = "DONE"
            sess.planner_used = False
            sess.retrieval_enabled = False
            sess.execution_agent_used = False
            sess.repo_profile = {}
            mgr = SessionTraceManager("task_xyz", tmp, "dev")
            p, short, detail = finish_session_trace(mgr, tmp, sess)
            self.assertIsNotNone(p)
            self.assertIn("task_xyz.json", mgr.artifact_path.replace("\\", "/"))
            self.assertTrue(len(short or "") > 0 or len(detail or "") > 0)

    def test_wrong_repo_note_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            note = wrong_repo_note_from_task("edit apps/cli/foo.py", tmp)
            self.assertEqual(note, "Requested paths appear outside current repo root.")

    def test_paths_outside_repo_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = paths_outside_repo_from_task("open src/foo.ts in repo", tmp)
            self.assertIsInstance(p, list)

    def test_missing_ttft_stays_null_non_stream_model(self) -> None:
        m = ModelCallTrace(
            role="general",
            model="coder",
            started_at="2026-01-01T00:00:00+00:00",
            first_token_at=None,
            ended_at="2026-01-01T00:00:01+00:00",
            ttft_ms=None,
            duration_ms=100.0,
            status=TRACE_STATUS_COMPLETED,
            ok=True,
        )
        dumped = m.model_dump()
        self.assertIsNone(dumped["ttft_ms"])
        self.assertIsNone(dumped["first_token_at"])

    def test_renderer_trace_detail_does_not_break(self) -> None:
        buf = io.StringIO()
        console = Console(file=buf, width=120, force_terminal=True, color_system=None)
        r = GhostRenderer(console)
        artifact = {
            "session_id": "task_demo",
            "timestamp": "2026-01-01T00:00:00",
            "task": "hi",
            "trace_path": "/tmp/.ghost/traces/task_demo.json",
            "trace_detail_md": "**Trace file:** `/tmp/t.json`\n**Total (wall):** 100 ms",
            "trace_summary_short": "total=100ms",
            "repo_profile": {"root": "/tmp/proj"},
            "task_outcome": "blocked",
            "outcome_confidence": 0.5,
            "evidence_score": 0,
        }
        with patch.dict(os.environ, {"GHOST_UI_VERBOSE": "1"}, clear=False):
            r.render_artifact_summary(artifact)
        out = buf.getvalue()
        self.assertIn("Trace", out)
        self.assertIn("Total (wall)", out)

    def test_verification_trace_from_result_counts(self) -> None:
        v = verification_trace_from_result(
            {
                "checks": [
                    {"name": "Lint", "status": "passed"},
                    {"name": "Build", "status": "failed"},
                ],
                "verification_source": "manager",
            },
            duration_ms=88.0,
        )
        self.assertGreaterEqual(v.checks_run, 1)
        self.assertEqual(v.total_duration_ms, 88.0)

    def test_summarize_helpers(self) -> None:
        m = [
            ModelCallTrace(
                role="g",
                model="x",
                started_at="a",
                ended_at="b",
                duration_ms=10.0,
                status=TRACE_STATUS_COMPLETED,
                ok=True,
            )
        ]
        t = [
            ToolCallTrace(
                tool_name="ls",
                target=".",
                started_at="a",
                ended_at="b",
                duration_ms=5.0,
                status=TRACE_STATUS_COMPLETED,
                ok=True,
            )
        ]
        a = [
            ApprovalTrace(
                approval_type="s",
                command_or_action="c",
                requested_at="a",
                resolved_at="b",
                wait_duration_ms=7.0,
                approved=False,
                status="denied",
            )
        ]
        self.assertEqual(summarize_model_time(m), 10.0)
        self.assertEqual(summarize_tool_time(t), 5.0)
        self.assertEqual(summarize_approval_wait(a), 7.0)

    def test_sync_from_artifact_session_carries_long_run_context(self) -> None:
        mgr = SessionTraceManager("s4", os.getcwd(), "dev")
        session = ArtifactSession("s4", "task")
        session.active_workset["candidate_files"] = ["apps/cli/main.py"]
        session.active_workset["related_tests"] = ["tests/test_provider_initialization.py"]
        session.append_phase_checkpoint(
            phase="explore",
            reason="focused exploration",
            focus_files=["apps/cli/main.py"],
            planned_next_step="edit target file",
        )
        session.record_incremental_verification(
            {
                "status": "success",
                "verification_scope": "python_targeted",
                "checks": [{"name": "Tests", "status": "passed"}],
                "steps_executed_count": 1,
            },
            diff_summary=[{"file": "apps/cli/main.py", "status": "success"}],
            context_label="iter_1",
        )
        session.approval_mode = "approval-first"
        session.expected_tool_call_contract = "native_function_calling"
        session.received_tool_call_contract = "legacy_inline_markup"
        session.tool_call_contract_mismatch = True

        mgr.sync_from_artifact_session(session)
        trace = mgr.build_session_trace()

        self.assertEqual(trace.active_workset["candidate_files"], ["apps/cli/main.py"])
        self.assertEqual(trace.phase_checkpoints[-1]["phase"], "explore")
        self.assertEqual(trace.incremental_verify_state["batches_run"], 1)
        self.assertEqual(trace.approval_mode, "approval-first")
        self.assertEqual(trace.expected_tool_call_contract, "native_function_calling")
        self.assertEqual(trace.received_tool_call_contract, "legacy_inline_markup")
        self.assertTrue(trace.tool_call_contract_mismatch)

    def test_finish_session_trace_markdown_includes_long_run_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SessionTraceManager("s5", tmp, "dev")
            session = ArtifactSession("s5", "task")
            session.active_workset["candidate_files"] = ["apps/cli/main.py"]
            session.append_phase_checkpoint(
                phase="act",
                reason="batch 1",
                focus_files=["apps/cli/main.py"],
                planned_next_step="verify batch 1",
            )
            session.record_incremental_verification(
                {
                    "status": "success",
                    "verification_scope": "python_targeted",
                    "checks": [{"name": "Tests", "status": "passed"}],
                    "steps_executed_count": 1,
                },
                diff_summary=[{"file": "apps/cli/main.py", "status": "success"}],
                context_label="iter_2",
            )

            path, short, detail = finish_session_trace(mgr, tmp, session)

            self.assertTrue(path)
            self.assertIsInstance(short, str)
            self.assertIn("LONG-RUN CONTEXT", detail)
            self.assertIn("INCREMENTAL VERIFY", detail)


class PhaseCoverageParallelTests(unittest.TestCase):
    def test_parallel_preamble_folds_planner_and_retrieval(self) -> None:
        phases = [
            PhaseTrace(
                phase_name="parallel_preamble",
                started_at="a",
                ended_at="b",
                duration_ms=120.0,
                status=TRACE_STATUS_COMPLETED,
                notes="planner_ms=10;index_ms=100",
            ),
        ]
        cov = build_phase_coverage(phases, [], "x")
        by_name = {c.phase_name: c for c in cov}
        self.assertEqual(by_name["parallel_preamble"].status, TRACE_STATUS_COMPLETED)
        self.assertIn("folded", by_name["decision_planner"].notes)
        self.assertIn("folded", by_name["retrieval"].notes)


if __name__ == "__main__":
    unittest.main()
