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
_AUDIT_CUE = re.compile(
    r"\b(audit|audita|analiza|analyze|investiga|inspect|inspecciona|revisa|scan|escanea|"
    r"bugs?|issues?|problemas?|riesgos?|hallazgos?)\b",
    re.I,
)
_FILE_CUE = re.compile(
    r"\b[\w./\\-]+\.(?:py|ts|tsx|js|jsx|json|md|yaml|yml|toml|go|java|rb|rs|cs|cpp|c|h)\b",
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


def detect_broad_readonly_plan_request(task_text: str) -> bool:
    """
    Treat broad `/plan` audits as planning work even when the user did not literally
    say "create a plan". Narrow file questions stay out of this path.
    """
    if not task_text or not isinstance(task_text, str):
        return False
    raw = task_text.strip()
    if not raw or len(raw) > 3000:
        return False
    if detect_strategy_plan_request(raw):
        return True

    normalized = _normalize_task_text(raw)
    if not normalized:
        return False

    if not _SLASH_PLAN.match(raw):
        return False
    if _FILE_CUE.search(normalized):
        return False
    return bool(_AUDIT_CUE.search(normalized))


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


def broad_plan_system_prompt_section() -> str:
    """Structured read-only planning/audit protocol for broad `/plan` requests."""
    return (
        "\n# Plan mode task\n"
        "- You are in read-only plan mode for a broad repository analysis or audit.\n"
        "- Do not treat this like a tiny bounded question.\n"
        "- Prefer a repo map or search first, then a few high-signal reads, then synthesize.\n"
        "- Avoid repeated shallow scans; keep gathering until you can support the top findings with evidence.\n"
        "- When you finish, output these exact sections in plain text:\n"
        "  Conclusion:\n"
        "  Findings:\n"
        "  Steps:\n"
        "  Evidence:\n"
        "  Next:\n"
        "- In Findings, rank the biggest risks or bugs first and keep each item concrete.\n"
        "- In Steps, list the recommended follow-up actions in sequence.\n"
        "- In Evidence, cite the files or checks that support the conclusion.\n"
    )
