"""
Operator-facing closure summary for artifacts and CLI (trust + debug).

Machine-readable fields (``closure_reason``, ``terminal_resolution_record``, etc.) stay authoritative;
this module adds a **stable narrative** that leads with *why Ghost stopped* and *how sure it is*,
and treats iteration limits as **secondary context** when soft DONE applies.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from apps.cli.runtime.session_phase import LOOP_ABORT_MAX_ITERATIONS

# Frases que implican verificación/certeza fuerte sin tier ``confirmed`` (solo lectura).
_TIER_STRONG_CLAIM_ES = (
    "bug confirmado",
    "confirmado que hay",
    "confirmado:",
    "está confirmado",
    "impacto crítico",
    "crítico y confirmado",
    "fallará",
    "va a fallar",
    "seguro que falla",
    "definitivamente",
    "sin duda hay un bug",
    "hay un bug en",
    "bloquea todo",
    "bloquea el servicio",
    "bloquea todo el servicio",
    "confirmed bug",
    "critical impact",
    "will fail",
    "definitely broken",
    "service will fail",
)


def apply_tier_language_guard_es(text: str, tier: Optional[str]) -> str:
    """
    Si la evidencia no es ``confirmed``, no dejamos pasar narrativa de bug/criticidad como hecho.
    """
    if not (text or "").strip():
        return text
    t = (tier or "").strip().lower()
    if t == "confirmed":
        return text
    if t not in ("suspected", "unverified"):
        return text
    low = text.lower()
    if any(p in low for p in _TIER_STRONG_CLAIM_ES):
        return (
            "Los hallazgos deben interpretarse como **hipótesis** (evidencia no confirmada con checks en disco). "
            "Revisa la respuesta completa y las citas de `read_file` antes de actuar; el modelo usó lenguaje "
            "demasiado tajante para el nivel de evidencia registrado."
        )
    return text

# Human / operator alias (English) for grep and dashboards — codes remain stable in closure_reason.
CLOSURE_REASON_OPERATOR_ALIASES_EN: Dict[str, str] = {
    "primary_verification_pass": "verified_write",
    "primary_implemented_verify_skipped": "implemented_verify_skipped",
    "primary_already_implemented": "already_implemented",
    "fallback_max_iterations_with_already_implemented_confidence": "iteration_soft_done_already_implemented",
    "primary_read_only_evidence": "read_only_evidence_sufficient",
    "repo_mismatch_graceful_closure": "read_only_wrong_repo_mismatch",
    "informative_missing_path_readonly": "read_only_missing_path_in_workspace",
    "informative_missing_symbol_readonly": "read_only_missing_symbol_or_path",
    "informative_explore_churn_readonly": "read_only_explore_churn_informative",
    "fallback_max_iterations_with_readonly_confidence": "iteration_soft_done_read_only",
    "verification_failed_low_confidence": "verification_failed",
    "insufficient_progress_no_op": "no_op_insufficient_progress",
    "blocked_or_partial_low_confidence": "blocked_or_partial",
    "fallback_max_iterations_abort": "iteration_hard_stop_abort",
    "fallback_stagnation": "stagnation_abort",
    "aborted_generic": "aborted_generic",
    "outcome_default": "outcome_default",
    "unspecified": "unspecified",
}

COMPLETION_FIRM = "firm"
COMPLETION_SOFT = "soft"
COMPLETION_ABORTED = "aborted"

_SOFT_ILA_KINDS = frozenset(
    {
        "iteration_cap_soft_done_answered",
        "iteration_cap_soft_done_evidence_only",
        "iteration_cap_explored_useful_evidence",
    }
)


def closure_reason_operator_alias_en(code: str) -> str:
    c = (code or "").strip()
    return CLOSURE_REASON_OPERATOR_ALIASES_EN.get(c, c or "unknown")


def _terminal_record(session: Any) -> Dict[str, Any]:
    rec = getattr(session, "terminal_resolution_record", None)
    return dict(rec) if isinstance(rec, dict) else {}


def _signals(session: Any) -> Dict[str, Any]:
    s = getattr(session, "task_confidence_signals", None)
    return dict(s) if isinstance(s, dict) else {}


def derive_completion_style(session: Any) -> Tuple[str, str]:
    """
    Return (style, explanation_es).

    - ``firm``: DONE sin narrativa de “cierre suave por tope de iteraciones”.
    - ``soft``: DONE donde el tope de iteraciones es contexto pero el cierre fue intencional.
    - ``aborted``: fase ABORTED.
    """
    phase = str(getattr(session, "phase", "") or "").strip().upper()
    cr = str(getattr(session, "closure_reason", "") or "").strip()
    rec = _terminal_record(session)
    ila = rec.get("iteration_limit_assessment") or {}
    ila_kind = str(ila.get("kind") or "") if isinstance(ila, dict) else ""
    loop_raw = rec.get("loop_stop_signal_raw")
    if loop_raw is None:
        loop_raw = (rec.get("resolution_sources") or {}).get("loop_abort_reason_raw")
    loop_raw = str(loop_raw or "")

    if phase == "ABORTED":
        return COMPLETION_ABORTED, (
            "La sesión terminó en **ABORTED**: no se considera un cierre exitoso de tarea."
        )
    if phase != "DONE":
        return COMPLETION_FIRM, "Estado terminal distinto de DONE/ABORTED (revisar `phase` en el artefacto)."

    if loop_raw == LOOP_ABORT_MAX_ITERATIONS and ila_kind in _SOFT_ILA_KINDS:
        return COMPLETION_SOFT, (
            "**Cierre suave (intencional):** el bucle alcanzó el tope de iteraciones, pero la política "
            "de outcome/evidencia permitió marcar **DONE**. Esto **no** es un fallo oculto: revisa la "
            "respuesta final y el historial de herramientas."
        )
    if cr.startswith("fallback_max_iterations_with_"):
        return COMPLETION_SOFT, (
            "**Cierre suave:** el tope de iteraciones apareció como **contexto técnico**; la decisión "
            "de DONE se apoya en outcome (p. ej. solo lectura / ya implementado) y confianza, no en "
            "verificación de escritura completa."
        )
    return COMPLETION_FIRM, (
        "**Cierre firme:** paro alineado con el outcome principal (p. ej. verificación correcta, o "
        "solo lectura sin depender del tope de iteraciones como narrativa central)."
    )


def primary_headline_es(session: Any) -> str:
    """
    Single operator-facing line: **why** Ghost stopped, evidence-first when applicable.
    """
    phase = str(getattr(session, "phase", "") or "").strip().upper()
    cr = str(getattr(session, "closure_reason", "") or "").strip()
    sig = _signals(session)
    rec = _terminal_record(session)
    ila = rec.get("iteration_limit_assessment") or {}
    ila_kind = str(ila.get("kind") or "") if isinstance(ila, dict) else ""

    if phase == "ABORTED":
        if cr == "verification_failed_low_confidence":
            return "Ghost paró tras **verificación fallida**; hace falta corregir y volver a verificar."
        if cr == "insufficient_progress_no_op":
            return "Ghost paró por **falta de progreso** (sin herramientas útiles ni respuesta final clara)."
        if cr == "fallback_max_iterations_abort":
            return "Ghost paró por **tope de iteraciones** sin umbral de confianza para un DONE suave."
        return "Ghost paró en **ABORTED**; revisa política, presupuesto o mensajes de error en el historial."

    # DONE paths — lead with success mechanism, not iteration cap
    if sig.get("had_evidence_sufficient_immediate_synthesis"):
        return (
            "Ghost consideró la tarea **completa** tras **evidencia suficiente** y un turno final "
            "sin herramientas (síntesis inmediata)."
        )
    if cr == "primary_verification_pass":
        return "Ghost cerró con **verificación estructurada correcta** tras aplicar cambios en el repositorio."
    if cr == "primary_implemented_verify_skipped":
        return (
            "Ghost cerró como **implementado** con cambios en disco; la verificación automática "
            "**no se ejecutó** — valida localmente si el cambio es crítico."
        )
    if cr == "primary_already_implemented":
        return "Ghost cerró con **ya implementado**: el código ya cubría lo pedido según exploración."
    if cr == "repo_mismatch_graceful_closure":
        return (
            "Ghost cerró en **solo lectura informativa**: el workspace **no coincide** con el repo o rutas "
            "que la tarea asume (no es un fallo opaco del modelo)."
        )
    if cr == "informative_missing_path_readonly":
        return (
            "Ghost cerró en **solo lectura informativa**: las **rutas o ficheros pedidos no están** "
            "en el workspace actual."
        )
    if cr == "informative_missing_symbol_readonly":
        return (
            "Ghost cerró en **solo lectura informativa**: el **símbolo o ruta** buscado **no aparece** "
            "en este repositorio (o hubo churn sin nueva evidencia)."
        )
    if cr == "informative_explore_churn_readonly":
        return (
            "Ghost cerró en **solo lectura informativa** tras **churn de exploración** "
            "(herramientas repetidas sin señal nueva) — distinto de un error duro del runtime."
        )
    if cr in (
        "fallback_max_iterations_with_already_implemented_confidence",
        "fallback_max_iterations_with_readonly_confidence",
    ):
        if cr.endswith("readonly_confidence"):
            return (
                "Ghost cerró en **solo lectura** con confianza en la **respuesta y el historial de "
                "herramientas**; el tope de iteraciones fue secundario."
            )
        return (
            "Ghost cerró como **ya implementado** con confianza pese al tope de iteraciones del bucle "
            "(cierre suave, no fallo principal)."
        )
    if cr == "primary_read_only_evidence":
        if sig.get("had_explore_answer_now_nudge"):
            return (
                "Ghost cerró en **solo lectura** con evidencia y política **answer-now** "
                "(respuesta acotada sin bucle largo)."
            )
        return "Ghost cerró en **solo lectura** apoyándose en la **evidencia del historial** y la respuesta final."

    if phase == "DONE" and ila_kind == "iteration_cap_soft_done_answered":
        return (
            "Ghost marcó **DONE** porque había **respuesta final** adecuada aunque el bucle "
            "alcanzó el tope de iteraciones (**cierre suave intencional**)."
        )
    if phase == "DONE" and ila_kind == "iteration_cap_soft_done_evidence_only":
        return (
            "Ghost marcó **DONE** por **evidencia de solo lectura** pese al tope de iteraciones; "
            "revisa el texto del asistente."
        )

    crs = (getattr(session, "closure_reason_summary_es", None) or "").strip()
    if crs:
        tier = str(getattr(session, "findings_evidence_tier", "") or "").strip().lower()
        line = crs.split(". ")[0][:220] + ("…" if len(crs) > 220 else "")
        return apply_tier_language_guard_es(line, tier)
    return "Ghost completó la sesión según el outcome y la fase terminal registrados en el artefacto."


def build_closure_operator_view(session: Any) -> Dict[str, Any]:
    """Structured block for JSON artifact (additive; does not replace canonical fields)."""
    style, style_es = derive_completion_style(session)
    rec = _terminal_record(session)
    ila = rec.get("iteration_limit_assessment") if isinstance(rec.get("iteration_limit_assessment"), dict) else {}
    loop_raw = rec.get("loop_stop_signal_raw")
    if loop_raw is None:
        loop_raw = (rec.get("resolution_sources") or {}).get("loop_abort_reason_raw")

    phase = str(getattr(session, "phase", "") or "").strip()
    cr = str(getattr(session, "closure_reason", "") or "").strip()
    alias = closure_reason_operator_alias_en(cr)

    iter_secondary = bool(loop_raw == LOOP_ABORT_MAX_ITERATIONS and style == COMPLETION_SOFT)

    tier = str(getattr(session, "findings_evidence_tier", "") or "").strip().lower()
    headline = primary_headline_es(session)
    headline = apply_tier_language_guard_es(headline, tier)
    return {
        "primary_headline_es": headline,
        "completion_style": style,
        "completion_style_label_es": {
            COMPLETION_FIRM: "cierre_firme",
            COMPLETION_SOFT: "cierre_suave_intencional",
            COMPLETION_ABORTED: "abortado",
        }.get(style, style),
        "completion_style_explanation_es": style_es,
        "closure_reason_code": cr,
        "closure_reason_operator_alias_en": alias,
        "terminal_phase": phase,
        "task_confidence_level": getattr(session, "task_confidence_level", None) or "",
        "task_completion_confidence": float(getattr(session, "task_completion_confidence", 0.0) or 0.0),
        "outcome_confidence": float(getattr(session, "outcome_confidence", 0.0) or 0.0),
        "task_outcome": getattr(session, "task_outcome", None) or "",
        "iteration_limit_context": {
            "loop_stop_signal_raw": loop_raw,
            "assessment_kind": ila.get("kind"),
            "summary_es": ila.get("summary_es"),
            "narrative_is_secondary": iter_secondary,
        },
    }


def build_closure_operator_markdown_section(session: Any) -> str:
    """Markdown block for ``ArtifactSession.to_markdown`` (insert after task text)."""
    if not (getattr(session, "task_outcome", None) or getattr(session, "closure_reason", None) or getattr(session, "phase", None)):
        return ""
    cov = build_closure_operator_view(session)
    style = cov.get("completion_style") or COMPLETION_FIRM
    style_label = cov.get("completion_style_label_es") or ""
    headline = cov.get("primary_headline_es") or ""
    style_es = cov.get("completion_style_explanation_es") or ""
    cr = cov.get("closure_reason_code") or ""
    alias = cov.get("closure_reason_operator_alias_en") or ""
    tcl = cov.get("task_confidence_level") or ""
    tcc = float(cov.get("task_completion_confidence") or 0.0)
    oc = float(cov.get("outcome_confidence") or 0.0)
    outcome = cov.get("task_outcome") or ""
    phase = cov.get("terminal_phase") or ""
    tier_md = str(getattr(session, "findings_evidence_tier", "") or "").strip().lower()
    crs = apply_tier_language_guard_es(
        (getattr(session, "closure_reason_summary_es", None) or "").strip(),
        tier_md,
    )
    cp = (getattr(session, "closure_posture_es", None) or "").strip()

    ila_ctx = cov.get("iteration_limit_context") or {}
    ila_kind = ila_ctx.get("assessment_kind")
    ila_sum = (ila_ctx.get("summary_es") or "").strip()
    loop_raw = ila_ctx.get("loop_stop_signal_raw")
    iter_secondary = bool(ila_ctx.get("narrative_is_secondary"))

    md = "## 🔚 Por qué paró Ghost (resumen operador)\n\n"
    md += f"**Motivo principal:** {headline}\n\n"
    md += f"**Tipo de cierre:** `{style}` (`{style_label}`) — {style_es}\n\n"
    md += "**Campos técnicos (auditoría):**\n"
    md += f"- `closure_reason`: `{cr}`\n"
    md += f"- `closure_reason_operator_alias_en`: `{alias}`\n"
    md += f"- `terminal_phase`: `{phase}`\n"
    md += f"- `task_outcome`: `{outcome}`\n\n"
    md += "**Confianza:**\n"
    md += f"- **Nivel de confianza de tarea:** `{tcl}`\n"
    md += f"- **Confianza de completitud (unificada):** {tcc:.0%}\n"
    md += f"- **Confianza del outcome engine:** {oc:.0%}\n\n"
    if iter_secondary and (ila_sum or ila_kind):
        md += "**Contexto técnico (iteraciones — no es el fallo principal):**\n"
        if loop_raw:
            md += f"- Señal de bucle conservada: `{loop_raw}`\n"
        if ila_kind:
            md += f"- Clasificación UX: `{ila_kind}`\n"
        if ila_sum:
            md += f"- {ila_sum}\n"
        md += "\n"
    elif (ila_sum or ila_kind) and loop_raw == LOOP_ABORT_MAX_ITERATIONS:
        md += "**Límite de iteraciones (contexto):**\n"
        md += f"- `{ila_kind or 'n/a'}` — {ila_sum or '—'}\n\n"

    if crs:
        md += f"**Explicación detallada (motor de confianza):**  \n{crs}\n\n"
    if cp:
        md += f"**Postura sugerida:** {cp}\n\n"

    return md
