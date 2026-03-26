"""
Micro-task mode: trivial bounded read-only prompts (RO-2 / RO-3 style).

This module is the **single authority** for:
- ``TASK_MODE_STANDARD`` vs ``TASK_MODE_MICRO_TASK`` (``resolve_task_mode``)
- Detection (``detect_micro_task_kind``) and feature toggles (env)

Detection reuses the same narrow classifiers as answer_now_policy (conservative).
Intake can skip planner/retrieval; EXPLORE can force a no-tools synthesis round right after
sufficient tool evidence to avoid burning iterations on planning chatter.

Integration (maintainers):
- **Iteration budget:** ``iteration_budget.category == "micro_task"`` when ``micro_task_kind`` is set
  (``apps/cli/runtime/iteration_budget.py``).
- **Planning depth:** explore planning governor treats micro-tasks as small scope; early pressure
  nudge uses ``micro_task_early_pressure_max_iter`` (``assistant.py``).
- **Evidence / early closure:** same sufficiency as bounded read-only; micro-task gates
  ``GHOST_MICRO_TASK_IMMEDIATE_SYNTHESIS`` (``evidence_sufficiency.py``).
- **Task confidence:** ``task_mode`` and ``micro_task_kind`` in snapshot signals
  (``task_confidence_engine.py``).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from apps.cli.runtime.answer_now_policy import is_high_ambiguity_or_broad_task
from apps.cli.runtime.narrow_factual_chat import (
    detect_narrow_factual_code_question,
    narrow_factual_synthesis_enabled,
)
from apps.cli.runtime.repo_overview_task import detect_repo_overview_question
from apps.cli.runtime.read_only_evidence import (
    detect_narrow_shallow_dir_listing,
    readonly_explore_early_exit_enabled,
)

# Canonical runtime mode for artifacts, logs, and confidence signals (not a SessionPhase).
TASK_MODE_STANDARD = "standard"
TASK_MODE_MICRO_TASK = "micro_task"


def resolve_task_mode(*, micro_task_kind: Optional[str], mode_enabled: bool) -> str:
    """
    Derive ``session.task_mode`` from detection output.

    ``micro_task_kind`` is ``shallow_list`` | ``narrow_factual`` | ``repo_overview`` | ``None``;
    mode is ``micro_task`` iff the flag is on and kind is non-empty.
    """
    if not mode_enabled or not (micro_task_kind and str(micro_task_kind).strip()):
        return TASK_MODE_STANDARD
    return TASK_MODE_MICRO_TASK


def micro_task_behavior_matrix() -> List[Dict[str, str]]:
    """
    Stable rows for docs/tests: what changes vs ``TASK_MODE_STANDARD``.
    Implementation lives in the modules cited in ``implementation``.
    """
    return [
        {
            "aspect": "INTAKE planner / retrieval / execution agent",
            "micro_task_behavior": "Skipped when GHOST_MICRO_TASK_SKIP_INTAKE_HEAVY=1 (default).",
            "implementation": "assistant._run_task_contract_intake",
        },
        {
            "aspect": "System prompt",
            "micro_task_behavior": "Omit heavy planner/retrieval/execution blocks; append micro-task section.",
            "implementation": "assistant._execute_stream_request / decision_planner, repo_retrieval, execution_agent",
        },
        {
            "aspect": "Iteration cap",
            "micro_task_behavior": "iteration_budget.category micro_task; lower effective_max vs write_verify.",
            "implementation": "iteration_budget.classify_iteration_budget_category",
        },
        {
            "aspect": "EXPLORE planning depth",
            "micro_task_behavior": "Small-scope governor; text-only streak nudge; early pressure iterations.",
            "implementation": "explore_planning_governor, assistant._maybe_inject_micro_task_early_pressure",
        },
        {
            "aspect": "Evidence sufficiency → early closure",
            "micro_task_behavior": "Same sufficiency eval as bounded tasks; optional immediate no-tools synthesis gated by GHOST_MICRO_TASK_IMMEDIATE_SYNTHESIS.",
            "implementation": "evidence_sufficiency.should_run_immediate_synthesis_after_sufficiency, assistant._try_evidence_sufficient_immediate_synthesis",
        },
    ]


def micro_task_mode_enabled() -> bool:
    v = os.getenv("GHOST_MICRO_TASK_MODE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def micro_task_skip_intake_heavy_enabled() -> bool:
    v = os.getenv("GHOST_MICRO_TASK_SKIP_INTAKE_HEAVY", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def micro_task_immediate_synthesis_enabled() -> bool:
    v = os.getenv("GHOST_MICRO_TASK_IMMEDIATE_SYNTHESIS", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def micro_task_early_pressure_max_iter() -> int:
    """Inject bounded-task pressure on explore iterations 1..N inclusive when micro_task."""
    try:
        n = int(os.getenv("GHOST_MICRO_TASK_EARLY_PRESSURE_ITERATIONS", "3"))
    except ValueError:
        n = 3
    return max(1, min(n, 8))


def detect_micro_task_kind(
    task_text: str,
    *,
    explore_act_blocked: bool,
    contract_spec: Optional[Dict[str, Any]],
) -> Optional[str]:
    """
    Return ``shallow_list`` | ``narrow_factual`` | ``repo_overview`` | None.

    Only activates when the prompt matches the same tight patterns as answer-now (single dir
    listing or narrow symbol question). Broad / implementation prompts never match.
    """
    if not micro_task_mode_enabled():
        return None
    if not task_text or not isinstance(task_text, str):
        return None
    if is_high_ambiguity_or_broad_task(task_text):
        return None

    if detect_narrow_shallow_dir_listing(task_text):
        listing_allowed = True
        if explore_act_blocked:
            if contract_spec is not None and contract_spec.get("readonly_early_exit") is False:
                listing_allowed = False
            elif not readonly_explore_early_exit_enabled():
                listing_allowed = False
        if listing_allowed:
            return "shallow_list"

    if detect_narrow_factual_code_question(task_text):
        if not narrow_factual_synthesis_enabled():
            return None
        return "narrow_factual"

    if detect_repo_overview_question(task_text):
        return "repo_overview"

    return None


def micro_task_system_prompt_section(kind: str) -> str:
    """Appended to the system prompt during EXPLORE (and ACT if ever reached)."""
    if kind == "shallow_list":
        return (
            "\n# Micro-task (bounded listing)\n"
            "- The user asked for a **single shallow directory listing** (one folder, one level).\n"
            "- Use **at most one or two** discovery tools (`ls` and optionally one `read_file` if needed).\n"
            "- Pattern: **one short intent → one `ls` if needed → final answer** — not plan → plan → `ls` → plan.\n"
            "- As soon as `ls` returns the requested names, **stop calling tools** and output the final answer "
            "in the exact format the user asked for (e.g. one file name per line).\n"
            "- Do **not** draft a multi-step exploration plan for this request.\n"
        )
    if kind == "narrow_factual":
        return (
            "\n# Micro-task (narrow factual)\n"
            "- The user asked a **short factual question** about a named symbol or file.\n"
            "- Use **at most one or two** `read_file` calls on the relevant path, then answer in prose.\n"
            "- Pattern: **brief plan → read if needed → answer**; avoid several text-only planning turns.\n"
            "- Do **not** explore unrelated directories or produce a long research plan.\n"
        )
    if kind == "repo_overview":
        return (
            "\n# Micro-task (repo overview)\n"
            "- The user asked for the repo/app structure and technology stack.\n"
            "- Use **one** `summarize_repo` first, then **at most two** targeted `read_file` calls on manifests if needed.\n"
            "- Pattern: **brief intent -> summarize_repo -> answer**; avoid long exploration chatter.\n"
            "- Answer with the actual stack, main modules, and structure relevant to the request.\n"
        )
    return ""
