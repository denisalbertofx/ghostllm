"""
Dynamic iteration budget: effective loop cap per task class, below global GHOST_MAX_ITERATIONS.

Small read-only / micro tasks get a tight cap to force earlier synthesis; complex analysis and
refactors keep the full global safety limit.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from apps.cli.runtime.answer_now_policy import (
    bounded_code_task_kind,
    is_high_ambiguity_or_broad_task,
)
from apps.cli.runtime.task_contract import Intent, infer_work_task_type


def iteration_budget_strategy_enabled() -> bool:
    v = os.getenv("GHOST_ITERATION_BUDGET_STRATEGY", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def fast_simple_write_budget_enabled() -> bool:
    return os.getenv("GHOST_FAST_SIMPLE_WRITE_BUDGET", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _heuristic_simple_write_task(task_text: str, intent: Intent) -> bool:
    """Short create/add-file style prompts without heavy multi-file signals."""
    if not task_text or len(task_text) > 380:
        return False
    if (intent.mode or "").strip().lower() == "plan":
        return False
    tl = task_text.lower()
    if any(
        x in tl
        for x in (
            "refactor",
            "migrate",
            "every file",
            "all files",
            "entire codebase",
            "test suite",
            "docker",
            "database",
            "kubernetes",
        )
    ):
        return False
    hints = (
        "create ",
        "create a ",
        "add ",
        "add a ",
        "new file",
        "write a file",
        "write file",
        "subfolder",
        "subdirectory",
        "mkdir",
        "touch ",
    )
    if any(h in tl for h in hints):
        return True

    explicit_file_path = bool(
        re.search(
            r"\b[\w./\\-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|json|md|yaml|yml|toml)\b",
            task_text,
            re.I,
        )
    )
    focused_edit_verbs = (
        "edit ",
        "update ",
        "modify ",
        "change ",
        "fix ",
        "adjust ",
        "corrige",
        "actualiza",
        "modifica",
        "cambia",
        "edita",
        "arregla",
    )
    return explicit_file_path and any(v in tl for v in focused_edit_verbs)


def _parse_positive_int(name: str, default: int) -> int:
    try:
        n = int(os.getenv(name, str(default)).strip())
    except ValueError:
        n = default
    return max(1, n)


def global_iteration_cap() -> int:
    return _parse_positive_int("GHOST_MAX_ITERATIONS", 15)


@dataclass(frozen=True)
class IterationBudgetPlan:
    """Snapshot applied once after INTAKE."""

    category: str
    global_cap: int
    effective_max: int
    recommended_min: int
    recommended_max: int
    rationale: str


# Recommended ranges are UX/documentation hints (artifact); effective_max drives the loop.
_RECOMMENDED_RANGES: Dict[str, tuple[int, int]] = {
    "micro_task": (3, 7),
    "small_read_only": (5, 11),
    "factual_code_question": (5, 12),
    "simple_write": (5, 11),
    "write_verify": (8, 18),
    "complex_multi_file": (10, 99),
    "refactor_multi_step": (12, 99),
}


def classify_iteration_budget_category(
    *,
    task_text: str,
    intent: Intent,
    micro_task_kind: Optional[str],
    task_intent: str,
    change_expectation: str,
) -> tuple[str, str]:
    """
    Return (category, short rationale) for telemetry.

    Order: most specific / smallest budget first where applicable.
    """
    if micro_task_kind:
        return "micro_task", f"micro_task_kind={micro_task_kind}"

    if fast_simple_write_budget_enabled() and _heuristic_simple_write_task(task_text, intent):
        return "simple_write", "GHOST_FAST_SIMPLE_WRITE_BUDGET heuristic"

    if is_high_ambiguity_or_broad_task(task_text):
        return "complex_multi_file", "high_ambiguity_or_broad_heuristic"

    bkind = bounded_code_task_kind(task_text)
    if bkind == "narrow_factual":
        return "factual_code_question", "bounded_narrow_factual"
    if bkind == "shallow_list":
        return "small_read_only", "bounded_shallow_listing"

    ti = (task_intent or "").strip().lower()
    if ti == "refactor" or infer_work_task_type(task_text) == "refactor":
        return "refactor_multi_step", "refactor_intent_or_work_type"

    if ti in ("analysis", "research") or (intent.mode or "").strip() == "Analyze" or (
        intent.task_type or ""
    ).strip().lower() == "architect":
        return "complex_multi_file", "analysis_research_or_architect"

    ce = (change_expectation or "").strip().lower()
    im = (intent.mode or "").strip().lower()
    if ce == "should_not_write" or im == "plan":
        return "small_read_only", "read_only_or_plan_mode"

    if im == "execute" or (intent.task_type or "").strip().lower() in (
        "fix",
        "code",
        "debug",
        "scaffold",
    ):
        return "write_verify", "execute_or_implementation_task_type"

    if ce in ("must_write", "may_write"):
        return "write_verify", "change_expectation_allows_write"

    return "write_verify", "default_implementation_shape"


def _raw_cap_for_category(category: str, global_cap: int) -> int:
    """Upper bound for category before clamping to global_cap (complex uses full global)."""
    if category in ("complex_multi_file", "refactor_multi_step"):
        return global_cap
    defaults = {
        "micro_task": 8,
        "small_read_only": 10,
        "factual_code_question": 11,
        "simple_write": 10,
        "write_verify": 14,
    }
    env_keys = {
        "micro_task": "GHOST_ITER_BUDGET_MICRO_MAX",
        "small_read_only": "GHOST_ITER_BUDGET_SMALL_READONLY_MAX",
        "factual_code_question": "GHOST_ITER_BUDGET_FACTUAL_MAX",
        "simple_write": "GHOST_ITER_BUDGET_SIMPLE_WRITE_MAX",
        "write_verify": "GHOST_ITER_BUDGET_WRITE_VERIFY_MAX",
    }
    d = defaults.get(category, global_cap)
    key = env_keys.get(category)
    raw = _parse_positive_int(key, d) if key else global_cap
    return min(raw, global_cap)


def compute_iteration_budget_plan(
    *,
    task_text: str,
    intent: Intent,
    session: Any,
    global_cap: Optional[int] = None,
) -> IterationBudgetPlan:
    cap = int(global_cap) if global_cap is not None else global_iteration_cap()
    cap = max(3, cap)

    if not iteration_budget_strategy_enabled():
        lo, hi = 1, cap
        return IterationBudgetPlan(
            category="strategy_disabled",
            global_cap=cap,
            effective_max=cap,
            recommended_min=lo,
            recommended_max=hi,
            rationale="GHOST_ITERATION_BUDGET_STRATEGY off",
        )

    micro = getattr(session, "micro_task_kind", None)
    cat, why = classify_iteration_budget_category(
        task_text=task_text,
        intent=intent,
        micro_task_kind=micro if isinstance(micro, str) else None,
        task_intent=str(getattr(session, "task_intent", "") or ""),
        change_expectation=str(getattr(session, "change_expectation", "") or ""),
    )

    raw = _raw_cap_for_category(cat, cap)
    if cat in ("complex_multi_file", "refactor_multi_step"):
        effective = cap
    else:
        effective = min(cap, raw)

    # Never below 4: need room for intake tail + at least a couple explore turns.
    effective = max(4, min(effective, cap))

    rmin, rmax = _RECOMMENDED_RANGES.get(cat, (5, cap))
    rmax = min(rmax, cap)

    return IterationBudgetPlan(
        category=cat,
        global_cap=cap,
        effective_max=effective,
        recommended_min=rmin,
        recommended_max=rmax,
        rationale=why,
    )


def apply_iteration_budget_to_session(session: Any, *, task_text: str, intent: Intent) -> IterationBudgetPlan:
    """Populate session.iteration_budget and append a session event."""
    plan = compute_iteration_budget_plan(task_text=task_text, intent=intent, session=session)
    blob: Dict[str, Any] = {
        "category": plan.category,
        "global_cap": plan.global_cap,
        "effective_max": plan.effective_max,
        "recommended_min": plan.recommended_min,
        "recommended_max": plan.recommended_max,
        "rationale": plan.rationale,
    }
    session.iteration_budget = blob
    ev = {"event": "iteration_budget_applied", **blob}
    if getattr(session, "events", None) is not None:
        session.events.append(ev)
    return plan


def adapt_synthesis_reserve_iterations(effective_iteration_cap: int, base_reserve: int) -> int:
    """Tighter reserve when the session cap is small (earlier pressure nudges)."""
    b = max(0, int(base_reserve))
    cap = int(effective_iteration_cap)
    if cap <= 7:
        return min(b, 1)
    if cap <= 10:
        return min(b, 2)
    return b
