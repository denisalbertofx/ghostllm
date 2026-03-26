"""
Reduce EXPLORE over-planning on small-scope tasks: cap text-only “planning” streaks and
suppress redundant ``ls`` on directories already listed successfully in-session.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Set

from apps.cli.runtime.answer_now_policy import bounded_code_task_kind
from apps.cli.runtime.exploration_redundancy import iter_ls_events_chronological
from apps.cli.runtime.read_only_evidence import normalize_path_hint_for_listing, paths_align_for_listing

_SMALL_SCOPE_ITER_BUDGET = frozenset(
    {"micro_task", "small_read_only", "factual_code_question", "simple_write"},
)


def explore_planning_governor_enabled() -> bool:
    v = os.getenv("GHOST_EXPLORE_PLANNING_GOVERNOR", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def small_scope_planning_text_threshold() -> int:
    try:
        n = int(os.getenv("GHOST_EXPLORE_SMALL_SCOPE_PLANNING_TEXT_THRESHOLD", "2"))
    except ValueError:
        n = 2
    return max(1, min(n, 6))


def session_is_small_explore_scope(session: Any) -> bool:
    """Tight read-only / bounded tasks where multi-turn planning is usually waste."""
    if session is None:
        return False
    if getattr(session, "micro_task_kind", None):
        return True
    ib = getattr(session, "iteration_budget", None) or {}
    if isinstance(ib, dict):
        cat = str(ib.get("category") or "")
        if cat in _SMALL_SCOPE_ITER_BUDGET:
            return True
    return False


def assistant_text_suggests_protracted_planning(text: str) -> bool:
    """Conservative: long assistant monologue or explicit multi-step plan markers."""
    if not text or not isinstance(text, str):
        return False
    t = text.strip()
    if len(t) >= 420:
        return True
    return bool(
        _PLANNING_MARKERS.search(t)
    )


_PLANNING_MARKERS = re.compile(
    r"(step\s*[0-9]|^\s*[-*]\s+step\b|my\s+plan|exploration\s+plan|##\s*plan\b|"
    r"first,?\s+i\s+will|next,?\s+i\s+will|i\s+will\s+(start|begin)|"
    r"let\s+me\s+outline|phase\s*[0-9]|paso\s*[0-9])",
    re.I | re.M,
)


def normalize_ls_path_key(path: str) -> str:
    return normalize_path_hint_for_listing(path or ".")


def ls_target_already_listed_successfully(
    history: List[Dict[str, Any]],
    requested_path: str,
    paths_listed_this_dispatch: Set[str],
) -> bool:
    """
    True if this directory was already listed successfully in history or earlier in the same
    tool round (duplicate ``ls`` in one assistant message).
    """
    req = normalize_ls_path_key(requested_path)
    for k in paths_listed_this_dispatch:
        if _paths_same_dir(req, k):
            return True
    for ls_path, _sig in iter_ls_events_chronological(history):
        if _paths_same_dir(req, ls_path):
            return True
    return False


def _paths_same_dir(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    return paths_align_for_listing(a, b) or paths_align_for_listing(b, a)
