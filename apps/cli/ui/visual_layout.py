"""
Sistema de diseño terminal para Ghost CLI: densidad, ancho y ASCII coherentes.

- GHOST_UI_DENSITY: compact | normal | comfortable (default normal)
- Ancho: deriva de ``Console.width``; umbrales ``narrow`` / ``ultra_narrow``
- ASCII: ``GHOST_ASCII_UI=1`` o ``ghost_ui_uses_ascii()`` → marcadores seguros

No dispersar heurísticas de spacing fuera de este módulo: el renderer consulta
``build_ghost_visual_layout`` o ``GhostVisualLayout`` inyectado.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import List, Optional, Tuple

from rich.markup import escape

from apps.cli.runtime.terminal_capability import ghost_ui_uses_ascii
from apps.cli.ui import ui_contract as _uc

# Umbrales de columna (caracteres)
WIDTH_NARROW = 64
WIDTH_ULTRA_NARROW = 52

DENSITY_COMPACT = "compact"
DENSITY_NORMAL = "normal"
DENSITY_COMFORTABLE = "comfortable"


def parse_ui_density() -> str:
    raw = os.getenv("GHOST_UI_DENSITY", DENSITY_NORMAL).strip().lower()
    if raw in (DENSITY_COMPACT, "tight", "dense"):
        return DENSITY_COMPACT
    if raw in (DENSITY_COMFORTABLE, "roomy", "relaxed"):
        return DENSITY_COMFORTABLE
    return DENSITY_NORMAL


def default_terminal_width() -> int:
    try:
        return max(40, min(shutil.get_terminal_size(fallback=(100, 24)).columns, 256))
    except OSError:
        return 100


@dataclass(frozen=True)
class GhostVisualLayout:
    """Snapshot de layout para una TTY concreta (inmutable)."""

    density: str
    width: int
    ascii_ui: bool
    verbose: bool
    narrow: bool
    ultra_narrow: bool
    # Paneles
    panel_padding: Tuple[int, int]
    # Espaciado vertical entre bloques (líneas en blanco extra)
    section_extra_blanks: int
    # Review packet (markdown en consola)
    review_md_lines_verbose: int
    review_md_lines_normal: int
    review_use_plain_fallback: bool
    review_plain_max_lines: int
    # Live status + rail
    merge_status_single_line: bool
    rail_use_abbrev_labels: bool
    live_detail_max_chars: int
    show_status_hints_under_spinner: bool
    # Herramientas
    tool_activity_max_visible: int
    tool_detail_max_chars: int
    merge_tool_rule_and_rail: bool
    # Verify
    verify_stderr_lines: int
    verify_stderr_lines_verbose: int
    verify_plan_one_max_chars: int
    # Cierre / workset / diff / artefactos
    workset_lines: int
    workset_lines_verbose: int
    diff_files_max: int
    artifact_paths_oneline: int
    closure_bar_max: int
    plan_body_cap_interactive: int
    plan_body_cap_verbose: int


def _scale(base: int, density: str, *, narrow: bool, ultra: bool) -> int:
    v = float(base)
    if density == DENSITY_COMPACT:
        v *= 0.72
    elif density == DENSITY_COMFORTABLE:
        v *= 1.22
    if ultra:
        v *= 0.65
    elif narrow:
        v *= 0.85
    return max(1, int(v))


def build_ghost_visual_layout(
    *,
    width: int,
    verbose: bool,
    ascii_ui: Optional[bool] = None,
    density: Optional[str] = None,
) -> GhostVisualLayout:
    """
    Construye el layout a partir del ancho real de consola y flags.

    ``width``: ``console.width`` o fallback razonable.
    """
    w = max(40, int(width or 80))
    d = (density or parse_ui_density()).strip().lower()
    if d not in (DENSITY_COMPACT, DENSITY_NORMAL, DENSITY_COMFORTABLE):
        d = DENSITY_NORMAL
    asc = bool(ghost_ui_uses_ascii() if ascii_ui is None else ascii_ui)
    narrow = w < WIDTH_NARROW
    ultra = w < WIDTH_ULTRA_NARROW

    # Padding: compact = más denso
    if d == DENSITY_COMPACT:
        pad = (0, 0)
        sec_blanks = 0
    elif d == DENSITY_COMFORTABLE:
        pad = (1, 1) if not narrow else (0, 1)
        sec_blanks = 1
    else:
        pad = (0, 1)
        sec_blanks = 0 if narrow else 0

    merge_status = narrow or ultra
    abbrev_rail = ultra or (narrow and d == DENSITY_COMPACT)
    # En ultra-estrecho, una sola línea obligatoria; hints solo si comfortable y ancho mínimo
    hints = verbose and d == DENSITY_COMFORTABLE and w >= 60 and not ultra

    detail_chars = max(24, min(w - 22, 72))
    if merge_status:
        detail_chars = max(20, min(w - 14, 56))

    review_v = _scale(56, d, narrow=narrow, ultra=ultra)
    review_n = _scale(34, d, narrow=narrow, ultra=ultra)
    if d == DENSITY_COMFORTABLE:
        review_v += 8
        review_n += 6

    plain_fb = ultra or (narrow and d == DENSITY_COMPACT)
    plain_lines = _scale(8, d, narrow=narrow, ultra=ultra)

    tool_vis = 4
    if narrow:
        tool_vis = 2 if d == DENSITY_COMPACT else 3
    if ultra:
        tool_vis = 2
    if d == DENSITY_COMFORTABLE and not narrow:
        tool_vis = 5

    tool_detail = 44 if not narrow else 32
    if ultra:
        tool_detail = 28

    merge_tools = narrow or ultra

    v_stderr = _scale(24, d, narrow=narrow, ultra=ultra)
    v_stderr_v = _scale(32, d, narrow=narrow, ultra=ultra)
    plan_max = 200 if narrow else 200
    if d == DENSITY_COMPACT:
        plan_max = 120
    elif d == DENSITY_COMFORTABLE:
        plan_max = 240

    ws = _scale(14, d, narrow=narrow, ultra=ultra)
    ws_v = _scale(24, d, narrow=narrow, ultra=ultra)
    diff_max = _scale(22, d, narrow=narrow, ultra=ultra)
    art_paths = 4 if not narrow else 2
    if ultra:
        art_paths = 2
    if d == DENSITY_COMPACT and not ultra:
        art_paths = min(art_paths, 3)

    bar_max = min(max(w - 4, 40), 72)
    plan_cap_i = 3200 if d == DENSITY_NORMAL else (2200 if d == DENSITY_COMPACT else 4200)
    plan_cap_v = 4500 if d == DENSITY_NORMAL else (3600 if d == DENSITY_COMPACT else 5200)
    if narrow:
        plan_cap_i = int(plan_cap_i * 0.85)
        plan_cap_v = int(plan_cap_v * 0.85)

    return GhostVisualLayout(
        density=d,
        width=w,
        ascii_ui=asc,
        verbose=verbose,
        narrow=narrow,
        ultra_narrow=ultra,
        panel_padding=pad,
        section_extra_blanks=sec_blanks,
        review_md_lines_verbose=review_v,
        review_md_lines_normal=review_n,
        review_use_plain_fallback=plain_fb,
        review_plain_max_lines=plain_lines,
        merge_status_single_line=merge_status,
        rail_use_abbrev_labels=abbrev_rail,
        live_detail_max_chars=detail_chars,
        show_status_hints_under_spinner=hints,
        tool_activity_max_visible=tool_vis,
        tool_detail_max_chars=tool_detail,
        merge_tool_rule_and_rail=merge_tools,
        verify_stderr_lines=v_stderr,
        verify_stderr_lines_verbose=v_stderr_v,
        verify_plan_one_max_chars=plan_max,
        workset_lines=ws,
        workset_lines_verbose=ws_v,
        diff_files_max=diff_max,
        artifact_paths_oneline=art_paths,
        closure_bar_max=bar_max,
        plan_body_cap_interactive=plan_cap_i,
        plan_body_cap_verbose=plan_cap_v,
    )


def layout_for_rail_api() -> GhostVisualLayout:
    """Layout estable para ``format_phase_rail`` / imports de tests (ancho fijo, sin modo estrecho)."""
    return build_ghost_visual_layout(
        width=100,
        verbose=False,
        ascii_ui=ghost_ui_uses_ascii(),
        density=parse_ui_density(),
    )


def format_live_phase_rail(
    phase: str,
    profile: str,
    *,
    layout: Optional[GhostVisualLayout] = None,
) -> str:
    """
    Rail de fase. Si ``layout`` es None, usa ``layout_for_rail_api()`` (ancho detectado).
    Con ``layout.ascii_ui`` o abreviaturas, adapta marcadores sin romper tests que pasan None
    en entornos anchos por defecto.
    """
    ly = layout or layout_for_rail_api()
    prof = (profile or _uc.LIVE_RAIL_PROFILE_IMPLEMENT).strip().lower()
    if prof not in (_uc.LIVE_RAIL_PROFILE_READONLY, _uc.LIVE_RAIL_PROFILE_IMPLEMENT):
        prof = _uc.LIVE_RAIL_PROFILE_IMPLEMENT
    if prof == _uc.LIVE_RAIL_PROFILE_READONLY:
        b = _readonly_rail_bucket(phase)
        labels_full = (
            _uc.PHASE_RAIL_LABEL_GATHER,
            _uc.PHASE_RAIL_LABEL_REVIEW,
            _uc.PHASE_RAIL_LABEL_CLOSE,
        )
    else:
        b = _phase_rail_bucket(phase)
        labels_full = (
            _uc.PHASE_RAIL_LABEL_GATHER,
            _uc.PHASE_RAIL_LABEL_PLAN,
            _uc.PHASE_RAIL_LABEL_ACT,
            _uc.PHASE_RAIL_LABEL_VERIFY,
        )

    if ly.rail_use_abbrev_labels:
        labels = tuple(x[:3] for x in labels_full)
    else:
        labels = labels_full

    sep_token = " > " if ly.ascii_ui else " [ghost.dim]›[/ghost.dim] "
    parts: List[str] = []
    for i, lab in enumerate(labels):
        el = escape(lab)
        if ly.ascii_ui:
            if i < b:
                parts.append(f"[ghost.dim]+[/ghost.dim] {el}")
            elif i == b:
                parts.append(f"[ghost.brand]*[/ghost.brand] [ghost.phase]{el}[/ghost.phase]")
            else:
                parts.append(f"[dim]. {el}[/dim]")
        else:
            if i < b:
                parts.append(f"[ghost.dim]✓[/ghost.dim] [dim]{el}[/dim]")
            elif i == b:
                parts.append(f"[ghost.brand]●[/ghost.brand] [ghost.phase]{el}[/ghost.phase]")
            else:
                parts.append(f"[dim]○ {el}[/dim]")
    return sep_token.join(parts)


def _phase_rail_bucket(phase: str) -> int:
    p = (phase or "EXPLORE").strip().upper()
    if p in ("IDLE", "INTAKE", "EXPLORE", "GATHER"):
        return 0
    if p == "PLAN":
        return 1
    if p == "ACT":
        return 2
    if p in ("VERIFY", "REPAIR", "CLOSING", "DONE", "ABORTED"):
        return 3
    return 0


def _readonly_rail_bucket(phase: str) -> int:
    p = (phase or "EXPLORE").strip().upper()
    if p in ("IDLE", "INTAKE", "EXPLORE", "GATHER"):
        return 0
    if p in ("PLAN", "ACT", "VERIFY", "REPAIR", "REVIEW"):
        return 1
    if p in ("CLOSING", "DONE", "ABORTED"):
        return 2
    return 0


def artifact_line_marker(layout: GhostVisualLayout) -> str:
    return "*" if layout.ascii_ui else "◇"


def tool_chip_prefix(layout: GhostVisualLayout) -> str:
    return "  | " if layout.ascii_ui else "  [ghost.dim]╎[/ghost.dim] "


def rule_markup(label: str, layout: GhostVisualLayout) -> str:
    lab = escape(label.strip())
    if layout.ascii_ui:
        return f"-- {lab} --"
    return f"[ghost.dim]──[/ghost.dim] [ghost.brand]{lab}[/ghost.brand] [ghost.dim]──[/ghost.dim]"


def truncate_visible(text: str, max_chars: int) -> str:
    t = (text or "").strip()
    if len(t) <= max_chars:
        return t
    return t[: max(1, max_chars - 1)] + "…"


def review_packet_plain_fallback(md: str, *, max_lines: int, max_chars_per_line: int = 96) -> str:
    """Extrae líneas legibles sin motor markdown (terminales muy estrechos)."""
    out: List[str] = []
    for line in (md or "").splitlines():
        s = line.strip()
        if not s or s == "---":
            continue
        if s.startswith("```"):
            continue
        if s.startswith("##"):
            out.append(s.lstrip("#").strip()[:max_chars_per_line])
        elif s.startswith(("-", "*", ">")):
            out.append(s[:max_chars_per_line])
        elif len(out) == 0 and s:
            out.append(s[:max_chars_per_line])
        if len(out) >= max_lines:
            break
    if not out:
        return (md or "")[:400].strip()
    return "\n".join(out)


def startup_lines(
    *,
    assistant_mode: str,
    model: str,
    command_mode: str,
    subtitle: str,
    layout: GhostVisualLayout,
) -> Tuple[str, Optional[str]]:
    """
    Devuelve (línea principal, línea secundaria opcional).
    En estrecho: una sola línea compacta.
    """
    if layout.ultra_narrow or (layout.narrow and layout.density == DENSITY_COMPACT):
        one = (
            f"[ghost.brand]Ghost[/ghost.brand] [cyan]{escape(assistant_mode)}[/cyan] "
            f"[dim]{escape(command_mode)}[/dim]"
        )
        return one, f"[dim]{escape(subtitle)}[/dim]"
    if layout.narrow:
        a = (
            f"[ghost.brand]Ghost[/ghost.brand] [cyan]{escape(assistant_mode)}[/cyan] "
            f"[ghost.dim]|[/ghost.dim] [white]{escape(model)}[/white]"
        )
        return a, f"[dim]{escape(command_mode)}[/dim] · [dim]{escape(subtitle)}[/dim]"
    line = (
        f"[ghost.brand]Ghost[/ghost.brand] [cyan]{escape(assistant_mode)}[/cyan] "
        f"[ghost.dim]│[/ghost.dim] [white]{escape(model)}[/white] "
        f"[ghost.dim]│[/ghost.dim] [dim]{escape(command_mode)}[/dim]"
    )
    return line, f"[dim]{escape(subtitle)}[/dim]"


def salidas_paths_line(paths: List[str], layout: GhostVisualLayout) -> str:
    """Une rutas para la línea compacta: prioriza mostrar todas (hasta ``cap``) íntegras si caben."""
    cap = layout.artifact_paths_oneline
    sep = ", " if layout.narrow else "  ·  "
    shown = paths[:cap]
    tail_suffix = f" +{len(paths) - cap}" if len(paths) > cap else ""
    tail_markup = f" [dim]{tail_suffix}[/dim]" if tail_suffix else ""
    if not shown:
        return ""
    full = sep.join(escape(p) for p in shown)
    budget = max(48, layout.width - 6)
    if len(full) + len(tail_suffix) <= budget:
        return full + tail_markup
    n = len(shown)
    avail = max(28, budget - len(tail_suffix))
    per = max(10, (avail - len(sep) * max(0, n - 1)) // n)
    parts: List[str] = []
    for p in shown:
        if len(p) <= per:
            parts.append(escape(p))
        else:
            parts.append(escape(p[: max(4, per - 1)]) + "…")
    return sep.join(parts) + tail_markup


def salidas_paths_lines_split(paths: List[str], layout: GhostVisualLayout) -> List[str]:
    """
    Una o dos líneas de rutas: si el join supera el ancho, parte en dos (íntegro, sin truncar).
    """
    cap = layout.artifact_paths_oneline
    sep = ", " if layout.narrow else "  ·  "
    shown = paths[:cap]
    tail_suffix = f" +{len(paths) - cap}" if len(paths) > cap else ""
    tail_markup = f" [dim]{tail_suffix}[/dim]" if tail_suffix else ""
    if not shown:
        return []
    plain = sep.join(escape(p) for p in shown)
    budget = max(40, layout.width - 10)
    if len(plain) + len(tail_suffix) <= budget:
        return [plain + tail_markup]
    if len(shown) <= 1:
        return [plain + tail_markup]
    mid = (len(shown) + 1) // 2
    first = sep.join(escape(p) for p in shown[:mid])
    second = sep.join(escape(p) for p in shown[mid:])
    return [first, second + tail_markup]
