import os
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apps.cli.runtime.runtime_health import compute_runtime_health, format_runtime_health_markdown
from apps.cli.runtime.session_metrics import (
    OPERATIONAL_METRICS_DEFAULTS,
    format_operational_metrics_markdown,
    on_phase_transition_recorded,
    refresh_operational_metrics_snapshot,
)
from apps.cli.runtime.closure_artifact_summary import (
    build_closure_operator_markdown_section,
    build_closure_operator_view,
)
from apps.cli.runtime.micro_task_mode import TASK_MODE_STANDARD
from apps.cli.runtime.task_contract import (
    RUNTIME_CONTRACT_SOURCE_SPEC_ENGINE_LEGACY,
    task_contract_to_jsonable,
)


def deduplicated_diff_summary(diff_summary: List[Dict[str, Any]]) -> Tuple[List[str], Dict[str, List[str]], int]:
    """
    Deduplicate diff by unique file. Returns (unique_files, operations_per_file, total_operations).
    """
    if not diff_summary:
        return [], {}, 0
    by_file: Dict[str, List[str]] = {}
    for item in diff_summary:
        f = item.get("file", "?")
        op = item.get("type", "unknown")
        if f not in by_file:
            by_file[f] = []
        by_file[f].append(op)
    unique = list(dict.fromkeys(by_file.keys()))
    total = sum(len(ops) for ops in by_file.values())
    return unique, by_file, total


class ArtifactSession:
    """
    GEP-6: Artifact Contract Session.
    Structured container for task evidence.

    Operational TaskSpec body and planner output are authoritative on ``task_contract``
    (``spec`` and ``runtime_annotations['decision_plan']``). The ``decision_plan``
    attribute is a deprecated read-only alias; there is no ``taskspec`` instance field
    (artifact JSON still uses the ``"taskspec"`` key as an export view of ``spec``).
    """
    def __init__(self, session_id: str, task: str, task_id: str = "N/A", task_type: str = "direct_edit", type_source: str = "inferred", batch_id: Optional[str] = None, step_id: Optional[int] = None):
        self.session_id = session_id
        self.task_id = task_id
        self.timestamp = datetime.now().isoformat()
        self.task = task
        self.task_type = task_type
        self.type_source = type_source
        self.batch_id = batch_id
        self.step_id = step_id
        self.plan: str = ""
        self.root_cause: str = ""
        self.diff_summary: List[Dict[str, Any]] = []
        # Terminal phase locked by the loop (finalize must not override DONE/ABORTED)
        self.verification: Dict[str, Any] = {}
        # Fingerprint of files in diff_summary when verification last executed (reuse on finalize)
        self.verification_diff_fingerprint: str = ""
        self.verification_justification: str = ""
        self.rollback_status: str = "none" # none, available, executed
        self.next_action: str = ""
        self.events: List[Dict[str, Any]] = [] # Runtime audit trail
        self.phase: str = ""  # SessionPhase.value; synced in real time during the loop
        self.phase_history: List[Dict[str, Any]] = []  # {phase, timestamp, detail?, from?, iteration?}
        # Machine-readable phase path (forensic); canonical for reconstructing runtime without logs
        self.phase_transitions: List[Dict[str, Any]] = []
        # Structured promotion / branch decisions (EXPLORE→ACT, VERIFY→REPAIR, etc.)
        self.promotion_decisions: List[Dict[str, Any]] = []
        # Each evaluation of verification reuse (finalize or coordinator), auditable
        self.verification_reuse_decisions: List[Dict[str, Any]] = []
        # Authoritative terminal resolution snapshot (filled once at finalize)
        self.terminal_resolution_record: Dict[str, Any] = {}
        # Compact repair audit (detail lives in repair_outcome_log / specialist fields)
        self.repair_forensic_summary: Dict[str, Any] = {}
        self.task_outcome: str = ""  # implemented | already_implemented | partially_implemented | blocked | read_only | verification_failed | no_op
        self.outcome_confidence: float = 0.0
        self.evidence_score: int = 0
        self.evidence_lines: List[str] = []
        self.task_intent: str = ""  # implementation | modification | bugfix | review | analysis | refactor | verification
        self.task_scope: str = ""  # ui | api | data | infra | fullstack | unknown
        self.change_expectation: str = ""  # must_write | may_write | should_not_write
        self.intent_confidence: float = 0.0
        self.intent_reasoning_lines: List[str] = []
        # Task confidence / closure narrative (finalize via terminal resolution).
        self.task_confidence_level: str = ""
        self.task_completion_confidence: float = 0.0
        self.closure_reason: str = ""
        self.closure_reason_summary_es: str = ""
        self.closure_posture_es: str = ""
        self.task_confidence_signals: Dict[str, Any] = {}
        # Trivial bounded read-only task (RO-2/RO-3 style); set during INTAKE. None = not a micro-task.
        self.micro_task_kind: Optional[str] = None
        # First-class runtime mode: standard | micro_task (see micro_task_mode.resolve_task_mode).
        self.task_mode: str = TASK_MODE_STANDARD
        # Dynamic iteration budget (effective_max <= global_cap); filled after INTAKE.
        self.iteration_budget: Dict[str, Any] = {}
        self.runtime_phase_breakdown: List[Dict[str, Any]] = []
        self.intake_timing_ms: Dict[str, Any] = {}
        self.intake_planning_wall_ms: float = 0.0
        self.repair_attempt_count: int = 0
        self.repair_summary: str = ""
        self.budget_exhausted_reason: str = ""
        self.last_failed_check: str = ""
        self.last_repair_applied: str = ""
        self.repair_specialist_prompt_chars: int = 0
        self.repair_specialist_usage_tokens: int = 0
        self.repair_specialist_last_raw: str = ""
        self.repair_specialist_outcome: str = ""  # applied_patch | no_effective_patch | ...
        self.repair_specialist_outcome_detail: Dict[str, Any] = {}
        self.repair_outcome_log: List[Dict[str, Any]] = []
        self.tool_output_archive: List[Dict[str, Any]] = []
        self.reverification_pending: bool = False
        # Unified TaskContract (INTAKE); operational spec = task_contract["spec"] only
        self.task_contract: Optional[Dict[str, Any]] = None
        # Spec-engine validation metadata (canonical names). JSON still emits taskspec_* aliases.
        self.contract_spec_validation_status: str = ""
        self.contract_spec_repaired: bool = False
        self.contract_spec_validation_errors: List[str] = []
        self.repo_profile: Dict[str, Any] = {}
        self.runtime_contract_source: str = "legacy"
        # RepoProfile v2 → exploration ranking + audit (sessions with operational spec)
        self.exploration_plan: Dict[str, Any] = {}
        self.repo_profile_influence: List[str] = []
        # Exploration Engine v2 — repo mismatch diagnostics
        self.repo_mismatch_detected: bool = False
        self.repo_mismatch_reason: str = ""
        self.repo_mismatch_reasons: List[str] = []
        self.requested_paths_missing: List[str] = []
        self.repo_mismatch_operator_message: str = ""
        # Exploration policy hint from INTAKE (EXPLORE_CLASS_* from explore_engine_v2.classify_explore_task).
        self.explore_class_hint: str = ""
        # Decision Planner v1 (advisory; GHOST_USE_DECISION_PLANNER=1).
        # Planner payload lives only on task_contract (runtime_annotations['decision_plan']).
        # See ``decision_plan`` property (deprecated read-only alias for legacy access).
        self.planner_version: str = ""
        self.planner_advisory_mode: bool = True
        self.planner_risk_level: str = ""
        self.planner_estimated_complexity: str = ""
        self.planner_provenance: Dict[str, Any] = {}
        self.planner_used: bool = False
        # Repo retrieval (Sprint 1; GHOST_USE_RETRIEVAL=1)
        self.retrieval_enabled: bool = False
        self.retrieval_query: Dict[str, Any] = {}
        self.retrieval_result: Dict[str, Any] = {}
        self.retrieval_top_files: List[Dict[str, Any]] = []
        self.retrieval_top_symbols: List[Dict[str, Any]] = []
        self.retrieval_index_stats: Dict[str, Any] = {}
        self.retrieval_cache_info: Dict[str, Any] = {}
        self.retrieval_used_embeddings: bool = False
        self.retrieval_provenance: Dict[str, Any] = {}
        # Repo retrieval Sprint 2 (confidence + merged read order)
        self.retrieval_actionable: bool = False
        self.retrieval_confidence: float = 0.0
        self.retrieval_mode: str = ""
        self.retrieval_runtime_influenced: bool = False
        self.likely_edit_targets: List[str] = []
        self.likely_edit_target_reasons: List[str] = []
        self.merged_candidate_order: List[Dict[str, Any]] = []
        self.retrieval_highlight_chunks: List[Dict[str, Any]] = []
        # Execution Agent v1 (GHOST_USE_EXECUTION_AGENT=1)
        self.execution_agent_used: bool = False
        self.execution_mode: str = ""
        self.selected_execution_model: str = ""
        self.selected_targets: List[str] = []
        self.execution_context_summary: Dict[str, Any] = {}
        self.proposed_edits: List[Dict[str, Any]] = []
        self.execution_confidence: float = 0.0
        self.execution_reasoning_lines: List[str] = []
        self.execution_agent_provenance: Dict[str, Any] = {}
        self.execution_agent_prompt: str = ""
        self.execution_agent_writes_enabled: bool = False
        self.planner_model: str = ""
        self.execution_model_resolved: str = ""
        self.repair_model_resolved: str = ""
        self.general_fallback_model: str = ""
        self.execution_fallback_model: str = ""
        self.provider_backend: str = ""
        self.live_generation_used: bool = False
        self.provider_ok: bool = True
        self.provider_error_message: str = ""
        self.provider_latency_ms: float = 0.0
        self.provider_model: str = ""
        self.proposal_parsed: bool = False
        self.execution_raw_proposal: str = ""
        self.provider_response_snapshot: Dict[str, Any] = {}
        self.benchmark_results: List[Dict[str, Any]] = []
        # CLI session trace (structured telemetry JSON under .ghost/traces/)
        self.trace_path: str = ""
        self.trace_summary_short: str = ""
        self.trace_detail_md: str = ""
        # Batch executor + approval compression (GHOST_USE_BATCH_EXECUTOR / GHOST_USE_APPROVAL_COMPRESSION)
        self.batch_executor_used: bool = False
        self.batch_plans: List[Dict[str, Any]] = []
        self.batch_results: List[Dict[str, Any]] = []
        self.approval_compression_used: bool = False
        self.compressed_approval_count: int = 0
        self.batched_read_count: int = 0
        self.batched_verification_count: int = 0
        # CLI runtime / config (startup; not task logic)
        self.config_source: str = ""
        self.autoload_env_used: bool = False
        self.env_file_loaded: str = ""
        self.active_feature_flags: Dict[str, Any] = {}
        self.startup_warnings: List[str] = []
        self.effective_repo_root: str = ""
        self.cli_command_mode: str = ""
        # Gateway LLM (CLI → servidor Ghost / OpenAI-compatible); preflight antes del loop principal
        self.llm_gateway_url: str = ""
        self.llm_gateway_preflight_ok: Optional[bool] = None
        self.llm_gateway_preflight_path: str = ""
        # Operational / analytics (stable keys in JSON — see session_metrics.OPERATIONAL_METRICS_DEFAULTS)
        self.operational_metrics: Dict[str, Any] = {
            **OPERATIONAL_METRICS_DEFAULTS,
            "model_turns_by_phase": {},
        }

    def contract_spec(self) -> Optional[Dict[str, Any]]:
        """Operational spec body from ``task_contract['spec']`` (structured dict from spec engine or legacy fill)."""
        tc = self.task_contract if isinstance(self.task_contract, dict) else None
        if not tc:
            return None
        sp = tc.get("spec")
        return sp if isinstance(sp, Mapping) else None

    def contract_decision_plan(self) -> Dict[str, Any]:
        """Planner output from task_contract (runtime_annotations or legacy)."""
        from apps.cli.runtime.task_contract import get_task_contract_decision_plan

        return get_task_contract_decision_plan(self)

    @property
    def decision_plan(self) -> Dict[str, Any]:
        """Deprecated: read-only view of planner output for legacy callers.

        Canonical storage: ``task_contract['runtime_annotations']['decision_plan']``.
        Use :meth:`contract_decision_plan` or ``get_task_contract_decision_plan(session)``.
        Do not assign to this attribute (no setter).
        """
        return self.contract_decision_plan()

    def record_phase_transition(
        self,
        phase: str,
        detail: str = "",
        *,
        from_phase: Optional[str] = None,
        iteration: Optional[int] = None,
    ) -> None:
        """Append to phase history and phase_transitions when the session phase advances."""
        if phase == self.phase and not detail:
            return
        prev = from_phase if from_phase is not None else self.phase
        ts = datetime.now().isoformat()
        self.phase_transitions.append(
            {
                "from": prev or "",
                "to": phase,
                "reason": detail or "",
                "ts": ts,
                "iteration": iteration,
            }
        )
        self.phase = phase
        self.phase_history.append(
            {
                "phase": phase,
                "timestamp": ts,
                "detail": detail or "",
                "from": prev or "",
                "iteration": iteration,
            }
        )
        try:
            on_phase_transition_recorded(self, phase)
        except Exception:
            pass

    def append_promotion_decision(
        self,
        *,
        kind: str,
        reason_code: str,
        from_phase: str,
        to_phase: str,
        iteration: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a semantic promotion (machine-readable); complements phase_transitions."""
        row: Dict[str, Any] = {
            "kind": kind,
            "reason_code": reason_code,
            "from": from_phase,
            "to": to_phase,
            "ts": datetime.now().isoformat(),
            "iteration": iteration,
        }
        if extra:
            row["extra"] = dict(extra)
        self.promotion_decisions.append(row)

    def refresh_repair_forensic_summary(self) -> None:
        """Refresh compact repair audit from current specialist + log state."""
        if (
            not (self.repair_outcome_log or [])
            and not int(self.repair_attempt_count or 0)
            and not (self.repair_specialist_outcome or "").strip()
            and not (self.repair_summary or "").strip()
        ):
            self.repair_forensic_summary = {}
            return
        last = self.repair_outcome_log[-1] if self.repair_outcome_log else {}
        self.repair_forensic_summary = {
            "repair_attempt_count": int(self.repair_attempt_count or 0),
            "repair_summary_excerpt": (self.repair_summary or "")[:500],
            "last_failed_check": self.last_failed_check or "",
            "last_specialist_outcome": self.repair_specialist_outcome or "",
            "outcome_log_len": len(self.repair_outcome_log or []),
            "last_log_outcome": last.get("outcome") if isinstance(last, dict) else None,
        }

    def to_dict(self) -> Dict[str, Any]:
        self.refresh_repair_forensic_summary()
        om = refresh_operational_metrics_snapshot(self)
        rh = compute_runtime_health(self, operational_metrics=om)
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "task": self.task,
            "task_type": self.task_type,
            "type_source": self.type_source,
            "batch_id": self.batch_id,
            "step_id": self.step_id,
            "plan": self.plan,
            "root_cause": self.root_cause,
            "diff_summary": self.diff_summary,
            "verification": self.verification,
            "verification_diff_fingerprint": self.verification_diff_fingerprint,
            "verification_justification": self.verification_justification,
            "rollback_status": self.rollback_status,
            "next_action": self.next_action,
            "events": self.events,
            "phase": self.phase,
            "phase_history": list(self.phase_history),
            "phase_transitions": list(self.phase_transitions),
            "promotion_decisions": list(self.promotion_decisions),
            "verification_reuse_decisions": list(self.verification_reuse_decisions),
            "terminal_resolution_record": dict(self.terminal_resolution_record),
            "repair_forensic_summary": dict(self.repair_forensic_summary),
            "task_outcome": self.task_outcome,
            "outcome_confidence": self.outcome_confidence,
            "evidence_score": self.evidence_score,
            "evidence_lines": self.evidence_lines,
            "task_intent": self.task_intent,
            "task_scope": self.task_scope,
            "change_expectation": self.change_expectation,
            "intent_confidence": self.intent_confidence,
            "intent_reasoning_lines": self.intent_reasoning_lines,
            "task_confidence_level": self.task_confidence_level,
            "task_completion_confidence": self.task_completion_confidence,
            "closure_reason": self.closure_reason,
            "closure_reason_summary_es": self.closure_reason_summary_es,
            "closure_posture_es": self.closure_posture_es,
            "task_confidence_signals": dict(self.task_confidence_signals or {}),
            "closure_operator_view": build_closure_operator_view(self),
            "micro_task_kind": self.micro_task_kind,
            "task_mode": str(getattr(self, "task_mode", TASK_MODE_STANDARD) or TASK_MODE_STANDARD),
            "repo_mismatch_detected": bool(getattr(self, "repo_mismatch_detected", False)),
            "repo_mismatch_reason": str(getattr(self, "repo_mismatch_reason", "") or ""),
            "repo_mismatch_reasons": list(getattr(self, "repo_mismatch_reasons", None) or []),
            "requested_paths_missing": list(getattr(self, "requested_paths_missing", None) or []),
            "repo_mismatch_operator_message": str(getattr(self, "repo_mismatch_operator_message", "") or ""),
            "explore_class_hint": str(getattr(self, "explore_class_hint", "") or ""),
            "iteration_budget": dict(self.iteration_budget or {}),
            "runtime_phase_breakdown": list(self.runtime_phase_breakdown or []),
            "intake_timing_ms": dict(self.intake_timing_ms or {}),
            "intake_planning_wall_ms": float(self.intake_planning_wall_ms or 0.0),
            "repair_attempt_count": self.repair_attempt_count,
            "repair_summary": self.repair_summary,
            "budget_exhausted_reason": self.budget_exhausted_reason,
            "last_failed_check": self.last_failed_check,
            "last_repair_applied": self.last_repair_applied,
            "repair_specialist_prompt_chars": self.repair_specialist_prompt_chars,
            "repair_specialist_usage_tokens": self.repair_specialist_usage_tokens,
            "repair_specialist_last_raw": (
                self.repair_specialist_last_raw[:6000]
                if len(self.repair_specialist_last_raw) > 6000
                else self.repair_specialist_last_raw
            ),
            "repair_specialist_outcome": self.repair_specialist_outcome,
            "repair_specialist_outcome_detail": dict(self.repair_specialist_outcome_detail),
            "repair_outcome_log": list(self.repair_outcome_log),
            "tool_output_archive_meta": [
                {
                    "ref": x.get("ref"),
                    "tool": x.get("tool"),
                    "approx_chars": x.get("approx_chars"),
                }
                for x in (self.tool_output_archive or [])[-40:]
                if isinstance(x, dict)
            ],
            "reverification_pending": self.reverification_pending,
            "task_contract": (
                task_contract_to_jsonable(self.task_contract)
                if isinstance(self.task_contract, dict)
                else None
            ),
            # Canonical export of task_contract["spec"] (machine-readable).
            "contract_spec": (
                task_contract_to_jsonable(self.contract_spec())
                if self.contract_spec() is not None
                else None
            ),
            # Deprecated JSON alias for contract_spec (same payload).
            "taskspec": (
                task_contract_to_jsonable(self.contract_spec())
                if self.contract_spec() is not None
                else None
            ),
            "contract_spec_validation_status": self.contract_spec_validation_status,
            "contract_spec_repaired": self.contract_spec_repaired,
            "contract_spec_validation_errors": list(self.contract_spec_validation_errors),
            # Deprecated JSON keys (same values as contract_spec_*).
            "taskspec_validation_status": self.contract_spec_validation_status,
            "taskspec_repaired": self.contract_spec_repaired,
            "taskspec_validation_errors": list(self.contract_spec_validation_errors),
            "repo_profile": self.repo_profile,
            "runtime_contract_source": self.runtime_contract_source,
            "exploration_plan": self.exploration_plan,
            "repo_profile_influence": list(self.repo_profile_influence),
            "decision_plan": self.contract_decision_plan(),  # same source as ``decision_plan`` property
            "planner_version": self.planner_version,
            "planner_advisory_mode": self.planner_advisory_mode,
            "planner_risk_level": self.planner_risk_level,
            "planner_estimated_complexity": self.planner_estimated_complexity,
            "planner_provenance": self.planner_provenance,
            "planner_used": self.planner_used,
            "retrieval_enabled": self.retrieval_enabled,
            "retrieval_query": self.retrieval_query,
            "retrieval_result": self.retrieval_result,
            "retrieval_top_files": list(self.retrieval_top_files),
            "retrieval_top_symbols": list(self.retrieval_top_symbols),
            "retrieval_index_stats": self.retrieval_index_stats,
            "retrieval_cache_info": self.retrieval_cache_info,
            "retrieval_used_embeddings": self.retrieval_used_embeddings,
            "retrieval_provenance": self.retrieval_provenance,
            "retrieval_actionable": self.retrieval_actionable,
            "retrieval_confidence": self.retrieval_confidence,
            "retrieval_mode": self.retrieval_mode,
            "retrieval_runtime_influenced": self.retrieval_runtime_influenced,
            "likely_edit_targets": list(self.likely_edit_targets),
            "likely_edit_target_reasons": list(self.likely_edit_target_reasons),
            "merged_candidate_order": list(self.merged_candidate_order),
            "retrieval_highlight_chunks": list(self.retrieval_highlight_chunks),
            "execution_agent_used": self.execution_agent_used,
            "execution_mode": self.execution_mode,
            "selected_execution_model": self.selected_execution_model,
            "selected_targets": list(self.selected_targets),
            "execution_context_summary": dict(self.execution_context_summary),
            "proposed_edits": list(self.proposed_edits),
            "execution_confidence": self.execution_confidence,
            "execution_reasoning_lines": list(self.execution_reasoning_lines),
            "execution_agent_provenance": dict(self.execution_agent_provenance),
            "execution_agent_prompt": self.execution_agent_prompt,
            "execution_agent_writes_enabled": self.execution_agent_writes_enabled,
            "planner_model": self.planner_model,
            "execution_model_resolved": self.execution_model_resolved,
            "repair_model_resolved": self.repair_model_resolved,
            "general_fallback_model": self.general_fallback_model,
            "execution_fallback_model": self.execution_fallback_model,
            "provider_backend": self.provider_backend,
            "live_generation_used": self.live_generation_used,
            "provider_ok": self.provider_ok,
            "provider_error_message": self.provider_error_message,
            "provider_latency_ms": self.provider_latency_ms,
            "provider_model": self.provider_model,
            "proposal_parsed": self.proposal_parsed,
            "execution_raw_proposal": self.execution_raw_proposal[:8000] if len(self.execution_raw_proposal) > 8000 else self.execution_raw_proposal,
            "provider_response_snapshot": dict(self.provider_response_snapshot),
            "benchmark_results": list(self.benchmark_results),
            "trace_path": self.trace_path,
            "trace_summary_short": self.trace_summary_short,
            "trace_detail_md": self.trace_detail_md,
            "batch_executor_used": self.batch_executor_used,
            "batch_plans": list(self.batch_plans),
            "batch_results": list(self.batch_results),
            "approval_compression_used": self.approval_compression_used,
            "compressed_approval_count": self.compressed_approval_count,
            "batched_read_count": self.batched_read_count,
            "batched_verification_count": self.batched_verification_count,
            "config_source": self.config_source,
            "autoload_env_used": self.autoload_env_used,
            "env_file_loaded": self.env_file_loaded,
            "active_feature_flags": dict(self.active_feature_flags),
            "startup_warnings": list(self.startup_warnings),
            "effective_repo_root": self.effective_repo_root,
            "cli_command_mode": self.cli_command_mode,
            "llm_gateway_url": getattr(self, "llm_gateway_url", "") or "",
            "llm_gateway_preflight_ok": getattr(self, "llm_gateway_preflight_ok", None),
            "llm_gateway_preflight_path": getattr(self, "llm_gateway_preflight_path", "") or "",
            "operational_metrics": dict(om),
            "runtime_health": dict(rh),
        }

    def to_markdown(self) -> str:
        self.refresh_repair_forensic_summary()
        om = refresh_operational_metrics_snapshot(self)
        rh = compute_runtime_health(self, operational_metrics=om)
        md = f"# 👻 Ghost Task Artifact: {self.session_id}\n\n"
        source_label = "Manual Override" if self.type_source == "manual" else "Inferred"
        md += f"**Task ID:** {self.task_id} | **Type:** `{self.task_type}` ({source_label})\n"
        _tm = str(getattr(self, "task_mode", TASK_MODE_STANDARD) or TASK_MODE_STANDARD)
        md += f"**Task mode:** `{_tm}`"
        if self.micro_task_kind:
            md += f" (`micro_task_kind`: `{self.micro_task_kind}`)"
        md += "\n"
        md += f"**Timestamp:** {self.timestamp}\n\n"
        md += format_operational_metrics_markdown(om)
        rb = list(getattr(self, "runtime_phase_breakdown", None) or [])
        if rb:
            md += "## ⏱ Runtime phase wall times\n\n"
            md += "| Phase | ms | Model turns (segment) | Iteration at transition |\n"
            md += "| --- | ---: | ---: | --- |\n"
            for r in rb:
                if not isinstance(r, dict):
                    continue
                md += (
                    f"| `{r.get('phase', '')}` | {r.get('duration_ms', 0)} | "
                    f"{r.get('model_turns_segment', 0)} | {r.get('iteration_at_transition', '')} |\n"
                )
            md += "\n"
        itp = float(getattr(self, "intake_planning_wall_ms", 0) or 0)
        itm = getattr(self, "intake_timing_ms", None) or {}
        if itp > 0 or (isinstance(itm, dict) and itm):
            md += "## 📥 INTAKE sub-step timing (ms)\n\n"
            md += f"**Planning wall (planner+retrieval, estimated):** {itp}\n\n"
            if isinstance(itm, dict) and itm:
                md += "```json\n"
                md += json.dumps(itm, indent=2, ensure_ascii=False)
                md += "\n```\n\n"
        md += format_runtime_health_markdown(rh)
        if self.trace_path or self.trace_summary_short or self.trace_detail_md:
            md += "## 📡 Session trace\n"
            if self.trace_detail_md:
                md += self.trace_detail_md + "\n\n"
            else:
                if self.trace_path:
                    md += f"**Trace file:** `{self.trace_path}`\n"
                if self.trace_summary_short:
                    md += f"**Summary:** {self.trace_summary_short}\n"
                md += "\n"
        _gw = str(getattr(self, "llm_gateway_url", "") or "")
        if _gw:
            md += "## 🌐 LLM gateway (CLI)\n"
            md += f"**Base URL:** `{_gw}`\n"
            _pok = getattr(self, "llm_gateway_preflight_ok", None)
            _pp = str(getattr(self, "llm_gateway_preflight_path", "") or "")
            if _pok is not None:
                md += f"**Preflight:** {'OK' if _pok else 'FAIL'} (`{_pp}`)\n"
            if self.provider_backend:
                md += f"**Provider (artifacto):** `{self.provider_backend}`\n"
            md += "\n"
        if self.config_source or self.effective_repo_root or self.startup_warnings:
            md += "## 🖥️ CLI runtime\n"
            md += f"**Command mode:** `{self.cli_command_mode}`\n"
            md += f"**Config source:** {self.config_source or '—'}\n"
            md += f"**Autoload file used:** {self.autoload_env_used} (`{self.env_file_loaded or '—'}`)\n"
            md += f"**Effective repo root:** `{self.effective_repo_root or '—'}`\n"
            if self.startup_warnings:
                md += "**Startup warnings:**\n"
                for w in self.startup_warnings[:8]:
                    md += f"- {w}\n"
            if self.active_feature_flags:
                md += "**Feature flags (snapshot):** "
                md += ", ".join(f"{k}={v}" for k, v in sorted(self.active_feature_flags.items())[:20])
                md += "\n"
            md += "\n"
        if self.batch_executor_used or self.batch_plans or self.batch_results:
            md += "## ⚡ BATCH EXECUTION\n"
            md += f"**batch_executor_used:** {self.batch_executor_used} | **plans:** {len(self.batch_plans)} | **results:** {len(self.batch_results)}\n"
            md += f"**batched_read_count:** {self.batched_read_count} | **batched_verification_count:** {self.batched_verification_count}\n\n"
        if self.approval_compression_used or self.compressed_approval_count:
            md += "## 🔐 APPROVAL COMPRESSION\n"
            md += f"**approval_compression_used:** {self.approval_compression_used} | **compressed_approval_count:** {self.compressed_approval_count}\n\n"
        md += f"## 🎯 Task\n{self.task}\n\n"
        _clos_md = build_closure_operator_markdown_section(self)
        if _clos_md:
            md += _clos_md
        if self.repair_attempt_count > 0 or self.repair_summary:
            md += "## 🔧 Repair Attempts\n"
            md += f"Count: {self.repair_attempt_count}\n"
            if self.repair_summary:
                md += f"Summary: {self.repair_summary}\n"
            if self.last_failed_check:
                md += f"**Last failed check:** {self.last_failed_check}\n"
            if self.last_repair_applied:
                md += f"**Last repair applied:** {self.last_repair_applied}\n"
            if self.reverification_pending:
                md += "**Re-verification:** pending (not completed)\n"
            md += "\n"
        if self.budget_exhausted_reason:
            md += "## ⚠️ Budget Exhaustion\n"
            md += f"{self.budget_exhausted_reason}\n\n"
        _cs = self.contract_spec()
        if self.runtime_contract_source == RUNTIME_CONTRACT_SOURCE_SPEC_ENGINE_LEGACY and _cs:
            md += "## 📋 Contract spec (runtime)\n"
            ts = _cs
            md += f"**Intent:** `{ts.get('intent', '')}` | **Scope:** `{', '.join(ts.get('scope') or [])}` | **Forbidden:** `{', '.join(ts.get('forbidden_layers') or []) or 'none'}`\n"
            md += f"**Change expectation:** `{ts.get('change_expectation', '')}` | **Confidence:** {float(ts.get('confidence') or 0):.0%}\n"
            md += f"**Validation:** {self.contract_spec_validation_status} | **Repaired:** {self.contract_spec_repaired}\n"
            if self.contract_spec_validation_errors:
                md += "**Validation notes:**\n"
                for e in self.contract_spec_validation_errors:
                    md += f"- {e}\n"
            md += f"**Contract source:** `{self.runtime_contract_source}`\n\n"
        if self.repo_profile:
            md += "## 📁 Repo profile\n"
            rp = self.repo_profile
            md += f"**Stack:** `{rp.get('stack', '')}` | **Layers:** `{', '.join(rp.get('layers_detected') or [])}` | **Root:** `{rp.get('root', '')}`\n"
            if rp.get("version"):
                md += f"**Profile version:** `{rp.get('version')}` | **Confidence:** `{rp.get('confidence', '')}`\n"
            sd = rp.get("stack_detail") or {}
            if isinstance(sd, dict) and sd:
                md += f"**Framework:** `{', '.join(sd.get('framework') or [])}` | **Languages:** `{', '.join(sd.get('language') or [])}` | **ORM:** `{', '.join(sd.get('orm') or [])}` | **DB:** `{', '.join(sd.get('database') or [])}`\n"
            ep = rp.get("entrypoints") or {}
            if isinstance(ep, dict) and ep.get("api_roots"):
                md += f"**API roots:** {', '.join(ep.get('api_roots') or [])}\n"
            kf = rp.get("key_files") or []
            if kf:
                md += f"**Key files (sample):** {', '.join(kf[:8])}\n"
            vc = rp.get("verification_commands") or {}
            if isinstance(vc, dict) and any(vc.get(k) for k in ("typecheck", "build", "lint", "tests")):
                md += (
                    f"**Verification:** typecheck=`{vc.get('typecheck')}` build=`{vc.get('build')}` "
                    f"lint=`{vc.get('lint')}` tests=`{vc.get('tests')}` source={vc.get('source')}\n"
                )
            if rp.get("important_folders"):
                md += f"**Folders:** {', '.join(rp.get('important_folders') or [])}\n"
            md += "\n"
        _cdp = self.contract_decision_plan()
        if self.planner_used and _cdp:
            md += "## 🧠 Decision Planner (advisory)\n"
            md += f"**Version:** `{self.planner_version}` | **Advisory:** {self.planner_advisory_mode} | **Risk:** `{self.planner_risk_level}` | **Complexity:** `{self.planner_estimated_complexity}`\n\n"
            dp = _cdp
            ex = dp.get("exploration_plan") or {}
            cand = ex.get("ranked_candidates") or []
            if cand:
                md += "### Exploration (top candidates)\n"
                for c in cand[:12]:
                    if isinstance(c, dict):
                        md += f"- `{c.get('path')}` — {c.get('reason', '')} (score={c.get('score')})\n"
                md += "\n"
            ep = dp.get("execution_plan") or {}
            if ep:
                md += "### Execution strategy\n"
                md += f"- Strategy: `{ep.get('execution_strategy')}` | Blast: `{ep.get('blast_radius')}`\n"
                exp = ep.get("expected_files_changed") or []
                if exp:
                    md += "- Expected files:\n"
                    for f in exp[:10]:
                        md += f"  - `{f}`\n"
                md += "\n"
            vp = dp.get("verification_plan") or {}
            if vp:
                md += "### Verification strategy\n"
                md += f"- Required: {vp.get('required')} | Strategy: `{vp.get('strategy')}`\n"
                for s in (vp.get("steps") or [])[:8]:
                    if isinstance(s, dict):
                        md += f"  - `{s.get('check')}` :: `{s.get('command')}` (gate={s.get('gate')})\n"
                md += "\n"
            rp = dp.get("repair_plan") or {}
            if rp:
                md += "### Repair strategy\n"
                md += f"- Max attempts: {rp.get('max_attempts')} | Rerun failed first: {rp.get('rerun_failed_first')}\n"
                md += f"- Stop on: {', '.join(rp.get('stop_on') or [])}\n\n"
        if self.retrieval_enabled:
            md += "## 🔎 Repo retrieval\n"
            rq = self.retrieval_query or {}
            md += (
                f"**Mode:** `{self.retrieval_mode or '—'}` | **Actionable:** {self.retrieval_actionable} | "
                f"**Confidence:** {float(self.retrieval_confidence or 0):.2f} | "
                f"**Runtime influenced:** {self.retrieval_runtime_influenced}\n"
            )
            md += f"**Embeddings:** {self.retrieval_used_embeddings} | **Query:** `{str(rq.get('text', ''))[:120]}`\n"
            st = self.retrieval_index_stats or {}
            md += f"**Index:** {st.get('chunk_count', 0)} chunks, {st.get('file_count', 0)} files\n"
            ci = self.retrieval_cache_info or {}
            md += f"**Cache:** rebuilt={ci.get('rebuilt')} incremental={ci.get('incremental')} updated={ci.get('files_updated', 0)}\n"
            for row in (self.retrieval_top_files or [])[:8]:
                if isinstance(row, dict):
                    md += f"- **TOP HIT** `{row.get('path')}` score={row.get('score')}\n"
            if self.likely_edit_targets:
                md += "**Likely edit targets (candidates):**\n"
                for i, p in enumerate(self.likely_edit_targets[:8]):
                    r = self.likely_edit_target_reasons[i] if i < len(self.likely_edit_target_reasons) else ""
                    md += f"- `{p}` — {r}\n"
            if self.merged_candidate_order:
                md += "**Merged read priority (deterministic):**\n"
                for row in self.merged_candidate_order[:12]:
                    if isinstance(row, dict):
                        md += f"- `{row.get('path')}` ({row.get('source')})\n"
            for row in (self.retrieval_top_symbols or [])[:8]:
                if isinstance(row, dict):
                    md += f"- symbol `{row.get('symbol')}` → `{row.get('file')}`\n"
            md += "\n"
        if self.execution_agent_used:
            md += "## ⚙️ Execution Agent (v1)\n"
            md += (
                f"**Mode:** `{self.execution_mode}` | **Model:** `{self.selected_execution_model}` | "
                f"**Confidence:** {float(self.execution_confidence or 0):.2f} | "
                f"**Writes env:** {self.execution_agent_writes_enabled}\n"
            )
            md += (
                f"**Backend:** `{self.provider_backend}` | **Live NIM:** {self.live_generation_used} | "
                f"**Provider OK:** {self.provider_ok} | **Latency ms:** {float(self.provider_latency_ms or 0):.1f} | "
                f"**Parsed proposals:** {self.proposal_parsed}\n"
            )
            if (
                self.planner_model
                or self.execution_model_resolved
                or self.repair_model_resolved
                or self.general_fallback_model
            ):
                md += (
                    f"**Role models:** planner=`{self.planner_model}` execution=`{self.execution_model_resolved}` "
                    f"repair=`{self.repair_model_resolved}`\n"
                )
                if self.general_fallback_model or self.execution_fallback_model:
                    md += (
                        f"**Fallbacks:** general=`{self.general_fallback_model}` "
                        f"execution_multi=`{self.execution_fallback_model}`\n"
                    )
            if not self.provider_ok and self.provider_error_message:
                md += f"**Provider error:** `{self.provider_error_message[:200]}`\n"
            if self.benchmark_results:
                md += f"**Benchmark runs:** {len(self.benchmark_results)} (see artifact JSON for detail)\n"
            if self.selected_targets:
                md += f"**Targets:** {', '.join(self.selected_targets[:12])}\n"
            if self.proposed_edits:
                md += "**Proposed edits (advisory):**\n"
                for pe in self.proposed_edits[:10]:
                    if isinstance(pe, dict):
                        md += f"- `{pe.get('file_path')}` — {pe.get('summary', '')}\n"
            if self.execution_reasoning_lines:
                md += "**Reasoning:**\n"
                for line in self.execution_reasoning_lines[:8]:
                    md += f"- {line}\n"
            md += "\n"
        if self.repo_profile_influence or (self.exploration_plan and any(self.exploration_plan.values())):
            md += "## 🧭 RepoProfile influence (runtime)\n"
            ep = self.exploration_plan or {}
            if ep.get("ranked_files"):
                md += f"**Exploration priority files (sample):** {', '.join(ep.get('ranked_files')[:12])}\n"
            if ep.get("ui_exploration_blocked"):
                md += "**UI exploration:** blocked by TaskSpec + RepoProfile ui_roots.\n"
            if self.repo_profile_influence:
                md += "**Evidence:**\n"
                for line in self.repo_profile_influence[:20]:
                    md += f"- {line}\n"
            md += "\n"
        if self.task_intent:
            md += "## 🎯 Task Intent\n"
            md += f"**Intent:** `{self.task_intent}` | **Scope:** `{self.task_scope}` | **Change expectation:** `{self.change_expectation}` | **Confidence:** {self.intent_confidence:.0%}\n\n"
            if self.intent_reasoning_lines:
                md += "**Reasoning:**\n"
                for line in self.intent_reasoning_lines:
                    md += f"- {line}\n"
                md += "\n"
        if self.verification_justification:
            md += f"**Verification Strategy:** {self.verification_justification}\n\n"
        md += f"## 📝 Plan\n{self.plan or 'N/A'}\n\n"
        md += f"## 🔍 Root Cause\n{self.root_cause or 'N/A'}\n\n"
        
        md += "## 🛠️ Diff Summary\n"
        unique_files, ops_per_file, total_ops = deduplicated_diff_summary(self.diff_summary)
        if not unique_files:
            md += "No files changed.\n"
        else:
            md += f"**Files changed:** {len(unique_files)}\n"
            if total_ops > len(unique_files):
                md += f"**Operations on changed files:** {total_ops}\n"
            for f in unique_files:
                ops = ops_per_file.get(f, [])
                op_labels = [o.replace(" File", "").lower() for o in ops]
                md += f"- **{f}** ({', '.join(op_labels)})\n"
        md += "\n"
        
        if self.verification:
            md += "## 🛡️ Verification\n"
            v = self.verification if isinstance(self.verification, dict) else {}
            md += f"**Status:** {v.get('status', 'N/A').upper()}\n"
            if self.verification_diff_fingerprint:
                md += f"**Diff fingerprint (reuse key):** `{self.verification_diff_fingerprint}`\n"
            steps = v.get("steps_executed_count")
            if steps is not None:
                md += f"**Steps executed:** {steps}\n"
            for check in v.get("checks", []):
                prov = check.get("provenance", "")
                line = f"- {check.get('name')}: {check.get('status')}"
                if prov:
                    line += f" (provenance: {prov})"
                md += line + "\n"
            notes = v.get("manual_verification_notes", "").strip()
            if notes:
                md += f"**Manual notes:** {notes}\n"
            src = v.get("verification_source", "none")
            md += f"**verification_source:** {src}\n"
            md += "\n"

        if self.events:
            md += "## 📂 Runtime Audit Trail\n"
            # Structured table-like header for better readability
            md += "| Timestamp | Event | Details |\n"
            md += "| :--- | :--- | :--- |\n"
            for event in self.events:
                ts = datetime.fromtimestamp(event.get('timestamp', 0)).strftime('%H:%M:%S')
                name = event.get('event', 'Unknown')
                details = event.get('details', '')
                # Ensure details is a string and clean it up if it's multiline
                if isinstance(details, dict):
                    details = json.dumps(details)
                details = str(details).replace("\n", " ").strip()
                md += f"| `{ts}` | **{name}** | {details} |\n"
            md += "\n"
            
        md += f"## 🔄 Rollback\nStatus: {self.rollback_status}\n\n"
        if self.phase or self.phase_history:
            md += "## ⚙️ Session phase\n"
            if self.phase:
                md += f"**Current:** `{self.phase}`\n\n"
            if self.phase_history:
                md += "| Timestamp | From | Phase | Iter | Detail |\n| :--- | :--- | :--- | :--- | :--- |\n"
                for h in self.phase_history:
                    ts = str(h.get("timestamp", ""))[:19].replace("T", " ")
                    prev = str(h.get("from", "") or "—")
                    ph = h.get("phase", "")
                    it = h.get("iteration", "")
                    it_s = str(it) if it is not None and it != "" else "—"
                    det = str(h.get("detail", "")).replace("\n", " ").replace("|", "/")[:120]
                    md += f"| `{ts}` | `{prev}` | **{ph}** | {it_s} | {det} |\n"
                md += "\n"
        forensic_bundle = {
            "phase_transitions": list(self.phase_transitions),
            "promotion_decisions": list(self.promotion_decisions),
            "verification_reuse_decisions": list(self.verification_reuse_decisions),
            "verification_diff_fingerprint": self.verification_diff_fingerprint or None,
            "terminal_resolution_record": dict(self.terminal_resolution_record),
            "repair_forensic_summary": dict(self.repair_forensic_summary),
        }
        _show_forensic = bool(
            self.phase_transitions
            or self.promotion_decisions
            or self.verification_reuse_decisions
            or self.terminal_resolution_record
            or self.repair_forensic_summary
            or (self.verification_diff_fingerprint or "").strip()
        )
        if _show_forensic:
            md += "## 🔬 Forensic runtime (JSON)\n"
            md += "Canonical machine-readable path for audits (full detail in artifact `.json`).\n\n"
            md += "```json\n"
            md += json.dumps(forensic_bundle, indent=2, ensure_ascii=False, default=str)
            md += "\n```\n\n"
        if self.task_outcome:
            md += "## 📊 Task Outcome (detalle)\n"
            md += (
                "Resumen operador arriba en **«Por qué paró Ghost»**. Aquí: outcome engine y líneas de evidencia.\n\n"
            )
            md += f"**Outcome:** `{self.task_outcome}` | **Outcome confidence:** {self.outcome_confidence:.0%} | **Evidence score:** {self.evidence_score}\n\n"
            if self.evidence_lines:
                md += "**Evidence lines:**\n"
                for line in self.evidence_lines:
                    md += f"- {line}\n"
                md += "\n"
        md += f"## 🚀 Next Action\n{self.next_action or 'N/A'}\n"
        
        return md

class ArtifactManager:
    """
    Manages the lifecycle and persistence of ArtifactSessions.
    """
    def __init__(self, project_root: str):
        self.project_root = project_root
        self.artifacts_dir = os.path.join(project_root, ".ghost", "artifacts")
        os.makedirs(self.artifacts_dir, exist_ok=True)
        self.current_session: Optional[ArtifactSession] = None

    def start_session(self, task: str, task_id: str = "N/A", task_type: str = "direct_edit", type_source: str = "inferred", batch_id: Optional[str] = None, step_id: Optional[int] = None) -> ArtifactSession:
        import uuid
        session_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:4]}"
        self.current_session = ArtifactSession(session_id, task, task_id=task_id, task_type=task_type, type_source=type_source, batch_id=batch_id, step_id=step_id)
        return self.current_session

    def persist(self):
        if not self.current_session:
            return

        session_id = self.current_session.session_id
        
        # Save JSON
        json_path = os.path.join(self.artifacts_dir, f"{session_id}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(self.current_session.to_dict(), f, indent=2)
            
        # Save Markdown
        md_path = os.path.join(self.artifacts_dir, f"{session_id}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(self.current_session.to_markdown())

    def add_diff(
        self,
        file_path: str,
        edit_type: str,
        status: str = "success",
        *,
        content_sha256: Optional[str] = None,
    ):
        if self.current_session:
            entry: Dict[str, Any] = {
                "file": file_path,
                "type": edit_type,
                "status": status,
            }
            if content_sha256:
                entry["content_sha256"] = content_sha256
            self.current_session.diff_summary.append(entry)
