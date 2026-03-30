"""
Operator harness: compact views, AGENTS.md hydration, plan/diff/verify formatting.

Keeps presentation logic out of assistant.py; JSON/trace remain authoritative.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apps.cli.ui import ui_contract as _ui_contract

AGENTS_CANDIDATES = ("AGENTS.md", "agents.md")


def resolve_agents_md_path(repo_root: str) -> Optional[Path]:
    root = Path(repo_root or "").resolve()
    if not root.is_dir():
        return None
    for name in AGENTS_CANDIDATES:
        p = root / name
        if p.is_file():
            return p
    return None


def load_agents_md_digest(repo_root: str, *, max_chars: int = 14000) -> Tuple[str, str, str]:
    """
    Returns (relative_path_or_empty, full_text_capped, sha256_prefix_or_empty).
    """
    p = resolve_agents_md_path(repo_root)
    if not p:
        return "", "", ""
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return str(p.name), "", ""
    digest = raw[: max(0, int(max_chars))]
    h = ""
    try:
        import hashlib

        h = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    except Exception:
        h = ""
    rel = str(p.relative_to(Path(repo_root).resolve())) if repo_root else p.name
    return rel, digest, h


def detect_agents_user_tension(user_task: str, agents_text: str) -> str:
    """
    Surface likely contradictions between user task and AGENTS.md (heuristic).
    """
    if not user_task.strip() or not agents_text.strip():
        return ""
    ut = user_task.lower()
    ag = agents_text.lower()
    notes: List[str] = []

    if re.search(r"\b(touch|edit|change|modify|tocar|edita|cambia)\b.*\b(ui|frontend|component)\b", ut):
        if re.search(r"do not touch ui|don't touch ui|no toques (la )?ui|never touch ui", ag):
            notes.append("El usuario pide cambios de UI pero AGENTS.md advierte de no tocar la UI.")

    if re.search(r"\b(skip|omit|without)\b.*\b(test|tests|pytest)\b", ut) or "sin tests" in ut:
        if re.search(r"\b(always|must|should)\b.*\b(test|pytest)\b", ag) or "run test" in ag:
            notes.append("El usuario sugiere omitir tests pero AGENTS.md enfatiza ejecutar tests.")

    if "database.py" in ut or "sqlite" in ut:
        if "do not touch" in ag and "database" in ag:
            notes.append("La tarea menciona DB; revisa si AGENTS.md restringe esos paths.")

    return " ".join(notes) if notes else ""


def hydrate_agents_on_session(session: Any, repo_root: str, user_task: str) -> None:
    """Attach AGENTS.md snapshot + optional tension note (INTAKE)."""
    rel, digest, h = load_agents_md_digest(repo_root)
    setattr(session, "agents_md_path", rel or "")
    setattr(session, "agents_md_digest", digest or "")
    setattr(session, "agents_md_sha12", h or "")
    tension = detect_agents_user_tension(user_task or "", digest or "")
    setattr(session, "agents_user_tension_note", tension or "")


def build_agents_project_prompt_block(session: Any) -> str:
    """First-class project rules for system prompt (after core Ghost rules)."""
    path = str(getattr(session, "agents_md_path", "") or "").strip()
    digest = str(getattr(session, "agents_md_digest", "") or "").strip()
    if not digest:
        return ""
    sha = str(getattr(session, "agents_md_sha12", "") or "").strip()
    tension = str(getattr(session, "agents_user_tension_note", "") or "").strip()
    meta = f"`{path}`" + (f" (sha256:{sha})" if sha else "")
    tension_block = f"\n**Atención (posible tensión con el pedido del usuario):** {tension}\n" if tension else ""
    return (
        f"[PROJECT AGENTS.md — reglas del repositorio ({meta})]\n"
        f"- Estas reglas tienen **prioridad** sobre suposiciones del modelo cuando describen límites del repo.\n"
        f"- No sustituyas este archivo con el texto del usuario; respétalo como contrato del proyecto.\n"
        f"{tension_block}\n"
        f"{digest}\n"
        f"[/PROJECT AGENTS.md]\n\n"
    )


def runtime_mode_label(task_intent: str, change_expectation: str, repair_count: int) -> str:
    ti = (task_intent or "").strip().lower()
    ce = (change_expectation or "").strip().lower()
    if int(repair_count or 0) > 0:
        return "repair"
    if ti in ("analysis", "review") or ce == "should_not_write":
        return "analysis" if ti == "analysis" else "review"
    if ti == "bugfix":
        return "bugfix"
    if ti in ("implementation", "modification", "refactor", "verification"):
        return "implementation"
    return "implementation"


def normalize_change_kind(raw: str) -> str:
    t = (raw or "").strip().lower()
    if "write" in t and "file" in t:
        return "new"
    if "edit" in t or "patch" in t:
        return "modified"
    if "delete" in t:
        return "deleted"
    return raw or "changed"


def build_workset_tree_lines(
    diff_summary: List[Dict[str, Any]],
    active_workset: Optional[Dict[str, Any]] = None,
    *,
    ascii_safe: bool = False,
) -> List[str]:
    """Compact tree: group paths by top-level folder, tag new/mod/del."""
    rows: List[Tuple[str, str]] = []
    for item in diff_summary or []:
        if not isinstance(item, dict):
            continue
        f = str(item.get("file") or "").replace("\\", "/").strip()
        if not f:
            continue
        kind = normalize_change_kind(str(item.get("type") or ""))
        rows.append((f, kind))
    ws = active_workset or {}
    for key, label in (
        ("written_files", "new"),
        ("edited_files", "modified"),
        ("deleted_files", "deleted"),
    ):
        for p in ws.get(key) or []:
            s = str(p).replace("\\", "/").strip()
            if s and not any(s == r[0] for r in rows):
                rows.append((s, label))
    if not rows:
        return []
    by_dir: Dict[str, List[Tuple[str, str]]] = {}
    for f, k in sorted(rows, key=lambda x: x[0].lower()):
        parts = f.split("/")
        top = parts[0] if len(parts) > 1 else "."
        by_dir.setdefault(top, []).append((f, k))
    lines: List[str] = []
    root_mark = "> " if ascii_safe else "▸ "
    for top in sorted(by_dir.keys(), key=str.lower):
        lines.append(f"{root_mark}{top}/")
        for f, k in by_dir[top]:
            sym = {"new": "+", "modified": "~", "deleted": "−", "changed": "·"}.get(k, "·")
            lines.append(f"   {sym} {f}")
    return lines


def format_operator_execution_plan(plan_text: str, decision_plan: Optional[Dict[str, Any]]) -> str:
    """
    Short actionable plan for humans — not a contract dump.
    """
    chunks: List[str] = []
    pt = (plan_text or "").strip()
    if pt:
        # Cap model plan spam
        if len(pt) > 3500:
            pt = pt[:3500] + "\n… [truncado para vista operador; ver artefacto completo]"
        chunks.append(pt)
    dp = decision_plan if isinstance(decision_plan, dict) else {}
    steps = dp.get("steps") or dp.get("planned_steps") or []
    if isinstance(steps, list) and steps:
        lines = []
        for i, s in enumerate(steps[:12], 1):
            if isinstance(s, str):
                lines.append(f"{i}. {s.strip()}")
            elif isinstance(s, dict):
                act = s.get("action") or s.get("title") or s.get("name") or ""
                tgt = s.get("target") or s.get("path") or ""
                lines.append(f"{i}. {act} {tgt}".strip())
        if lines:
            chunks.append("**Siguiente (planner)**\n" + "\n".join(lines))
    summary = (dp.get("summary") or dp.get("rationale") or "").strip()
    if summary and summary not in (pt or ""):
        chunks.append(f"**Contexto planner** — {summary[:800]}")
    return "\n\n".join(chunks).strip()


def verification_plan_summary_from_spec(contract_spec: Optional[Dict[str, Any]]) -> str:
    if not isinstance(contract_spec, dict):
        return "(sin spec)"
    vp = contract_spec.get("verification_policy") or {}
    if not vp.get("required"):
        return "Verificación no requerida por contrato (p. ej. solo lectura)."
    parts = []
    for k in ("typecheck", "build", "lint", "tests"):
        if vp.get(k):
            parts.append(k)
    return "Checks previstos: " + (", ".join(parts) if parts else "(ninguno explícito)")


def compact_phase_trace_lines_from_list(hist: Optional[List[Dict[str, Any]]], *, max_events: int = 14) -> List[str]:
    """Observe → act-style audit from phase_transitions (no decorative CoT)."""
    if not isinstance(hist, list) or not hist:
        return []
    lines: List[str] = []
    for row in hist[-max_events:]:
        if not isinstance(row, dict):
            continue
        ph = str(row.get("phase") or row.get("to") or "?")
        detail = str(row.get("detail") or row.get("reason") or "")[:120]
        it = row.get("iteration")
        suf = f" iter={it}" if it is not None else ""
        lines.append(f"• {ph}{suf}: {detail}".rstrip())
    return lines


def strip_coverage_tail(text: str, *, max_lines: int = 30) -> str:
    """Avoid dumping huge pytest/coverage blobs in TTY."""
    if not text or len(text) < 4000:
        return text
    ls = text.splitlines()
    if len(ls) <= max_lines:
        return text
    head = ls[:max_lines]
    return "\n".join(head) + f"\n… [{len(ls) - max_lines} líneas omitidas; ver artefacto]"


def readonly_analysis_truthfulness_prompt_section() -> str:
    """
    Guardrails for /plan, analysis, and review: reduce hallucinated concrete bugs and fake snippets.
    Injected when change_expectation is read-only or intent is analysis/review.
    """
    return (
        "\n# Read-only truthfulness (mandatory for analysis / plan / review)\n"
        "- **No concrete bugs without grounding**: Do not claim a specific bug (file path + line + code) unless "
        "the exact text appears in a `read_file` tool result from *this* session, or in an executed test/check output. "
        "If you are inferring, label it explicitly as **suspected**, **possible risk**, or **needs verification** — never as confirmed fact.\n"
        "- **Snippets**: Any code block shown as evidence must be a **verbatim substring** of tool output you received. "
        "Do not reconstruct, complete, or “fix” truncated identifiers (e.g. inventing `initial_tas` from memory). "
        "If you did not read the line, do not quote it as code.\n"
        "- **Prefer fewer, stronger claims**: For questions like “biggest bug”, prefer 0–1 well-grounded observations over a list of speculative issues.\n"
        "- **If evidence is thin** after a few reads: say so honestly and propose the **single smallest** next step "
        "(one `read_file` path, or one check) — do not invent a critical bug to satisfy the user.\n"
        "- **No full verify** unless the user explicitly asks to run checks; stay read-only unless they request execution.\n"
        "- **Runtime note:** Ghost may strip or replace fenced/inline code in the final reply if it does not match "
        "`read_file` output from this session (see artifact `analysis_grounding_digest` / session events).\n"
        "- **Prohibido sin ejecución de checks o cita literal verificada:** no escribas frases como "
        "**bug confirmado**, **impacto crítico**, **fallará seguro**, **está roto en producción**, "
        "o equivalentes en inglés, salvo que acabes de mostrar salida de test/check que falle o un "
        "fragmento copiado de `read_file` de esta sesión.\n"
        "- **No implementation CTAs (enforced by runtime):** never close with offers or questions to apply fixes "
        "(e.g. «¿Desea que proceda con las correcciones?», «¿Quieres que lo arregle?», «Puedo aplicar el fix ahora», "
        "«Want me to fix this?»). Close with: **hypothesis**, **evidence**, **one minimal validation step** "
        "(a single `read_file` path or one reproducible check) — not execution.\n"
    )


def truncate_unified_diff(diff_text: str, *, max_lines: int = 56) -> Tuple[str, bool]:
    if not diff_text:
        return "", False
    lines = diff_text.splitlines()
    if len(lines) <= max_lines:
        return diff_text, False
    return "\n".join(lines[:max_lines]) + f"\n… ({len(lines) - max_lines} líneas más en artefacto)", True


def _collect_open_risks(session: Any) -> List[str]:
    risks: List[str] = []
    rs = getattr(session, "risks", None)
    if isinstance(rs, list):
        for r in rs[:12]:
            if isinstance(r, str) and r.strip():
                risks.append(r.strip()[:400])
    ber = str(getattr(session, "budget_exhausted_reason", "") or "").strip()
    if ber:
        risks.append(f"Budget: {ber[:300]}")
    lfc = str(getattr(session, "last_failed_check", "") or "").strip()
    if lfc:
        risks.append(f"Último check fallido: {lfc[:200]}")
    if getattr(session, "repo_mismatch_detected", False):
        msg = str(getattr(session, "repo_mismatch_operator_message", "") or "").strip()
        risks.append(f"Repo mismatch: {msg[:300]}" if msg else "Repo mismatch detectado.")
    if int(getattr(session, "repair_attempt_count", 0) or 0) > 0:
        ro = str(getattr(session, "repair_specialist_outcome", "") or "").strip()
        if ro:
            risks.append(f"Repair: {ro[:200]}")
    return risks[:16]


def _steps_executed_count(ver: Any) -> int:
    if not isinstance(ver, dict):
        return 0
    try:
        v = ver.get("steps_executed_count")
        if v is None:
            return 0
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _checks_executed_on_disk(ver: Any) -> int:
    if not isinstance(ver, dict):
        return 0
    n = 0
    for c in (ver.get("checks") or [])[:48]:
        if isinstance(c, dict) and str(c.get("provenance") or "") == "executed":
            n += 1
    return n


def _what_changed_lines_from_diff_summary(ds: Any) -> List[str]:
    if not isinstance(ds, list):
        return []
    out: List[str] = []
    for it in ds[:32]:
        if not isinstance(it, dict):
            continue
        fp = str(it.get("file") or "").strip()
        if not fp:
            continue
        raw_t = str(it.get("type") or "")
        kind = normalize_change_kind(raw_t)
        out.append(f"`{fp}` — {kind}")
    return out


def _compute_review_readiness(
    *,
    phase_done: bool,
    has_changes: bool,
    vstat: str,
    steps_n: int,
    checks_exec: int,
) -> Tuple[str, str, bool]:
    """
    Devuelve (código estable, detalle operador ES, ready_for_review).
    ``has_changes``: hay archivos en diff_summary o outcome de implementación explícito.
    """
    if not phase_done:
        return (
            "session_open",
            "La sesión no está en DONE; tratar como trabajo en curso o bloqueado.",
            False,
        )
    if not has_changes:
        return (
            "no_changes",
            "No hay archivos listados en el diff de sesión; poco que revisar como patch.",
            False,
        )
    has_verify_execution = steps_n > 0 or checks_exec > 0
    if vstat in ("failed", "error"):
        return (
            "verify_failed",
            "Verify falló: revisar salida de checks antes de aprobar.",
            False,
        )
    if vstat == "incomplete":
        return (
            "verify_incomplete",
            "Verify incompleto: confirmar qué checks faltan por ejecutar.",
            False,
        )
    if vstat == "skipped":
        return (
            "verify_skipped",
            "Verify omitido por política; el reviewer debe validar manualmente si aplica.",
            True,
        )
    if vstat in ("passed", "success"):
        if has_verify_execution:
            return (
                "ready",
                "Cambios registrados y comprobaciones ejecutadas en disco.",
                True,
            )
        return (
            "verify_not_executed",
            "El estado indica éxito pero no hay ejecución registrada en disco; no asumir CI verde.",
            False,
        )
    return (
        "verify_pending",
        "Sin verify acreditado en esta sesión para estos cambios.",
        False,
    )


def _recommended_reviewer_action_for_code(code: str) -> str:
    m = {
        "ready": (
            "Recorrer archivos tocados y el diff; contrastar con verify ejecutado; "
            "aprobar o pedir ajustes puntuales."
        ),
        "verify_skipped": (
            "Confirmar si omitir verify es aceptable para el riesgo del cambio; si no, lanzar checks antes de merge."
        ),
        "verify_failed": "Reproducir el fallo, corregir o documentar el riesgo explícitamente antes de aprobar.",
        "verify_not_executed": "Exigir una corrida real de verify en disco antes de considerar listo para merge.",
        "verify_incomplete": "Completar o aclarar los checks pendientes; no cerrar la revisión con lagunas.",
        "verify_pending": "Acordar y ejecutar verify según taskspec antes de dar por bueno el patch.",
        "session_open": "No integrar: la sesión no cerró formalmente; revisar next_action y riesgos.",
        "no_changes": "Si se esperaba código, revisar handoff/plan; si era solo análisis, el cierre es coherente.",
    }
    return m.get(
        code,
        "Revisar resumen, riesgos abiertos y enlaces de continuidad antes de integrar o re-delegar.",
    )


def _recommended_reviewer_action(
    session: Any,
    *,
    ready_for_review: bool,
    verify_summary: Dict[str, Any],
    review_code: str = "",
) -> str:
    if review_code:
        return _recommended_reviewer_action_for_code(review_code)
    if ready_for_review:
        return _recommended_reviewer_action_for_code("ready")
    vstat = str(verify_summary.get("status") or "").lower()
    if vstat in ("failed", "error", "incomplete"):
        return _recommended_reviewer_action_for_code(
            "verify_failed" if vstat in ("failed", "error") else "verify_incomplete"
        )
    if str(getattr(session, "phase", "") or "").upper() != "DONE":
        return _recommended_reviewer_action_for_code("session_open")
    return _recommended_reviewer_action_for_code("verify_pending")


def build_change_proposal_packet(
    session: Any,
    *,
    plan_relpath: str,
    handoff_relpath: str,
    envelope_relpath: str = "",
) -> Dict[str, Any]:
    """
    Paquete tipo «cuerpo base de PR / review request» (compacto, escaneable).
    No crea ramas ni PRs.
    """
    closure = str(getattr(session, "closure_reason_summary_es", "") or "").strip()[:1200]
    if not closure:
        closure = str(getattr(session, "closure_reason", "") or "").strip()[:1200]
    task_out = str(getattr(session, "task_outcome", "") or "")
    phase = str(getattr(session, "phase", "") or "")
    exec_summary = closure or (f"Fase={phase} outcome={task_out}".strip()) or "(sin cierre registrado)"
    one_line = str(getattr(session, "task", "") or "").strip()[:240] or exec_summary[:240]

    files: List[str] = []
    ds = getattr(session, "diff_summary", None) or []
    if isinstance(ds, list):
        for it in ds[:48]:
            if isinstance(it, dict) and it.get("file"):
                files.append(str(it["file"]))

    ver = getattr(session, "verification", None) or {}
    verify_summary: Dict[str, Any] = {}
    if isinstance(ver, dict):
        verify_summary = {
            "status": str(ver.get("status") or ""),
            "steps_executed_count": ver.get("steps_executed_count"),
            "scope": str(getattr(session, "verification_scope", "") or ""),
        }

    phase_done = str(phase).upper() == "DONE"
    vstat = str(verify_summary.get("status") or "").lower()
    steps_n = _steps_executed_count(ver if isinstance(ver, dict) else {})
    checks_exec = _checks_executed_on_disk(ver if isinstance(ver, dict) else {})
    tout_l = task_out.strip().lower()
    has_changes = bool(files) or tout_l in (
        "implemented",
        "partially_implemented",
        "already_implemented",
        "verification_failed",
    )
    review_code, review_detail_es, ready_for_review = _compute_review_readiness(
        phase_done=phase_done,
        has_changes=has_changes,
        vstat=vstat,
        steps_n=steps_n,
        checks_exec=checks_exec,
    )
    what_changed = _what_changed_lines_from_diff_summary(ds)
    open_risks = _collect_open_risks(session)
    rec_action = _recommended_reviewer_action(
        session,
        ready_for_review=ready_for_review,
        verify_summary=verify_summary,
        review_code=review_code,
    )

    actors = {
        "author": str(getattr(session, "role_author", "") or "ghost_model")[:120],
        "operator": str(getattr(session, "role_operator", "") or "human_operator")[:120],
        "reviewer": str(getattr(session, "role_reviewer", "") or "")[:120],
    }

    continuity = {
        "plan": (plan_relpath or "").replace("\\", "/"),
        "handoff": (handoff_relpath or "").replace("\\", "/"),
        "envelope": (envelope_relpath or "").replace("\\", "/"),
    }

    return {
        "format_version": 2,
        "kind": "ghost_change_proposal",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_id": str(getattr(session, "session_id", "") or ""),
        "task_id": str(getattr(session, "task_id", "") or ""),
        "summary": one_line,
        "executive_summary": exec_summary[:2000],
        "what_changed": what_changed,
        "files_touched": files,
        "verification_summary": verify_summary,
        "open_risks": open_risks,
        "recommended_reviewer_action": rec_action[:1500],
        "next_action": str(getattr(session, "next_action", "") or "")[:1200],
        "ready_for_review": ready_for_review,
        "review_readiness": review_code,
        "review_readiness_detail_es": review_detail_es[:800],
        "task_outcome": task_out,
        "phase": phase,
        "actors": actors,
        "continuity": continuity,
        "links": {
            "plan": continuity["plan"],
            "handoff": continuity["handoff"],
            "envelope": continuity["envelope"],
        },
    }


def _format_verification_for_pr(verify: Any) -> str:
    """Bloque legible para PR (evita JSON crudo)."""
    if not isinstance(verify, dict) or not verify:
        return "_Sin datos de verificación._"
    st = verify.get("status") or "—"
    parts = [f"- **Estado:** `{st}`"]
    steps = verify.get("steps_executed_count")
    if steps is not None:
        parts.append(f"- **Pasos ejecutados (contador):** {steps}")
    for c in (verify.get("checks") or [])[:12]:
        if not isinstance(c, dict):
            continue
        nm = str(c.get("name") or "?")
        cs = str(c.get("status") or "?")
        prov = str(c.get("provenance") or "")
        parts.append(f"- **{nm}:** `{cs}` · _{prov}_")
    ch = verify.get("checks")
    if isinstance(ch, list) and len(ch) > 12:
        parts.append(f"- _…{len(ch) - 12} checks más en JSON del artefacto._")
    return "\n".join(parts)


def _format_continuity_for_pr(continuity: Any, links: Any) -> str:
    if not isinstance(continuity, dict):
        continuity = {}
    if not isinstance(links, dict):
        links = {}
    rows: List[str] = []
    for key, label in (
        ("plan", "Plan operativo"),
        ("handoff", "Handoff"),
        ("envelope", "Delegation / envelope"),
    ):
        v = continuity.get(key) or links.get(key)
        if v:
            rows.append(f"- **{label}:** `{v}`")
    return "\n".join(rows) if rows else "_Sin rutas de continuidad._"


def format_review_packet_as_pr_body(packet: Dict[str, Any]) -> str:
    """Markdown tipo cuerpo de PR: secciones fijas, compacto, sin volcados JSON."""
    if not isinstance(packet, dict):
        return ""
    headline = str(packet.get("summary") or "")[:500]
    details = str(packet.get("executive_summary") or "")[:2400]
    ready = bool(packet.get("ready_for_review"))
    rcode = str(packet.get("review_readiness") or "").strip()
    rdetail = str(packet.get("review_readiness_detail_es") or "").strip()
    badge = "**Estado review:** `listo`" if ready else "**Estado review:** `pendiente`"
    if rcode:
        badge += f" · `{rcode}`"

    lines: List[str] = [
        "> _Ghost · review packet (markdown exportable; JSON canónico en `.ghost/review_packets/`)._",
        "",
        "---",
        "",
        _ui_contract.HARNESS_PR_H2_SUMMARY,
        headline or "_Sin línea de tarea._",
        "",
        badge,
    ]
    if rdetail:
        lines.extend(["", rdetail])

    what_lines = packet.get("what_changed") or []
    lines.extend(["", "---", "", _ui_contract.HARNESS_PR_H2_WHAT_CHANGED])
    if isinstance(what_lines, list) and any(str(x).strip() for x in what_lines):
        for w in what_lines[:28]:
            ws = str(w).strip()
            if ws:
                lines.append(f"- {ws}")
    elif details and details.strip() != headline.strip():
        lines.append(details)
    else:
        lines.append("_Sin detalle de cambios más allá del resumen._")

    lines.extend(["", "---", "", _ui_contract.HARNESS_PR_H2_FILES])
    files = packet.get("files_touched") or []
    if files:
        for f in files[:40]:
            lines.append(f"- `{f}`")
        if len(files) > 40:
            lines.append(f"- _…{len(files) - 40} rutas más (ver JSON)._")
    else:
        lines.append("_Ningún archivo listado._")

    lines.extend(
        [
            "",
            "---",
            "",
            _ui_contract.HARNESS_PR_H2_VERIFICATION,
            _format_verification_for_pr(packet.get("verification_summary")),
        ]
    )

    risks = packet.get("open_risks") or []
    if risks:
        lines.extend(["", "---", "", _ui_contract.HARNESS_PR_H2_OPEN_RISKS])
        for r in risks[:14]:
            lines.append(f"- {r}")

    lines.extend(
        [
            "",
            "---",
            "",
            _ui_contract.HARNESS_PR_H2_REVIEWER,
            str(packet.get("recommended_reviewer_action") or "_Nada sugerido._"),
            "",
            "---",
            "",
            _ui_contract.HARNESS_PR_H2_LINKS_HANDOFF,
            _format_continuity_for_pr(packet.get("continuity"), packet.get("links")),
            "",
            "---",
            "",
            _ui_contract.HARNESS_PR_H2_NEXT_OPERATOR,
            str(packet.get("next_action") or "_—_"),
        ]
    )
    return "\n".join(lines)
