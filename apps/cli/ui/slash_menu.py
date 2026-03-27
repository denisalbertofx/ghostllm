from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class SlashMenuItem:
    command: str
    label: str
    hint: str
    accepts_args: bool = True


@dataclass(frozen=True)
class SlashMenuState:
    active: bool
    query: str
    items: List[SlashMenuItem]
    selected: int


SLASH_MENU_ITEMS: List[SlashMenuItem] = [
    SlashMenuItem("/plan", "Plan sin cambios", "Explora, revisa y propone siguiente paso"),
    SlashMenuItem("/do", "Ejecuta una tarea", "Actúa sobre el repo con herramientas"),
    SlashMenuItem("/edit", "Editar archivo", "Abre un flujo directo de patch"),
    SlashMenuItem("/fix", "Buscar y arreglar", "Encuentra issues y corrígelos"),
    SlashMenuItem("/doctor", "Diagnóstico", "Chequeo rápido del sistema", accepts_args=False),
    SlashMenuItem("/status", "Estado", "Ver daemon y gateway", accepts_args=False),
    SlashMenuItem("/logs", "Logs", "Últimas líneas del daemon"),
    SlashMenuItem("/tasks", "Tareas", "Lista de tareas activas", accepts_args=False),
    SlashMenuItem("/board", "Board", "Vista rápida de workers", accepts_args=False),
    SlashMenuItem("/swarm", "Swarm", "Inicializa o inspecciona workers", accepts_args=False),
    SlashMenuItem("/review", "Review bundle", "Abre un paquete de revisión"),
    SlashMenuItem("/approve", "Aprobar", "Aprueba una review con task id"),
    SlashMenuItem("/reject", "Rechazar", "Rechaza una review con task id"),
    SlashMenuItem("/batch start", "Batch", "Lanza varias tareas en lote"),
]


def slash_menu_quick_actions() -> List[str]:
    return ["/plan", "/do", "/fix", "/doctor"]


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


def _filter_slash_items(query: str) -> List[SlashMenuItem]:
    q = (query or "").strip().lower()
    if not q:
        return SLASH_MENU_ITEMS[:8]

    def rank(item: SlashMenuItem) -> tuple[int, int, str]:
        cmd = item.command.lower()
        label = item.label.lower()
        hint = item.hint.lower()
        if cmd == f"/{q}":
            return (0, len(cmd), cmd)
        if cmd.startswith(f"/{q}"):
            return (1, len(cmd), cmd)
        if q in label:
            return (2, len(label), cmd)
        if q in hint:
            return (3, len(hint), cmd)
        return (9, 999, cmd)

    ranked = [it for it in SLASH_MENU_ITEMS if rank(it)[0] < 9]
    ranked.sort(key=rank)
    return ranked[:8]
