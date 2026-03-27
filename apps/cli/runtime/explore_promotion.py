"""
EXPLORE → ACT promotion: explicit rules from runtime evidence (not model whim).

Reason codes are stable snake_case strings for logs and artifact events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Tuple


# Machine-readable promotion reason codes (stable contract)
REASON_REJECTED_WRITE_ATTEMPT = "rejected_write_attempt"
REASON_EXPLORATION_BUDGET_EXHAUSTED = "exploration_budget_exhausted"
REASON_DISCOVERY_CAP_REACHED = "discovery_cap_reached"
REASON_READ_ONLY_STAGNATION_THRESHOLD = "read_only_stagnation_threshold"
REASON_REPO_CONTEXT_SUFFICIENT = "repo_context_sufficient"
REASON_FOCUSED_WRITE_CONTEXT_READY = "focused_write_context_ready"
REASON_SIMPLE_WRITE_MIN_CONTEXT = "simple_write_min_context"
REASON_REPEATED_TARGET_READ = "repeated_target_read"
REASON_EXPLICIT_MODEL_MODIFY_INTENT = "explicit_model_modify_intent"
REASON_EXPLORE_TEXT_ONLY_STREAK = "explore_text_only_streak"


_MODIFY_INTENT_PATTERNS = (
    re.compile(
        r"\b("
        r"write_file|edit_file|delete_file|patch_file|apply(_|\s)edit|"
        r"will\s+(edit|modify|change|update|write)|"
        r"going\s+to\s+(edit|modify|change|update|write)|"
        r"i(?:'ll|\s+will)\s+(edit|modify|patch|change|update)|"
        r"implement(ing)?\s+(the\s+)?(change|fix|feature)|"
        r"modif(y|ying)\s+(the\s+)?(file|code)|"
        r"create\s+(a\s+)?new\s+file|"
        r"a├▒ade|crea|modifica|cambia|actualiza|edita|corrige|"
        r"voy\s+a\s+(a├▒adir|crear|modificar|cambiar|actualizar|editar|corregir)"
        r")\b",
        re.I,
    ),
)


@dataclass(frozen=True)
class ExploreTurnResult:
    """One EXPLORE loop iteration outcome (assistant message + tool dispatch)."""

    kind: str
    executed_tool_names: Tuple[str, ...] = ()
    rejected_write_tool_names: Tuple[str, ...] = ()
    assistant_text: str = ""
    stagnation_reason: str = ""


@dataclass
class ExplorationMetrics:
    discovery_action_count: int
    discovery_cap: int
    budget_remaining: int
    budget_exhausted: bool
    explore_text_only_streak: int
    unique_read_paths: int
    target_file_count: int
    relevant_read_hits: int
    max_target_read_count: int
    focused_write_task: bool
    simple_write_task: bool
    policy_allows_act: bool
    min_unique_reads_for_context: int = 3
    max_text_only_streak: int = 2
    min_discovery_for_context: int = 2
    last_promotion_reason: str = field(default="", repr=False)


def _session_blocks_act(session: Any) -> bool:
    if session is None:
        return False
    ce = getattr(session, "change_expectation", "") or ""
    return ce.strip().lower() == "should_not_write"


def text_signals_modify_intent(text: str) -> bool:
    if not text or not isinstance(text, str):
        return False
    t = text.strip()
    if len(t) < 12:
        return False
    return any(p.search(t) for p in _MODIFY_INTENT_PATTERNS)


def should_promote_explore_to_act(
    session: Any,
    turn_result: ExploreTurnResult,
    exploration_metrics: ExplorationMetrics,
) -> bool:
    """
    True if EXPLORE should transition to ACT on explicit runtime/policy evidence.

    API note: the function returns only bool; on True it sets
    ``exploration_metrics.last_promotion_reason`` to a stable reason code
    (see REASON_* module constants) for logging and artifacts.
    """
    exploration_metrics.last_promotion_reason = ""
    if not exploration_metrics.policy_allows_act or _session_blocks_act(session):
        return False

    m = exploration_metrics
    tr = turn_result

    if tr.rejected_write_tool_names:
        exploration_metrics.last_promotion_reason = REASON_REJECTED_WRITE_ATTEMPT
        return True

    if m.budget_exhausted or m.budget_remaining <= 0:
        exploration_metrics.last_promotion_reason = REASON_EXPLORATION_BUDGET_EXHAUSTED
        return True

    if m.discovery_action_count >= m.discovery_cap:
        exploration_metrics.last_promotion_reason = REASON_DISCOVERY_CAP_REACHED
        return True

    if tr.stagnation_reason or tr.kind in ("stagnation_mid_tools", "precheck_stagnation"):
        exploration_metrics.last_promotion_reason = REASON_READ_ONLY_STAGNATION_THRESHOLD
        return True

    if (
        m.focused_write_task
        and m.target_file_count > 0
        and m.relevant_read_hits >= 1
        and m.discovery_action_count >= 1
    ):
        exploration_metrics.last_promotion_reason = REASON_FOCUSED_WRITE_CONTEXT_READY
        return True

    if (
        m.focused_write_task
        and m.target_file_count > 0
        and m.max_target_read_count >= 2
    ):
        exploration_metrics.last_promotion_reason = REASON_REPEATED_TARGET_READ
        return True

    if (
        m.simple_write_task
        and m.discovery_action_count >= m.min_discovery_for_context
        and (m.relevant_read_hits >= 1 or m.unique_read_paths >= 1)
    ):
        exploration_metrics.last_promotion_reason = REASON_SIMPLE_WRITE_MIN_CONTEXT
        return True

    if (
        m.unique_read_paths >= m.min_unique_reads_for_context
        and m.discovery_action_count >= m.min_discovery_for_context
    ):
        exploration_metrics.last_promotion_reason = REASON_REPO_CONTEXT_SUFFICIENT
        return True

    if text_signals_modify_intent(tr.assistant_text):
        exploration_metrics.last_promotion_reason = REASON_EXPLICIT_MODEL_MODIFY_INTENT
        return True

    if tr.kind == "text_only" and m.explore_text_only_streak >= m.max_text_only_streak:
        exploration_metrics.last_promotion_reason = REASON_EXPLORE_TEXT_ONLY_STREAK
        return True

    return False
