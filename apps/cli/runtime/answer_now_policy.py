"""
Unified \"answer now\" policy for tiny, bounded code questions (RO-2, RO-3 style).

Combines shallow directory listings and narrow factual symbol questions under one gate:
prompt narrowness, low ambiguity, evidence sufficiency, no writes (policy is read-side only).

Does not replace TaskSpec; conservative extra reject patterns block architecture-wide asks.

Implementation delegates evidence checks to ``evaluate_evidence_sufficiency_for_task`` so tests,
logging, and future phase logic share one path.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from apps.cli.runtime.evidence_sufficiency import (
    evaluate_evidence_sufficiency_for_task,
    EvidenceSufficiencyResult,
    sufficiency_result_to_answer_now_tuple,
)
from apps.cli.runtime.narrow_factual_chat import detect_narrow_factual_code_question
from apps.cli.runtime.read_only_evidence import detect_narrow_shallow_dir_listing
from apps.cli.runtime.repo_overview_task import detect_repo_overview_question


def answer_now_policy_enabled() -> bool:
    v = os.getenv("GHOST_ANSWER_NOW_POLICY", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


# Tasks that need deep exploration — never answer_now from this policy
_HIGH_AMBIGUITY = re.compile(
    r"(arquitectura|architecture|dise[ñn]o\s+de\s+sistema|system\s+design|"
    r"redesign|refactor(?:izar|ing)?\s+(?:el\s+|the\s+)?(?:proyecto\s+completo|entire|whole|codebase|toda\s+la\s+base)|"
    r"codebase-?wide|todos\s+los\s+m[oó]dulos|every\s+module|"
    r"compare\s+and\s+contrast|audit\s+completo|full\s+security\s+review|"
    r"migrat(?:e|ar)\s+(?:el\s+)?(?:proyecto\s+entero|entire\s+stack)|"
    r"roadmap|plan\s+de\s+sprint)",
    re.I,
)


def is_high_ambiguity_or_broad_task(task_text: str) -> bool:
    if not task_text or not isinstance(task_text, str):
        return True
    t = task_text.strip()
    if len(t) > 3500:
        return True
    return bool(_HIGH_AMBIGUITY.search(t))


@dataclass(frozen=True)
class AnswerNowDecision:
    """Emit after tools when we should nudge final synthesis next turn."""

    kind: str  # "shallow_list" | "narrow_factual"
    evidence_reason: str
    synthesis_context: str  # "readonly" | "factual" — maps to explore_to_closing_*


def decide_answer_now_after_tools_with_details(
    *,
    task_text: str,
    history: List[Dict[str, Any]],
    contract_spec: Optional[Dict[str, Any]],
    discovery_action_count: int,
    explore_act_blocked: bool,
) -> Tuple[Optional[AnswerNowDecision], EvidenceSufficiencyResult]:
    """
    Same as ``decide_answer_now_after_tools`` but also returns the full sufficiency result
    (for logging / telemetry without re-evaluating).
    """
    r = evaluate_evidence_sufficiency_for_task(
        task_text=task_text,
        history=history,
        contract_spec=contract_spec,
        discovery_action_count=discovery_action_count,
        explore_act_blocked=explore_act_blocked,
    )
    t = sufficiency_result_to_answer_now_tuple(r)
    if t is None:
        return None, r
    kind, evidence_reason, synthesis_context = t
    return AnswerNowDecision(kind, evidence_reason, synthesis_context), r


def decide_answer_now_after_tools(
    *,
    task_text: str,
    history: List[Dict[str, Any]],
    contract_spec: Optional[Dict[str, Any]],
    discovery_action_count: int,
    explore_act_blocked: bool,
) -> Optional[AnswerNowDecision]:
    """
    If evidence already satisfies a bounded task, return decision to nudge answer (no more tools).

    ``explore_act_blocked`` is True in Plan mode or should_not_write — shallow listing only applies
    when read-only early exit is enabled OR we still classify listing for Chat (see below).

    Shallow listing: on success returns ``synthesis_context`` ``readonly`` (same closing path as before).

    Narrow factual: returns ``factual`` when symbol evidence is in read_file history.
    """
    dec, _ = decide_answer_now_after_tools_with_details(
        task_text=task_text,
        history=history,
        contract_spec=contract_spec,
        discovery_action_count=discovery_action_count,
        explore_act_blocked=explore_act_blocked,
    )
    return dec


def bounded_code_task_kind(task_text: str) -> Optional[str]:
    """Lightweight classifier label for pressure / last-iter tool suppression (no history)."""
    if not answer_now_policy_enabled() or is_high_ambiguity_or_broad_task(task_text):
        return None
    if detect_narrow_shallow_dir_listing(task_text):
        return "shallow_list"
    if detect_narrow_factual_code_question(task_text):
        return "narrow_factual"
    if detect_repo_overview_question(task_text):
        return "repo_overview"
    return None
