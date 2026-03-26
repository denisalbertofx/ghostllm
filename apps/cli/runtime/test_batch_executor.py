"""Tests for batch_executor planning, grouping, and telemetry helpers."""
from __future__ import annotations

from apps.cli.runtime import batch_executor as be
from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.session_trace import (
    BatchExecutionTrace,
    SessionTraceManager,
    attach_trace_manager,
    detach_trace_manager,
    record_policy_approval,
    trace_timestamp_iso,
)
from apps.cli.runtime.verification import (
    CHECK_DENIED,
    PROVENANCE_DENIED_BY_USER,
    VerificationManager,
)
from apps.cli.runtime.verification_coordinator import VerificationCoordinator


def _pc(name: str, **kwargs):
    return {"name": name, "args": dict(kwargs), "level": None, "is_authorized": False}


def test_read_tools_grouped_into_read_batch():
    prepared = [
        _pc("read_file", path="a.py"),
        _pc("read_file", path="b.py"),
        _pc("write_file", path="z.py", content=""),
    ]
    segs = be.segment_prepared_call_indices(prepared)
    kinds = [s[0] for s in segs]
    assert kinds[0] == "read_batch"
    assert segs[0][1] == [0, 1]
    assert kinds[1] == "single"


def test_verification_shells_grouped_into_verify_batch():
    prepared = [
        _pc("run_shell", command="npx tsc --noEmit"),
        _pc("run_shell", command="npm run lint"),
        _pc("run_shell", command="npm run build"),
    ]
    segs = be.segment_prepared_call_indices(prepared)
    assert len(segs) == 1
    assert segs[0][0] == "verify_batch"
    assert segs[0][1] == [0, 1, 2]


def test_unsafe_or_nonverify_shell_not_grouped_with_verify_batch():
    prepared = [
        _pc("run_shell", command="npx tsc --noEmit"),
        _pc("run_shell", command="rm -rf /"),
    ]
    segs = be.segment_prepared_call_indices(prepared)
    assert segs[0] == ("single", [0])
    assert segs[1][0] == "single"


def test_write_actions_not_merged_into_read_batch():
    prepared = [
        _pc("read_file", path="a.py"),
        _pc("write_file", path="x.py", content="x"),
        _pc("read_file", path="b.py"),
    ]
    segs = be.segment_prepared_call_indices(prepared)
    kinds = [s[0] for s in segs]
    assert "read_batch" not in kinds


def test_approval_compression_bundles_verify_into_one_request_object():
    prepared = [
        _pc("run_shell", command="npx tsc --noEmit"),
        _pc("run_shell", command="npm run lint"),
    ]
    segs = be.segment_prepared_call_indices(prepared)
    plan = be.build_batch_plan_for_segment(segs[0][0], segs[0][1], prepared)
    assert plan is not None
    req = be.build_verify_batch_approval_request(plan)
    assert req.step_count == 2
    assert len(req.shell_commands) == 2
    assert be.should_compress_verify_approval(
        "verify_batch", 2, compression_enabled=True
    )
    assert not be.should_compress_verify_approval(
        "verify_batch", 2, compression_enabled=False
    )


def test_read_batch_preserves_deterministic_file_order():
    paths = ["z.py", "a.py", "m.py"]
    plan = be.plan_read_batch_from_paths(paths)
    assert plan is not None
    targets = [s.target for s in plan.steps]
    assert targets == paths


def test_verify_batch_preserves_order():
    prepared = [
        _pc("run_shell", command="npm run build"),
        _pc("run_shell", command="npx tsc --noEmit"),
    ]
    segs = be.segment_prepared_call_indices(prepared)
    plan = be.build_batch_plan_for_segment(segs[0][0], segs[0][1], prepared)
    cmds = [s.args.get("command") for s in plan.steps]
    assert cmds == ["npm run build", "npx tsc --noEmit"]


def test_taskspec_ui_block_is_per_tool_assistant_contract():
    """batch_executor only groups indices; TaskSpec deny must run per step in assistant."""
    prepared = [
        _pc("read_file", path="apps/web/src/x.tsx"),
        _pc("read_file", path="apps/web/src/y.tsx"),
    ]
    segs = be.segment_prepared_call_indices(prepared)
    assert segs[0][0] == "read_batch"
    # Grouping does not imply bypass — assistant still calls _taskspec_allow_tool per step.


def test_fallback_single_segments_when_batch_disabled_simulation():
    n = 4
    singles = [("single", [i]) for i in range(n)]
    assert be.flatten_segment_order(singles) == [0, 1, 2, 3]


def test_telemetry_records_batch_duration_and_step_timing():
    res = be.BatchExecutionResult(
        batch_id="b1",
        ok=True,
        duration_ms=42.0,
        step_results=[
            be.BatchStepResult(
                step_id="s0",
                tool_name="read_file",
                duration_ms=10.0,
                result_summary="ok",
            ),
            be.BatchStepResult(
                step_id="s1",
                tool_name="read_file",
                duration_ms=12.0,
                result_summary="ok",
            ),
        ],
    )
    telem = be.telemetry_from_execution_result(res)
    assert telem.batch_duration_ms == 42.0
    assert telem.total_steps == 2
    assert telem.executed_steps == 2


def test_artifact_markdown_contains_batch_sections():
    s = ArtifactSession("sid", "task")
    s.batch_executor_used = True
    s.batch_plans = [{"batch_id": "x", "batch_type": "read_batch"}]
    s.batch_results = [{"batch_id": "x", "ok": True}]
    s.approval_compression_used = True
    s.compressed_approval_count = 1
    s.batched_read_count = 2
    s.batched_verification_count = 3
    md = s.to_markdown()
    assert "BATCH EXECUTION" in md
    assert "APPROVAL COMPRESSION" in md


def test_compressed_batch_records_single_approval_trace_entry_via_policy_hook():
    mgr = SessionTraceManager(session_id="t1", cwd=".")
    token = attach_trace_manager(mgr)
    try:
        before = len(mgr.approvals)
        record_policy_approval(
            "verify_batch_approval",
            "cmd1; cmd2",
            trace_timestamp_iso(),
            True,
            wait_duration_ms=15.0,
        )
        assert len(mgr.approvals) == before + 1
        assert mgr.approvals[-1].approval_type == "verify_batch_approval"
        assert mgr.approvals[-1].wait_duration_ms == 15.0
    finally:
        detach_trace_manager(token)


def test_ambiguous_verify_then_random_shell_splits_batches():
    prepared = [
        _pc("run_shell", command="npx tsc --noEmit"),
        _pc("run_shell", command="echo hello"),
        _pc("run_shell", command="npm run lint"),
    ]
    segs = be.segment_prepared_call_indices(prepared)
    kinds = [x[0] for x in segs]
    assert kinds[0] == "single"
    assert kinds[1] == "single"
    assert kinds[2] == "single"


def test_verification_manager_batch_denial(tmp_path):
    pkg = tmp_path / "package.json"
    pkg.write_text('{"scripts":{"build":"exit 0","lint":"exit 0"}}', encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    vm = VerificationManager(str(tmp_path))
    vm.set_repo_verification_commands(
        {"typecheck": "npx tsc --noEmit", "lint": "npm run lint", "build": "npm run build"}
    )

    def deny(_cmds):
        return False

    coord = VerificationCoordinator(vm)
    res = coord.run(
        diff_summary=[{"file": "x.ts"}],
        task_contract=None,
        task_type="direct_edit",
        scope="unknown",
        budget_remaining=100,
        skip_execution=False,
        verify_batch_approval_fn=deny,
        session=None,
        reuse_if_verified=False,
    )
    assert res.get("status") == "blocked"
    checks = res.get("checks") or []
    denied = [c for c in checks if c.get("status") == CHECK_DENIED]
    assert len(denied) >= 2
    assert all(c.get("provenance") == PROVENANCE_DENIED_BY_USER for c in denied[:2])


def test_session_trace_manager_batch_counters():
    mgr = SessionTraceManager(session_id="t2", cwd=".")
    mgr.record_batch_execution(
        BatchExecutionTrace(
            batch_id="r1",
            batch_type="read_batch",
            step_count=3,
            compressed_approval_used=False,
            total_batch_duration_ms=9.0,
            ok=True,
        )
    )
    mgr.record_batch_execution(
        BatchExecutionTrace(
            batch_id="v1",
            batch_type="verify_batch",
            step_count=2,
            approval_class="shell_verify",
            compressed_approval_used=True,
            total_batch_duration_ms=100.0,
            ok=True,
        )
    )
    assert mgr.batched_read_count == 3
    assert mgr.batched_verification_count == 2
    assert mgr.compressed_approval_count == 1
    assert mgr.batch_executor_used
