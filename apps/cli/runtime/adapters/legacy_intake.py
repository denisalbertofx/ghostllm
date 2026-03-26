"""
Legacy TaskContract fill: keyword/heuristic intent classifier → task_contract['spec'].

Used only when the TaskSpec engine is disabled (env) or returns invalid output.
Operational runtime must read spec via get_task_contract_spec(session), not this module.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from apps.cli.runtime.execution_agent import clear_execution_agent_fields
from apps.cli.runtime.intent_classifier import classify_task_intent
from apps.cli.runtime.repo_retrieval import clear_retrieval_session_fields
from apps.cli.runtime.task_contract import (
    Intent,
    ensure_task_contract_foundation,
    update_task_contract_after_legacy_fill,
)

logger = logging.getLogger(__name__)

ENV_USE_TASKSPEC = "GHOST_USE_TASKSPEC"


def is_taskspec_engine_enabled() -> bool:
    """False when GHOST_USE_TASKSPEC disables the structured TaskSpec engine (legacy fill only)."""
    v = os.environ.get(ENV_USE_TASKSPEC, "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def legacy_fill_session(session: Any, user_prompt: str) -> None:
    """Classify intent from raw text and write task_contract['spec'] (legacy source tag)."""
    if not isinstance(getattr(session, "task_contract", None), dict):
        ensure_task_contract_foundation(
            session,
            Intent(mode="Chat", task=user_prompt, original_text=user_prompt),
        )
    r = classify_task_intent(user_prompt)
    session.task_intent = r.intent
    session.task_scope = r.scope
    session.change_expectation = r.change_expectation
    session.intent_confidence = r.confidence
    session.intent_reasoning_lines = list(r.reasoning_lines)
    session.runtime_contract_source = "legacy"
    session.contract_spec_validation_status = ""
    session.contract_spec_repaired = False
    session.contract_spec_validation_errors = []
    session.repo_profile = {}
    session.exploration_plan = {}
    session.repo_profile_influence = []
    session.planner_version = ""
    session.planner_advisory_mode = True
    session.planner_risk_level = ""
    session.planner_estimated_complexity = ""
    session.planner_provenance = {}
    session.planner_used = False
    clear_retrieval_session_fields(session)
    clear_execution_agent_fields(session)
    scope_val = r.scope
    scope_list: List[str] = [scope_val] if scope_val and str(scope_val) != "unknown" else []
    legacy_spec: Dict[str, Any] = {
        "intent": r.intent,
        "scope": scope_list,
        "change_expectation": r.change_expectation,
        "confidence": r.confidence,
        "reasoning_lines": list(r.reasoning_lines),
        "forbidden_layers": [],
        "target_files": [],
        "acceptance_criteria": [],
        "verification_policy": {},
        "repair_policy": {},
        "budget_policy": {},
    }
    update_task_contract_after_legacy_fill(session, legacy_spec)
