"""
Wall-clock timing per session phase (INTAKE, EXPLORE, ACT, VERIFY, …) for profiling.

Durations are best-effort: they measure time between ``_apply_session_phase`` transitions,
excluding IDLE. Model-turn counts come from the main chat completion path only (see
``record_main_model_turn_ok`` with ``phase``).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def ensure_runtime_breakdown_list(session: Any) -> List[Dict[str, Any]]:
    rows = getattr(session, "runtime_phase_breakdown", None)
    if not isinstance(rows, list):
        rows = []
        session.runtime_phase_breakdown = rows
    return rows


def append_intake_timing_step(session: Any, name: str, duration_ms: float) -> None:
    """Merge into ``session.intake_timing_ms`` (cumulative INTAKE sub-steps)."""
    if not name:
        return
    cur = getattr(session, "intake_timing_ms", None)
    if not isinstance(cur, dict):
        cur = {}
    cur[name] = round(float(duration_ms), 2)
    session.intake_timing_ms = cur


def summarize_breakdown_for_log(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "{}"
    parts = [f"{r.get('phase')}={r.get('duration_ms', 0):.0f}ms" for r in rows if isinstance(r, dict)]
    return " | ".join(parts)


def log_phase_timing_row(row: Dict[str, Any]) -> None:
    try:
        logger.info("runtime_phase_timing %s", json.dumps(row, ensure_ascii=False, default=str))
    except Exception:
        logger.info("runtime_phase_timing %s", row)
