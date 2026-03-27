"""
Single authority for final session terminal: phase, outcome, and abort reason.

The main loop may enter CLOSING (work halted); only resolve_terminal_result + apply
produces definitive DONE/ABORTED and outcome fields. After outcome resolution,
``compute_task_confidence_closure`` attaches **task confidence** and **closure_reason_*** for
artifacts and UX (does not replace phase rules; iteration cap / stagnation remain fallbacks).

CodexAssistant must apply results via _apply_terminal_resolution only (see phase_invariants tests).

Extension guide: docs/RUNTIME_EXTENSION_BOUNDARIES.md
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from apps.cli.runtime.outcome_engine import (
    OUTCOME_ALREADY_IMPLEMENTED,
    OUTCOME_BLOCKED,
    OUTCOME_IMPLEMENTED,
    OUTCOME_NO_OP,
    OUTCOME_PARTIALLY_IMPLEMENTED,
    OUTCOME_READ_ONLY,
    OUTCOME_VERIFICATION_FAILED,
    _has_final_nl_response,
    determine_task_outcome,
)
from apps.cli.runtime.session_phase import (
    LOOP_ABORT_MAX_ITERATIONS,
    LOOP_ABORT_POLICY,
    LOOP_ABORT_PROVIDER,
    LOOP_ABORT_STAGNATION,
    SessionPhase,
)
from apps.cli.runtime.task_confidence_engine import compute_task_confidence_closure

PolicyTerminalStatus = Literal["ok", "terminal_stop", "policy_block", "stagnation", "max_iterations"]
ProviderTerminalStatus = Literal["ok", "fatal_error"]
BudgetTerminalStatus = Literal["ok", "exhausted"]


def iteration_limit_soft_done_min_evidence() -> int:
    try:
        n = int(os.getenv("GHOST_ITERATION_LIMIT_SOFT_DONE_MIN_EVIDENCE", "5"))
    except ValueError:
        n = 5
    return max(0, min(n, 100))


def iteration_limit_operator_assessment(
    *,
    terminal_phase_value: str,
    outcome: str,
    evidence_score: int,
    has_final_nl: bool,
    session_failed: bool,
    loop_stop_raw: str,
) -> Dict[str, str]:
    """
    Operator-facing classification when the loop stopped on max_iterations.

    ``loop_stop_raw`` is the preserved stop signal (e.g. ``max_iterations``). Phase/outcome
    still reflect resolve_terminal_result; this dict is UX-only.
    """
    if loop_stop_raw != LOOP_ABORT_MAX_ITERATIONS:
        return {}
    if session_failed:
        return {
            "kind": "iteration_cap_policy_or_session_failed",
            "summary_es": "Parada con límite de iteraciones además de fallo de sesión o política; revisa errores anteriores.",
        }
    if terminal_phase_value == SessionPhase.DONE.value and outcome in (
        OUTCOME_READ_ONLY,
        OUTCOME_ALREADY_IMPLEMENTED,
    ):
        if has_final_nl:
            return {
                "kind": "iteration_cap_soft_done_answered",
                "summary_es": "Se alcanzó el límite de iteraciones, pero la tarea se cerró como completada: había respuesta final o evidencia suficiente (solo lectura / ya implementado). No es un fallo duro.",
            }
        return {
            "kind": "iteration_cap_soft_done_evidence_only",
            "summary_es": "Se alcanzó el límite de iteraciones y la sesión se marcó completada por evidencia de solo lectura; conviene revisar el texto del asistente y el historial de herramientas.",
        }
    if outcome == OUTCOME_NO_OP:
        return {
            "kind": "iteration_cap_no_progress",
            "summary_es": "Límite de iteraciones sin herramientas útiles ni respuesta final: poco progreso. Reformula el objetivo o sube GHOST_MAX_ITERATIONS.",
        }
    if outcome in (OUTCOME_PARTIALLY_IMPLEMENTED, OUTCOME_BLOCKED):
        return {
            "kind": "iteration_cap_incomplete_or_blocked",
            "summary_es": "Límite de iteraciones con resultado incompleto o bloqueado; la evidencia no alcanzó el umbral de cierre automático.",
        }
    if outcome in (OUTCOME_READ_ONLY, OUTCOME_ALREADY_IMPLEMENTED) and not has_final_nl:
        return {
            "kind": "iteration_cap_explored_useful_evidence",
            "summary_es": "Límite de iteraciones con evidencia en el historial pero sin respuesta final clara; si está activo, Ghost intenta una síntesis de cierre sin herramientas. Revisa también ls/read_file arriba.",
        }
    return {
        "kind": "iteration_cap_hard_stop",
        "summary_es": "Límite de iteraciones en una tarea que no encaja en cierre suave (p. ej. implementación); tratar como parada dura o aumentar el presupuesto de iteraciones.",
    }


@dataclass(frozen=True)
class TerminalResolution:
    """Authoritative end-of-session result (apply once at finalize)."""

    phase: SessionPhase
    outcome: str
    outcome_confidence: float
    evidence_score: int
    evidence_lines: List[str]
    root_cause: str
    next_action: str
    loop_abort_reason: Optional[str]
    task_confidence_level: str = "medium"
    task_completion_confidence: float = 0.0
    closure_reason_code: str = "unspecified"
    closure_reason_summary_es: str = ""
    closure_posture_es: str = ""
    task_confidence_signals: Dict[str, Any] = field(default_factory=dict)
    findings_evidence_tier: Optional[str] = None


def terminal_phase_from_signals(
    *,
    budget_exhausted: bool,
    session_failed: bool,
    outcome: str,
    loop_abort_reason: Optional[str],
    provider_ok: bool = True,
    policy_status: PolicyTerminalStatus = "ok",
    evidence_score: int = 0,
    has_final_nl_response: bool = False,
) -> SessionPhase:
    """
    Map signals to DONE/ABORTED. Used by tests and compute_terminal_session_phase wrapper.

    ``max_iterations`` alone does not force ABORTED for read-only / already-implemented outcomes
    when there is a final NL answer or evidence score reaches the soft threshold (product UX).
    """
    if policy_status != "ok":
        return SessionPhase.ABORTED
    if not provider_ok:
        return SessionPhase.ABORTED
    if loop_abort_reason == LOOP_ABORT_MAX_ITERATIONS:
        if outcome in (OUTCOME_READ_ONLY, OUTCOME_ALREADY_IMPLEMENTED):
            if has_final_nl_response or evidence_score >= iteration_limit_soft_done_min_evidence():
                pass  # defer to outcome-based rules below
            else:
                return SessionPhase.ABORTED
        else:
            return SessionPhase.ABORTED
    if loop_abort_reason in (
        LOOP_ABORT_STAGNATION,
        LOOP_ABORT_POLICY,
        LOOP_ABORT_PROVIDER,
    ):
        return SessionPhase.ABORTED
    if budget_exhausted:
        return SessionPhase.ABORTED
    if session_failed:
        return SessionPhase.ABORTED
    if outcome in (
        OUTCOME_VERIFICATION_FAILED,
        OUTCOME_NO_OP,
        OUTCOME_BLOCKED,
        OUTCOME_PARTIALLY_IMPLEMENTED,
    ):
        return SessionPhase.ABORTED
    if outcome in (OUTCOME_IMPLEMENTED, OUTCOME_ALREADY_IMPLEMENTED, OUTCOME_READ_ONLY):
        return SessionPhase.DONE
    return SessionPhase.ABORTED


def _normalize_loop_abort(
    policy_status: PolicyTerminalStatus,
    loop_abort_reason: Optional[str],
) -> Optional[str]:
    if policy_status == "terminal_stop" or policy_status == "policy_block":
        return loop_abort_reason or LOOP_ABORT_POLICY
    if policy_status == "stagnation":
        return loop_abort_reason or LOOP_ABORT_STAGNATION
    if policy_status == "max_iterations":
        return loop_abort_reason or LOOP_ABORT_MAX_ITERATIONS
    return loop_abort_reason


def resolve_terminal_result(
    session: Any,
    verification_result: Dict[str, Any],
    *,
    history: List[Dict[str, Any]],
    budget_status: BudgetTerminalStatus,
    provider_status: ProviderTerminalStatus,
    policy_status: PolicyTerminalStatus,
    session_failed: bool,
    loop_abort_reason: Optional[str],
) -> TerminalResolution:
    """
    Compute final phase and outcome exactly once from session + verification + status signals.

    Always runs determine_task_outcome (evidence-based outcome), then derives phase from
    budget / provider / policy / session_failed / outcome / loop_abort_reason.
    """
    outcome_result = determine_task_outcome(session, history, verification_result)
    budget_exhausted = budget_status == "exhausted"
    provider_ok = provider_status == "ok"
    lar = _normalize_loop_abort(policy_status, loop_abort_reason)
    has_final_nl = _has_final_nl_response(history)

    phase = terminal_phase_from_signals(
        budget_exhausted=budget_exhausted,
        session_failed=session_failed,
        outcome=outcome_result.outcome,
        loop_abort_reason=lar,
        provider_ok=provider_ok,
        policy_status=policy_status,
        evidence_score=outcome_result.evidence_score,
        has_final_nl_response=has_final_nl,
    )

    effective_abort: Optional[str] = None
    if phase == SessionPhase.ABORTED:
        effective_abort = lar
        if not effective_abort and policy_status != "ok":
            effective_abort = LOOP_ABORT_POLICY
        if not effective_abort and not provider_ok:
            effective_abort = LOOP_ABORT_PROVIDER
        if not effective_abort and budget_exhausted:
            effective_abort = "budget_exhausted"

    tc_snap = compute_task_confidence_closure(
        session=session,
        outcome=outcome_result.outcome,
        outcome_result=outcome_result,
        terminal_phase=phase,
        loop_abort_reason=lar,
        has_final_nl=has_final_nl,
        verification=verification_result,
    )

    return TerminalResolution(
        phase=phase,
        outcome=outcome_result.outcome,
        outcome_confidence=outcome_result.confidence,
        evidence_score=outcome_result.evidence_score,
        evidence_lines=list(outcome_result.evidence_lines),
        root_cause=outcome_result.summary,
        next_action=outcome_result.recommended_next_action,
        loop_abort_reason=effective_abort,
        task_confidence_level=tc_snap.task_confidence_level,
        task_completion_confidence=tc_snap.task_completion_confidence,
        closure_reason_code=tc_snap.closure_reason_code,
        closure_reason_summary_es=tc_snap.closure_reason_summary_es,
        closure_posture_es=tc_snap.recommended_runtime_posture_es,
        task_confidence_signals=dict(tc_snap.signals),
        findings_evidence_tier=getattr(outcome_result, "findings_evidence_tier", None),
    )
