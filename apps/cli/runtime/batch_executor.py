"""
Batch tool execution planning + approval compression helpers (Sprint 1).

Deterministic grouping only; no parallel execution; fail-safe fallback to single-step.
Feature flags (read in assistant): GHOST_USE_BATCH_EXECUTOR, GHOST_USE_APPROVAL_COMPRESSION.
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

# Align with verification._VERIFY_KEYWORDS for shell_verify classification
_VERIFY_KEYWORDS_FLAT = (
    "npm run build",
    "yarn build",
    "pnpm build",
    "npx tsc",
    "tsc",
    "typecheck",
    "type-check",
    "eslint",
    "npm run lint",
    "lint",
    "pytest",
    "npm test",
    "jest",
    "build",
    "test",
)

_DESTRUCTIVE_PATTERNS = [
    re.compile(r"rm\s+-rf", re.I),
    re.compile(r"\brm\s+(-[rf]+\s+|\s+)(/|\.\./)", re.I),
    re.compile(r"del\s+/s\s+/q", re.I),
    re.compile(r"rd\s+/s\s+/q", re.I),
    re.compile(r"format\s+", re.I),
    re.compile(r"mkfs", re.I),
    re.compile(r"curl\s+.*\|\s*(ba)?sh", re.I),
    re.compile(r"wget\s+.*\|\s*(ba)?sh", re.I),
]

READ_TOOL_NAMES = frozenset({"ls", "read_file", "summarize_repo"})

BatchType = Literal["read_batch", "verify_batch", "patch_batch", "mixed_batch"]
StepType = Literal["read", "write", "verify", "shell", "other"]
ApprovalClass = Literal["safe_read", "shell_verify", "write_patch", "destructive", "other"]


class BatchStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    tool_name: str
    target: str = ""
    args: Dict[str, Any] = Field(default_factory=dict)
    step_type: StepType = "other"
    approval_class: ApprovalClass = "other"
    can_run_in_batch: bool = False
    reason: str = ""


class BatchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    batch_type: BatchType
    steps: List[BatchStep] = Field(default_factory=list)
    requires_approval: bool = False
    approval_scope: str = ""
    created_from_phase: str = "tool_loop"
    notes: str = ""


class BatchStepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    tool_name: str
    ok: bool = True
    status: str = "completed"
    error_message: str = ""
    duration_ms: float = 0.0
    result_summary: str = ""


class BatchApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    approval_class: ApprovalClass
    summary: str = ""
    step_count: int = 0
    representative_targets: List[str] = Field(default_factory=list)
    shell_commands: List[str] = Field(default_factory=list)


class BatchApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool = False
    requested_at: str = ""
    resolved_at: str = ""
    wait_duration_ms: float = 0.0
    reason: str = ""


class BatchExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    ok: bool = True
    step_results: List[BatchStepResult] = Field(default_factory=list)
    approval_requested: bool = False
    approval_decision: Optional[BatchApprovalDecision] = None
    started_at: str = ""
    ended_at: str = ""
    duration_ms: float = 0.0
    summary: str = ""


class BatchTelemetrySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_steps: int = 0
    executed_steps: int = 0
    skipped_steps: int = 0
    failed_steps: int = 0
    approval_wait_ms: float = 0.0
    batch_duration_ms: float = 0.0


def _new_batch_id(prefix: str = "b") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def is_read_tool(name: str) -> bool:
    return (name or "").strip() in READ_TOOL_NAMES


def is_shell_destructive_or_ambiguous(command: str) -> bool:
    cmd = (command or "").strip()
    if not cmd:
        return True
    for pat in _DESTRUCTIVE_PATTERNS:
        if pat.search(cmd):
            return True
    # Ambiguous: pipes to arbitrary shells, sudo power patterns
    if re.search(r"\|\s*(ba)?sh\b", cmd, re.I):
        return True
    if re.search(r"\bsudo\b", cmd, re.I) and not re.search(r"npx|npm|yarn|pnpm|tsc|eslint|jest", cmd, re.I):
        return True
    return False


def is_verification_shell_command(command: str) -> bool:
    """True only when command looks like typecheck/lint/build/test (not general shell)."""
    cmd = (command or "").strip().lower()
    if not cmd or is_shell_destructive_or_ambiguous(command):
        return False
    return any(kw.lower() in cmd for kw in _VERIFY_KEYWORDS_FLAT)


def _step_from_prepared(
    idx: int,
    name: str,
    args: Dict[str, Any],
    *,
    step_type: StepType,
    approval_class: ApprovalClass,
    can_batch: bool,
    reason: str = "",
) -> BatchStep:
    target = str(args.get("path") or args.get("command") or "")
    return BatchStep(
        step_id=f"s{idx}",
        tool_name=name,
        target=target,
        args=dict(args) if args else {},
        step_type=step_type,
        approval_class=approval_class,
        can_run_in_batch=can_batch,
        reason=reason,
    )


def plan_read_batch_from_paths(paths: List[str], *, phase: str = "retrieval") -> Optional[BatchPlan]:
    """Build a read_batch plan from ordered paths (deterministic order preserved)."""
    clean = [p for p in paths if p and str(p).strip()]
    if len(clean) < 2:
        return None
    steps: List[BatchStep] = []
    for i, p in enumerate(clean):
        steps.append(
            BatchStep(
                step_id=f"r{i}",
                tool_name="read_file",
                target=str(p),
                args={"path": str(p)},
                step_type="read",
                approval_class="safe_read",
                can_run_in_batch=True,
                reason="ordered_read_candidate",
            )
        )
    return BatchPlan(
        batch_id=_new_batch_id("read"),
        batch_type="read_batch",
        steps=steps,
        requires_approval=False,
        approval_scope="safe_read",
        created_from_phase=phase,
        notes="read-only batch (AUTO policy)",
    )


def plan_patch_batch_proposal_only(write_steps: List[BatchStep]) -> Optional[BatchPlan]:
    """Sprint 1: proposal metadata only; execution stays conservative single-step."""
    if len(write_steps) < 2:
        return None
    return BatchPlan(
        batch_id=_new_batch_id("patch"),
        batch_type="patch_batch",
        steps=write_steps,
        requires_approval=True,
        approval_scope="write_patch",
        created_from_phase="tool_loop",
        notes="proposal_only: do not auto-apply multi-write in Sprint 1",
    )


def segment_prepared_call_indices(
    prepared_calls: List[Dict[str, Any]],
    *,
    mixed_batch_enabled: bool = False,
) -> List[Tuple[str, List[int]]]:
    """
    Partition indices into read_batch, verify_batch, or single segments.
    Contiguous grouping only; mixed types split (mixed_batch disabled by default).
    """
    n = len(prepared_calls)
    if n == 0:
        return []
    segments: List[Tuple[str, List[int]]] = []
    i = 0
    while i < n:
        pc = prepared_calls[i]
        name = pc.get("name") or ""

        if is_read_tool(name):
            j = i + 1
            while j < n and is_read_tool(prepared_calls[j].get("name") or ""):
                j += 1
            if j - i >= 2:
                segments.append(("read_batch", list(range(i, j))))
            else:
                segments.append(("single", [i]))
            i = j
            continue

        if name == "run_shell":
            j = i
            verify_indices: List[int] = []
            while j < n:
                pcj = prepared_calls[j]
                nm = pcj.get("name") or ""
                if nm != "run_shell":
                    break
                args = pcj.get("args") or {}
                cmd = str(args.get("command") or "")
                if is_verification_shell_command(cmd):
                    verify_indices.append(j)
                    j += 1
                else:
                    break
            if len(verify_indices) >= 2:
                segments.append(("verify_batch", verify_indices))
                i = verify_indices[-1] + 1
                continue
            if len(verify_indices) == 1:
                segments.append(("single", verify_indices))
                i = verify_indices[-1] + 1
                continue
            # run_shell but not batchable verify (ambiguous or non-verify)
            segments.append(("single", [i]))
            i += 1
            continue

        segments.append(("single", [i]))
        i += 1

    if not mixed_batch_enabled:
        return segments
    # mixed_batch path reserved — same as above when disabled
    return segments


def build_batch_plan_for_segment(
    segment_kind: str,
    indices: List[int],
    prepared_calls: List[Dict[str, Any]],
    *,
    phase: str = "tool_loop",
) -> Optional[BatchPlan]:
    if segment_kind == "read_batch":
        steps: List[BatchStep] = []
        for k, idx in enumerate(indices):
            pc = prepared_calls[idx]
            name = pc.get("name") or ""
            args = pc.get("args") or {}
            steps.append(
                _step_from_prepared(
                    k,
                    name,
                    args,
                    step_type="read",
                    approval_class="safe_read",
                    can_batch=True,
                )
            )
        return BatchPlan(
            batch_id=_new_batch_id("read"),
            batch_type="read_batch",
            steps=steps,
            requires_approval=False,
            approval_scope="safe_read",
            created_from_phase=phase,
        )

    if segment_kind == "verify_batch":
        steps: List[BatchStep] = []
        for k, idx in enumerate(indices):
            pc = prepared_calls[idx]
            args = pc.get("args") or {}
            cmd = str(args.get("command") or "")
            steps.append(
                _step_from_prepared(
                    k,
                    "run_shell",
                    args,
                    step_type="verify",
                    approval_class="shell_verify",
                    can_batch=is_verification_shell_command(cmd),
                    reason="verification_keyword_match",
                )
            )
        if any(not s.can_run_in_batch for s in steps):
            return None
        return BatchPlan(
            batch_id=_new_batch_id("verify"),
            batch_type="verify_batch",
            steps=steps,
            requires_approval=True,
            approval_scope="shell_verify",
            created_from_phase=phase,
            notes="verification shell batch",
        )
    return None


def build_verify_batch_approval_request(plan: BatchPlan) -> BatchApprovalRequest:
    cmds = [str(s.args.get("command") or s.target) for s in plan.steps]
    return BatchApprovalRequest(
        batch_id=plan.batch_id,
        approval_class="shell_verify",
        summary=f"Approve verification batch ({len(cmds)} commands)",
        step_count=len(cmds),
        representative_targets=cmds[:5],
        shell_commands=cmds,
    )


def telemetry_from_execution_result(res: BatchExecutionResult) -> BatchTelemetrySummary:
    total = len(res.step_results)
    failed = sum(1 for r in res.step_results if not r.ok)
    ok = sum(1 for r in res.step_results if r.ok)
    wait = 0.0
    if res.approval_decision:
        wait = float(res.approval_decision.wait_duration_ms)
    return BatchTelemetrySummary(
        total_steps=total,
        executed_steps=total,
        skipped_steps=0,
        failed_steps=failed,
        approval_wait_ms=wait,
        batch_duration_ms=float(res.duration_ms),
    )


def flatten_segment_order(segments: List[Tuple[str, List[int]]]) -> List[int]:
    out: List[int] = []
    for _, idxs in segments:
        out.extend(idxs)
    return out


def should_compress_verify_approval(
    segment_kind: str,
    num_steps: int,
    *,
    compression_enabled: bool,
) -> bool:
    return compression_enabled and segment_kind == "verify_batch" and num_steps >= 2
