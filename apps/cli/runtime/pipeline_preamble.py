"""
Parallel preamble: overlap decision planner (CPU) with retrieval index build (I/O).

Controlled by GHOST_PARALLEL_PIPELINE (partial | on | aggressive | 1 | true | yes).
Requires non-empty task_contract['spec'] + retrieval enabled; see parallel_preamble_eligible().
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional, Tuple

from apps.cli.runtime.task_contract import contract_has_operational_spec
from apps.cli.runtime.repo_retrieval import (
    RepoRetrievalBuildResult,
    apply_retrieval_with_build_result,
    build_retrieval_index,
    is_retrieval_enabled,
    run_retrieval_for_session,
)

logger = logging.getLogger(__name__)


def parallel_pipeline_enabled() -> bool:
    v = os.environ.get("GHOST_PARALLEL_PIPELINE", "off").strip().lower()
    if v in ("off", "", "0", "no", "false"):
        return False
    return v in ("partial", "on", "aggressive", "1", "true", "yes")


def parallel_preamble_eligible(session: Any) -> bool:
    if not parallel_pipeline_enabled():
        return False
    if not is_retrieval_enabled():
        return False
    if not contract_has_operational_spec(session):
        return False
    return True


class ParallelPreambleTimings(NamedTuple):
    planner_ms: float
    index_ms: float
    apply_ms: float
    wall_ms: float
    serialized_would_ms: float
    overlap_saved_ms: float


def run_planner_and_retrieval_parallel(
    root: Path,
    session: Any,
    user_text: str,
    *,
    budget_remaining: int,
    apply_decision_planner: Callable[..., None],
    force_refresh_index: bool = False,
) -> ParallelPreambleTimings:
    """
    Run apply_decision_planner and build_retrieval_index concurrently,
    then apply_retrieval_with_build_result (reads planner via task_contract / get_task_contract_decision_plan).
    """
    rpd = getattr(session, "repo_profile", None) or {}
    v2 = rpd.get("profile_v2") if isinstance(rpd, dict) else None
    repo_v2 = v2 if isinstance(v2, dict) else {}

    def timed_planner() -> float:
        t0 = time.monotonic()
        apply_decision_planner(session, budget_remaining=budget_remaining)
        return (time.monotonic() - t0) * 1000.0

    def timed_index() -> Tuple[Optional[RepoRetrievalBuildResult], float]:
        t0 = time.monotonic()
        try:
            br = build_retrieval_index(root, repo_v2, force_refresh=force_refresh_index)
            return br, (time.monotonic() - t0) * 1000.0
        except Exception as e:
            logger.warning("build_retrieval_index failed: %s", e)
            return None, (time.monotonic() - t0) * 1000.0

    t_wall0 = time.monotonic()
    planner_ms = 0.0
    index_ms = 0.0
    build_res: Optional[RepoRetrievalBuildResult] = None
    planner_error: Optional[BaseException] = None

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_planner = ex.submit(timed_planner)
        f_index = ex.submit(timed_index)
        for fut in as_completed((f_planner, f_index)):
            if fut is f_planner:
                try:
                    planner_ms = fut.result()
                except BaseException as e:
                    planner_error = e
            else:
                build_res, index_ms = fut.result()

    if planner_error is not None:
        raise planner_error

    wall_parallel = (time.monotonic() - t_wall0) * 1000.0
    apply_ms = 0.0

    if build_res is None:
        t_fb0 = time.monotonic()
        run_retrieval_for_session(root, session, user_text, force_refresh=force_refresh_index)
        apply_ms = (time.monotonic() - t_fb0) * 1000.0
        serialized = planner_ms + index_ms + apply_ms
        wall_total = wall_parallel + apply_ms
        return ParallelPreambleTimings(
            planner_ms=planner_ms,
            index_ms=index_ms,
            apply_ms=apply_ms,
            wall_ms=wall_total,
            serialized_would_ms=serialized,
            overlap_saved_ms=max(0.0, serialized - wall_total),
        )

    t_apply0 = time.monotonic()
    try:
        apply_retrieval_with_build_result(root, session, user_text, build_res)
    except Exception as e:
        logger.warning("apply_retrieval_with_build_result failed, sequential fallback: %s", e)
        run_retrieval_for_session(root, session, user_text, force_refresh=force_refresh_index)
    apply_ms = (time.monotonic() - t_apply0) * 1000.0

    wall_total = (time.monotonic() - t_wall0) * 1000.0
    serialized = planner_ms + index_ms + apply_ms
    return ParallelPreambleTimings(
        planner_ms=planner_ms,
        index_ms=index_ms,
        apply_ms=apply_ms,
        wall_ms=wall_total,
        serialized_would_ms=serialized,
        overlap_saved_ms=max(0.0, serialized - wall_total),
    )
