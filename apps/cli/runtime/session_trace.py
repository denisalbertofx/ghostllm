"""
Structured per-session CLI telemetry (local JSON only, fail-safe).
Hardened: consistent totals, explicit phase coverage, bottleneck detection, statuses.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from apps.cli.ui.ui_contract import TRACE_MD_ACTIVE_WORKSET_BOLD

logger = logging.getLogger(__name__)

_trace_ctx: ContextVar[Optional["SessionTraceManager"]] = ContextVar(
    "ghost_session_trace_manager", default=None
)

# Canonical phases (order preserved for coverage manifest)
EXPECTED_PHASE_NAMES: Tuple[str, ...] = (
    "repo_profile",
    "contract_spec",
    "parallel_preamble",
    "decision_planner",
    "retrieval",
    "execution_agent",
    "verification",
    "repair",
    "conclusion",
)

TRACE_STATUS_COMPLETED = "completed"
TRACE_STATUS_FAILED = "failed"
TRACE_STATUS_SKIPPED = "skipped"
TRACE_STATUS_BLOCKED = "blocked"
TRACE_STATUS_DENIED = "denied"
TRACE_STATUS_INCOMPLETE = "incomplete"


def get_trace_manager() -> Optional["SessionTraceManager"]:
    return _trace_ctx.get()


def attach_trace_manager(mgr: "SessionTraceManager") -> Token:
    return _trace_ctx.set(mgr)


def detach_trace_manager(token: Token) -> None:
    try:
        _trace_ctx.reset(token)
    except Exception:
        pass


@contextmanager
def trace_manager_attached(mgr: "SessionTraceManager") -> Iterator["SessionTraceManager"]:
    token = attach_trace_manager(mgr)
    try:
        yield mgr
    finally:
        detach_trace_manager(token)


# --- Pydantic models ---


class PhaseTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase_name: str
    started_at: str
    ended_at: str
    duration_ms: float
    status: str = TRACE_STATUS_COMPLETED
    notes: str = ""


class ModelCallTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    model: str
    provider_backend: str = "openai_compatible"
    started_at: str
    first_token_at: Optional[str] = None
    ended_at: str
    ttft_ms: Optional[float] = None
    duration_ms: float
    status: str = TRACE_STATUS_COMPLETED
    ok: bool = True
    error_message: str = ""
    prompt_chars: int = 0
    output_chars: int = 0
    correlation_id: str = ""


class ToolCallTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    target: str = ""
    started_at: str
    ended_at: str
    duration_ms: float
    status: str = TRACE_STATUS_COMPLETED
    ok: bool = True
    error_message: str = ""
    result_summary: str = ""
    correlation_id: str = ""


class ApprovalTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_type: str
    command_or_action: str
    requested_at: str
    resolved_at: str
    wait_duration_ms: float
    approved: bool
    status: str = TRACE_STATUS_COMPLETED
    reason: str = ""
    correlation_id: str = ""


class BatchExecutionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    batch_type: str
    step_count: int = 0
    approval_class: str = ""
    compressed_approval_used: bool = False
    total_batch_duration_ms: float = 0.0
    approval_wait_ms: float = 0.0
    ok: bool = True
    notes: str = ""


class VerificationTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks_run: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    checks_blocked: int = 0
    checks_not_run: int = 0
    total_duration_ms: float = 0.0
    verification_source: str = ""
    status: str = TRACE_STATUS_COMPLETED
    correlation_id: str = ""


class RepairTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_number: int
    started_at: str
    ended_at: str
    duration_ms: float
    failed_check: str = ""
    repair_summary: str = ""
    causal_error_signature: str = ""
    error_signature_delta: str = ""
    reverify_attempted: bool = False
    result: str = ""
    status: str = TRACE_STATUS_COMPLETED
    correlation_id: str = ""


class SessionTraceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_model_time_ms: float = 0.0
    total_tool_time_ms: float = 0.0
    total_approval_wait_ms: float = 0.0
    total_verification_time_ms: float = 0.0
    accounted_wall_components_ms: float = 0.0
    overhead_ms: float = 0.0
    duration_consistency_note: str = ""
    bottleneck_phase: str = ""
    bottleneck_tool: str = ""
    bottleneck_model: str = ""
    bottleneck_reason: str = ""
    execution_loop_model_turns: int = 0
    execution_loop_tool_rounds: int = 0


class PhaseCoverageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase_name: str
    status: str
    duration_ms: float = 0.0
    notes: str = ""


class SessionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    command_mode: str = "dev"
    cwd: str
    repo_root: str
    runtime_contract_source: str = ""
    planner_used: bool = False
    retrieval_used: bool = False
    execution_agent_used: bool = False
    fast_path_eligible: bool = False
    fast_path_used: bool = False
    first_edit_turn: int = 0
    verification_scope: str = ""
    planned_check_cwds: Dict[str, str] = Field(default_factory=dict)
    fallback_reason: str = ""
    batch_executor_used: bool = False
    approval_compression_used: bool = False
    compressed_approval_count: int = 0
    batched_read_count: int = 0
    batched_verification_count: int = 0
    batch_executions: List[BatchExecutionTrace] = Field(default_factory=list)
    started_at: str
    ended_at: str = ""
    total_duration_ms: float = 0.0
    artifact_path: str = ""
    final_outcome: str = ""
    final_state: str = ""
    phases: List[PhaseTrace] = Field(default_factory=list)
    phase_coverage: List[PhaseCoverageEntry] = Field(default_factory=list)
    model_calls: List[ModelCallTrace] = Field(default_factory=list)
    tool_calls: List[ToolCallTrace] = Field(default_factory=list)
    approvals: List[ApprovalTrace] = Field(default_factory=list)
    verification: List[VerificationTrace] = Field(default_factory=list)
    repairs: List[RepairTrace] = Field(default_factory=list)
    active_workset: Dict[str, Any] = Field(default_factory=dict)
    phase_checkpoints: List[Dict[str, Any]] = Field(default_factory=list)
    incremental_verify_state: Dict[str, Any] = Field(default_factory=dict)
    summary: Optional[SessionTraceSummary] = None
    trace_notes: List[str] = Field(default_factory=list)
    feature_flags: Dict[str, str] = Field(default_factory=dict)
    wrong_repo_diagnostic: str = ""
    # Conteos del _chat_loop del asistente (turnos de modelo / rondas con herramientas).
    execution_loop_model_turns: int = 0
    execution_loop_tool_rounds: int = 0


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def trace_timestamp_iso() -> str:
    """Public alias for trace event timestamps (UTC ISO-8601)."""
    return _utc_iso()


def _safe_int_env(name: str) -> str:
    v = os.environ.get(name)
    if v is None:
        return "0"
    return "1" if v.strip().lower() in ("1", "true", "yes", "on") else v.strip()[:32]


def collect_feature_flag_snapshot() -> Dict[str, str]:
    keys = (
        "GHOST_USE_TASKSPEC",
        "GHOST_USE_DECISION_PLANNER",
        "GHOST_USE_RETRIEVAL",
        "GHOST_RETRIEVAL_INFLUENCES_RUNTIME",
        "GHOST_USE_EXECUTION_AGENT",
        "GHOST_EXECUTION_AGENT_WRITES",
        "GHOST_EXECUTION_AGENT_LIVE",
        "GHOST_PARALLEL_PIPELINE",
        "GHOST_USE_NVIDIA_NIM",
        "GHOST_USE_BATCH_EXECUTOR",
        "GHOST_USE_APPROVAL_COMPRESSION",
        "GHOST_AUTOLOAD_ENV",
        "GHOST_CLI_TRACE_SUMMARY",
    )
    return {k: _safe_int_env(k) for k in keys}


def wrong_repo_note_from_task(task_text: str, repo_root: str) -> str:
    if not task_text or not repo_root:
        return ""
    root = Path(repo_root).resolve()
    t = task_text.replace("\\", "/").lower()
    markers = (
        ("apps/", root / "apps"),
        ("packages/", root / "packages"),
    )
    for needle, sub in markers:
        if needle in t:
            if not sub.is_dir():
                return "Requested paths appear outside current repo root."
    return ""


_PATH_TOKEN_RE = re.compile(
    r"(?:^|[\s\"\'`])((?:[\w.-]+/)+[\w.-]+\.(?:py|ts|tsx|js|jsx|json|md|yaml|yml))\b",
    re.IGNORECASE,
)


def paths_outside_repo_from_task(task_text: str, repo_root: str) -> List[str]:
    """Deterministic: path-like tokens in task that do not exist under repo_root."""
    if not task_text or not repo_root:
        return []
    root = Path(repo_root).resolve()
    out: List[str] = []
    seen: set[str] = set()
    for m in _PATH_TOKEN_RE.finditer(task_text):
        raw = m.group(1).strip().replace("\\", "/")
        if not raw or raw in seen:
            continue
        seen.add(raw)
        if raw.startswith(("http://", "https://", "//")):
            continue
        candidate = (root / raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            out.append(raw)
            continue
        if not candidate.exists():
            parts = raw.split("/")
            if len(parts) >= 2 and parts[0] in ("apps", "packages", "src", "lib"):
                out.append(raw)
    return out[:24]


def summarize_model_time(model_calls: List[ModelCallTrace]) -> float:
    return sum(m.duration_ms for m in model_calls)


def summarize_tool_time(tool_calls: List[ToolCallTrace]) -> float:
    return sum(t.duration_ms for t in tool_calls)


def summarize_approval_wait(approvals: List[ApprovalTrace]) -> float:
    return sum(a.wait_duration_ms for a in approvals)


def summarize_phase_durations(phases: List[PhaseTrace]) -> Dict[str, float]:
    acc: Dict[str, float] = {}
    for p in phases:
        acc[p.phase_name] = acc.get(p.phase_name, 0.0) + p.duration_ms
    return acc


def build_phase_coverage(
    phases: List[PhaseTrace],
    repairs: List[RepairTrace],
    started_at_fallback: str,
) -> List[PhaseCoverageEntry]:
    """One entry per expected phase; skipped if never entered."""
    by_name: Dict[str, List[PhaseTrace]] = {}
    for p in phases:
        # Legacy traces used phase name "taskspec"; fold into contract_spec for coverage.
        nm = "contract_spec" if p.phase_name == "taskspec" else p.phase_name
        by_name.setdefault(nm, []).append(p)
    folded_parallel = "parallel_preamble" in by_name
    cov: List[PhaseCoverageEntry] = []
    for name in EXPECTED_PHASE_NAMES:
        if name in by_name:
            plist = by_name[name]
            total = sum(x.duration_ms for x in plist)
            worst = max(plist, key=lambda x: x.duration_ms)
            cov.append(
                PhaseCoverageEntry(
                    phase_name=name,
                    status=worst.status,
                    duration_ms=total,
                    notes=(worst.notes or "")[:200],
                )
            )
        elif name == "parallel_preamble" and not folded_parallel:
            cov.append(
                PhaseCoverageEntry(
                    phase_name=name,
                    status=TRACE_STATUS_SKIPPED,
                    duration_ms=0.0,
                    notes="sequential planner+retrieval path",
                )
            )
        elif name in ("decision_planner", "retrieval") and folded_parallel and name not in by_name:
            cov.append(
                PhaseCoverageEntry(
                    phase_name=name,
                    status=TRACE_STATUS_COMPLETED,
                    duration_ms=0.0,
                    notes="folded into parallel_preamble",
                )
            )
        elif name == "repair":
            if repairs:
                cov.append(
                    PhaseCoverageEntry(
                        phase_name=name,
                        status=TRACE_STATUS_COMPLETED,
                        duration_ms=0.0,
                        notes="repair cycle recorded (no dedicated phase span)",
                    )
                )
            else:
                cov.append(
                    PhaseCoverageEntry(
                        phase_name=name,
                        status=TRACE_STATUS_SKIPPED,
                        duration_ms=0.0,
                        notes="no repair cycle",
                    )
                )
        else:
            cov.append(
                PhaseCoverageEntry(
                    phase_name=name,
                    status=TRACE_STATUS_SKIPPED,
                    duration_ms=0.0,
                    notes="phase not entered",
                )
            )
    return cov


def detect_bottleneck(
    trace: SessionTrace,
    total_model: float,
    total_tool: float,
    total_appr: float,
    total_verif: float,
    phase_totals: Dict[str, float],
) -> Tuple[str, str, str, str]:
    """
    Returns (bottleneck_phase, bottleneck_tool, bottleneck_model, reason).
    Each candidate is (weight_ms, kind, tool_name, model_label, reason).
    """
    candidates: List[Tuple[float, str, str, str, str]] = []

    if total_appr > 0:
        candidates.append(
            (
                total_appr,
                "approval_wait",
                "",
                "",
                f"approval_wait dominated ({total_appr:.0f}ms of human wait)",
            )
        )
    if total_model > 0:
        candidates.append(
            (
                total_model,
                "model_total",
                "",
                "",
                f"model calls sum to {total_model:.0f}ms",
            )
        )
    if total_tool > 0:
        candidates.append(
            (
                total_tool,
                "tool_total",
                "",
                "",
                f"tool calls sum to {total_tool:.0f}ms",
            )
        )
    if total_verif > 0:
        candidates.append(
            (
                total_verif,
                "verification",
                "",
                "",
                f"verification manager took {total_verif:.0f}ms",
            )
        )

    for pname, ms in phase_totals.items():
        if ms > 0 and pname not in ("verification",):
            candidates.append((ms, pname, "", "", f"phase {pname} took {ms:.0f}ms"))

    if trace.tool_calls:
        max_tool = max(trace.tool_calls, key=lambda t: t.duration_ms)
        candidates.append(
            (
                max_tool.duration_ms,
                "single_tool",
                max_tool.tool_name,
                "",
                f"{max_tool.tool_name} took {max_tool.duration_ms:.0f}ms",
            )
        )

    if trace.model_calls:
        max_model = max(trace.model_calls, key=lambda m: m.duration_ms)
        label = f"{max_model.role}:{max_model.model}"
        candidates.append(
            (
                max_model.duration_ms,
                "single_model",
                "",
                label,
                f"model call ({label}) took {max_model.duration_ms:.0f}ms",
            )
        )

    shell_denied = sum(
        1
        for a in trace.approvals
        if not a.approved and "shell" in (a.approval_type or "").lower()
    )
    if shell_denied >= 2 and total_appr > 0:
        candidates.append(
            (
                total_appr + 1.0,
                "approval_wait",
                "",
                "",
                "verification blocked by repeated shell approvals",
            )
        )

    if not candidates:
        return "", "", "", "insufficient samples for bottleneck"

    candidates.sort(key=lambda x: -x[0])
    _ms, kind, btool, bmodel, reason = candidates[0]

    if kind in ("approval_wait", "model_total", "tool_total", "verification"):
        return kind, btool, bmodel, reason
    if kind == "single_tool":
        return "tool", btool, bmodel, reason
    if kind == "single_model":
        return "model", "", bmodel, reason
    return kind, "", "", reason


def compute_trace_summary(trace: SessionTrace) -> SessionTraceSummary:
    total_model = summarize_model_time(trace.model_calls)
    total_tool = summarize_tool_time(trace.tool_calls)
    total_appr = summarize_approval_wait(trace.approvals)
    total_verif = sum(v.total_duration_ms for v in trace.verification)
    phase_totals = summarize_phase_durations(trace.phases)

    accounted = total_model + total_tool + total_appr + total_verif
    wall = trace.total_duration_ms
    overhead = wall - accounted
    note = ""
    if wall <= 0:
        note = "total_duration_ms not positive"
    elif overhead < -50:
        note = (
            "accounted components exceed wall clock (overlap or clock); "
            "components are additive per category, not disjoint wall spans"
        )
    elif overhead > wall * 0.5 and wall > 5000:
        note = (
            "large orchestration overhead vs model+tool+approval+verification "
            "(phases, indexing, planner, non-instrumented work)"
        )
    else:
        note = "components are non-overlapping sums; remainder is orchestration/overhead"

    bp, bt, bm, br = detect_bottleneck(
        trace, total_model, total_tool, total_appr, total_verif, phase_totals
    )

    if total_appr > total_model and total_appr > total_tool and total_appr > 500:
        if "approval" not in br.lower():
            br = f"approval_wait dominated total duration (~{total_appr:.0f}ms)"

    if trace.tool_calls:
        slow = max(trace.tool_calls, key=lambda t: t.duration_ms)
        if slow.tool_name == "summarize_repo" and slow.duration_ms > max(
            total_model * 0.4, 2000.0
        ):
            br = f"summarize_repo dominated tool time ({slow.duration_ms:.0f}ms)"
            bt = "summarize_repo"
            bp = bp or "tool"

    if trace.model_calls:
        slow_m = max(trace.model_calls, key=lambda m: m.duration_ms)
        if slow_m.duration_ms > total_tool + total_appr and slow_m.duration_ms > 3000:
            br = f"execution model latency dominated ({slow_m.duration_ms:.0f}ms)"
            bm = f"{slow_m.role}:{slow_m.model}"
            bp = bp or "model"

    mt = int(getattr(trace, "execution_loop_model_turns", 0) or 0)
    tr = int(getattr(trace, "execution_loop_tool_rounds", 0) or 0)
    if mt >= 8 and (not br or "latency" not in br.lower()):
        hint = (
            f"alto número de turnos en el loop del asistente "
            f"(modelo={mt} con_tools={tr}); acotar TaskSpec / objetivos reduce latencia"
        )
        br = f"{br + '; ' if br else ''}{hint}"

    return SessionTraceSummary(
        total_model_time_ms=total_model,
        total_tool_time_ms=total_tool,
        total_approval_wait_ms=total_appr,
        total_verification_time_ms=total_verif,
        accounted_wall_components_ms=accounted,
        overhead_ms=overhead,
        duration_consistency_note=note,
        bottleneck_phase=bp,
        bottleneck_tool=bt,
        bottleneck_model=bm,
        bottleneck_reason=br,
        execution_loop_model_turns=mt,
        execution_loop_tool_rounds=tr,
    )


def verification_trace_from_result(
    v_res: Dict[str, Any], duration_ms: float, source: str = "verification_manager"
) -> VerificationTrace:
    checks = v_res.get("checks") or []
    run = passed = failed = blocked = not_run = 0
    for c in checks:
        st = str(c.get("status", "") or "").lower()
        prov = str(c.get("provenance", "") or "").lower()
        if st in ("not_run", "skipped", "") and not prov:
            not_run += 1
            continue
        run += 1
        if st in ("passed", "success", "ok"):
            passed += 1
        elif st in ("failed", "error"):
            failed += 1
        elif st in ("blocked", "denied"):
            blocked += 1
        else:
            not_run += 1
    skip_exec = bool(v_res.get("skip_execution"))
    st = TRACE_STATUS_SKIPPED if skip_exec and run == 0 else TRACE_STATUS_COMPLETED
    if failed > 0:
        st = TRACE_STATUS_FAILED
    return VerificationTrace(
        checks_run=run,
        checks_passed=passed,
        checks_failed=failed,
        checks_blocked=blocked,
        checks_not_run=not_run,
        total_duration_ms=duration_ms,
        verification_source=v_res.get("verification_source") or source,
        status=st,
    )


def format_compact_trace_summary(
    trace_path: str,
    summary: Optional[SessionTraceSummary],
    repo_root: str,
    total_ms: float,
    wrong_repo: str = "",
) -> str:
    wr = f" | wrong_repo=yes" if wrong_repo else ""
    if not summary:
        return f"trace={trace_path} | root={repo_root} | total={total_ms:.0f}ms{wr}"
    bn = summary.bottleneck_reason or (
        f"{summary.bottleneck_phase or '—'}"
        f"{('/' + summary.bottleneck_tool) if summary.bottleneck_tool else ''}"
    )
    loop = ""
    if summary.execution_loop_model_turns or summary.execution_loop_tool_rounds:
        loop = (
            f" | loop_turns={summary.execution_loop_model_turns} "
            f"tool_rounds={summary.execution_loop_tool_rounds}"
        )
    return (
        f"total={total_ms:.0f}ms | model={summary.total_model_time_ms:.0f}ms | "
        f"tool={summary.total_tool_time_ms:.0f}ms | approval_wait={summary.total_approval_wait_ms:.0f}ms | "
        f"verif={summary.total_verification_time_ms:.0f}ms | overhead≈{summary.overhead_ms:.0f}ms | "
        f"bottleneck={bn[:120]}{loop}{wr}"
    )


def format_batch_execution_markdown(trace: SessionTrace) -> str:
    if not trace.batch_executor_used:
        return ""
    lines = [
        "",
        "## BATCH EXECUTION",
        f"**Batches recorded:** {len(trace.batch_executions)}",
        f"**Compressed approvals:** {trace.compressed_approval_count} | "
        f"**Batched read steps:** {trace.batched_read_count} | "
        f"**Batched verification steps:** {trace.batched_verification_count}",
        f"**Approval compression flag:** {trace.approval_compression_used}",
    ]
    for b in trace.batch_executions[:24]:
        lines.append(
            f"- `{b.batch_id}` **{b.batch_type}** steps={b.step_count} "
            f"duration={b.total_batch_duration_ms:.0f}ms "
            f"compressed={b.compressed_approval_used} ok={b.ok}"
        )
    lines.append("")
    lines.append("## APPROVAL COMPRESSION")
    lines.append(
        f"**Used:** {trace.approval_compression_used} | "
        f"**Compressed approval events:** {trace.compressed_approval_count}"
    )
    lines.append("")
    return "\n".join(lines)


def format_trace_markdown_lines(
    trace_path: str,
    summary: Optional[SessionTraceSummary],
    repo_root: str,
    cwd: str,
    total_ms: float,
    wrong_repo: str,
    batch_extra: str = "",
) -> str:
    lines = [
        f"**Trace file:** `{trace_path}`",
        f"**cwd:** `{cwd}` | **repo_root:** `{repo_root}`",
        f"**Total (wall):** {total_ms:.0f} ms",
    ]
    if summary:
        lines.append(
            f"**Model:** {summary.total_model_time_ms:.0f} ms | **Tool:** {summary.total_tool_time_ms:.0f} ms | "
            f"**Approval wait:** {summary.total_approval_wait_ms:.0f} ms | **Verification:** {summary.total_verification_time_ms:.0f} ms | "
            f"**Overhead (est.):** {summary.overhead_ms:.0f} ms"
        )
        lines.append(f"**Bottleneck:** {summary.bottleneck_reason or '—'}")
        if summary.bottleneck_tool:
            lines.append(f"**Slowest tool:** `{summary.bottleneck_tool}`")
        if summary.bottleneck_model:
            lines.append(f"**Slowest model call:** `{summary.bottleneck_model}`")
        if summary.execution_loop_model_turns or summary.execution_loop_tool_rounds:
            lines.append(
                f"**Chat loop:** model_turns={summary.execution_loop_model_turns} | "
                f"tool_rounds={summary.execution_loop_tool_rounds}"
            )
        lines.append(f"*{summary.duration_consistency_note}*")
    if wrong_repo:
        lines.append(f"**⚠ Wrong-repo hint:** {wrong_repo}")
    if batch_extra:
        lines.append(batch_extra.rstrip())
    return "\n".join(lines)


def format_trace_workset_markdown(
    active_workset: Dict[str, Any],
    phase_checkpoints: List[Dict[str, Any]],
    incremental_verify_state: Dict[str, Any],
) -> str:
    lines: List[str] = []
    if active_workset:
        focus = list(active_workset.get("edited_files") or []) + list(active_workset.get("candidate_files") or [])
        dedup_focus: List[str] = []
        for item in focus:
            if item and item not in dedup_focus:
                dedup_focus.append(str(item))
        lines.append("## LONG-RUN CONTEXT")
        if dedup_focus:
            lines.append(f"{TRACE_MD_ACTIVE_WORKSET_BOLD} {', '.join(dedup_focus[:8])}")
        if active_workset.get("related_tests"):
            lines.append(f"**Related tests:** {', '.join((active_workset.get('related_tests') or [])[:6])}")
        if active_workset.get("last_focus_reason"):
            lines.append(f"**Focus reason:** {active_workset.get('last_focus_reason')}")
    if phase_checkpoints:
        last = phase_checkpoints[-1]
        lines.append("## PHASE CHECKPOINT")
        lines.append(
            f"**Latest:** phase={last.get('phase','')} | reason={last.get('reason','')} | "
            f"focus={', '.join((last.get('focus_files') or [])[:6])}"
        )
    if incremental_verify_state:
        lines.append("## INCREMENTAL VERIFY")
        lines.append(
            f"**Batches:** {int(incremental_verify_state.get('batches_run') or 0)} | "
            f"**Last status:** {incremental_verify_state.get('last_status') or 'n/a'} | "
            f"**Last checks:** {', '.join((incremental_verify_state.get('last_checks') or [])[:6]) or 'n/a'}"
        )
    return "\n".join(lines)


def persist_trace_file(project_root: str, trace: SessionTrace) -> str:
    traces_dir = Path(project_root) / ".ghost" / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    path = traces_dir / f"{trace.session_id}.json"
    path.write_text(
        trace.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return str(path.resolve())


def _session_log_root() -> Path:
    return Path.home() / ".ghost" / "logs"


def _session_log_path(session_id: str, started_at_iso: str) -> Path:
    stamp = (
        str(started_at_iso or "")
        .replace("-", "")
        .replace(":", "")
        .replace("T", "_")
        .replace("+00:00", "Z")
    )
    return _session_log_root() / f"session-{stamp}-{session_id}.ndjson"


def _normalize_phase_status(status: str) -> str:
    if status in (TRACE_STATUS_COMPLETED, TRACE_STATUS_FAILED, TRACE_STATUS_SKIPPED, TRACE_STATUS_BLOCKED, TRACE_STATUS_INCOMPLETE):
        return status
    if status == "ok":
        return TRACE_STATUS_COMPLETED
    return status if status else TRACE_STATUS_COMPLETED


class SessionTraceManager:
    """Mutable builder; all mutating methods are fail-safe."""

    def __init__(
        self,
        session_id: str,
        cwd: str,
        command_mode: str = "dev",
    ):
        self._start_mono = time.monotonic()
        self._start_iso = _utc_iso()
        self.session_id = session_id
        self.cwd = os.path.abspath(cwd)
        self.repo_root = self.cwd
        self.command_mode = command_mode
        self._event_seq = 0
        self._tool_seq = 0
        self._model_seq = 0
        self._approval_seq = 0
        self._verification_seq = 0
        self._repair_seq = 0
        self.ndjson_log_path = str(_session_log_path(session_id, self._start_iso))
        self._phase_stack: List[Tuple[str, float, str]] = []
        self._file_not_found_paths: List[str] = []
        self._task_text_for_diagnostic = ""
        self.phases: List[PhaseTrace] = []
        self.model_calls: List[ModelCallTrace] = []
        self.tool_calls: List[ToolCallTrace] = []
        self.approvals: List[ApprovalTrace] = []
        self.verification: List[VerificationTrace] = []
        self.repairs: List[RepairTrace] = []
        self.trace_notes: List[str] = []
        self.runtime_contract_source = ""
        self.planner_used = False
        self.retrieval_used = False
        self.execution_agent_used = False
        self.batch_executions: List[BatchExecutionTrace] = []
        self.batch_executor_used = False
        self.approval_compression_used = False
        self.compressed_approval_count = 0
        self.batched_read_count = 0
        self.batched_verification_count = 0
        self.artifact_path = ""
        self.final_outcome = ""
        self.final_state = ""
        self.execution_loop_model_turns = 0
        self.execution_loop_tool_rounds = 0
        self.active_workset: Dict[str, Any] = {}
        self.phase_checkpoints: List[Dict[str, Any]] = []
        self.incremental_verify_state: Dict[str, Any] = {}
        self._log_event("session_start", {"cwd": self.cwd, "command_mode": self.command_mode})

    def _log_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        try:
            self._event_seq += 1
            path = Path(self.ndjson_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "ts": _utc_iso(),
                "event_id": f"{self.session_id}:event:{self._event_seq}",
                "session_id": self.session_id,
                "session_correlation_id": self.session_id,
                "command_mode": self.command_mode,
                "event_type": event_type,
                "payload": payload,
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("session ndjson log failed: %s", e)

    def record_chat_model_turn(self) -> None:
        """Un turno donde el modelo principal devolvió mensaje (haya o no tool calls)."""
        try:
            self.execution_loop_model_turns += 1
        except Exception:
            pass

    def record_tool_round(self) -> None:
        """Una ronda en la que el modelo solicitó ejecución de herramientas."""
        try:
            self.execution_loop_tool_rounds += 1
        except Exception:
            pass

    def set_task_text(self, text: str) -> None:
        try:
            self._task_text_for_diagnostic = text or ""
        except Exception:
            pass

    def set_repo_root(self, root: str) -> None:
        try:
            if root:
                self.repo_root = os.path.abspath(root)
                self._log_event("repo_root", {"repo_root": self.repo_root})
        except Exception:
            pass

    def sync_from_artifact_session(self, session: Any) -> None:
        try:
            self.runtime_contract_source = str(
                getattr(session, "runtime_contract_source", "") or ""
            )
            self.planner_used = bool(getattr(session, "planner_used", False))
            self.retrieval_used = bool(getattr(session, "retrieval_enabled", False))
            self.execution_agent_used = bool(getattr(session, "execution_agent_used", False))
            self.fast_path_eligible = bool(getattr(session, "fast_path_eligible", False))
            self.fast_path_used = bool(getattr(session, "fast_path_used", False))
            self.first_edit_turn = int(getattr(session, "first_edit_turn", 0) or 0)
            self.verification_scope = str(getattr(session, "verification_scope", "") or "")
            self.planned_check_cwds = dict(getattr(session, "planned_check_cwds", {}) or {})
            self.fallback_reason = str(getattr(session, "fallback_reason", "") or "")
            self.active_workset = dict(getattr(session, "active_workset", {}) or {})
            self.phase_checkpoints = list(getattr(session, "phase_checkpoints", []) or [])
            self.incremental_verify_state = dict(getattr(session, "incremental_verify_state", {}) or {})
            rp = getattr(session, "repo_profile", None) or {}
            if isinstance(rp, dict) and rp.get("root"):
                self.set_repo_root(str(rp["root"]))
        except Exception:
            pass

    def note_file_not_found(self, path: str) -> None:
        try:
            if path and path not in self._file_not_found_paths:
                self._file_not_found_paths.append(path[:500])
        except Exception:
            pass

    def maybe_append_wrong_repo_note(self) -> None:
        try:
            note = wrong_repo_note_from_task(self._task_text_for_diagnostic, self.repo_root)
            if note and note not in self.trace_notes:
                self.trace_notes.append(note)
            outside = paths_outside_repo_from_task(
                self._task_text_for_diagnostic, self.repo_root
            )
            if outside:
                msg = (
                    "Task references paths that do not exist under repo_root: "
                    + ", ".join(outside[:6])
                )
                if msg not in self.trace_notes:
                    self.trace_notes.append(msg)
        except Exception:
            pass

    def append_wrong_repo_repeated_missing(self) -> None:
        try:
            if len(self._file_not_found_paths) >= 2:
                msg = "Repeated file-not-found on paths; check repo root vs requested paths."
                if msg not in self.trace_notes:
                    self.trace_notes.append(msg)
        except Exception:
            pass

    def start_phase(self, phase_name: str) -> None:
        try:
            self._phase_stack.append((phase_name, time.monotonic(), _utc_iso()))
            self._log_event("phase_start", {"phase_name": phase_name})
        except Exception as e:
            logger.debug("start_phase failed: %s", e)

    def end_phase(
        self, phase_name: str, status: str = TRACE_STATUS_COMPLETED, notes: str = ""
    ) -> None:
        try:
            if not self._phase_stack:
                return
            name, t0, start_iso = self._phase_stack.pop()
            if name != phase_name:
                self.trace_notes.append(
                    f"phase_end_mismatch: expected={phase_name} closed={name}"
                )
            dur_ms = (time.monotonic() - t0) * 1000.0
            end_iso = _utc_iso()
            st = _normalize_phase_status(status)
            self.phases.append(
                PhaseTrace(
                    phase_name=name,
                    started_at=start_iso,
                    ended_at=end_iso,
                    duration_ms=dur_ms,
                    status=st,
                    notes=notes,
                )
            )
            self._log_event(
                "phase_end",
                {
                    "phase_name": name,
                    "status": st,
                    "duration_ms": dur_ms,
                    "notes": notes,
                },
            )
        except Exception as e:
            logger.debug("end_phase failed: %s", e)

    def record_model_call(self, m: ModelCallTrace) -> None:
        try:
            if not getattr(m, "correlation_id", ""):
                self._model_seq += 1
                m.correlation_id = f"{self.session_id}:model:{self._model_seq}"
            self.model_calls.append(m)
            self._log_event(
                "model_call",
                {
                    "correlation_id": m.correlation_id,
                    "model": m.model,
                    "role": m.role,
                    "status": m.status,
                    "duration_ms": m.duration_ms,
                    "ttft_ms": m.ttft_ms,
                    "ok": m.ok,
                },
            )
        except Exception as e:
            logger.debug("record_model_call failed: %s", e)

    def record_tool_call(self, t: ToolCallTrace) -> None:
        try:
            if not getattr(t, "correlation_id", ""):
                self._tool_seq += 1
                t.correlation_id = f"{self.session_id}:tool:{self._tool_seq}"
            self.tool_calls.append(t)
            err = (t.error_message or "").lower()
            if not t.ok and ("path not found" in err or "not found" in err) and t.target:
                self.note_file_not_found(t.target)
                self.append_wrong_repo_repeated_missing()
            self._log_event(
                "tool_call",
                {
                    "correlation_id": t.correlation_id,
                    "tool_name": t.tool_name,
                    "target": t.target,
                    "status": t.status,
                    "duration_ms": t.duration_ms,
                    "ok": t.ok,
                    "error_message": t.error_message[:500],
                },
            )
        except Exception as e:
            logger.debug("record_tool_call failed: %s", e)

    def record_approval_wait(self, a: ApprovalTrace) -> None:
        try:
            if not getattr(a, "correlation_id", ""):
                self._approval_seq += 1
                a.correlation_id = f"{self.session_id}:approval:{self._approval_seq}"
            self.approvals.append(a)
            self._log_event(
                "approval",
                {
                    "correlation_id": a.correlation_id,
                    "approval_type": a.approval_type,
                    "approved": a.approved,
                    "wait_duration_ms": a.wait_duration_ms,
                    "status": a.status,
                },
            )
        except Exception as e:
            logger.debug("record_approval_wait failed: %s", e)

    def record_verification_trace(self, v: VerificationTrace) -> None:
        try:
            if not getattr(v, "correlation_id", ""):
                self._verification_seq += 1
                v.correlation_id = f"{self.session_id}:verify:{self._verification_seq}"
            self.verification.append(v)
            self._log_event(
                "verification",
                {
                    "correlation_id": v.correlation_id,
                    "status": v.status,
                    "checks_run": v.checks_run,
                    "checks_failed": v.checks_failed,
                    "total_duration_ms": v.total_duration_ms,
                },
            )
        except Exception as e:
            logger.debug("record_verification_trace failed: %s", e)

    def record_repair_trace(self, r: RepairTrace) -> None:
        try:
            if not getattr(r, "correlation_id", ""):
                self._repair_seq += 1
                r.correlation_id = f"{self.session_id}:repair:{self._repair_seq}"
            self.repairs.append(r)
            self._log_event(
                "repair",
                {
                    "correlation_id": r.correlation_id,
                    "attempt_number": r.attempt_number,
                    "status": r.status,
                    "duration_ms": r.duration_ms,
                    "result": r.result,
                },
            )
        except Exception as e:
            logger.debug("record_repair_trace failed: %s", e)

    def record_batch_execution(self, b: BatchExecutionTrace) -> None:
        try:
            self.batch_executor_used = True
            self.batch_executions.append(b)
            if b.compressed_approval_used:
                self.approval_compression_used = True
                self.compressed_approval_count += 1
            if b.batch_type == "read_batch":
                self.batched_read_count += b.step_count
            elif b.batch_type == "verify_batch":
                self.batched_verification_count += b.step_count
        except Exception as e:
            logger.debug("record_batch_execution failed: %s", e)

    def build_session_trace(self) -> SessionTrace:
        ended = _utc_iso()
        total_ms = max(0.001, (time.monotonic() - self._start_mono) * 1000.0)
        wrong = next(
            (n for n in self.trace_notes if "repo root" in n.lower() or "repo_root" in n.lower()),
            "",
        )
        if not wrong:
            wrong = next(
                (
                    n
                    for n in self.trace_notes
                    if "do not exist under repo_root" in n
                    or "file-not-found" in n.lower()
                ),
                "",
            )
        st = SessionTrace(
            session_id=self.session_id,
            command_mode=self.command_mode,
            cwd=self.cwd,
            repo_root=self.repo_root,
            runtime_contract_source=self.runtime_contract_source,
            planner_used=self.planner_used,
            retrieval_used=self.retrieval_used,
            execution_agent_used=self.execution_agent_used,
            batch_executor_used=self.batch_executor_used,
            approval_compression_used=self.approval_compression_used,
            compressed_approval_count=self.compressed_approval_count,
            batched_read_count=self.batched_read_count,
            batched_verification_count=self.batched_verification_count,
            batch_executions=list(self.batch_executions),
            started_at=self._start_iso,
            ended_at=ended,
            total_duration_ms=total_ms,
            artifact_path=self.artifact_path,
            final_outcome=self.final_outcome,
            final_state=self.final_state,
            phases=list(self.phases),
            phase_coverage=build_phase_coverage(
                self.phases, self.repairs, self._start_iso
            ),
            model_calls=list(self.model_calls),
            tool_calls=list(self.tool_calls),
            approvals=list(self.approvals),
            verification=list(self.verification),
            repairs=list(self.repairs),
            active_workset=dict(self.active_workset or {}),
            phase_checkpoints=list(self.phase_checkpoints or []),
            incremental_verify_state=dict(self.incremental_verify_state or {}),
            trace_notes=list(self.trace_notes),
            feature_flags=collect_feature_flag_snapshot(),
            wrong_repo_diagnostic=wrong
            or wrong_repo_note_from_task(self._task_text_for_diagnostic, self.repo_root),
            execution_loop_model_turns=int(self.execution_loop_model_turns),
            execution_loop_tool_rounds=int(self.execution_loop_tool_rounds),
        )
        st.summary = compute_trace_summary(st)
        return st

    def finish_and_persist(
        self, project_root: str
    ) -> Tuple[Optional[str], Optional[str], str]:
        try:
            self.maybe_append_wrong_repo_note()
            trace = self.build_session_trace()
            path = persist_trace_file(project_root, trace)
            short = ""
            if trace.summary:
                short = format_compact_trace_summary(
                    path,
                    trace.summary,
                    trace.repo_root,
                    trace.total_duration_ms,
                    wrong_repo=trace.wrong_repo_diagnostic or "",
                )
            detail_md = format_trace_markdown_lines(
                path or "",
                trace.summary,
                trace.repo_root,
                trace.cwd,
                trace.total_duration_ms,
                trace.wrong_repo_diagnostic or "",
                batch_extra=format_batch_execution_markdown(trace),
            )
            extra = format_trace_workset_markdown(
                trace.active_workset,
                trace.phase_checkpoints,
                trace.incremental_verify_state,
            )
            if extra:
                detail_md = detail_md + "\n" + extra
            self._log_event(
                "session_finish",
                {
                    "trace_path": path,
                    "artifact_path": self.artifact_path,
                    "final_outcome": trace.final_outcome,
                    "final_state": trace.final_state,
                },
            )
            return path, short, detail_md
        except Exception as e:
            logger.warning("session trace persist failed (non-fatal): %s", e)
            return None, None, ""


def record_policy_approval(
    approval_type: str,
    command_or_action: str,
    requested_at: str,
    approved: bool,
    reason: str = "",
    wait_duration_ms: Optional[float] = None,
) -> None:
    mgr = get_trace_manager()
    if not mgr:
        return
    try:
        resolved = _utc_iso()
        wait_ms = wait_duration_ms
        if wait_ms is None:
            wait_ms = 0.0
            try:
                t0 = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(resolved.replace("Z", "+00:00"))
                wait_ms = max(0.0, (t1 - t0).total_seconds() * 1000.0)
            except Exception:
                wait_ms = 0.0
        st = TRACE_STATUS_COMPLETED if approved else TRACE_STATUS_DENIED
        mgr.record_approval_wait(
            ApprovalTrace(
                approval_type=approval_type,
                command_or_action=command_or_action[:2000],
                requested_at=requested_at,
                resolved_at=resolved,
                wait_duration_ms=float(wait_ms),
                approved=approved,
                status=st,
                reason=reason[:500],
            )
        )
    except Exception as e:
        logger.debug("record_policy_approval failed: %s", e)


def start_session_trace(
    session_id: str,
    cwd: str,
    command_mode: str = "dev",
) -> SessionTraceManager:
    return SessionTraceManager(
        session_id=session_id,
        cwd=cwd,
        command_mode=command_mode,
    )


def finish_session_trace(
    mgr: SessionTraceManager,
    project_root: str,
    artifact_session: Any = None,
) -> Tuple[Optional[str], Optional[str], str]:
    if artifact_session is not None:
        try:
            mgr.sync_from_artifact_session(artifact_session)
            mgr.final_outcome = str(
                getattr(artifact_session, "task_outcome", "") or ""
            )
            mgr.final_state = str(getattr(artifact_session, "phase", None) or "")
            aid = getattr(artifact_session, "session_id", "")
            if aid:
                json_path = os.path.join(
                    project_root, ".ghost", "artifacts", f"{aid}.json"
                )
                mgr.artifact_path = os.path.abspath(json_path)
        except Exception:
            pass
    return mgr.finish_and_persist(project_root)
