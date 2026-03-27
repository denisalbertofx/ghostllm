"""Ghost CLI — tema Rich y banner de marca (solo presentación)."""
from __future__ import annotations

import os
import shutil
from typing import List

from rich.console import Console
from rich.theme import Theme

from apps.cli.runtime.terminal_capability import ghost_ui_uses_ascii

GHOST_RICH_THEME = Theme(
    {
        # Tokens semánticos (alias para copy/markup consistente)
        "ghost.primary": "#B794F6 bold",
        "ghost.danger": "#F87171 bold",
        # Violeta frío + cyan: identidad Ghost (legible en temas oscuros)
        "ghost.brand": "#B794F6 bold",
        "ghost.accent": "#7DD3FC",
        "ghost.phase": "#5EEAD4 bold",
        "ghost.success": "#34D399 bold",
        "ghost.error": "#F87171 bold",
        "ghost.warn": "#FBBF24",
        "ghost.info": "#22D3EE",
        "ghost.dim": "#64748B",
        "ghost.muted": "#94A3B8",
        "ghost.blue": "#60A5FA",
    }
)


def make_ghost_console(**kwargs) -> Console:
    merged = {"theme": GHOST_RICH_THEME, **kwargs}
    if kwargs.get("file") is None and "emoji" not in kwargs:
        merged["emoji"] = not ghost_ui_uses_ascii()
    return Console(**merged)


def ghost_box_rounded():
    from rich import box

    return box.ASCII if ghost_ui_uses_ascii() else box.ROUNDED


def ghost_box_simple():
    from rich import box

    return box.ASCII if ghost_ui_uses_ascii() else box.SIMPLE


def ghost_box_minimal():
    from rich import box

    return box.ASCII if ghost_ui_uses_ascii() else box.MINIMAL


def ghost_box_double():
    from rich import box

    return box.ASCII if ghost_ui_uses_ascii() else box.DOUBLE


def ghost_panel(*args, **kwargs):
    """Panel with a safe default border (ASCII when the console cannot render Unicode box-drawing)."""
    from rich.panel import Panel

    kwargs.setdefault("box", ghost_box_rounded())
    return Panel(*args, **kwargs)


def ghost_banner_enabled() -> bool:
    v = os.getenv("GHOST_BANNER", "1").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def ghost_banner_width_slim() -> bool:
    try:
        return shutil.get_terminal_size(fallback=(100, 24)).columns < 72
    except OSError:
        return True


def ghost_banner_markup_full() -> str:
    return (
        "[ghost.brand]  ╭──────────────────────────────────────────╮[/ghost.brand]\n"
        "[ghost.brand]  │[/ghost.brand] [bold white]GHOST[/bold white]  ·  coding agent (terminal-first)  "
        "[ghost.brand]│[/ghost.brand]\n"
        "[ghost.brand]  │[/ghost.brand] [dim]Rápido · verificable · enfocado[/dim]              [ghost.brand]│[/ghost.brand]\n"
        "[ghost.brand]  ╰──────────────────────────────────────────╯[/ghost.brand]"
    )


def ghost_banner_markup_slim() -> str:
    return (
        "[ghost.brand]GHOST[/ghost.brand] [dim]·[/dim] coding agent [dim]·[/dim] "
        "[dim]verificado · enfocado[/dim]"
    )


def ghost_tool_ui_mode() -> str:
    """compact = una línea por lote (flujo tipo agente); panel = tabla con borde."""
    v = os.getenv("GHOST_TOOL_UI", "compact").strip().lower()
    if v in ("panel", "full", "table", "boxed"):
        return "panel"
    return "compact"


def ghost_budget_line_wanted(remaining: int, total: int) -> bool:
    """Línea de presupuesto cada turno solo si GHOST_BUDGET_LINE=1; si no, solo cuando queda poco."""
    raw = os.getenv("GHOST_BUDGET_LINE", "").strip().lower()
    if raw in ("1", "true", "yes", "on", "always"):
        return True
    if raw in ("0", "false", "no", "off", "never"):
        return False
    try:
        r, t = int(remaining), int(total)
        return t > 0 and r < max(20, t // 4)
    except (TypeError, ValueError):
        return False


def ghost_banner_plain_lines() -> List[str]:
    if ghost_banner_width_slim():
        return ["GHOST · coding agent · verificado · enfocado"]
    return [
        "  ╭──────────────────────────────────────────╮",
        "  │ GHOST  ·  coding agent (terminal-first)  │",
        "  │ Rápido · verificable · enfocado            │",
        "  ╰──────────────────────────────────────────╯",
    ]


def ghost_banner_ascii_only_lines() -> List[str]:
    """CP1252-safe / ASCII-only banner (NO_COLOR or limited encoding fallback)."""
    if ghost_banner_width_slim():
        return ["GHOST | coding agent | verificado | enfocado"]
    return [
        "  +------------------------------------------+",
        "  | GHOST  |  coding agent (terminal-first)   |",
        "  | rapido | verificable | enfocado           |",
        "  +------------------------------------------+",
    ]


def ghost_mark_lines(*, ascii_only: bool = False, compact: bool = False) -> List[str]:
    """Pequeño emblema reconocible para la cabecera de Ghost."""
    if compact:
        return [" .-. ", "(o o)", "'v-v'"] if ascii_only else ["  👻  "]
    return [
        "  .-^-.  ",
        " / o o \\ ",
        "|   ^   |",
        "| \\___/ |",
        " 'v-v-v' ",
    ]


def ghost_wordmark_lines(*, compact: bool = False) -> List[str]:
    if compact:
        return ["GHOST"]
    return [
        "   ____ _   _  ___  ____ _____",
        "  / ___| | | |/ _ \\/ ___|_   _|",
        " | |  _| |_| | | | \\___ \\ | |",
        " | |_| |  _  | |_| |___) || |",
        "  \\____|_| |_|\\___/|____/ |_|",
    ]
