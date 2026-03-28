"""
GhostRenderer — única implementación UI usada por CodexAssistant.

La interfaz pública de este módulo debe coincidir con todas las llamadas
``self.renderer.<method>(...)`` en ``assistant.py``. Cualquier kwargs nuevo
en el asistente debe aceptarse aquí (p. ej. ``append_tool_trace(..., auto_approved=)``).

Ver: docs/GHOST_RENDERER_ASSISTANT_CONTRACT.md
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.markup import escape
from rich.syntax import Syntax
from rich import box
from rich.table import Table

from apps.cli.ui import ui_contract as _ui_contract
from apps.cli.ui.theme import GHOST_RICH_THEME, ghost_box_rounded, ghost_wordmark_lines
from apps.cli.ui.slash_menu import (
    SlashMenuState,
    slash_menu_apply_selection,
    slash_menu_examples,
    slash_menu_group_label,
    slash_menu_quick_actions,
    slash_menu_query_items,
    slash_menu_start_suggestions,
    slash_menu_state,
)
from apps.cli.ui.visual_layout import (
    GhostVisualLayout,
    artifact_line_marker,
    build_ghost_visual_layout,
    default_terminal_width,
    format_live_phase_rail,
    layout_for_rail_api,
    review_packet_plain_fallback,
    rule_markup,
    salidas_paths_lines_split,
    startup_lines,
    tool_chip_prefix,
    truncate_visible,
)

_UNSET = object()

from apps.cli.runtime.closure_artifact_summary import apply_tier_language_guard_es
from apps.cli.runtime.harness_bundle import (
    build_workset_tree_lines,
    format_operator_execution_plan,
    normalize_change_kind,
    verification_plan_summary_from_spec,
    compact_phase_trace_lines_from_list,
    strip_coverage_tail,
    truncate_unified_diff,
)


def _tool_ui_mode() -> str:
    return os.getenv("GHOST_TOOL_UI", "compact").strip().lower()


def _ui_verbose() -> bool:
    """GHOST_UI_VERBOSE=1: contract spec, repo profile, traces, phase log, grounding digest, etc."""
    return os.getenv("GHOST_UI_VERBOSE", "0").strip().lower() in ("1", "true", "yes", "on")


def _status_spinner_name() -> str:
    """GHOST_STATUS_SPINNER: Rich spinner id (dots12, dots2, line, etc.)."""
    s = os.getenv("GHOST_STATUS_SPINNER", "dots12").strip() or "dots12"
    return s


def format_phase_rail(phase: str) -> str:
    """API estable: rail de implementación (4 pasos); layout fijo ancho 100 para tests."""
    return format_live_phase_rail(
        phase,
        _ui_contract.LIVE_RAIL_PROFILE_IMPLEMENT,
        layout=layout_for_rail_api(),
    )


def _default_live_detail_line(state: str) -> str:
    return {
        "thinking": _ui_contract.LIVE_STATE_THINKING,
        "waiting": _ui_contract.LIVE_STATE_WAITING_MODEL,
        "tool_exec": _ui_contract.LIVE_STATE_TOOL_GENERIC,
        "tool_result": _ui_contract.LIVE_STATE_TOOL_RESULT,
        "building": _ui_contract.LIVE_STATE_STREAMING,
        "closing": _ui_contract.LIVE_STATE_CLOSING,
        "ready": _ui_contract.LIVE_STATE_READY,
    }.get(state, _ui_contract.LIVE_STATE_WORKING)


_STATUS_HINT: Dict[str, str] = {
    "thinking": "El modelo sigue activo; prompts muy largos alargan este tramo.",
    "tool_exec": "read · patch · shell según política del gate",
    "building": "Stream de tokens o bloque final",
    "closing": "Síntesis sin nuevas herramientas",
}

_READLIKE_TOOL_NAMES = {"read_file", "ls", "search_code", "summarize_repo"}


def _tool_chip_label(name: str) -> str:
    m = {
        "read_file": "read",
        "read_batch": "reads",
        "edit_file": "patch",
        "write_file": "write",
        "delete_file": "del",
        "run_shell": "shell",
        "ls": "ls",
        "summarize_repo": "map",
        "search_code": "find",
    }
    return m.get(str(name or ""), str(name or "?")[:14])


def _is_readlike_tool(name: str) -> bool:
    return str(name or "").strip().lower() in _READLIKE_TOOL_NAMES


def _dedupe_tool_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse pending/final duplicates within one tool round by keeping the latest state."""
    latest: Dict[Tuple[str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("name") or ""), str(row.get("detail") or ""))
        if key not in latest:
            order.append(key)
        latest[key] = row
    return [latest[k] for k in order]


def _strip_inline_markdown(text: str) -> str:
    s = str(text or "")
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    return s.replace("**", "").replace("__", "").strip()


def _plan_mode_lines(text: str) -> List[str]:
    out: List[str] = []
    for raw in str(text or "").splitlines():
        s = _strip_inline_markdown(raw).strip()
        if not s or s.startswith("```") or s == "---":
            continue
        if s.startswith("#"):
            s = s.lstrip("#").strip()
        low = s.lower()
        if low.startswith("modo /plan") or low.startswith("/plan mode"):
            continue
        out.append(s)
    return out


def _plan_mode_steps(lines: List[str], *, limit: int = 6) -> List[str]:
    numbered = [re.sub(r"^\d+\.\s*", "", ln).strip() for ln in lines if re.match(r"^\d+\.\s+", ln)]
    if numbered:
        return numbered[:limit]
    bullets = [
        re.sub(r"^[-*•]\s*", "", ln).strip()
        for ln in lines
        if re.match(r"^[-*•]\s+", ln)
    ]
    if bullets:
        return bullets[:limit]
    fallback: List[str] = []
    skip_prefixes = (
        "plan de",
        "estado ",
        "problemas ",
        "hallazgos",
        "resumen",
        "siguiente paso",
        "conclusion",
        "conclusión",
    )
    for ln in lines:
        low = ln.lower()
        if low.endswith(":") or any(low.startswith(pref) for pref in skip_prefixes):
            continue
        if low.startswith(("voy a ", "empezar", "empezaré", "empezare", "analizar", "buscar", "explorar")):
            continue
        if len(ln) < 18:
            continue
        fallback.append(ln)
        if len(fallback) >= limit:
            break
    return fallback


def _plan_mode_summary(lines: List[str]) -> str:
    skip_prefixes = (
        "plan de",
        "estado ",
        "problemas ",
        "hallazgos",
        "resumen",
        "siguiente paso",
        "plan de corrección",
    )
    for ln in lines:
        low = ln.lower()
        if low.endswith(":") or any(low.startswith(pref) for pref in skip_prefixes):
            continue
        if low.startswith(("voy a ", "empezar", "empezaré", "empezare", "analizar", "buscar", "explorar")):
            continue
        if len(ln) < 28:
            continue
        return ln
    return ""


def _plan_mode_sections(lines: List[str]) -> Dict[str, List[str]]:
    aliases = {
        "conclusion": "conclusion",
        "conclusión": "conclusion",
        "findings": "findings",
        "hallazgos": "findings",
        "steps": "steps",
        "pasos": "steps",
        "evidence": "evidence",
        "evidencia": "evidence",
        "next": "next",
        "siguiente": "next",
    }
    out: Dict[str, List[str]] = {k: [] for k in ("conclusion", "findings", "steps", "evidence", "next")}
    current: Optional[str] = None
    for raw in lines:
        s = str(raw or "").strip()
        if not s:
            continue
        head, sep, tail = s.partition(":")
        key = aliases.get(head.strip().lower()) if sep else None
        if key:
            current = key
            if tail.strip():
                out[key].append(tail.strip())
            continue
        if current:
            out[current].append(s)
    return out


def _change_symbol_markup(kind: str, *, ascii_ui: bool) -> str:
    low = str(kind or "").strip().lower()
    if "new" in low or "create" in low or "write" in low:
        sym = "+"
        style = "ghost.success"
    elif "delete" in low or "remove" in low:
        sym = "-"
        style = "ghost.error"
    else:
        sym = "~"
        style = "ghost.accent"
    if ascii_ui:
        return f"[{style}]{sym}[/{style}]"
    return f"[{style}]{sym}[/{style}]"


def format_tool_live_hint_from_prepared(prepared_calls: List[Dict[str, Any]]) -> str:
    """Una línea corta para el spinner (primera herramienta del lote + sufijo si hay más)."""
    if not prepared_calls:
        return _ui_contract.LIVE_STATE_TOOL_GENERIC
    names = [str(pc.get("name") or "").strip() for pc in prepared_calls if pc.get("name")]
    if not names:
        return _ui_contract.LIVE_STATE_TOOL_GENERIC
    if len(names) > 1 and all(_is_readlike_tool(name) for name in names):
        reads = sum(1 for name in names if name == "read_file")
        searches = sum(1 for name in names if name == "search_code")
        listings = sum(1 for name in names if name == "ls")
        repo_maps = sum(1 for name in names if name == "summarize_repo")
        parts: List[str] = []
        if reads:
            parts.append(f"{reads} read")
        if searches:
            parts.append(f"{searches} search")
        if listings:
            parts.append(f"{listings} ls")
        if repo_maps:
            parts.append(f"{repo_maps} map")
        tail = " · ".join(parts) if parts else f"{len(names)} actions"
        return f"{_ui_contract.LIVE_ACTION_READING} workspace · {tail}"
    first = names[0]
    args = prepared_calls[0].get("args")
    if not isinstance(args, dict):
        args = {}
    path = str(args.get("path") or args.get("file") or "").strip()
    cmd = str(args.get("command") or "").strip().replace("\n", " ")
    short_p = escape(_short_tool_target(path, 50)) if path else ""
    short_c = escape(_short_tool_target(cmd, 42)) if cmd else ""
    extra = f" (+{len(names) - 1} más)" if len(names) > 1 else ""
    n = first.lower()
    if n == "read_file":
        return (
            f"{_ui_contract.LIVE_ACTION_READING} {short_p}{extra}"
            if short_p
            else f"{_ui_contract.LIVE_ACTION_READING}…{extra}"
        )
    if n == "edit_file":
        return (
            f"{_ui_contract.LIVE_ACTION_PATCHING} {short_p}{extra}"
            if short_p
            else f"{_ui_contract.LIVE_ACTION_PATCHING}…{extra}"
        )
    if n == "write_file":
        return (
            f"{_ui_contract.LIVE_ACTION_WRITING} {short_p}{extra}"
            if short_p
            else f"{_ui_contract.LIVE_ACTION_WRITING}…{extra}"
        )
    if n == "delete_file":
        return (
            f"{_ui_contract.LIVE_ACTION_DELETING} {short_p}{extra}"
            if short_p
            else f"{_ui_contract.LIVE_ACTION_DELETING}…{extra}"
        )
    if n == "run_shell":
        return (
            f"{_ui_contract.LIVE_ACTION_SHELL} {short_c}{extra}"
            if short_c
            else f"{_ui_contract.LIVE_ACTION_SHELL} comando{extra}"
        )
    if n == "ls":
        return (
            f"{_ui_contract.LIVE_ACTION_LISTING} {short_p}{extra}"
            if short_p
            else f"{_ui_contract.LIVE_ACTION_LISTING}…{extra}"
        )
    if n == "search_code":
        return _ui_contract.LIVE_ACTION_SEARCH + extra
    if n == "summarize_repo":
        return _ui_contract.LIVE_ACTION_MAP + extra
    return f"{escape(_tool_chip_label(first))}…{extra}"


def _short_tool_target(detail: str, max_len: int = 44) -> str:
    d = (detail or "").strip().replace("\n", " ")
    if len(d) <= max_len:
        return d
    return d[: max_len - 1] + "…"


def _feature_flag_visible(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return bool(raw) and raw not in ("0", "false", "off", "no")


def _artifact_readonly_intent(artifact: Dict[str, Any]) -> bool:
    spec = artifact.get("taskspec") or artifact.get("contract_spec")
    if not isinstance(spec, dict):
        return False
    intent = str(spec.get("intent") or "").strip().lower()
    ce = str(spec.get("change_expectation") or "").strip().lower()
    return intent in ("analysis", "review") or ce == "should_not_write"


def _evidence_lines_without_tier_banner(ev: Any, *, max_items: int = 8) -> List[str]:
    if not isinstance(ev, list):
        return []
    out: List[str] = []
    for x in ev[:max_items]:
        s = str(x).strip()
        if s.lower().startswith("findings tier:"):
            continue
        out.append(str(x))
    return out


def _readonly_tier_operator_copy(tier: str) -> Tuple[str, str]:
    """(badge Rich, nota breve) — /plan editorial, sin alarmismo."""
    t = (tier or "").strip().lower()
    if t == "confirmed":
        return (
            "[green]Alta · confirmada[/green]",
            "Checks ejecutados en disco; un fallo cuenta como señal dura.",
        )
    if t == "suspected":
        return (
            "[ghost.warn]Media · suspected[/ghost.warn]",
            "Lecturas amplias, sin prueba cerrada: calibra el lenguaje y confirma con cita literal o test.",
        )
    if t == "unverified":
        return (
            "[ghost.muted]Baja · sin verificar[/ghost.muted]",
            "Inspección ligera: trata afirmaciones fuertes como pendientes de evidencia.",
        )
    return ("[dim]—[/dim]", "Nivel de evidencia no clasificado.")


def _readonly_operator_review_body(
    artifact: Dict[str, Any],
    *,
    headline_raw: str,
    tier: str,
    ev_filtered: List[str],
    max_ev: int,
    max_chars: int,
) -> str:
    primary = (headline_raw or str(artifact.get("task_outcome") or "—")).strip()
    lab, expl = _readonly_tier_operator_copy(tier)
    parts: List[str] = [
        "[bold white]Conclusión[/bold white]",
        escape(primary) if primary else "—",
        "",
        "[bold white]Confianza[/bold white]",
        lab,
        f"[dim]{escape(expl)}[/dim]",
        "",
        "[bold white]Evidencia[/bold white]",
    ]
    if ev_filtered:
        for x in ev_filtered[:max_ev]:
            parts.append(f"  [ghost.muted]·[/ghost.muted] {escape(str(x)[:max_chars])}")
    else:
        parts.append("  [dim]Sin extractos de herramientas en este bloque.[/dim]")
    na = str(artifact.get("next_action") or "").strip()
    parts.extend(["", "[bold white]Siguiente paso[/bold white]"])
    if na:
        parts.append(f"  {escape(na[:420])}")
    else:
        parts.append("  [dim]Ver el .md de sesión para el detalle.[/dim]")
    return "\n".join(parts)


def _artifact_salidas_rows(artifact: Dict[str, Any]) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    rp = str(artifact.get("review_packet_path") or "").strip()
    if rp:
        rows.append((_ui_contract.ARTIFACT_SALIDA_REVIEW_PACKET, rp))
    sid = str(artifact.get("session_id") or "").strip()
    if sid and sid != "?":
        rows.append((_ui_contract.ARTIFACT_SALIDA_RESUMEN_MD, f".ghost/artifacts/{sid}.md"))
    else:
        rows.append((_ui_contract.ARTIFACT_SALIDA_CARPETA, ".ghost/artifacts/"))
    for key, label in (
        ("persisted_plan_path", _ui_contract.ARTIFACT_SALIDA_PLAN),
        ("persisted_handoff_path", _ui_contract.ARTIFACT_SALIDA_HANDOFF),
        ("delegation_envelope_path", _ui_contract.ARTIFACT_SALIDA_ENVELOPE),
    ):
        p = str(artifact.get(key) or "").strip()
        if p:
            rows.append((label, p))
    tp = str(artifact.get("trace_path") or "").strip()
    if tp:
        rows.append((_ui_contract.ARTIFACT_SALIDA_TRACE, tp))
    return rows


def _artifact_interactive_compact(artifact: Dict[str, Any]) -> bool:
    """
    Sesiones read-only sin diff: resumen compacto en consola; el detalle completo queda en .json/.md del artefacto.
    Desactivar con GHOST_ARTIFACT_INTERACTIVE_COMPACT=0.
    """
    v = os.getenv("GHOST_ARTIFACT_INTERACTIVE_COMPACT", "1").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if artifact.get("diff_summary"):
        return False
    return _artifact_readonly_intent(artifact)


def summarize_test_runner_output(blob: str, max_lines: int = 28) -> Optional[str]:
    """
    Resumen tipo suite (pytest / jest / vitest) a partir de stdout/stderr.
    Devuelve None si no reconoce el patrón (el caller usa el blob completo).
    """
    if not blob or not isinstance(blob, str):
        return None
    if len(blob) > 400_000:
        return None
    sample = blob[:12000]
    low = sample.lower()
    looks_pytest = "pytest" in low or re.search(
        r"^=+ .+ (passed|failed|error|skipped)", sample, re.I | re.M
    )
    looks_jest = bool(
        re.search(r"^(PASS|FAIL)\s+\S+\.(?:test|spec)\.(?:[jt]sx?|[cm]?js)", sample, re.I | re.M)
    )
    looks_vitest = "vitest" in low or re.search(r"vitest.*\b(pass|fail)\b", low)
    if not (looks_pytest or looks_jest or looks_vitest):
        return None

    py_node = re.compile(
        r"^(\S+(?:[/\\]\S+)*::\S+)\s+(PASSED|FAILED|SKIPPED|ERROR)(?:\s+\[([\d.]+)s\])?",
        re.I | re.M,
    )
    jv_line = re.compile(r"^(PASS|FAIL)\s+(\S+)", re.I)
    summary_line = re.compile(r"^=+\s*.+\s*=+\s*$", re.M)
    dur_tail = re.compile(
        r"(?:in\s+)(\d+(?:\.\d+)?)\s*s(?:ec(?:onds)?)?\b|\((\d+(?:\.\d+)?)\s*s\)",
        re.I,
    )

    picked: List[str] = []
    duration_s = ""
    for m in py_node.finditer(sample):
        node, st, secs = m.group(1), m.group(2).upper(), m.group(3) or ""
        frag = f"{node} · {st}"
        if secs:
            frag += f" · {secs}s"
        picked.append(frag)
    for line in sample.splitlines():
        jm = jv_line.match(line.strip())
        if jm:
            picked.append(f"{jm.group(2)} · {jm.group(1).upper()}")
        if not duration_s:
            dm = dur_tail.search(line)
            if dm:
                duration_s = dm.group(1) or dm.group(2) or ""
    for line in sample.splitlines():
        if summary_line.match(line.strip()) and re.search(r"passed|failed|error|skipped", line, re.I):
            s = line.strip()
            if s not in picked:
                picked.append(s)

    if not picked:
        return None
    header = _ui_contract.VERIFY_PYTEST_SUMMARY_HEADER_EXTRACTO
    if duration_s:
        header += f" · duración ~{duration_s}s"
    body = "\n".join(picked[:max_lines])
    if len(picked) > max_lines:
        body += f"\n… (+{len(picked) - max_lines} entradas)"
    return header + "\n" + body


def _verification_normalize_row(check: Dict[str, Any]) -> Tuple[str, str, str, bool]:
    """
    (name, display_label, style_name, executed_failed).

    executed_failed: True solo si provenance=executed y el check falló de verdad.
    """
    name = str(check.get("name") or "?")
    st = str(check.get("status") or "").lower()
    prov = str(check.get("provenance") or "")
    executed = prov == "executed"

    if not executed:
        nr = _ui_contract.VERIFY_CHECK_DISPLAY_NOT_RUN
        if st in ("passed", "success"):
            return name, nr, "yellow", False
        if st in ("not_run", "skipped") or "not_run" in prov.lower():
            return name, nr, "yellow", False
        if not st:
            return name, nr, "yellow", False
        return name, st.upper().replace("_", " "), "yellow", False

    exit_code = check.get("exit_code")
    if exit_code is None:
        exit_code = 0 if st in ("passed", "success") else 1
    try:
        exit_code_i = int(exit_code)
    except (TypeError, ValueError):
        exit_code_i = 1

    if st in ("passed", "success") and exit_code_i == 0:
        return name, "PASSED", "green", False
    if st in ("passed", "success") and exit_code_i != 0:
        return name, "FAILED", "red", True
    if st in ("failed", "error"):
        return name, "FAILED", "red", True
    return name, st.upper().replace("_", " "), "red", True


def _ghost_rule_markup(label: str, *, layout: GhostVisualLayout) -> str:
    """Regla visual — respeta ASCII / ancho vía ``GhostVisualLayout``."""
    return rule_markup(label, layout)


def _approval_purpose_for_action(name: str, detail: str, args: Any) -> str:
    if not isinstance(args, dict):
        args = {}
    n = (name or "").lower()
    path = str(args.get("path") or detail or "").strip()
    cmd = str(args.get("command") or detail or "").strip()
    if n == "run_shell":
        c = cmd.replace("\n", " ")
        return (c[:88] + "…") if len(c) > 89 else (c or "Ejecutar comando shell")
    if n == "write_file":
        return f"Crear o sobrescribir `{path or '—'}`"
    if n == "edit_file":
        return f"Modificar `{path or '—'}`"
    if n == "delete_file":
        return f"Eliminar `{path or '—'}`"
    if n == "read_file":
        return f"Leer `{path or '—'}`"
    if n == "apply_patch":
        return "Aplicar parche al repo"
    d = str(detail or "").replace("\n", " ").strip()
    if d:
        return (d[:100] + "…") if len(d) > 101 else d
    return f"Herramienta `{name or '?'}`"


def _approval_impact_for_action(name: str) -> str:
    n = (name or "").lower()
    if n == "run_shell":
        return "Shell local · mismo usuario que esta consola"
    if n in ("write_file", "edit_file", "delete_file", "apply_patch"):
        return "Cambia archivos en el workspace (revisar antes de commit)"
    if n in ("read_file", "ls", "search_code", "summarize_repo"):
        return "Solo lectura / mapa del repo"
    return "Efectos según política del gate"


def _verify_pair_sort_key(pr: Tuple[Dict[str, Any], Tuple[str, str, str, bool]]) -> Tuple[int, str]:
    c, r = pr
    name, label, _style, _xf = r
    ex = str(c.get("provenance") or "") == "executed"
    if ex and label == "FAILED":
        return (0, name)
    if ex and label == "PASSED":
        return (1, name)
    return (2, name)


def _markdown_tty_cap(text: str, *, max_lines: int) -> str:
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return text or ""
    return "\n".join(lines[:max_lines]) + "\n\n…"


def _operator_input_handle() -> str:
    try:
        u = (getpass.getuser() or "").strip()
    except Exception:
        u = ""
    if not u:
        return "you"
    return u[:14] if len(u) > 14 else u


class GhostRenderer:
    def __init__(self, console: Console):
        self.console = console
        # Some tests and utility entrypoints instantiate a plain Rich Console.
        # Push the Ghost theme here so semantic styles like ``ghost.brand`` always resolve.
        self.console.push_theme(GHOST_RICH_THEME)
        self._tool_segment_active: bool = False
        self._tool_segment_buffer: List[Dict[str, Any]] = []
        self._last_status_phase: str = "EXPLORE"
        self._last_status_state: str = "ready"
        self._tool_activity: deque = deque(maxlen=12)
        self._last_tool_flush_signature: Optional[str] = None
        self._live_rail_profile: str = _ui_contract.LIVE_RAIL_PROFILE_IMPLEMENT
        self._live_detail: Optional[str] = None
        self._last_status_render_sig: Optional[str] = None
        self._input_history: deque[str] = deque(maxlen=32)
        self._slash_recent: deque[str] = deque(maxlen=6)
        self._pt_session: Any = None
        self._composer_multiline: bool = False
        self._tool_section_live: bool = False
        self._auto_approve_shell: bool = False
        self._workspace_snapshot_cache: Dict[str, Any] = {}
        self._suppress_next_readonly_review_panel: bool = False

    def _tty_layout(self) -> GhostVisualLayout:
        w = self.console.width
        if w is None or w <= 0:
            w = default_terminal_width()
        return build_ghost_visual_layout(width=w, verbose=_ui_verbose())

    def print_header(self, product_name: str, mode: str, profile: str, model: str):
        """Cabecera compacta alineada con la marca Ghost (legacy / herramientas auxiliares)."""
        line1 = (
            f"[ghost.brand]{escape(_ui_contract.BRAND_WORDMARK.upper())}[/ghost.brand] "
            f"[dim]·[/dim] [white]{escape(product_name)}[/white]  "
            f"[ghost.dim]│[/ghost.dim]  [cyan]{escape(mode)}[/cyan]  "
            f"[ghost.dim]│[/ghost.dim]  [green]{escape(profile)}[/green]"
        )
        line2 = (
            f"[dim]modelo[/dim] [white]{escape(model)}[/white]  "
            f"[ghost.dim]│[/ghost.dim]  [dim]herramientas[/dim] [green]listas[/green]  "
            f"[ghost.dim]│[/ghost.dim]  [dim]contexto[/dim] [blue]vivo[/blue]"
        )
        self.console.print(
            Panel(
                f"{line1}\n{line2}",
                border_style="ghost.brand",
                box=ghost_box_rounded(),
                padding=(0, 1),
                expand=False,
            )
        )

    def print_tool_trace(self, name: str, target: str):
        """Log a tool execution in a professional, dimmed style."""
        self.console.print(f" [dim]⚙ tool execution: {name} {target}[/dim]")

    def print_success(self, message: str):
        self.console.print(f" [bold green]✓[/bold green] {message}")

    def print_error(self, message: str):
        self.console.print(f" [bold red]✘[/bold red] {message}")

    def render_diff(self, path: str, diff_text: str, *, max_lines: int = 48):
        """Display code changes: capped unified diff + syntax highlight (full diff en artefacto)."""
        if not (diff_text or "").strip():
            return
        ly = self._tty_layout()
        try:
            ml = int(os.getenv("GHOST_DIFF_VIEW_MAX_LINES", str(max_lines)))
        except ValueError:
            ml = max_lines
        ml = max(12, min(ml, 200))
        if ly.density == "compact":
            ml = max(12, int(ml * 0.88))
        elif ly.density == "comfortable":
            ml = min(200, int(ml * 1.08))
        if ly.ultra_narrow:
            ml = max(12, int(ml * 0.82))
        body, truncated = truncate_unified_diff(diff_text, max_lines=ml)
        title = f"Δ {path}"
        if truncated:
            title += " · recortado"
        if truncated:
            sub = f"[dim]{_ui_contract.CLOSURE_DIFF_REVIEW_CAPTION} · íntegro en artefacto .md[/dim]"
        else:
            sub = f"[dim]{_ui_contract.CLOSURE_DIFF_REVIEW_CAPTION}[/dim]"
        self.console.print(
            Panel(
                Syntax(body, "diff", theme="monokai", line_numbers=True, background_color="default"),
                title=title,
                subtitle=sub,
                border_style="ghost.accent",
                box=ghost_box_rounded(),
                padding=ly.panel_padding,
                expand=False,
            )
        )

    def render_local_inspection(self, stats: Dict[str, Any]):
        """Special UI for structural local inspection results."""
        if "error" in stats:
            self.print_error(f"Local inspection failed: {stats['error']}")
            return

        res = f"\n[bold green]✓ RESULTADO DE INSPECCIÓN LOCAL EXACTO:[/bold green]\n"
        res += f"  1) Primera línea exacta: [cyan]`{escape(stats['first_line'])}`[/cyan]\n"
        res += f"  2) Total de líneas: [bold blue]{stats['total_lines']}[/bold blue]\n"
        res += f"  3) Línea exacta encontrada: [cyan]`{escape(stats['found_line'])}`[/cyan] (Índice: {stats['found_index']})\n"
        res += f"  4) Versión detectada: [yellow]{stats['version_line']}[/yellow]\n"
        res += f"\n[dim]Nota: Datos extraídos mediante el Extractor Local (By-passing LLM).[/dim]\n"
        self.console.print(res)

    def _status_full_message(self, phase: str, state: str) -> str:
        ly = self._tty_layout()
        rail = format_live_phase_rail(
            self._display_phase_for_rail(phase, state),
            self._live_rail_profile,
            layout=ly,
        )
        detail = (self._live_detail or "").strip()
        if not detail:
            detail = _default_live_detail_line(state)
        detail_disp = truncate_visible(detail, ly.live_detail_max_chars)
        if ly.merge_status_single_line:
            one = f"{rail} [ghost.dim]·[/ghost.dim] [white]{detail_disp}[/white]"
            if _ui_verbose() and state in _STATUS_HINT and ly.show_status_hints_under_spinner:
                one += f"\n[dim]{escape(_STATUS_HINT[state])}[/dim]"
            return one
        line2 = f"[ghost.muted]·[/ghost.muted] [white]{detail_disp}[/white]"
        out = f"{rail}\n{line2}"
        if _ui_verbose() and state in _STATUS_HINT and ly.show_status_hints_under_spinner:
            out += f"\n[dim]{escape(_STATUS_HINT[state])}[/dim]"
        return out

    def _permission_display(self) -> str:
        bits = [_ui_contract.PERMISSION_READONLY]
        if self._live_rail_profile != _ui_contract.LIVE_RAIL_PROFILE_READONLY:
            bits = [_ui_contract.PERMISSION_WRITE]
            bits.append(
                _ui_contract.PERMISSION_SHELL_AUTO
                if self._auto_approve_shell
                else _ui_contract.PERMISSION_SHELL_APPROVAL
            )
        if self._tty_layout().ascii_ui:
            bits = [b.replace("↗", "^") for b in bits]
        return " ".join(bits)

    def _display_phase_for_rail(self, phase: str, state: str) -> str:
        prof = str(self._live_rail_profile or _ui_contract.LIVE_RAIL_PROFILE_IMPLEMENT).strip().lower()
        p = str(phase or "EXPLORE").strip().upper() or "EXPLORE"
        s = str(state or "").strip().lower()
        if prof == _ui_contract.LIVE_RAIL_PROFILE_READONLY:
            if p in ("CLOSING", "DONE", "ABORTED"):
                return "CLOSING"
            if p in ("ACT", "VERIFY", "REPAIR", "REVIEW"):
                return "REVIEW"
            if s in ("tool_exec", "tool_result"):
                return "GATHER"
            return "PLAN"
        if p in ("VERIFY", "REPAIR", "CLOSING", "DONE", "ABORTED"):
            return "VERIFY"
        if p == "ACT":
            return "ACT"
        if s in ("tool_exec", "tool_result"):
            return "GATHER"
        return "PLAN"

    def _resolve_git_dir(self, root: Path) -> Optional[Path]:
        dotgit = root / ".git"
        try:
            if dotgit.is_dir():
                return dotgit
            if dotgit.is_file():
                raw = dotgit.read_text(encoding="utf-8", errors="ignore").strip()
                if raw.lower().startswith("gitdir:"):
                    rel = raw.split(":", 1)[1].strip()
                    return (root / rel).resolve()
        except OSError:
            return None
        return None

    def _discover_git_branch(self, root: Path) -> str:
        git_dir = self._resolve_git_dir(root)
        if not git_dir:
            return ""
        head = git_dir / "HEAD"
        try:
            raw = head.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            return ""
        if raw.startswith("ref:"):
            ref = raw.split(":", 1)[1].strip()
            return ref.replace("refs/heads/", "")
        return truncate_visible(raw[:12], 12)

    def _count_workspace_files(self, root: Path) -> int:
        ignored_dirs = {
            ".git",
            ".ghost",
            ".next",
            ".turbo",
            ".venv",
            "node_modules",
            "__pycache__",
            "build",
            "dist",
            "coverage",
        }
        total = 0
        try:
            for _base, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in ignored_dirs]
                total += len(files)
                if total >= 9999:
                    return 9999
        except OSError:
            return 0
        return total

    def _latest_session_timestamp(self, root: Path) -> float:
        latest = 0.0
        ghost_root = root / ".ghost"
        for name in ("artifacts", "plans", "handoffs", "review_packets", "traces"):
            bucket = ghost_root / name
            if not bucket.exists():
                continue
            try:
                for child in bucket.iterdir():
                    if child.is_file():
                        latest = max(latest, child.stat().st_mtime)
            except OSError:
                continue
        return latest

    def _format_age_label(self, ts: float) -> str:
        if not ts:
            return ""
        delta = max(0, int(time.time() - ts))
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{max(1, delta // 60)}m ago"
        if delta < 86400:
            return f"{max(1, delta // 3600)}h ago"
        return f"{max(1, delta // 86400)}d ago"

    def _workspace_snapshot(self, root: Path) -> Dict[str, Any]:
        key = str(root.resolve())
        now = time.time()
        cached = self._workspace_snapshot_cache.get(key)
        if isinstance(cached, dict) and (now - float(cached.get("ts") or 0.0)) < 30.0:
            return dict(cached.get("data") or {})
        data = {
            "name": root.name or str(root),
            "branch": self._discover_git_branch(root),
            "file_count": self._count_workspace_files(root),
            "last_session": self._format_age_label(self._latest_session_timestamp(root)),
            "path": str(root),
        }
        self._workspace_snapshot_cache[key] = {"ts": now, "data": dict(data)}
        return data

    def _workspace_context(self, root: Path, ly: GhostVisualLayout) -> Tuple[str, str]:
        snap = self._workspace_snapshot(root)
        max_chars = min(max(40, ly.width - 24), 62)
        workspace_name = truncate_visible(str(snap.get("name") or root.name or root), 20)
        bits = [workspace_name]
        candidates: List[str] = []
        branch = truncate_visible(str(snap.get("branch") or ""), 22)
        if branch:
            candidates.append(f"branch {branch}")
        last_session = str(snap.get("last_session") or "").strip()
        if last_session:
            candidates.append(f"last {last_session}")
        file_count = int(snap.get("file_count") or 0)
        if file_count > 0:
            candidates.append(f"{file_count} files")
        for piece in candidates:
            proposal = " · ".join(bits + [piece])
            if len(proposal) <= max_chars or len(bits) == 1:
                bits.append(piece)
        context_line = " · ".join(bits)
        return context_line, workspace_name

    def _ghost_prompt_plain(self) -> str:
        mark = ">" if self._tty_layout().ascii_ui else "›"
        return f"{_ui_contract.PROMPT_HANDLE} {mark} "

    def _ghost_prompt_markup(self) -> str:
        mark = ">" if self._tty_layout().ascii_ui else "›"
        return f"[ghost.brand]{escape(_ui_contract.PROMPT_HANDLE)}[/ghost.brand] [ghost.dim]{mark}[/ghost.dim] "

    def session_status(
        self,
        phase: str,
        *,
        initial: str = "thinking",
        rail_profile: str = _ui_contract.LIVE_RAIL_PROFILE_IMPLEMENT,
    ):
        """
        Spinner con rail vivo (implementación o /plan) + línea de acción.
        Use: ``with renderer.session_status(phase, rail_profile=...) as status:``
        """
        rp = str(rail_profile or _ui_contract.LIVE_RAIL_PROFILE_IMPLEMENT).strip().lower()
        if rp not in (
            _ui_contract.LIVE_RAIL_PROFILE_READONLY,
            _ui_contract.LIVE_RAIL_PROFILE_IMPLEMENT,
        ):
            rp = _ui_contract.LIVE_RAIL_PROFILE_IMPLEMENT
        self._live_rail_profile = rp
        self._last_status_render_sig = None
        self._live_detail = None
        self._last_status_phase = str(phase or "EXPLORE").strip().upper() or "EXPLORE"
        self._last_status_state = str(initial or "thinking").strip().lower() or "thinking"
        return self.console.status(
            self._status_full_message(self._last_status_phase, initial),
            spinner=_status_spinner_name(),
            spinner_style="ghost.accent",
            refresh_per_second=14,
        )

    def status_context(self, initial_state: str = "thinking"):
        """Context manager for professional status management (legacy API)."""
        return self.session_status(
            self._last_status_phase,
            initial=initial_state,
            rail_profile=self._live_rail_profile,
        )

    def update_status(
        self,
        status_obj,
        state: str,
        *,
        phase: Optional[str] = None,
        live_detail: Any = _UNSET,
        rail_profile: Any = _UNSET,
    ) -> None:
        """Actualiza el spinner; evita ``update`` redundante si el mensaje no cambió."""
        if rail_profile is not _UNSET:
            rp = str(rail_profile or "").strip().lower()
            if rp in (
                _ui_contract.LIVE_RAIL_PROFILE_READONLY,
                _ui_contract.LIVE_RAIL_PROFILE_IMPLEMENT,
            ):
                self._live_rail_profile = rp
        if phase is not None:
            new_ph = str(phase).strip().upper() or self._last_status_phase
            if new_ph != self._last_status_phase:
                self._last_status_render_sig = None
            self._last_status_phase = new_ph
        self._last_status_state = str(state or "working").strip().lower() or "working"
        if live_detail is not _UNSET:
            self._live_detail = (str(live_detail).strip() or None) if live_detail else None
        elif state in ("building", "thinking", "closing", "waiting", "tool_result", "ready"):
            self._live_detail = None
        if status_obj is None:
            return
        msg = self._status_full_message(self._last_status_phase, state)
        if msg == self._last_status_render_sig:
            return
        self._last_status_render_sig = msg
        try:
            status_obj.update(msg)
        except Exception:
            pass

    def _format_tool_chip_row(self, row: Dict[str, Any], ly: GhostVisualLayout) -> str:
        label = _tool_chip_label(str(row.get("name") or "?"))
        tgt = escape(_short_tool_target(str(row.get("detail") or ""), ly.tool_detail_max_chars))
        st = str(row.get("estado") or "")
        if st == "ok":
            st_s = "[ghost.success]ok[/ghost.success]" if not ly.ascii_ui else "[ghost.success]+[/ghost.success]"
        elif st == "error":
            st_s = "[ghost.error]fail[/ghost.error]" if not ly.ascii_ui else "[ghost.error]x[/ghost.error]"
        elif st == "pending":
            st_s = "[ghost.warn]…[/ghost.warn]" if not ly.ascii_ui else "[ghost.warn]..[/ghost.warn]"
        elif st == "auto":
            st_s = "[dim]auto[/dim]"
        else:
            st_s = f"[dim]{escape(st)}[/dim]"
        dur = row.get("duration_ms")
        dur_s = ""
        if dur is not None:
            try:
                ms = float(dur)
                if ms >= 1000:
                    dur_s = f" [dim]{ms/1000.0:.1f}s[/dim]"
                else:
                    dur_s = f" [dim]{ms:.0f}ms[/dim]"
            except (TypeError, ValueError):
                dur_s = ""
        ap = row.get("auto_approved")
        tag = ""
        if ap is True:
            tag = " [dim]· auto[/dim]"
        elif ap is False:
            tag = " [dim]· aprobación[/dim]"
        prefix = tool_chip_prefix(ly)
        return f"{prefix}[ghost.accent]{escape(label)}[/ghost.accent]  {tgt}  {st_s}{dur_s}{tag}"

    def render_mode_change(self, new_mode: str):
        """Display a banner for mode transitions."""
        self.console.print(f"\n[bold green]✓ Modo cambiado a: {new_mode}[/bold green]")

    def render_help(self):
        """Comandos esenciales — densidad alta, sin ruido."""
        help_table = Table(
            title=f"[ghost.brand]{escape(_ui_contract.BRAND_WORDMARK)}[/ghost.brand] [dim]· comandos[/dim]",
            box=box.SIMPLE_HEAD,
            header_style="ghost.accent",
            show_lines=False,
            padding=(0, 1),
        )
        help_table.add_column("Comando", style="cyan", no_wrap=True)
        help_table.add_column("Qué hace", style="white")

        help_table.add_row("/plan …", "Estrategia y análisis en solo lectura")
        help_table.add_row("/do …", "Implementación autónoma con verify y artefactos")
        help_table.add_row("/edit …", "Parche directo sobre un archivo")
        help_table.add_row("/mode …", "Chat · Plan · Execute · Review · Fix")
        help_table.add_row("/debug", "Traza diagnóstica on/off")
        help_table.add_row("/clear", "Limpia el hilo de conversación")
        help_table.add_row("/help", "Esta referencia")

        self.console.print("")
        self.console.print(help_table)
        self.console.print("[dim]Salir:[/dim] [white]exit[/white] [dim]o[/dim] [white]quit[/white]\n")

    def render_task_list(self, tasks: List[Dict[str, Any]]):
        """Display a table of persistent tasks."""
        if not tasks:
            self.print_error("No persistent tasks found.")
            return

        table = Table(title="👻 Ghost Persistent Tasks", box=box.SIMPLE, header_style="bold magenta")
        table.add_column("Task ID", style="cyan")
        table.add_column("Title", style="white")
        table.add_column("Status", style="bold")
        table.add_column("Updated At", style="dim")

        for t in tasks:
            status = t["status"]
            style = "green" if status == "done" else "red" if status == "failed" else "blue"
            table.add_row(
                t["id"],
                t["title"][:50] + "..." if len(t["title"]) > 50 else t["title"],
                f"[{style}]{status.upper()}[/{style}]",
                t["updated_at"],
            )

        self.console.print("\n")
        self.console.print(table)
        self.console.print("[dim]Use '/resume <id>' to continue a specific task.[/dim]\n")

    def render_swarm_board(self, workers: List[Any]):
        """Display a professional dashboard for active swarm workers."""
        active = sum(1 for w in workers if getattr(w, "status", "") == "active")
        terminated = sum(1 for w in workers if getattr(w, "status", "") == "terminated")
        table = Table(
            title=(
                f"[ghost.brand]SWARM[/ghost.brand] [ghost.dim]·[/ghost.dim] "
                f"[white]{active} active[/white]"
                + (f" [ghost.dim]·[/ghost.dim] [dim]{terminated} terminated[/dim]" if terminated else "")
            ),
            box=box.SIMPLE_HEAVY,
            header_style="bold white",
        )
        table.add_column("Worker", style="cyan")
        table.add_column("Role", style="magenta")
        table.add_column("Task", style="dim")
        table.add_column("Status", style="bold")
        table.add_column("Worktree", style="dim")

        for w in workers:
            status = getattr(w, "status", "unknown")
            style = "green" if status == "active" else "red" if status == "terminated" else "white"
            icon = "●" if status == "active" else "○"
            worktree_name = ""
            raw_worktree = str(getattr(w, "worktree_path", "") or "").strip()
            if raw_worktree:
                try:
                    worktree_name = Path(raw_worktree).name or raw_worktree
                except Exception:
                    worktree_name = raw_worktree
            table.add_row(
                w.worker_id,
                w.role.upper(),
                w.task_id,
                f"[{style}]{icon} {status}[/{style}]",
                worktree_name or "N/A",
            )

        if not workers:
            self.console.print("\n[dim]SWARM · no active workers.[/dim]\n")
        else:
            self.console.print("\n")
            self.console.print(table)
            self.console.print("[dim]Use /board to refresh this view or /swarm cleanup to prune terminated workers.[/dim]\n")

    def read_input(self) -> str:
        """Prompt del operador — identidad Ghost, sin nombres hardcodeados."""
        if self._supports_prompt_toolkit_input():
            try:
                return self._read_input_prompt_toolkit().strip()
            except (KeyboardInterrupt, EOFError):
                return "exit"
        if self._supports_windows_slash_menu():
            try:
                return self._read_input_windows_slash_menu().strip()
            except (KeyboardInterrupt, EOFError):
                return "exit"
        try:
            return self.console.input(self._ghost_prompt_markup()).strip()
        except (KeyboardInterrupt, EOFError):
            return "exit"

    def _supports_prompt_toolkit_input(self) -> bool:
        if os.getenv("GHOST_PROMPT_TOOLKIT", "1").strip().lower() in ("0", "false", "off", "no"):
            return False
        out = getattr(sys, "stdout", None)
        if not bool(getattr(out, "isatty", lambda: False)()):
            return False
        try:
            import prompt_toolkit  # noqa: F401
        except Exception:
            return False
        return True

    def _read_input_prompt_toolkit(self) -> str:
        session = self._get_prompt_toolkit_session()
        prompt = self._ghost_prompt_plain()
        result = session.prompt(
            prompt,
            bottom_toolbar=self._prompt_toolkit_bottom_toolbar,
        )
        self._record_input_history(result)
        stripped = str(result or "").strip()
        if stripped.startswith("/"):
            first = stripped.split(" ", 1)[0]
            self._record_recent_slash(first)
        return result

    def _get_prompt_toolkit_session(self) -> Any:
        if self._pt_session is not None:
            return self._pt_session
        from prompt_toolkit import PromptSession
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.shortcuts.prompt import CompleteStyle
        from prompt_toolkit.styles import Style

        history = InMemoryHistory()
        for entry in list(self._input_history):
            history.append_string(entry)
        completer = WordCompleter(
            [
                item.command
                for item in slash_menu_query_items(
                    "",
                    recent_commands=list(self._slash_recent),
                    recommended_commands=self._recommended_actions(),
                )
            ],
            ignore_case=True,
            sentence=True,
        )
        kb = KeyBindings()

        @Condition
        def _multiline_mode() -> bool:
            return self._composer_multiline

        @kb.add("f2")
        def _toggle_multiline(event) -> None:
            self._composer_multiline = not self._composer_multiline

        @kb.add("enter", filter=~_multiline_mode)
        def _accept_singleline(event) -> None:
            event.current_buffer.validate_and_handle()

        @kb.add("escape", "enter")
        def _insert_newline(event) -> None:
            event.current_buffer.insert_text("\n")

        style = Style.from_dict(
            {
                "bottom-toolbar": "bg:#0F172A #94A3B8",
                "completion-menu": "bg:#111827 #E5E7EB",
                "completion-menu.completion.current": "bg:#7C3AED #FFFFFF bold",
                "completion-menu.meta.completion.current": "bg:#7C3AED #E9D5FF",
                "scrollbar.background": "bg:#111827",
                "scrollbar.button": "bg:#475569",
            }
        )
        self._pt_session = PromptSession(
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer,
            complete_while_typing=True,
            complete_style=CompleteStyle.MULTI_COLUMN,
            reserve_space_for_menu=8,
            enable_history_search=True,
            multiline=_multiline_mode,
            key_bindings=kb,
            style=style,
        )
        return self._pt_session

    def _prompt_toolkit_bottom_toolbar(self) -> str:
        recent = list(self._slash_recent)[:2]
        recent_text = ""
        if recent:
            recent_text = "  ·  recientes " + " ".join(recent)
        rec = " ".join(self._recommended_actions()[:2])
        mode_label = "plan mode" if self._live_rail_profile == _ui_contract.LIVE_RAIL_PROFILE_READONLY else "dev mode"
        multiline = "multiline on" if self._composer_multiline else "multiline off"
        perms = self._permission_display()
        return f"{mode_label}  ·  {perms}  ·  next {rec}  ·  F2 {multiline}{recent_text}"

    def _supports_windows_slash_menu(self) -> bool:
        if os.name != "nt":
            return False
        if os.getenv("GHOST_INPUT_MENU", "1").strip().lower() in ("0", "false", "off", "no"):
            return False
        out = getattr(sys, "stdout", None)
        return bool(getattr(out, "isatty", lambda: False)())

    def _read_input_windows_slash_menu(self) -> str:
        import msvcrt

        ly = self._tty_layout()
        ascii_ui = ly.ascii_ui
        prompt_prefix = self._ghost_prompt_plain()
        buffer = ""
        cursor = 0
        selected = 0
        history_index: Optional[int] = None
        menu_lines = 0
        out = getattr(self.console, "file", sys.stdout)

        def render() -> None:
            nonlocal menu_lines, selected
            state = slash_menu_state(
                buffer,
                selected=selected,
                recent_commands=list(self._slash_recent),
                recommended_commands=self._recommended_actions(),
            )
            selected = state.selected
            lines = self._format_slash_menu_lines(state, width=max(ly.width, 40), ascii_ui=ascii_ui)
            menu_lines = len(lines)
            out.write("\r\x1b[2K\x1b[J")
            out.write(prompt_prefix + buffer + "\n")
            for line in lines:
                out.write(line + "\n")
            if menu_lines:
                out.write(f"\x1b[{menu_lines}A")
            out.write("\r")
            prompt_cols = len(prompt_prefix) + cursor
            if prompt_cols > 0:
                out.write(f"\x1b[{prompt_cols}C")
            out.flush()

        render()
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                state = slash_menu_state(
                    buffer,
                    selected=selected,
                    recent_commands=list(self._slash_recent),
                    recommended_commands=self._recommended_actions(),
                )
                if state.active and state.items:
                    chosen = state.items[selected]
                    stripped = buffer.lstrip()
                    if stripped.startswith("/") and " " not in stripped:
                        buffer = slash_menu_apply_selection(buffer, chosen)
                        self._record_recent_slash(chosen.command)
                        cursor = len(buffer)
                        history_index = None
                        render()
                        continue
                out.write("\r\x1b[2K\x1b[J")
                out.write(prompt_prefix + buffer + "\n")
                out.flush()
                self._record_input_history(buffer)
                return buffer
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x08":
                if cursor > 0:
                    buffer = buffer[: cursor - 1] + buffer[cursor:]
                    cursor -= 1
                history_index = None
                render()
                continue
            if ch in ("\x00", "\xe0"):
                key = msvcrt.getwch()
                state = slash_menu_state(
                    buffer,
                    selected=selected,
                    recent_commands=list(self._slash_recent),
                    recommended_commands=self._recommended_actions(),
                )
                if key == "H" and state.items:
                    selected = (selected - 1) % len(state.items)
                elif key == "H":
                    history_index, buffer = self._move_history(history_index, -1)
                    cursor = len(buffer)
                elif key == "P" and state.items:
                    selected = (selected + 1) % len(state.items)
                elif key == "P":
                    history_index, buffer = self._move_history(history_index, 1)
                    cursor = len(buffer)
                elif key == "K" and cursor > 0:
                    cursor -= 1
                elif key == "M" and cursor < len(buffer):
                    cursor += 1
                elif key == "G":
                    cursor = 0
                elif key == "O":
                    cursor = len(buffer)
                elif key == "S" and cursor < len(buffer):
                    buffer = buffer[:cursor] + buffer[cursor + 1 :]
                if key not in ("H", "P"):
                    history_index = None
                render()
                continue
            if ch == "\x1b":
                selected = 0
                if buffer.startswith("/"):
                    buffer = ""
                    cursor = 0
                history_index = None
                render()
                continue
            if ch == "\t":
                state = slash_menu_state(
                    buffer,
                    selected=selected,
                    recent_commands=list(self._slash_recent),
                    recommended_commands=self._recommended_actions(),
                )
                if state.active and state.items:
                    buffer = slash_menu_apply_selection(buffer, state.items[selected])
                    self._record_recent_slash(state.items[selected].command)
                    cursor = len(buffer)
                history_index = None
                render()
                continue
            if ch and ch >= " ":
                buffer = buffer[:cursor] + ch + buffer[cursor:]
                cursor += 1
                selected = 0
                history_index = None
                render()

    def _format_slash_menu_lines(
        self,
        state: SlashMenuState,
        *,
        width: int,
        ascii_ui: bool,
    ) -> List[str]:
        if not state.active:
            return [truncate_visible(f"  {self._input_context_hint('', ascii_ui=ascii_ui)}", max(width - 2, 24))]
        hint = "Type / to browse commands" if ascii_ui else "Escribe / para ver acciones"
        if not state.items:
            return [truncate_visible(f"  {hint}", max(width - 2, 20))]
        lines: List[str] = []
        footer = (
            "up/down move · Enter or Tab choose · Esc close"
            if ascii_ui
            else "↑↓ mover · Enter o Tab completa · Esc cierra"
        )
        for idx, item in enumerate(state.items[:6]):
            mark = ">" if ascii_ui else "›"
            prefix = f"{mark} " if idx == state.selected else "  "
            group = slash_menu_group_label(item)
            cmd = item.command.ljust(12)
            if idx > 0 and state.items[idx - 1].category != item.category:
                lines.append(truncate_visible(f"  {group.lower()}:", max(width - 2, 24)))
            line = f"{prefix}{cmd} [{group}] {item.label} · {item.hint}"
            lines.append(truncate_visible(line, max(width - 2, 24)))
        examples = slash_menu_examples(state.items, limit=2)
        for ex in examples:
            lines.append(truncate_visible(f"    ejemplo: {ex}", max(width - 2, 24)))
        lines.append(truncate_visible(f"  {footer}", max(width - 2, 24)))
        return lines

    def _record_input_history(self, text: str) -> None:
        value = str(text or "").strip()
        if not value:
            return
        if self._input_history and self._input_history[-1] == value:
            return
        self._input_history.append(value)
        hist = getattr(self._pt_session, "history", None)
        if hist is not None:
            try:
                hist.append_string(value)
            except Exception:
                pass

    def _record_recent_slash(self, command: str) -> None:
        value = str(command or "").strip()
        if not value.startswith("/"):
            return
        while value in self._slash_recent:
            self._slash_recent.remove(value)
        self._slash_recent.appendleft(value)
        self._pt_session = None

    def _move_history(self, history_index: Optional[int], delta: int) -> Tuple[Optional[int], str]:
        if not self._input_history:
            return None, ""
        entries = list(self._input_history)
        if history_index is None:
            next_index = len(entries) - 1 if delta < 0 else None
        else:
            next_index = history_index + delta
            if next_index >= len(entries):
                return None, ""
        if next_index is None:
            return None, ""
        next_index = max(0, min(next_index, len(entries) - 1))
        return next_index, entries[next_index]

    def _input_context_hint(self, buffer: str, *, ascii_ui: bool) -> str:
        if (buffer or "").startswith("/"):
            return "Tab or Enter complete current action" if ascii_ui else "Tab o Enter completa la accion actual"
        return "Use / for actions or write a task directly" if ascii_ui else "Usa / para acciones o escribe una tarea directa"

    def _recommended_actions(self) -> List[str]:
        prof = str(self._live_rail_profile or _ui_contract.LIVE_RAIL_PROFILE_IMPLEMENT).strip().lower()
        phase = str(self._last_status_phase or "EXPLORE").strip().upper()
        if prof == _ui_contract.LIVE_RAIL_PROFILE_READONLY:
            if phase in ("CLOSE", "CLOSING", "DONE", "ABORTED"):
                return ["/review", "/do", "/doctor"]
            return ["/plan", "/do", "/doctor"]
        if phase == "VERIFY":
            return ["/fix", "/review", "/do"]
        if phase == "ACT":
            return ["/do", "/edit", "/fix"]
        if phase in ("CLOSE", "CLOSING", "DONE", "ABORTED"):
            return ["/review", "/do", "/logs"]
        return ["/plan", "/do", "/fix"]

    def _confidence_dots_markup(self, tier: str) -> str:
        t = str(tier or "").strip().lower()
        if self._tty_layout().ascii_ui:
            filled_char, empty_char = "*", "."
        else:
            filled_char, empty_char = "●", "○"
        if t == "confirmed":
            filled, color, label = 5, "ghost.success", "high"
        elif t == "suspected":
            filled, color, label = 4, "ghost.brand", "medium"
        else:
            filled, color, label = 2, "ghost.warn", "low"
        return f"[{color}]{filled_char * filled}[/{color}][dim]{empty_char * (5 - filled)}[/dim] {label}"

    def render_readonly_plan_response(
        self,
        text: str,
        *,
        tier: str = "",
        evidence_count: int = 0,
        next_command: str = "",
    ) -> None:
        ly = self._tty_layout()
        lines = _plan_mode_lines(text)
        sections = _plan_mode_sections(lines)
        summary = " ".join(sections.get("conclusion") or []).strip() or _plan_mode_summary(lines)
        findings = list(sections.get("findings") or [])
        steps = list(sections.get("steps") or [])
        evidence_lines = list(sections.get("evidence") or [])
        if not steps:
            steps = _plan_mode_steps(lines, limit=6)
        if summary and steps and steps[0].strip().lower() == summary.strip().lower():
            steps = steps[1:]
        if not summary:
            t = str(tier or "").strip().lower()
            if t == "confirmed":
                summary = "Exploracion suficiente para ejecutar el siguiente cambio sin ampliar el scope."
            elif t == "suspected":
                summary = "Exploracion suficiente para proponer un plan; confirma los hallazgos fuertes con checks antes de editar."
            else:
                summary = "Hay contexto para orientar el siguiente paso, pero la evidencia sigue ligera y debe verificarse."
        body_lines: List[str] = []
        body_lines.append("[bold white]conclusion[/bold white]")
        body_lines.append(f"{escape(truncate_visible(summary, max(72, ly.width - 16)))}")
        if findings:
            body_lines.extend(["", "[bold white]findings[/bold white]"])
            for idx, finding in enumerate(findings[:5], 1):
                body_lines.append(f"{idx}. {escape(truncate_visible(finding, max(68, ly.width - 14)))}")
        if steps:
            body_lines.extend(["", "[bold white]steps[/bold white]"])
            for idx, step in enumerate(steps, 1):
                body_lines.append(f"{idx}. {escape(truncate_visible(step, max(68, ly.width - 14)))}")
        body_lines.extend(["", "[bold white]confidence[/bold white]", self._confidence_dots_markup(tier)])
        if evidence_lines or evidence_count > 0:
            body_lines.extend(["", "[bold white]evidence[/bold white]"])
            for ev in evidence_lines[:3]:
                body_lines.append(f"[dim]{escape(truncate_visible(ev, max(68, ly.width - 14)))}[/dim]")
            if evidence_count > 0:
                body_lines.append(f"[dim]{evidence_count} lecturas relevantes en esta sesion[/dim]")
        if next_command:
            body_lines.extend(["", "[bold white]execute[/bold white]", f"[ghost.accent]{escape(next_command)}[/ghost.accent]"])
        self.console.print("")
        self.console.print(
            Panel(
                "\n".join(body_lines),
                title=f"[ghost.brand]{escape(_ui_contract.PANEL_TITLE_PLAN_MODE)}[/ghost.brand]",
                subtitle=f"[dim]{escape(_ui_contract.PANEL_SUBTITLE_PLAN_MODE)}[/dim]",
                border_style="ghost.brand",
                box=ghost_box_rounded(),
                expand=False,
                padding=ly.panel_padding,
            )
        )

    def render_verification_results(
        self,
        results: Dict[str, Any],
        *,
        contract_spec: Optional[Dict[str, Any]] = None,
    ):
        """
        Verification summary with provenance-aware labels.

        No muestra ALL SYSTEMS CLEAR si ningún check tuvo provenance=executed.
        Fallos ejecutados primero; salida de error antes de la tabla en modo operador.
        """
        if results.get("status") == "skipped":
            return

        checks_raw: List[Dict[str, Any]] = list(results.get("checks") or [])
        if not checks_raw:
            return

        verbose = _ui_verbose()
        ly = self._tty_layout()
        pairs = list(zip(checks_raw, [_verification_normalize_row(c) for c in checks_raw]))
        pairs.sort(key=_verify_pair_sort_key)
        checks = [p[0] for p in pairs]
        rows = [p[1] for p in pairs]

        any_executed_failed = any(r[3] for r in rows)
        any_executed = any(str(c.get("provenance") or "") == "executed" for c in checks)

        steps_executed = results.get("steps_executed_count")
        if steps_executed is None:
            steps_executed = sum(1 for c in checks if str(c.get("provenance") or "") == "executed")

        global_failed = str(results.get("status") or "").lower() == "failed" or any_executed_failed

        dur_ms = results.get("total_duration_ms") or results.get("duration_ms")
        dur_s = ""
        if dur_ms is not None:
            try:
                dur_s = f" · {float(dur_ms) / 1000.0:.1f}s"
            except (TypeError, ValueError):
                dur_s = ""

        status_lower = str(results.get("status") or "").lower()
        show_stderr_for: List[Dict[str, Any]] = [
            c
            for c in checks
            if str(c.get("provenance") or "") == "executed"
            and str(c.get("status") or "").lower() not in ("passed", "success")
        ]

        self.console.print("")
        self.console.print(_ghost_rule_markup(_ui_contract.RULE_LABEL_VERIFY, layout=ly))
        if verbose:
            self.console.print("[bold dim]1 · plan (contrato)[/bold dim]")
            self.console.print(
                escape(verification_plan_summary_from_spec(contract_spec if isinstance(contract_spec, dict) else None))
            )
            self.console.print(f"\n[bold dim]2 · ejecución{escape(dur_s)}[/bold dim]")
        else:
            self.console.print(
                f"[ghost.brand]{escape(_ui_contract.LABEL_VERIFY_LINE)}[/ghost.brand][dim]{escape(dur_s)}[/dim]"
            )
            plan_one = verification_plan_summary_from_spec(
                contract_spec if isinstance(contract_spec, dict) else None
            )
            if plan_one:
                self.console.print(
                    f"[dim]{escape(truncate_visible(plan_one, ly.verify_plan_one_max_chars))}[/dim]"
                )

        cwds = {str(c.get("cwd") or ".") for c in checks}
        show_cwd_col = verbose or len(cwds) > 1

        if show_stderr_for:
            names = ", ".join(str(c.get("name") or "?") for c in show_stderr_for)
            alert_title = "Verify · fallos en disco" if not verbose else "Verify · detalle de fallos (auditoría)"
            self.console.print(
                Panel(
                    f"[bold red]{len(show_stderr_for)} check(s) con error en disco[/bold red]\n[white]{escape(names)}[/white]",
                    title=alert_title,
                    border_style="red",
                    box=ghost_box_rounded(),
                    expand=False,
                    padding=ly.panel_padding,
                )
            )
            for check in show_stderr_for:
                cwd = str(check.get("cwd") or ".")
                cause = str(check.get("cause") or "")
                header = f"[red]{check.get('name', '?')}[/red] [dim]({cwd}"
                if cause:
                    header += f" · {cause}"
                header += ")[/dim]"
                self.console.print(header)
                blob_raw = check.get("stderr") or check.get("stdout") or "No output"
                blob_str = str(blob_raw)
                suite_blob = summarize_test_runner_output(blob_str)
                blob = suite_blob if suite_blob else blob_str
                max_lines = ly.verify_stderr_lines if not verbose else ly.verify_stderr_lines_verbose
                blob = strip_coverage_tail(blob, max_lines=max_lines)
                self.console.print(
                    Panel(blob, style="dim red", expand=False, padding=ly.panel_padding)
                )

        if verbose:
            self.console.print("\n[bold dim]3 · detalle de checks[/bold dim]")

        table = Table(
            box=box.SIMPLE_HEAD if not verbose else box.MINIMAL,
            show_header=True,
            header_style="bold magenta" if verbose else "dim",
            padding=(0, 1) if not verbose else (0, 2),
        )
        table.add_column("Check", style="cyan")
        if show_cwd_col:
            table.add_column("cwd", style="dim")
        table.add_column("Estado", style="bold")

        for check, (_name, label, style, _xf) in zip(checks, rows):
            icon = "✓" if label == "PASSED" else "✘"
            cwd_cell = str(check.get("cwd") or ".")
            if show_cwd_col and not verbose and len(cwd_cell) > 28:
                cwd_cell = cwd_cell[:25] + "…"
            estado_cell = f"[{style}]{icon} {label}[/{style}]"
            if show_cwd_col:
                table.add_row(_name, cwd_cell, estado_cell)
            else:
                table.add_row(_name, estado_cell)

        self.console.print("")
        if verbose:
            panel_title = f"{_ui_contract.BRAND_WORDMARK} · matriz de checks"
            self.console.print(
                Panel(
                    table,
                    title=panel_title,
                    border_style="ghost.brand",
                    box=ghost_box_rounded(),
                    expand=False,
                    padding=ly.panel_padding,
                )
            )
        else:
            self.console.print(table)

        if steps_executed == 0 and not any_executed and status_lower == "incomplete":
            self.console.print(
                "[bold yellow]Ningún check se ejecutó en disco[/bold yellow] "
                + ("[dim](steps_executed_count=0)[/dim]\n" if verbose else "\n")
            )

        can_all_systems_clear = (
            not global_failed
            and any_executed
            and not (steps_executed == 0 and not any_executed and status_lower == "incomplete")
        )

        if verbose:
            self.console.print("\n[bold dim]4 · resultado[/bold dim]")
        else:
            self.console.print(_ghost_rule_markup(_ui_contract.RULE_LABEL_RESULTADO, layout=ly))
        if global_failed:
            self.console.print(f"[bold red]{escape(_ui_contract.RESULTADO_LINE_FAILED)}[/bold red]")
        elif can_all_systems_clear:
            self.console.print(f"[bold green]{escape(_ui_contract.RESULTADO_LINE_OK)}[/bold green]")
        else:
            self.console.print(f"[bold yellow]{escape(_ui_contract.RESULTADO_LINE_INCOMPLETO)}[/bold yellow]")

        if can_all_systems_clear:
            cp = _ui_contract.VERIFY_MSG_CHECKS_PASSED
            asc = _ui_contract.VERIFY_MSG_ALL_SYSTEMS_CLEAR
            self.console.print(
                f"[bold green]✓ {escape(cp)}[/bold green]\n"
                if not verbose
                else f" [bold green]✓ {escape(asc)}[/bold green]\n"
            )
            if not verbose:
                self.console.print("[dim]next /review to inspect diffs · /approve to commit[/dim]")
        elif global_failed and not verbose:
            self.console.print("[dim]next /fix to repair the failing checks[/dim]")
        elif not verbose:
            self.console.print("[dim]next rerun VERIFY with at least one executed check[/dim]")

    def _section_kicker(self, title: str, *, verbose: bool = False) -> None:
        """Título de bloque: verbose = auditoría; operador = una línea con viñeta Ghost."""
        ly = self._tty_layout()
        for _ in range(1 + ly.section_extra_blanks):
            self.console.print("")
        if verbose:
            self.console.print(f"[bold cyan]{escape(title)}[/bold cyan]")
        else:
            self.console.print(f"  [ghost.muted]·[/ghost.muted] [bold white]{escape(title)}[/bold white]")

    def _render_salidas_generadas(self, rows: List[Tuple[str, str]], *, verbose: bool) -> None:
        if not rows:
            return
        ly = self._tty_layout()
        mark = artifact_line_marker(ly)
        if not verbose:
            paths_raw = [p for _lab, p in rows]
            lines_out = salidas_paths_lines_split(paths_raw, ly)
            head = (
                f"[ghost.brand]{mark}[/ghost.brand] [dim]{escape(_ui_contract.ARTIFACT_LINE_MARKER)}[/dim]  "
                f"[cyan]{lines_out[0]}[/cyan]"
            )
            self.console.print(head)
            cont = "  [dim]+[/dim] " if ly.ascii_ui else "  [dim]·[/dim] "
            for extra in lines_out[1:]:
                self.console.print(f"{cont}[cyan]{extra}[/cyan]")
            return
        inner = "\n".join(
            f"  [bold]{escape(lab)}[/bold]  [dim]·[/dim]  [cyan]{escape(p)}[/cyan]" for lab, p in rows
        )
        self.console.print(
            Panel(
                inner,
                title=f"{_ui_contract.BRAND_WORDMARK} · salidas persistidas",
                subtitle="Continuidad — mismo contenido en .md / JSON del artefacto",
                border_style="ghost.brand",
                box=ghost_box_rounded(),
                expand=False,
                padding=ly.panel_padding,
            )
        )

    def _render_review_packet_console(
        self, artifact: Dict[str, Any], *, readonly: bool, verbose: bool
    ) -> bool:
        """
        Review packet en consola (markdown derivado del JSON). True si pintó UI
        que ya incluye estado de review (evita duplicar líneas abajo).
        """
        ly = self._tty_layout()
        rp = str(artifact.get("review_packet_path") or "").strip()
        prev = str(artifact.get("review_packet_pr_body_preview") or "").strip()
        if readonly or not rp:
            return False
        rfr = artifact.get("review_ready_for_review")
        rdetail = str(artifact.get("review_readiness_detail_es") or "").strip()
        title = f"[ghost.brand]{escape(_ui_contract.PANEL_TITLE_REVIEW_PACKET_TTY)}[/ghost.brand]"
        sub = (
            _ui_contract.PANEL_SUBTITLE_REVIEW_PACKET_TTY
            if not ly.ultra_narrow
            else "[dim]JSON en .ghost/review_packets/[/dim]"
        )
        pad = ly.panel_padding

        if verbose:
            if prev:
                self.console.print("")
                if ly.review_use_plain_fallback:
                    plain = review_packet_plain_fallback(
                        prev, max_lines=max(ly.review_plain_max_lines, ly.review_md_lines_verbose // 2)
                    )
                    self.console.print(
                        Panel(
                            escape(plain),
                            title=f"{title} [dim]· auditoría[/dim]",
                            subtitle=sub,
                            border_style="ghost.accent",
                            box=ghost_box_rounded(),
                            padding=pad,
                            expand=False,
                        )
                    )
                else:
                    self.console.print(
                        Panel(
                            Markdown(_markdown_tty_cap(prev, max_lines=ly.review_md_lines_verbose)),
                            title=f"{title} [dim]· auditoría[/dim]",
                            subtitle=sub,
                            border_style="ghost.accent",
                            box=ghost_box_rounded(),
                            padding=pad,
                            expand=False,
                        )
                    )
                return True
            self.console.print("")
            self.console.print(
                Panel(
                    escape(rp),
                    title=title,
                    subtitle=sub,
                    border_style="ghost.accent",
                    box=ghost_box_rounded(),
                    padding=pad,
                    expand=False,
                )
            )
            return True

        if prev:
            self.console.print("")
            if ly.review_use_plain_fallback:
                plain = review_packet_plain_fallback(prev, max_lines=ly.review_plain_max_lines)
                sub_body = f"[dim]{escape(rp)}[/dim]\n{escape(plain)}"
                self.console.print(
                    Panel(
                        sub_body,
                        title=title,
                        border_style="ghost.accent",
                        box=ghost_box_rounded(),
                        padding=pad,
                        expand=False,
                    )
                )
            else:
                st = f"[dim]{escape(rp)}[/dim]" if not ly.ultra_narrow else ""
                self.console.print(
                    Panel(
                        Markdown(_markdown_tty_cap(prev, max_lines=ly.review_md_lines_normal)),
                        title=title,
                        subtitle=st if st else None,
                        border_style="ghost.accent",
                        box=ghost_box_rounded(),
                        padding=pad,
                        expand=False,
                    )
                )
            return True

        meta: List[str] = []
        if rfr is True:
            meta.append(f"[bold green]{escape(_ui_contract.CLOSURE_REVIEW_STATUS_READY)}[/bold green]")
        elif rfr is False:
            meta.append(f"[bold yellow]{escape(_ui_contract.CLOSURE_REVIEW_STATUS_PENDING)}[/bold yellow]")
        if rdetail:
            lim = 220 if ly.ultra_narrow else 300
            meta.append(f"[dim]{escape(truncate_visible(rdetail, lim))}[/dim]")
        body_tail = "\n".join(meta) if meta else "[dim]Contenido completo en el JSON del packet.[/dim]"
        self.console.print("")
        self.console.print(
            Panel(
                f"[cyan]{escape(rp)}[/cyan]\n{body_tail}",
                title=title,
                subtitle=sub,
                border_style="ghost.accent",
                box=ghost_box_rounded(),
                padding=pad,
                expand=False,
            )
        )
        return True

    def render_artifact_summary(self, artifact: Dict[str, Any]):
        """Cierre: modo operador (limpio) por defecto; GHOST_UI_VERBOSE=1 expone auditoría."""
        self._tool_section_live = False
        verbose = _ui_verbose()
        ly = self._tty_layout()
        sid = artifact.get("session_id", "?")
        ts = artifact.get("timestamp", "")
        compact = _artifact_interactive_compact(artifact)
        spec_body = artifact.get("taskspec") or artifact.get("contract_spec") or {}
        intent = str(spec_body.get("intent") or "") if isinstance(spec_body, dict) else ""
        ce = str(spec_body.get("change_expectation") or "") if isinstance(spec_body, dict) else ""
        readonly = _artifact_readonly_intent(artifact)
        suppress_readonly_panel = bool(self._suppress_next_readonly_review_panel)
        self._suppress_next_readonly_review_panel = False
        mode = escape(str(artifact.get("harness_mode_label") or "—"))
        outcome = escape(str(artifact.get("task_outcome") or "—"))
        cov = artifact.get("closure_operator_view") if isinstance(artifact.get("closure_operator_view"), dict) else {}
        headline_raw = str(cov.get("primary_headline_es") or "").strip()
        if readonly:
            _tier_g = str(artifact.get("findings_evidence_tier") or "").strip().lower()
            headline_raw = apply_tier_language_guard_es(headline_raw, _tier_g)
        headline = escape(headline_raw or "—")
        task_one = escape(str(artifact.get("task") or "").strip()[:220])
        agents_p = escape(str(artifact.get("agents_md_path") or "").strip())

        if verbose:
            self.console.print("\n" + "─" * 40)
            self.console.print(
                f"[ghost.brand]{escape(_ui_contract.BRAND_WORDMARK.upper())}[/ghost.brand] "
                f"[dim]· cierre de sesión[/dim]"
            )
            self.console.print(f"[dim]session {sid} · {ts}[/dim]\n")
            exec_body = (
                f"[cyan]Modo[/cyan]: {mode}\n"
                f"[cyan]Outcome[/cyan]: {outcome}\n"
                f"[cyan]Cierre[/cyan]: {headline}\n"
            )
            if task_one:
                exec_body += f"[cyan]Tarea[/cyan]: {task_one}\n"
            if agents_p:
                exec_body += f"[dim]AGENTS.md[/dim]: {agents_p}\n"
            rfr_v = artifact.get("review_ready_for_review")
            if rfr_v is not None and not readonly:
                rs = (
                    _ui_contract.CLOSURE_REVIEW_STATUS_READY
                    if rfr_v
                    else _ui_contract.CLOSURE_REVIEW_STATUS_PENDING
                )
                exec_body += f"[cyan]Review[/cyan]: {escape(rs)}\n"
                rdv = str(artifact.get("review_readiness_detail_es") or "").strip()
                if rdv:
                    exec_body += f"[dim]{escape(rdv[:420])}[/dim]\n"
            self.console.print(
                Panel(
                    exec_body,
                    title=f"{_ui_contract.BRAND_WORDMARK} · resumen",
                    border_style="ghost.brand",
                    box=ghost_box_rounded(),
                    expand=False,
                    padding=ly.panel_padding,
                )
            )
            self._render_review_packet_console(artifact, readonly=readonly, verbose=True)
        elif readonly and not suppress_readonly_panel:
            tier = str(artifact.get("findings_evidence_tier") or "").strip().lower()
            ev_filtered = _evidence_lines_without_tier_banner(artifact.get("evidence_lines") or [], max_items=10)
            max_ev = 6
            max_chars = 400
            body = _readonly_operator_review_body(
                artifact,
                headline_raw=headline_raw,
                tier=tier,
                ev_filtered=ev_filtered,
                max_ev=max_ev,
                max_chars=max_chars,
            )
            self.console.print("")
            self.console.print(
                Panel(
                    body,
                    title=f"[ghost.brand]{escape(_ui_contract.PANEL_TITLE_REVIEW)}[/ghost.brand]",
                    subtitle=_ui_contract.PANEL_SUBTITLE_REVIEW,
                    border_style="ghost.accent",
                    box=ghost_box_rounded(),
                    expand=False,
                    padding=ly.panel_padding,
                )
            )
        else:
            bar_w = min(max(self.console.width or 72, 40), ly.closure_bar_max)
            self.console.print("")
            self.console.print(f"[ghost.dim]{'─' * bar_w}[/ghost.dim]")
            self._section_kicker(_ui_contract.KICKER_SESION_CERRADA, verbose=False)
            hl_short = headline_raw[:100] + ("…" if len(headline_raw) > 100 else "") if headline_raw else str(artifact.get("task_outcome") or "—")
            self.console.print(f"  [bold white]{escape(hl_short)}[/bold white]")
            self.console.print(f"  [dim]{outcome} · {mode}[/dim]")
            rfr = artifact.get("review_ready_for_review")
            rdetail = str(artifact.get("review_readiness_detail_es") or "").strip()
            implish = str(artifact.get("harness_mode_label") or "") in (
                "implementation",
                "repair",
                "bugfix",
            )
            packet_ui = (
                self._render_review_packet_console(artifact, readonly=False, verbose=False)
                if implish
                else False
            )
            if (rfr is not None or rdetail) and not readonly and implish and not packet_ui:
                if rfr is True:
                    self.console.print(
                        f"  [bold green]{escape(_ui_contract.CLOSURE_REVIEW_STATUS_READY)}[/bold green]"
                    )
                else:
                    self.console.print(
                        f"  [bold yellow]{escape(_ui_contract.CLOSURE_REVIEW_STATUS_PENDING)}[/bold yellow]"
                    )
                if rdetail:
                    self.console.print(f"  [dim]{escape(rdetail[:360])}[/dim]")
            self.console.print("")

        tension = str(artifact.get("agents_user_tension_note") or "").strip()
        if tension:
            self.console.print(
                Panel(
                    escape(tension),
                    title="[yellow]AGENTS.md[/yellow] [dim]· tensión con el pedido[/dim]",
                    border_style="yellow",
                    box=ghost_box_rounded(),
                    expand=False,
                    padding=ly.panel_padding,
                )
            )

        if readonly and verbose:
            tier = str(artifact.get("findings_evidence_tier") or "").strip().lower()
            ev_filtered = _evidence_lines_without_tier_banner(artifact.get("evidence_lines") or [], max_items=10)
            max_ev = 6
            max_chars = 520
            agd = str(artifact.get("analysis_grounding_digest") or "").strip()

            if tier:
                tier_style = "green" if tier == "confirmed" else "yellow" if tier == "suspected" else "dim"
                tier_note = {
                    "confirmed": "Hallazgos: evidencia confirmada (checks ejecutados).",
                    "suspected": "Hallazgos: hipótesis — sin prueba dura salvo cita literal de read_file.",
                    "unverified": "Hallazgos: no verificada — inspección superficial.",
                }.get(tier, tier)
                self.console.print(
                    Panel(
                        escape(tier_note),
                        title=_ui_contract.PANEL_TITLE_EVIDENCIA_TIER,
                        border_style=tier_style,
                        expand=False,
                    )
                )
            if agd:
                self.console.print(
                    Panel(
                        escape(f"Grounding: {agd}"),
                        title="Debug",
                        border_style="dim",
                        expand=False,
                    )
                )
            if ev_filtered:
                lines = "\n".join(f"• {escape(str(x)[:max_chars])}" for x in ev_filtered[:max_ev])
                self.console.print(
                    Panel(
                        lines,
                        title=_ui_contract.PANEL_TITLE_EVIDENCIA_HERRAMIENTAS,
                        border_style="cyan",
                        expand=False,
                    )
                )

        tree_lines = build_workset_tree_lines(
            list(artifact.get("diff_summary") or []),
            artifact.get("active_workset") if isinstance(artifact.get("active_workset"), dict) else None,
            ascii_safe=ly.ascii_ui,
        )
        ds = artifact.get("diff_summary") or []
        has_diff = bool(ds)

        if tree_lines and (verbose or (not has_diff)):
            cap_lines = ly.workset_lines_verbose if verbose else ly.workset_lines
            slice_lines = tree_lines[:cap_lines]
            if verbose:
                tree_txt = "\n".join(slice_lines)
                if len(tree_lines) > cap_lines:
                    tree_txt += f"\n… (+{len(tree_lines) - cap_lines} líneas en artefacto)"
                self.console.print(
                    Panel(escape(tree_txt), title="Árbol de trabajo", border_style="blue", expand=False)
                )
            else:
                self._section_kicker(_ui_contract.KICKER_WORKSET, verbose=False)
                for tl in slice_lines:
                    self.console.print(f"  [dim]{escape(tl)}[/dim]")
                if len(tree_lines) > cap_lines:
                    self.console.print(f"  [dim]… (+{len(tree_lines) - cap_lines} en artefacto)[/dim]")

        plan_raw = str(artifact.get("plan") or "").strip()
        dp = artifact.get("decision_plan") if isinstance(artifact.get("decision_plan"), dict) else {}
        plan_fmt = format_operator_execution_plan(plan_raw, dp)
        show_plan = bool(plan_fmt) and (verbose or not readonly)
        if show_plan:
            if not compact:
                cap = ly.plan_body_cap_verbose if verbose else ly.plan_body_cap_interactive
            elif verbose:
                cap = int(ly.plan_body_cap_verbose * 0.82)
            else:
                cap = int(ly.plan_body_cap_interactive * 0.72)
            body = plan_fmt if len(plan_fmt) <= cap else plan_fmt[:cap] + "\n… [ver .md]"
            if verbose:
                self.console.print(Panel(escape(body), title="Plan", border_style="cyan", expand=False))
            else:
                self._section_kicker(_ui_contract.KICKER_PLAN, verbose=False)
                self.console.print(escape(body))

        if artifact.get("root_cause"):
            rc = escape(str(artifact["root_cause"])[:6000 if verbose else 4000])
            if verbose:
                self.console.print(Panel(rc, title="Diagnóstico", border_style="yellow", expand=False))
            else:
                self._section_kicker(_ui_contract.KICKER_DIAGNOSTICO, verbose=False)
                self.console.print(f"  {rc}")

        if ds:
            if verbose:
                self._section_kicker("Cambios en el repo", verbose=True)
                table = Table(
                    box=box.SIMPLE_HEAD,
                    show_header=True,
                    header_style="bold cyan",
                    padding=(0, 1),
                )
                table.add_column("Archivo", style="cyan", overflow="fold", max_width=None)
                table.add_column("Herramienta", style="dim", max_width=14)
                table.add_column("Tipo", style="white")
                for item in ds:
                    if not isinstance(item, dict):
                        continue
                    raw_t = str(item.get("type") or "")
                    fp = str(item.get("file") or "")
                    short_op = raw_t.replace(" File", "").strip() or "—"
                    if len(short_op) > 12:
                        short_op = short_op[:11] + "…"
                    table.add_row(fp, short_op, normalize_change_kind(raw_t))
                self.console.print(
                    Panel(
                        table,
                        title="Archivos tocados",
                        border_style="blue",
                        expand=False,
                        padding=(0, 0),
                    )
                )
            else:
                self._section_kicker(_ui_contract.CLOSURE_DIFF_REVIEW_CAPTION, verbose=False)
                self.console.print(
                    f"  [dim]{len(ds)} archivo(s) · vista rápida "
                    f"(tabla completa con GHOST_UI_VERBOSE=1)[/dim]"
                )
                fp_budget = max(36, min(72, ly.width - 22))
                for item in ds[: ly.diff_files_max]:
                    if not isinstance(item, dict):
                        continue
                    raw_t = str(item.get("type") or "")
                    fp = str(item.get("file") or "")
                    if len(fp) > fp_budget:
                        fp = fp[: max(12, fp_budget - 3)] + "…"
                    kind = normalize_change_kind(raw_t)
                    sym = _change_symbol_markup(kind, ascii_ui=ly.ascii_ui)
                    self.console.print(
                        f"  {sym} [cyan]{escape(fp)}[/cyan]  "
                        f"[dim]{escape(kind)}[/dim]"
                    )
                if len(ds) > ly.diff_files_max:
                    self.console.print(
                        f"  [dim]… +{len(ds) - ly.diff_files_max} más en el artefacto[/dim]"
                    )
                self.console.print("")

        ver = artifact.get("verification") if isinstance(artifact.get("verification"), dict) else {}
        vchecks = ver.get("checks") or []
        if vchecks:
            st = str(ver.get("status") or "")
            n = len(vchecks)
            prov = sum(1 for c in vchecks if str(c.get("provenance") or "") == "executed")
            if verbose:
                self.console.print(
                    Panel(
                        escape(f"status={st} · checks={n} · ejecutados={prov}"),
                        title="Verificación (snapshot de cierre)",
                        border_style="green",
                        expand=False,
                        padding=(0, 1),
                    )
                )
            elif prov > 0:
                self._section_kicker(_ui_contract.KICKER_VERIFY, verbose=False)
                self.console.print(
                    f"  [green]{escape(st)}[/green] [dim]· {prov}/{n} ejecutados en disco[/dim]"
                )
            elif st.lower() in ("failed", "error"):
                self._section_kicker(_ui_contract.KICKER_VERIFY, verbose=False)
                self.console.print(
                    f"  [yellow]{escape(st)}[/yellow] [dim]· {n} checks registrados[/dim]"
                )

        pt = compact_phase_trace_lines_from_list(list(artifact.get("phase_transitions") or []))
        if pt and verbose:
            self.console.print(
                Panel(
                    escape("\n".join(pt)),
                    title="Fases",
                    border_style="dim",
                    expand=False,
                )
            )

        trace_md = (artifact.get("trace_detail_md") or "").strip()
        trace_short = (artifact.get("trace_summary_short") or "").strip()
        trace_path = (artifact.get("trace_path") or "").strip()
        if verbose:
            if trace_md:
                if compact and len(trace_md) > 4500:
                    stub = (trace_short or "Trace largo → artefacto JSON.").strip()
                    if trace_path:
                        stub = f"{stub}\n{trace_path}"
                    self.console.print(Panel(escape(stub), title="Trace", border_style="dim", expand=False))
                else:
                    self.console.print(Panel(trace_md, title="Trace", border_style="dim", expand=False))
            elif trace_short or trace_path:
                self.console.print(
                    Panel(escape(str(trace_short or trace_path)), title="Trace", border_style="dim", expand=False)
                )
        elif trace_path and verbose:
            self.console.print(f"[dim]Trazas:[/dim] {escape(trace_path)}\n")

        if isinstance(spec_body, dict) and spec_body and verbose:
            vp = spec_body.get("verification_policy") or {}
            spec_line = (
                f"intent={spec_body.get('intent')} "
                f"change_expectation={spec_body.get('change_expectation')} "
                f"verify_required={vp.get('required')} "
                f"scope={spec_body.get('scope')}"
            )
            if compact:
                self.console.print(
                    Panel(
                        escape(spec_line),
                        title=_ui_contract.PANEL_TITLE_CONTRACT_RESUMEN,
                        border_style="blue",
                        expand=False,
                    )
                )
            else:
                try:
                    spec_txt = json.dumps(spec_body, indent=2, ensure_ascii=False)
                except (TypeError, ValueError):
                    spec_txt = str(spec_body)
                self.console.print(
                    Panel(
                        spec_txt,
                        title=_ui_contract.PANEL_TITLE_CONTRACT_SPEC,
                        border_style="blue",
                        expand=False,
                    )
                )

        rp = artifact.get("repo_profile")
        if isinstance(rp, dict) and rp and verbose:
            if compact:
                v2 = rp.get("profile_v2") if isinstance(rp.get("profile_v2"), dict) else {}
                stack = v2.get("stack") or rp.get("stack") or ""
                layers = v2.get("layers_detected") or rp.get("layers_detected") or []
                mini = json.dumps({"stack": stack, "layers_detected": layers}, ensure_ascii=False)
                self.console.print(Panel(escape(mini), title="Repo profile", border_style="green", expand=False))
            else:
                try:
                    rp_txt = json.dumps(rp, indent=2, ensure_ascii=False)
                except (TypeError, ValueError):
                    rp_txt = str(rp)
                self.console.print(Panel(rp_txt, title="Repo profile", border_style="green", expand=False))

        if artifact.get("next_action"):
            na = str(artifact["next_action"])[:4000 if verbose else 2800]
            skip_footer_next = readonly and not verbose
            if not skip_footer_next:
                if verbose:
                    self._section_kicker("Próximo paso", verbose=True)
                    self.console.print(
                        Panel(
                            escape(na),
                            title="Siguiente acción",
                            subtitle="Un paso concreto para el operador",
                            border_style="green",
                            box=ghost_box_rounded(),
                            expand=False,
                            padding=(0, 1),
                        )
                    )
                else:
                    self._section_kicker(_ui_contract.KICKER_SIGUIENTE_PASO, verbose=False)
                    self.console.print(f"  {escape(na)}")

        self._render_salidas_generadas(_artifact_salidas_rows(artifact), verbose=verbose)
        if verbose:
            self.console.print("─" * 40 + "\n")
        else:
            self.console.print("")

    # ─── CodexAssistant contract ─────────────────────────────────────────────

    def render_cli_startup_summary(
        self,
        command_mode: str = "dev",
        assistant_mode: str = "Chat",
        model: str = "",
        prep=None,
        cwd_same_as_repo: bool = True,
        role_models=None,
        *,
        llm_gateway_url: str = "",
        llm_gateway_reachable: Optional[bool] = None,
        llm_gateway_checked: str = "",
        provider_backend_label: str = "",
        auto_approve: bool = False,
        **kwargs: Any,
    ):
        """Render startup banner showing active mode, model, gateway URL and feature flags."""
        self._tool_section_live = False
        _ = kwargs
        self._auto_approve_shell = bool(auto_approve)
        verbose = _ui_verbose()
        ly = self._tty_layout()
        flags = []
        if prep and hasattr(prep, "active_feature_flags"):
            for k, v in (prep.active_feature_flags or {}).items():
                if _feature_flag_visible(v):
                    flags.append(k)
        flags_line = "  ".join(flags) if flags else "—"
        cwd = os.getcwd()
        root = Path(cwd)
        workspace_context, workspace_name = self._workspace_context(root, ly)
        runtime_bits: List[str] = [
            truncate_visible(model or "sin modelo", 24),
            truncate_visible(command_mode or "dev", 12),
        ]
        if llm_gateway_reachable is True:
            runtime_bits.append("gateway ready")
        elif llm_gateway_reachable is False:
            runtime_bits.append("gateway down")
        runtime_line = "  ·  ".join(runtime_bits)
        permission_line = self._permission_display()
        if ly.ultra_narrow:
            if verbose:
                self.console.print(
                    f"\n[ghost.brand]{escape(_ui_contract.BRAND_WORDMARK)}[/ghost.brand] "
                    f"[dim]mode:[/dim] [white]{escape(command_mode)}[/white]  "
                    f"[dim]model:[/dim] [white]{escape(model)}[/white]  "
                    f"[dim]{escape(truncate_visible(workspace_context, max(ly.width - 40, 18)))}[/dim]  "
                    f"[dim]permissions[/dim] [white]{escape(permission_line)}[/white]  "
                    f"[dim]flags[/dim] [dim]{escape(truncate_visible(flags_line, max(ly.width - 24, 40)))}[/dim]\n"
                )
            else:
                line1, line2 = startup_lines(
                    assistant_mode=assistant_mode,
                    model=model,
                    command_mode=command_mode,
                    subtitle=_ui_contract.BRAND_RUNNER_SUBTITLE,
                    layout=ly,
                )
                self.console.print(f"\n{line1}\n{line2}\n")
        else:
            wordmark = Text("\n".join(ghost_wordmark_lines(compact=False)), style="ghost.brand")
            meta = Table.grid(padding=(0, 0))
            meta.add_row(
                f"[dim]mode:[/dim] [white]{escape(command_mode)}[/white] "
                f"[ghost.dim]·[/ghost.dim] [dim]model:[/dim] [white]{escape(model)}[/white]"
            )
            meta.add_row(f"[white]{escape(workspace_context)}[/white]")
            meta.add_row(
                f"[dim]{escape(_ui_contract.STARTUP_LABEL_WORKSPACE)}:[/dim] [white]{escape(workspace_name)}[/white]"
            )
            meta.add_row(
                f"[dim]{escape(_ui_contract.STARTUP_LABEL_RUNTIME)}:[/dim] [white]{escape(runtime_line)}[/white]"
            )
            meta.add_row(
                f"[dim]{escape(_ui_contract.STARTUP_LABEL_PERMISSIONS)}:[/dim] [white]{escape(permission_line)}[/white]"
            )
            if llm_gateway_url:
                if llm_gateway_reachable is True:
                    gw_status = "[ghost.success]ready[/ghost.success]"
                elif llm_gateway_reachable is False:
                    gw_status = "[ghost.error]down[/ghost.error]"
                else:
                    gw_status = "[dim]unknown[/dim]"
                meta.add_row(
                    f"[dim]gateway:[/dim] [cyan]{escape(llm_gateway_url)}[/cyan] [ghost.dim]·[/ghost.dim] {gw_status}"
                )
            if provider_backend_label:
                meta.add_row(
                    f"[dim]provider:[/dim] [white]{escape(provider_backend_label)}[/white]"
                )
            if verbose:
                meta.add_row(
                    f"[dim]flags:[/dim] [dim]{escape(truncate_visible(flags_line, max(ly.width - 28, 32)))}[/dim]"
                )
            header = Table.grid(padding=(0, 2))
            header.add_column(no_wrap=True)
            header.add_column()
            header.add_row(wordmark, meta)
            self.console.print("")
            self.console.print(header)
        if llm_gateway_url and ly.ultra_narrow:
            if llm_gateway_reachable is True:
                gw_line = (
                    f"[dim]gateway:[/dim] [cyan]{escape(llm_gateway_url)}[/cyan] "
                    f"[bold green]reachable[/bold green]"
                )
                if llm_gateway_checked:
                    gw_line += f" [dim]({escape(llm_gateway_checked)})[/dim]"
            elif llm_gateway_reachable is False:
                gw_line = (
                    f"[dim]gateway:[/dim] [cyan]{escape(llm_gateway_url)}[/cyan] "
                    f"[bold red]unreachable[/bold red]"
                )
            else:
                gw_line = f"[dim]gateway:[/dim] [cyan]{escape(llm_gateway_url)}[/cyan] [dim](sin preflight)[/dim]"
            self.console.print(gw_line)
        if provider_backend_label and ly.ultra_narrow:
            self.console.print(
                f"[dim]provider (transporte):[/dim] [white]{escape(provider_backend_label)}[/white]"
            )
        start_rows = slash_menu_start_suggestions()
        first_cmd, first_tail = start_rows[0]
        next_line = first_cmd if not first_tail else f"{first_cmd} {first_tail}"
        quick = " ".join(slash_menu_quick_actions()[1:])
        startup_help = (
            f"[dim]next:[/dim] [white]{escape(truncate_visible(next_line, max(22, ly.width // 2)))}[/white] "
            f"[ghost.dim]·[/ghost.dim] [dim]quick[/dim] [white]{escape(quick)}[/white] "
            f"[ghost.dim]·[/ghost.dim] [dim]{escape(_ui_contract.STARTUP_HINT_SLASH)}[/dim]"
        )
        self.console.print(startup_help)
        if verbose and role_models and isinstance(role_models, dict) and role_models:
            rm = ", ".join(f"{k}={v}" for k, v in list(role_models.items())[:6])
            self.console.print(f"[dim]role_models:[/dim] [dim]{escape(rm)}[/dim]")
        self.console.print("")

    def begin_tool_segment(self) -> None:
        """Start buffering tool trace lines for one model tool round."""
        self._tool_segment_active = True
        self._tool_segment_buffer = []

    def _record_tool_activity_from_payload(self, row: Dict[str, Any]) -> None:
        ly = self._tty_layout()
        nm = str(row.get("name") or "")
        detail = str(row.get("detail") or "").replace("\n", " ").strip()
        detail = _short_tool_target(detail, ly.tool_detail_max_chars)
        st = str(row.get("estado") or "")
        self._tool_activity.append((_tool_chip_label(nm), detail, st))

    def _format_tool_activity_rail(self) -> Optional[str]:
        if not self._tool_activity:
            return None
        return self._format_tool_entries_rail(list(self._tool_activity))

    def _format_tool_entries_rail(self, entries: List[Tuple[str, str, str]]) -> Optional[str]:
        if not entries:
            return None
        ly = self._tty_layout()
        parts: List[str] = []
        tail = list(entries)[-ly.tool_activity_max_visible :]
        for label, detail, st in tail:
            det = escape(truncate_visible(detail, ly.tool_detail_max_chars + 8))
            if ly.ascii_ui:
                if st == "ok":
                    sym = "[ghost.success]+[/ghost.success]"
                elif st == "error":
                    sym = "[ghost.error]x[/ghost.error]"
                elif st == "pending":
                    sym = "[ghost.warn]..[/ghost.warn]"
                else:
                    sym = "[dim].[/dim]"
            else:
                if st == "ok":
                    sym = "[ghost.success]✓[/ghost.success]"
                elif st == "error":
                    sym = "[ghost.error]✗[/ghost.error]"
                elif st == "pending":
                    sym = "[ghost.warn]…[/ghost.warn]"
                else:
                    sym = "[dim]·[/dim]"
            parts.append(f"[cyan]{escape(label)}[/cyan] [dim]{det}[/dim] {sym}")
        if ly.merge_tool_rule_and_rail:
            return " ".join(parts)
        joiner = " [ghost.muted]|[/ghost.muted] " if ly.ascii_ui else " [ghost.muted]›[/ghost.muted] "
        return joiner.join(parts)

    def _summarize_read_batch(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        reads = sum(1 for row in rows if str(row.get("name") or "") == "read_file")
        searches = sum(1 for row in rows if str(row.get("name") or "") == "search_code")
        listings = sum(1 for row in rows if str(row.get("name") or "") == "ls")
        repo_maps = sum(1 for row in rows if str(row.get("name") or "") == "summarize_repo")
        total_ms = 0.0
        ok = True
        for row in rows:
            if str(row.get("estado") or "") == "error":
                ok = False
            dur = row.get("duration_ms")
            try:
                if dur is not None:
                    total_ms += float(dur)
            except (TypeError, ValueError):
                pass
        parts: List[str] = []
        if reads:
            parts.append(f"{reads} read")
        if searches:
            parts.append(f"{searches} search")
        if listings:
            parts.append(f"{listings} ls")
        if repo_maps:
            parts.append(f"{repo_maps} map")
        detail = "lote de lectura"
        if parts:
            detail += " · " + " · ".join(parts)
        return {
            "name": "read_batch",
            "detail": detail,
            "estado": "ok" if ok else "error",
            "line": "",
            "duration_ms": total_ms if total_ms > 0 else None,
            "auto_approved": None,
        }

    def flush_tool_segment(self) -> None:
        """Flush buffered tool traces: panel tabla, o filas tipo chip (compact/chips)."""
        self._tool_segment_active = False
        if not self._tool_segment_buffer:
            return
        ly = self._tty_layout()
        rows = _dedupe_tool_rows(self._tool_segment_buffer)
        mode = _tool_ui_mode()
        if mode == "compact":
            read_rows = [row for row in rows if _is_readlike_tool(row.get("name"))]
            non_read_rows = [row for row in rows if not _is_readlike_tool(row.get("name"))]
            display_rows: List[Dict[str, Any]] = list(non_read_rows)
            if len(read_rows) >= 2:
                display_rows.insert(0, self._summarize_read_batch(read_rows))
            elif read_rows:
                display_rows = list(read_rows) + display_rows
        else:
            display_rows = list(rows)
        for row in display_rows:
            self._record_tool_activity_from_payload(row)
        batch_entries = [
            (
                _tool_chip_label(str(r.get("name") or "")),
                str(r.get("detail") or "").replace("\n", " ").strip(),
                str(r.get("estado") or ""),
            )
            for r in display_rows
        ]
        rail = self._format_tool_entries_rail(batch_entries if mode == "compact" else list(self._tool_activity))
        sig = json.dumps(
            [
                (str(r.get("name")), str(r.get("detail")), str(r.get("estado")))
                for r in display_rows
            ],
            ensure_ascii=True,
        )
        repeat_batch = sig == self._last_tool_flush_signature
        self._last_tool_flush_signature = sig
        if repeat_batch and mode == "compact":
            self._tool_segment_buffer = []
            return

        rule = _ghost_rule_markup(_ui_contract.RULE_LABEL_HERRAMIENTAS, layout=ly)
        if not self._tool_section_live:
            self.console.print("")
        show_rule = not self._tool_section_live or mode == "panel" or ly.merge_tool_rule_and_rail
        if ly.merge_tool_rule_and_rail and rail and mode != "panel":
            self.console.print(f"{rule}  [dim]::[/dim]  [dim]últimas[/dim] {rail}")
        elif not repeat_batch:
            if show_rule:
                self.console.print(rule)
            if rail:
                self.console.print(f"  [dim]últimas[/dim]  {rail}")
        elif rail:
            self.console.print(f"  [dim]últimas[/dim]  {rail}")
        self._tool_section_live = True
        if mode == "panel":
            t = Table(
                title=f"[ghost.brand]Ghost[/ghost.brand] [dim]· {escape(_ui_contract.TOOL_TABLE_TITLE_MARKER)}[/dim]",
                box=box.SIMPLE_HEAD,
                header_style="bold cyan",
                padding=(0, 1),
            )
            t.add_column("Tool", style="cyan")
            t.add_column("Target", style="white", overflow="fold")
            t.add_column("Estado", style="dim")
            t.add_column("Tiempo", style="dim", justify="right")
            for row in display_rows:
                dur = row.get("duration_ms")
                dur_cell = ""
                if dur is not None:
                    try:
                        ms = float(dur)
                        dur_cell = f"{ms/1000.0:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"
                    except (TypeError, ValueError):
                        dur_cell = ""
                t.add_row(
                    _tool_chip_label(str(row.get("name") or "")),
                    str(row.get("detail") or ""),
                    str(row.get("estado") or ""),
                    dur_cell,
                )
            self.console.print(t)
        else:
            if mode == "compact":
                pass
            else:
                if not repeat_batch:
                    self.console.print(f"[dim]{escape(_ui_contract.BRAND_WORDMARK)} · operaciones[/dim]")
                for row in display_rows:
                    self.console.print(self._format_tool_chip_row(row, ly))
        self._tool_segment_buffer = []

    def append_tool_trace(
        self,
        name: str,
        detail: str,
        ok: Optional[bool] = None,
        *,
        auto_approved: Optional[bool] = None,
        **kwargs: Any,
    ) -> None:
        """
        Registra una herramienta.

        - Llamadas legacy: ``append_tool_trace(name, detail, False)`` → ``ok`` bool finalizado.
        - Pre-exec (assistant ``_execute_tool``): solo ``auto_approved=...`` → línea neutra.
        - ``**kwargs`` absorbe argumentos futuros sin romper el runtime.
        """
        duration_ms = kwargs.get("duration_ms")

        if ok is not None:
            estado = "ok" if ok else "error"
            line = ""
        else:
            estado = "auto" if auto_approved else "pending"
            line = ""

        payload: Dict[str, Any] = {
            "name": name,
            "detail": detail,
            "estado": estado,
            "line": line,
            "duration_ms": duration_ms,
            "auto_approved": auto_approved,
        }

        if self._tool_segment_active:
            self._tool_segment_buffer.append(payload)
        else:
            if ok is not None:
                self._record_tool_activity_from_payload(payload)
            self.console.print(self._format_tool_chip_row(payload, self._tty_layout()))

    def render_budget_status(self, remaining: int, total: int):
        """Show autonomy budget bar (compact)."""
        if total <= 0:
            return
        pct = int((remaining / total) * 100)
        color = "green" if pct > 50 else "yellow" if pct > 20 else "red"
        self.console.print(
            f" [dim]Budget:[/dim] [{color}]{remaining}/{total}[/{color}]",
            end="\r",
        )

    def render_autonomy_checkpoint(self, checkpoint):
        """Display an autonomy / stagnation checkpoint."""
        reason = getattr(checkpoint, "reason", None) or str(checkpoint)
        self.console.print(
            f"\n[bold yellow]⚠ Autonomy checkpoint:[/bold yellow] [dim]{escape(str(reason))}[/dim]\n"
        )

    def render_bundled_approval_request(self, actions) -> bool:
        """Ask user to approve a bundle of write/shell actions. Returns True if approved."""
        self.console.print("")
        ly = self._tty_layout()
        self.console.print(_ghost_rule_markup("aprobación", layout=ly))
        tbl = Table(
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold cyan",
            padding=(0, 1),
        )
        tbl.add_column("Tool", style="cyan", max_width=12, overflow="fold")
        tbl.add_column("Objetivo", style="white", overflow="fold")
        tbl.add_column("Impacto", style="dim", overflow="fold")
        for raw in actions or []:
            a = raw if isinstance(raw, dict) else {}
            nm = str(a.get("name") or "?")
            detail = str(a.get("detail") or "")
            args = a.get("args")
            if not isinstance(args, dict):
                args = {}
            tool_lab = _tool_chip_label(nm)
            purpose = _approval_purpose_for_action(nm, detail, args)
            impact = _approval_impact_for_action(nm)
            tbl.add_row(tool_lab, escape(purpose), escape(impact))
        self.console.print(
            Panel(
                tbl,
                title=f"[ghost.brand]{escape(_ui_contract.BRAND_WORDMARK)}[/ghost.brand] [dim]· aprobar lote[/dim]",
                subtitle="[dim]Solo lo confirmado se ejecuta · Esc cancela[/dim]",
                border_style="yellow",
                box=ghost_box_rounded(),
                expand=False,
                padding=ly.panel_padding,
            )
        )
        try:
            ans = (
                self.console.input(
                    "[ghost.brand]›[/ghost.brand] [bold]¿Autorizar todo el lote?[/bold] [dim](y/n)[/dim] "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            return False
        return ans == "y"

    def render_verify_batch_approval_request(self, shell_commands) -> bool:
        """Ask user to approve a batch of verification shell commands."""
        cmds = [str(c) for c in (shell_commands or [])]
        n = len(cmds)
        self.console.print("")
        ly_v = self._tty_layout()
        self.console.print(_ghost_rule_markup("verificación · shell", layout=ly_v))
        body_lines: List[str] = []
        for i, c in enumerate(cmds[:14], 1):
            line = c.replace("\n", " ")
            if len(line) > 90:
                line = line[:89] + "…"
            body_lines.append(f"[dim]{i:>2}.[/dim]  [white]{escape(line)}[/white]")
        if n > 14:
            body_lines.append(f"[dim]… +{n - 14} más en el plan de verify[/dim]")
        body = "\n".join(body_lines) if body_lines else "[dim](sin comandos listados)[/dim]"
        self.console.print(
            Panel(
                body,
                title=f"[ghost.brand]{escape(_ui_contract.BRAND_WORDMARK)}[/ghost.brand] [dim]· verify en shell[/dim]",
                subtitle=f"[dim]{n} comando(s) · local · mismo usuario que esta consola[/dim]",
                border_style="ghost.accent",
                box=ghost_box_rounded(),
                expand=False,
                padding=ly_v.panel_padding,
            )
        )
        try:
            ans = (
                self.console.input(
                    "[ghost.brand]›[/ghost.brand] [bold]¿Ejecutar en tu máquina?[/bold] [dim](y/n)[/dim] "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            return False
        return ans == "y"

    def render_merge_preview(self, bundle):
        """Display merge preview bundle."""
        self.console.print(
            f"\n[bold cyan]Merge Preview:[/bold cyan] risk=[yellow]{getattr(bundle, 'conflict_risk', 'unknown')}[/yellow]"
        )

    def render_merge_success(self, task_id: str, files):
        """Display successful merge confirmation."""
        self.console.print(
            f"\n[bold green]✓ Merge applied:[/bold green] {task_id} ({len(files or [])} file(s))"
        )

    def render_review_bundle(self, bundle):
        """Display review bundle."""
        self.console.print(f"\n[bold magenta]Review Bundle:[/bold magenta] {getattr(bundle, 'task_id', '?')}")

    def render_review_decision(self, task_id: str, decision: str):
        """Display review decision confirmation."""
        icon = "✓" if decision == "approved" else "✘"
        color = "green" if decision == "approved" else "red"
        self.console.print(f"\n[bold {color}]{icon} Review {decision}:[/bold {color}] {task_id}")

    def print_help(self):
        """Alias for render_help (used by assistant)."""
        self.render_help()
