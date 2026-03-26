"""
INTAKE: populate task_contract['spec'] from the TaskSpec engine or legacy adapter.

Single entry for assistant INTAKE after ensure_task_contract_foundation + repo profile scan.
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional, Tuple

from apps.cli.runtime.repo_profile import RepoProfile
from apps.cli.runtime.task_contract import get_task_contract_spec
from apps.cli.runtime.taskspec_engine import build_taskspec
from apps.cli.runtime.taskspec_adapter import apply_spec_engine_build_to_session
from apps.cli.runtime.adapters.legacy_intake import (
    is_taskspec_engine_enabled,
    legacy_fill_session,
)

logger = logging.getLogger(__name__)

IntakeSource = Literal["taskspec", "legacy_engine_off", "legacy_invalid_engine"]


def intake_fill_task_contract_spec(
    session: Any,
    user_prompt: str,
    repo_profile: RepoProfile,
) -> Tuple[IntakeSource, Optional[dict]]:
    """
    Fill task_contract['spec'] and session mirrors. Returns (source_tag, budget_policy or None).
    """
    if not is_taskspec_engine_enabled():
        legacy_fill_session(session, user_prompt)
        return "legacy_engine_off", None

    build = build_taskspec(user_prompt, repo_profile)
    if build.validation_status == "invalid":
        logger.warning(
            "TaskSpec remained invalid after repair; legacy contract. Errors: %s",
            build.validation_errors,
        )
        legacy_fill_session(session, user_prompt)
        return "legacy_invalid_engine", None

    apply_spec_engine_build_to_session(session, build, repo_profile, user_prompt=user_prompt)
    spec = get_task_contract_spec(session)
    bp = (spec or {}).get("budget_policy") if isinstance(spec, dict) else None
    return "taskspec", bp if isinstance(bp, dict) else None
