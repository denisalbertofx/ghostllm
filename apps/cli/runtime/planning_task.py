"""
Fast-path detection and prompt guidance for broad planning / architecture tasks.

Examples:
- "/plan creame un plan para mejorar la arquitectura de esta app"
- "dame un roadmap para reorganizar el runtime"
- "plan por fases para refactorizar el gateway"
"""
from __future__ import annotations

import re

_LEADING_SLASH = re.compile(r"^\s*/[a-zA-Z0-9_-]+\b\s*")
_SLASH_PLAN = re.compile(r"^\s*/plan\b", re.I)

_PLAN_CUE = re.compile(
    r"\b(plan|roadmap|estrategia|strategy|fases?|pasos?|iteraciones?|outline)\b",
    re.I,
)
_ARCHITECTURE_CUE = re.compile(
    r"\b(arquitectura|architecture|estructura|structure|reorganiz(?:ar|arlo|ation|e)|"
    r"organizar|mejorar|improve|refactor(?:izar|ing)?|capas?|layers?|runtime|gateway|cli|"
    r"producto|product|escalable|scalable|monorepo|repo|repositorio|codebase|app)\b",
    re.I,
)
_REQUEST_CUE = re.compile(
    r"\b(crea(?:me)?|dame|haz(?:me)?|arma|prop[oó]n|draft|design|planifica|quiero)\b",
    re.I,
)


def _normalize_task_text(task_text: str) -> str:
    t = (task_text or "").strip()
    return _LEADING_SLASH.sub("", t, count=1).strip()


def detect_strategy_plan_request(task_text: str) -> bool:
    """
    True when the user is asking for a broad plan / roadmap, not direct execution.

    Conservative enough to avoid small factual prompts, but broad enough to treat
    "/plan crea un plan..." as planning even when it starts with verbs like "crea".
    """
    if not task_text or not isinstance(task_text, str):
        return False
    raw = task_text.strip()
    if not raw or len(raw) > 3000:
        return False

    normalized = _normalize_task_text(raw)
    if not normalized:
        return False

    has_plan = bool(_PLAN_CUE.search(normalized))
    has_architecture = bool(_ARCHITECTURE_CUE.search(normalized))
    has_request = bool(_REQUEST_CUE.search(normalized))
    slash_plan = bool(_SLASH_PLAN.match(raw))

    if slash_plan and (has_plan or has_architecture):
        return True
    if has_plan and has_architecture:
        return True
    if has_plan and has_request:
        return True
    return False


def strategy_plan_system_prompt_section() -> str:
    """Extra planning discipline injected into the system prompt for broad `/plan` tasks."""
    return (
        "\n# Strategic planning task\n"
        "- This request is asking for a plan / roadmap, not code changes.\n"
        "- Stay read-only. Do not behave as if you must edit files or run verification.\n"
        "- Prefer a few high-signal reads over broad repo crawling.\n"
        "- Output structure should be: current state, main bottlenecks, target architecture, phased plan, next iteration.\n"
        "- Keep the plan concrete and sequenced; avoid generic clean-architecture filler.\n"
    )
