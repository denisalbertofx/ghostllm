"""
Explicit evidence sufficiency for bounded read-only tasks (RO-2, RO-3).

Centralizes the question: given task scope, prompt specificity, and tool history,
can the requested answer be produced now, and is more exploration unlikely to help?

``decide_answer_now_after_tools`` delegates here so logs, tests, and future callers
share one implementation.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from apps.cli.runtime.exploration_redundancy import (
    redundant_ls_answer_now_enabled,
    shallow_scope_has_repeated_identical_ls,
)
from apps.cli.runtime.narrow_factual_chat import (
    detect_narrow_factual_code_question,
    evidence_sufficient_for_narrow_factual,
    narrow_factual_synthesis_enabled,
    read_file_texts_from_history,
)
from apps.cli.runtime.read_only_evidence import (
    detect_narrow_shallow_dir_listing,
    evidence_sufficient_for_shallow_listing,
    readonly_explore_early_exit_enabled,
)
from apps.cli.runtime.repo_overview_task import (
    detect_repo_overview_question,
    evidence_sufficient_for_repo_overview,
)

# Kept local to avoid import cycles with answer_now_policy (which imports this module).
_HIGH_AMBIGUITY = re.compile(
    r"(arquitectura|architecture|dise[ñn]o\s+de\s+sistema|system\s+design|"
    r"redesign|refactor(?:izar|ing)?\s+(?:el\s+|the\s+)?(?:proyecto\s+completo|entire|whole|codebase|toda\s+la\s+base)|"
    r"codebase-?wide|todos\s+los\s+m[oó]dulos|every\s+module|"
    r"compare\s+and\s+contrast|audit\s+completo|full\s+security\s+review|"
    r"migrat(?:e|ar)\s+(?:el\s+)?(?:proyecto\s+entero|entire\s+stack)|"
    r"roadmap|plan\s+de\s+sprint)",
    re.I,
)


def _answer_now_policy_enabled_flag() -> bool:
    v = os.getenv("GHOST_ANSWER_NOW_POLICY", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _is_high_ambiguity_or_broad_task(task_text: str) -> bool:
    if not task_text or not isinstance(task_text, str):
        return True
    t = task_text.strip()
    if len(t) > 3500:
        return True
    return bool(_HIGH_AMBIGUITY.search(t))


def evidence_sufficient_immediate_synthesis_enabled() -> bool:
    """After tools, one no-tools synthesis turn + EXPLORE→CLOSING when sufficiency holds."""
    v = os.getenv("GHOST_EVIDENCE_SUFFICIENT_IMMEDIATE_SYNTHESIS", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def should_run_immediate_synthesis_after_sufficiency(session: Any) -> bool:
    """
    Immediate synthesis when bounded evidence is sufficient.

    ``GHOST_MICRO_TASK_IMMEDIATE_SYNTHESIS=0`` disables the fast path **only** for sessions
    flagged as micro-task; other bounded tasks (e.g. RO-2 in chat) still follow
    ``GHOST_EVIDENCE_SUFFICIENT_IMMEDIATE_SYNTHESIS``.
    """
    if not evidence_sufficient_immediate_synthesis_enabled():
        return False
    mtk = getattr(session, "micro_task_kind", None) if session is not None else None
    if mtk:
        v = os.getenv("GHOST_MICRO_TASK_IMMEDIATE_SYNTHESIS", "1").strip().lower()
        if v in ("0", "false", "no", "off"):
            return False
    return True


@dataclass
class GatheredEvidenceSummary:
    """Lightweight view of tool history (read-side)."""

    read_file_success_count: int = 0
    ls_success_count: int = 0
    tool_messages_with_error: int = 0
    read_file_non_empty_bodies: int = 0
    read_file_approx_chars: int = 0


def summarize_gathered_evidence(history: List[Dict[str, Any]]) -> GatheredEvidenceSummary:
    s = GatheredEvidenceSummary()
    for m in history:
        if m.get("role") != "tool":
            continue
        name = m.get("name") or ""
        raw = m.get("content", "")
        if name == "read_file":
            try:
                import json

                payload = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else {}
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                continue
            if payload.get("error"):
                s.tool_messages_with_error += 1
                continue
            c = payload.get("content")
            if isinstance(c, str) and c.strip():
                s.read_file_success_count += 1
                s.read_file_non_empty_bodies += 1
                s.read_file_approx_chars += len(c)
            else:
                s.read_file_success_count += 1
        elif name == "ls":
            try:
                import json

                payload = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else {}
            except Exception:
                payload = {}
            if isinstance(payload, dict) and not payload.get("error") and isinstance(payload.get("files"), list):
                s.ls_success_count += 1
            elif isinstance(payload, dict) and payload.get("error"):
                s.tool_messages_with_error += 1
    return s


@dataclass
class EvidenceSufficiencyResult:
    """Outcome of ``evaluate_evidence_sufficiency_for_task``."""

    sufficient: bool
    reason: str
    confidence: float
    task_family: str  # shallow_list | narrow_factual | repo_overview | none
    synthesis_context: str  # readonly | factual | none
    unlikely_more_exploration_helps: bool
    details: Dict[str, Any] = field(default_factory=dict)


def evaluate_evidence_sufficiency_for_task(
    *,
    task_text: str,
    history: List[Dict[str, Any]],
    contract_spec: Optional[Dict[str, Any]],
    discovery_action_count: int,
    explore_act_blocked: bool,
    policy_enabled: Optional[bool] = None,
) -> EvidenceSufficiencyResult:
    """
    Decide if gathered evidence is enough to answer without further exploration.

    Conservative: only ``shallow_list`` and ``narrow_factual`` families (same as answer-now).
    """
    pol = _answer_now_policy_enabled_flag() if policy_enabled is None else bool(policy_enabled)
    gath = summarize_gathered_evidence(history)
    base_details: Dict[str, Any] = {
        "gathered": {
            "read_file_success": gath.read_file_success_count,
            "ls_success": gath.ls_success_count,
            "read_chars": gath.read_file_approx_chars,
            "tool_errors": gath.tool_messages_with_error,
        },
        "discovery_action_count": discovery_action_count,
    }

    def _insufficient(reason: str) -> EvidenceSufficiencyResult:
        return EvidenceSufficiencyResult(
            sufficient=False,
            reason=reason,
            confidence=0.0,
            task_family="none",
            synthesis_context="none",
            unlikely_more_exploration_helps=False,
            details=dict(base_details),
        )

    if not pol:
        return _insufficient("answer_now_policy_disabled")
    if not task_text.strip():
        return _insufficient("empty_task")
    if contract_spec is not None and contract_spec.get("answer_now_policy") is False:
        return _insufficient("spec_opt_out_answer_now")
    if _is_high_ambiguity_or_broad_task(task_text):
        return _insufficient("high_ambiguity_or_broad")

    list_scope = detect_narrow_shallow_dir_listing(task_text)
    if list_scope:
        listing_allowed = True
        if explore_act_blocked:
            if contract_spec is not None and contract_spec.get("readonly_early_exit") is False:
                listing_allowed = False
            elif not readonly_explore_early_exit_enabled():
                listing_allowed = False
        if (
            listing_allowed
            and redundant_ls_answer_now_enabled()
            and shallow_scope_has_repeated_identical_ls(history, list_scope.path_hint)
        ):
            return EvidenceSufficiencyResult(
                sufficient=True,
                reason="repeated_identical_ls_no_delta",
                confidence=0.88,
                task_family="shallow_list",
                synthesis_context="readonly",
                unlikely_more_exploration_helps=True,
                details={**base_details, "path_hint": list_scope.path_hint},
            )
        ok, why = evidence_sufficient_for_shallow_listing(
            list_scope, history, discovery_action_count=discovery_action_count
        )
        if ok and listing_allowed and discovery_action_count >= 1:
            return EvidenceSufficiencyResult(
                sufficient=True,
                reason=why,
                confidence=0.9 if why == "shallow_listing_ok" else 0.82,
                task_family="shallow_list",
                synthesis_context="readonly",
                unlikely_more_exploration_helps=True,
                details={**base_details, "path_hint": list_scope.path_hint, "extension": list_scope.extension},
            )
        # Shallow scope matched but listing not satisfied yet: allow narrow-factual fallback (same as answer-now).

    factual_scope = detect_narrow_factual_code_question(task_text)
    if factual_scope:
        if not narrow_factual_synthesis_enabled():
            return _insufficient("narrow_factual_synthesis_disabled")
        if discovery_action_count < 1:
            return _insufficient("no_discovery_yet")
        if not evidence_sufficient_for_narrow_factual(factual_scope, history):
            texts = read_file_texts_from_history(history)
            return _insufficient(
                "symbol_not_in_read_evidence"
                if texts
                else "no_read_file_evidence",
            )
        return EvidenceSufficiencyResult(
            sufficient=True,
            reason="symbol_evidence_ok",
            confidence=0.87,
            task_family="narrow_factual",
            synthesis_context="factual",
            unlikely_more_exploration_helps=True,
            details={**base_details, "symbols": list(factual_scope.symbols)},
        )

    if detect_repo_overview_question(task_text):
        ok, why = evidence_sufficient_for_repo_overview(
            history,
            discovery_action_count=discovery_action_count,
        )
        if ok:
            return EvidenceSufficiencyResult(
                sufficient=True,
                reason=why,
                confidence=0.86 if why == "repo_summary_ok" else 0.8,
                task_family="repo_overview",
                synthesis_context="readonly",
                unlikely_more_exploration_helps=True,
                details=dict(base_details),
            )
        return _insufficient(why)

    return _insufficient("not_bounded_readonly_family")


def sufficiency_result_to_answer_now_tuple(
    r: EvidenceSufficiencyResult,
) -> Optional[tuple[str, str, str]]:
    """(kind, evidence_reason, synthesis_context) for legacy callers, or None."""
    if not r.sufficient or r.task_family == "none":
        return None
    return (r.task_family, r.reason, r.synthesis_context)
