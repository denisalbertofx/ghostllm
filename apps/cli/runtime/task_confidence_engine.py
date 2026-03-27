"""
Unified task confidence and closure narrative (GEP-style).

Interprets outcome, verification, evidence, session signals, and loop abort reasons into:
``task_confidence_level``, ``task_completion_confidence``, and ``closure_reason_*``.

Phase/outcome rules stay in ``terminal_resolution`` / ``outcome_engine``; this module adds a single
operator-facing model for *why* the session closed and how confident Ghost is — without replacing
those authorities.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from apps.cli.runtime.outcome_engine import (
    INFORMATIVE_KIND_EXPLORE_CHURN,
    INFORMATIVE_KIND_MISSING_PATH_TOOLS,
    INFORMATIVE_KIND_MISSING_SYMBOL_HINT,
    OUTCOME_ALREADY_IMPLEMENTED,
    OUTCOME_BLOCKED,
    OUTCOME_IMPLEMENTED,
    OUTCOME_NO_OP,
    OUTCOME_PARTIALLY_IMPLEMENTED,
    OUTCOME_READ_ONLY,
    OUTCOME_VERIFICATION_FAILED,
    TaskOutcomeResult,
    verification_integrity_stale,
)
from apps.cli.runtime.session_phase import (
    LOOP_ABORT_MAX_ITERATIONS,
    LOOP_ABORT_STAGNATION,
    SessionPhase,
)


@dataclass(frozen=True)
class TaskConfidenceSnapshot:
    """Serialized to artifact (``terminal_resolution_record.task_confidence`` + session mirrors)."""

    task_confidence_level: str  # high | medium | low
    task_completion_confidence: float  # 0..1 unified score for product UX
    closure_reason_code: str
    closure_reason_summary_es: str
    recommended_runtime_posture_es: str
    signals: Dict[str, Any] = field(default_factory=dict)


def _collect_signal_flags(
    session: Any,
    outcome_result: Optional[TaskOutcomeResult] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "task_mode": getattr(session, "task_mode", None) or "standard",
        "micro_task_kind": getattr(session, "micro_task_kind", None),
        "iteration_budget_category": (getattr(session, "iteration_budget", None) or {}).get("category")
        if isinstance(getattr(session, "iteration_budget", None), dict)
        else None,
    }
    codes: List[str] = []
    last_churn_reason = ""
    for e in getattr(session, "events", None) or []:
        if isinstance(e, dict) and e.get("event"):
            codes.append(str(e["event"]))
    for e in reversed(getattr(session, "events", None) or []):
        if isinstance(e, dict) and e.get("event") == "explore_v2_churn_abort":
            last_churn_reason = str(e.get("reason") or "")
            break
    out["had_evidence_sufficiency_eval"] = "evidence_sufficiency_eval" in codes
    out["had_evidence_sufficient_immediate_synthesis"] = "evidence_sufficient_immediate_synthesis" in codes
    out["had_explore_answer_now_nudge"] = "explore_answer_now_nudge" in codes
    out["had_micro_task_detected"] = "micro_task_detected" in codes
    out["had_explore_redundant_ls_suppressed"] = "explore_redundant_ls_suppressed" in codes
    out["repo_mismatch_detected"] = bool(getattr(session, "repo_mismatch_detected", False))
    out["had_explore_churn_abort"] = bool(last_churn_reason)
    out["explore_churn_abort_reason"] = last_churn_reason[:500]
    out["had_symbol_search_no_match"] = "explore_symbol_search_no_match" in codes
    if outcome_result is not None:
        out["informative_readonly_kind"] = getattr(outcome_result, "informative_readonly_kind", None)
    return out


def _verification_success(verification: Dict[str, Any], session: Optional[Any] = None) -> bool:
    if not verification:
        return False
    if verification.get("status") != "success":
        return False
    if session is not None and verification_integrity_stale(session):
        return False
    checks = verification.get("checks") or []
    if not checks:
        return bool(verification.get("steps_executed_count", 0))
    return any(
        c.get("provenance") == "executed" and c.get("status") in ("passed", "success") for c in checks
    )


def compute_task_confidence_closure(
    *,
    session: Any,
    outcome: str,
    outcome_result: TaskOutcomeResult,
    terminal_phase: SessionPhase,
    loop_abort_reason: Optional[str],
    has_final_nl: bool,
    verification: Dict[str, Any],
) -> TaskConfidenceSnapshot:
    """
    Derive confidence level and Spanish closure narrative from authoritative outcome + phase.

    Iteration cap and stagnation are labeled as *fallback* drivers when they explain the stop
    while outcome still allows DONE (soft) or ABORTED.
    """
    signals = _collect_signal_flags(session, outcome_result)
    oc = float(outcome_result.confidence or 0.0)
    ev = int(outcome_result.evidence_score or 0)
    made_changes = bool(getattr(session, "diff_summary", None))

    v_ok = _verification_success(verification or {}, session)
    v_status = str((verification or {}).get("status") or "")

    completion = oc
    level = "medium"
    code = "outcome_default"
    summary_es = "Cierre según motor de outcome y fase terminal."
    posture_es = "Revisa el outcome y el historial de herramientas."

    is_fallback_iter = loop_abort_reason == LOOP_ABORT_MAX_ITERATIONS
    is_fallback_stag = loop_abort_reason == LOOP_ABORT_STAGNATION

    if terminal_phase == SessionPhase.DONE:
        if signals.get("repo_mismatch_detected"):
            op = (getattr(session, "repo_mismatch_operator_message", None) or "").strip()
            rr = (getattr(session, "repo_mismatch_reason", None) or "").strip()
            level = "medium"
            completion = round(max(min(completion, 0.82), 0.7), 4)
            code = "repo_mismatch_graceful_closure"
            summary_es = (
                "Cierre **informativo**: el workspace activo no parece ser el repositorio que la tarea asume "
                "(repo mismatch). No se trata de un fallo opaco del modelo."
            )
            if op:
                summary_es += " " + op[:450]
            elif rr:
                summary_es += f" Motivos registrados: {rr[:200]}."
            else:
                summary_es += " Revisa en el artefacto `repo_mismatch_*` y `requested_paths_missing`."
            if is_fallback_iter:
                summary_es += (
                    " El tope de iteraciones es **secundario** frente al mismatch de repositorio."
                )
            posture_es = (
                "Abre la raíz del repositorio correcto y vuelve a ejecutar el mismo prompt."
            )
        elif outcome == OUTCOME_IMPLEMENTED and v_ok:
            level = "high"
            completion = max(completion, 0.93)
            code = "primary_verification_pass"
            summary_es = (
                "Alta confianza: cambios aplicados y verificación estructurada correcta. "
                "El cierre se apoya en evidencia de verify, no en tope de iteraciones."
            )
            posture_es = "Ninguna acción obligatoria; revisión humana opcional."
        elif outcome == OUTCOME_IMPLEMENTED and v_status in ("skipped", "") and made_changes:
            level = "medium"
            completion = min(max(completion, 0.78), 0.88)
            code = "primary_implemented_verify_skipped"
            summary_es = (
                "Confianza media: hubo escritura y el outcome es implementado, pero la verificación "
                "no se ejecutó o se omitió. Conviene validar build/lint localmente."
            )
            posture_es = "Ejecuta comprobaciones locales si el cambio es crítico."
        elif outcome == OUTCOME_ALREADY_IMPLEMENTED:
            level = "high" if oc >= 0.85 else "medium"
            completion = max(completion, 0.82) if level == "high" else min(completion, 0.84)
            code = "primary_already_implemented"
            summary_es = (
                "Confianza "
                + ("alta" if level == "high" else "media")
                + ": el modelo concluyó que la funcionalidad ya estaba cubierta, con evidencia de lectura/exploración."
            )
            posture_es = "Si el producto no coincide, pide un cambio explícito con rutas."
            if is_fallback_iter:
                code = "fallback_max_iterations_with_already_implemented_confidence"
                completion = min(completion, 0.8)
                summary_es = (
                    "Diagnóstico «ya implementado» con confianza media-alta según exploración. "
                    "El tope de iteraciones del bucle es **contexto técnico**, no la causa principal del cierre. "
                    + summary_es
                )
        elif outcome == OUTCOME_READ_ONLY:
            ik = getattr(outcome_result, "informative_readonly_kind", None) or ""
            if ik == INFORMATIVE_KIND_MISSING_PATH_TOOLS:
                level = "medium"
                completion = round(max(min(completion, 0.8), 0.68), 4)
                code = "informative_missing_path_readonly"
                summary_es = (
                    "Cierre **informativo** (no es un fallo duro del runtime): en modo solo lectura, "
                    "las rutas o ficheros pedidos **no están en el workspace actual**. "
                    "Revisa rutas o abre el repo correcto."
                )
                posture_es = "Comprueba que el cwd sea la raíz del proyecto correcto y que las rutas existan."
                if is_fallback_iter:
                    summary_es += (
                        " El tope de iteraciones es **secundario** frente a este cierre informativo."
                    )
            elif ik == INFORMATIVE_KIND_MISSING_SYMBOL_HINT:
                level = "medium"
                completion = round(max(min(completion, 0.79), 0.66), 4)
                code = "informative_missing_symbol_readonly"
                summary_es = (
                    "Cierre **informativo**: la exploración se frenó por repetición de baja señal o porque "
                    "el **símbolo** no aparece en este repo. No confundir con un error opaco del modelo."
                )
                posture_es = "Usa el repo adecuado, `search_code`, o reformula el identificador."
                if is_fallback_iter:
                    summary_es += (
                        " El tope de iteraciones es **secundario** frente a este cierre informativo."
                    )
            elif ik in (INFORMATIVE_KIND_EXPLORE_CHURN, "wrong_repo_churn"):
                level = "medium"
                completion = round(max(min(completion, 0.78), 0.65), 4)
                code = "informative_explore_churn_readonly"
                summary_es = (
                    "Cierre **informativo**: Ghost detuvo EXPLORE por **churn** (listados o herramientas repetidas "
                    "sin evidencia nueva). El tope global de iteraciones no fue el principal cortafuegos."
                )
                if signals.get("explore_churn_abort_reason"):
                    summary_es += f" Detalle: {str(signals.get('explore_churn_abort_reason'))[:200]}."
                posture_es = "Sintetiza con lo ya visto, lee un fichero concreto, o cambia de repositorio."
                if is_fallback_iter:
                    summary_es += (
                        " El tope de iteraciones es **secundario** frente a este cierre informativo."
                    )
            else:
                if has_final_nl and ev >= 8:
                    level = "high"
                    completion = max(completion, 0.86)
                elif has_final_nl:
                    level = "medium"
                    completion = max(completion, 0.72)
                else:
                    level = "medium"
                    completion = max(completion, 0.62)
                code = "primary_read_only_evidence"
                summary_es = (
                    "Cierre de solo lectura: confianza basada en respuesta final "
                    + ("y evidencia fuerte" if ev >= 8 else "y evidencia disponible")
                    + " en el historial."
                )
                if signals.get("had_evidence_sufficient_immediate_synthesis") or signals.get(
                    "had_explore_answer_now_nudge"
                ):
                    summary_es += " Política de evidencia suficiente / answer-now apoyó el cierre temprano."
                posture_es = "Si falta detalle, reabre con un alcance más concreto."
                if is_fallback_iter:
                    code = "fallback_max_iterations_with_readonly_confidence"
                    completion = min(completion, 0.78)
                    summary_es = (
                        "Cierre de solo lectura con confianza en la **respuesta final** y la **evidencia del historial** "
                        "(DONE suave intencional). El tope de iteraciones es **secundario**: no indica fallo por sí solo. "
                        + summary_es
                    )
        else:
            level = "medium"
            code = f"done_outcome_{outcome}"
            summary_es = f"Sesión cerrada en DONE con outcome `{outcome}`."
    elif terminal_phase == SessionPhase.ABORTED:
        level = "low"
        completion = min(completion, 0.55)
        if outcome == OUTCOME_VERIFICATION_FAILED:
            code = "verification_failed_low_confidence"
            summary_es = (
                "Baja confianza en completitud: verificación fallida tras cambios. "
                "Reparación o ajustes recomendados antes de dar por cerrado."
            )
            posture_es = "Corrige según checks fallidos y vuelve a ejecutar verify."
        elif outcome == OUTCOME_NO_OP:
            code = "insufficient_progress_no_op"
            summary_es = (
                "Baja confianza: no hubo herramientas útiles ni respuesta final clara. "
                "Explorar más o reformular la tarea."
            )
            posture_es = "Reformula el objetivo o usa /do con rutas explícitas."
        elif outcome in (OUTCOME_BLOCKED, OUTCOME_PARTIALLY_IMPLEMENTED):
            code = "blocked_or_partial_low_confidence"
            summary_es = "Confianza baja: resultado bloqueado o parcial; no se considera tarea completa."
            posture_es = "Desbloquea permisos/rutas o continúa la implementación."
        elif is_fallback_iter:
            code = "fallback_max_iterations_abort"
            completion = min(completion, 0.42)
            summary_es = (
                "Parada por tope de iteraciones sin umbral de confianza para DONE suave: "
                "el límite actuó como respaldo, no como señal de éxito."
            )
            posture_es = "Aumenta GHOST_MAX_ITERATIONS o reduce alcance de la tarea."
        elif is_fallback_stag:
            code = "fallback_stagnation"
            completion = min(completion, 0.4)
            summary_es = (
                "Parada por estancamiento (respaldo): poco progreso nuevo en exploración. "
                "No se trató como cierre por alta confianza."
            )
            posture_es = "Cambia de estrategia o divide la tarea."
        else:
            code = "aborted_generic"
            summary_es = "Sesión abortada; revisa política, presupuesto o errores previos."
            posture_es = "Consulta root_cause y eventos de sesión."

    sig_out = dict(signals)
    if sig_out.get("task_mode") == "micro_task" or sig_out.get("micro_task_kind"):
        sig_out["note"] = "micro_task_mode"
    completion = round(max(0.0, min(1.0, completion)), 4)

    return TaskConfidenceSnapshot(
        task_confidence_level=level,
        task_completion_confidence=completion,
        closure_reason_code=code,
        closure_reason_summary_es=summary_es,
        recommended_runtime_posture_es=posture_es,
        signals=sig_out,
    )


def merge_iteration_limit_summary_with_confidence(
    iteration_assessment: Dict[str, str],
    *,
    task_confidence_level: str,
    task_completion_confidence: float,
    closure_reason_summary_es: str,
) -> Dict[str, str]:
    """Enrich iteration-limit UX copy with confidence framing when both apply."""
    if not iteration_assessment:
        return iteration_assessment
    kind = iteration_assessment.get("kind", "")
    if kind not in (
        "iteration_cap_soft_done_answered",
        "iteration_cap_soft_done_evidence_only",
        "iteration_cap_explored_useful_evidence",
    ):
        return iteration_assessment
    prefix = (
        f"[Confianza de tarea: {task_confidence_level} "
        f"({task_completion_confidence:.0%})] "
        f"{closure_reason_summary_es} "
    )
    out = dict(iteration_assessment)
    out["summary_es"] = prefix + str(out.get("summary_es", "")).strip()
    out["task_confidence_level"] = task_confidence_level
    out["task_completion_confidence"] = f"{task_completion_confidence:.4f}"
    return out
