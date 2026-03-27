from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple


@dataclass(frozen=True)
class SlashMenuItem:
    command: str
    label: str
    hint: str
    category: str
    example: str = ""
    accepts_args: bool = True


@dataclass(frozen=True)
class SlashMenuState:
    active: bool
    query: str
    items: List[SlashMenuItem]
    selected: int


SLASH_MENU_ITEMS: List[SlashMenuItem] = [
    SlashMenuItem(
        "/plan",
        "Plan sin cambios",
        "Explora, revisa y propone siguiente paso",
        "explorar",
        "/plan encuentra los bugs mas importantes",
    ),
    SlashMenuItem(
        "/do",
        "Ejecuta una tarea",
        "Actua sobre el repo con herramientas",
        "actuar",
        "/do corrige el bug del arranque",
    ),
    SlashMenuItem(
        "/edit",
        "Editar archivo",
        "Abre un flujo directo de patch",
        "actuar",
        "/edit apps/cli/main.py",
    ),
    SlashMenuItem(
        "/fix",
        "Buscar y arreglar",
        "Encuentra issues y corrige",
        "actuar",
        "/fix",
        accepts_args=False,
    ),
    SlashMenuItem(
        "/doctor",
        "Diagnostico",
        "Chequeo rapido del sistema",
        "runtime",
        "/doctor",
        accepts_args=False,
    ),
    SlashMenuItem(
        "/status",
        "Estado",
        "Ver daemon y gateway",
        "runtime",
        "/status",
        accepts_args=False,
    ),
    SlashMenuItem(
        "/logs",
        "Logs",
        "Ultimas lineas del daemon",
        "runtime",
        "/logs",
        accepts_args=False,
    ),
    SlashMenuItem(
        "/tasks",
        "Tareas",
        "Lista de tareas activas",
        "coordinar",
        "/tasks",
        accepts_args=False,
    ),
    SlashMenuItem(
        "/board",
        "Board",
        "Vista rapida de workers",
        "coordinar",
        "/board",
        accepts_args=False,
    ),
    SlashMenuItem(
        "/swarm",
        "Swarm",
        "Inicializa o inspecciona workers",
        "coordinar",
        "/swarm",
        accepts_args=False,
    ),
    SlashMenuItem(
        "/review",
        "Review bundle",
        "Abre un paquete de revision",
        "review",
        "/review task_123",
    ),
    SlashMenuItem(
        "/approve",
        "Aprobar",
        "Aprueba una review con task id",
        "review",
        "/approve task_123",
    ),
    SlashMenuItem(
        "/reject",
        "Rechazar",
        "Rechaza una review con task id",
        "review",
        "/reject task_123",
    ),
    SlashMenuItem(
        "/batch start",
        "Batch",
        "Lanza varias tareas en lote",
        "coordinar",
        "/batch start",
        accepts_args=False,
    ),
]


def slash_menu_quick_actions() -> List[str]:
    return ["/plan", "/do", "/fix", "/doctor"]


def slash_menu_start_suggestions() -> List[Tuple[str, str]]:
    return [
        ("/plan", "encuentra los bugs mas importantes"),
        ("/do", "corrige el bug del arranque"),
        ("/fix", ""),
    ]


def slash_menu_state(text: str, *, selected: int = 0) -> SlashMenuState:
    raw = str(text or "")
    stripped = raw.lstrip()
    if not stripped.startswith("/"):
        return SlashMenuState(False, "", [], 0)

    token = stripped.split(" ", 1)[0]
    query = token[1:].strip().lower()
    if " " in stripped and stripped.endswith(" "):
        return SlashMenuState(False, query, [], 0)

    items = _filter_slash_items(query)
    if not items:
        return SlashMenuState(True, query, [], 0)
    selected = max(0, min(int(selected), len(items) - 1))
    return SlashMenuState(True, query, items, selected)


def slash_menu_apply_selection(current_text: str, item: SlashMenuItem) -> str:
    _ = current_text
    return item.command + (" " if item.accepts_args else "")


def slash_menu_group_label(item: SlashMenuItem) -> str:
    return item.category.upper()


def _filter_slash_items(query: str) -> List[SlashMenuItem]:
    q = (query or "").strip().lower()
    if not q:
        return SLASH_MENU_ITEMS[:10]

    def rank(item: SlashMenuItem) -> tuple[int, int, int, str]:
        cmd = item.command.lower()
        label = item.label.lower()
        hint = item.hint.lower()
        category = item.category.lower()
        if cmd == f"/{q}":
            return (0, len(cmd), 0, cmd)
        if cmd.startswith(f"/{q}"):
            return (1, len(cmd), 0, cmd)
        if q in label:
            return (2, len(label), 1, cmd)
        if q in category:
            return (3, len(category), 2, cmd)
        if q in hint:
            return (4, len(hint), 3, cmd)
        return (9, 999, 9, cmd)

    ranked = [it for it in SLASH_MENU_ITEMS if rank(it)[0] < 9]
    ranked.sort(key=rank)
    return ranked[:10]


def slash_menu_examples(items: Sequence[SlashMenuItem], limit: int = 2) -> List[str]:
    out: List[str] = []
    for item in items:
        if item.example:
            out.append(item.example)
        if len(out) >= limit:
            break
    return out
