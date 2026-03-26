"""
Per-session operational metrics (stable JSON keys for analytics).

Counters are updated during the assistant loop; :func:`refresh_operational_metrics_snapshot`
normalizes derived fields before artifact export.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

# Stable, machine-readable keys — do not rename without a migration note.
OPERATIONAL_METRICS_DEFAULTS: Dict[str, Any] = {
    "total_loop_iterations": 0,
    "total_model_turns": 0,
    "total_act_phases": 0,
    "total_verify_phases": 0,
    "total_repair_phases": 0,
    "repair_attempts": 0,
    "repair_success_count": 0,
    "verification_pass_count": 0,
    "verification_fail_count": 0,
    "verification_reuse_count": 0,
    "total_tool_calls": 0,
    "total_write_tool_calls": 0,
    "total_read_tool_calls": 0,
    "prompt_chars_sent": 0,
    "repair_prompt_chars_sent": 0,
    "tool_output_archived_count": 0,
    "final_aborted_reason": "",
    # Per-session counts of main-chat turns (and REPAIR specialist) by SessionPhase.value
    "model_turns_by_phase": {},
}

_WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})
_READ_TOOLS = frozenset({"ls", "read_file", "summarize_repo"})


def ensure_operational_metrics(session: Any) -> Dict[str, Any]:
    raw = getattr(session, "operational_metrics", None)
    if not isinstance(raw, dict):
        raw = {**OPERATIONAL_METRICS_DEFAULTS, "model_turns_by_phase": {}}
        setattr(session, "operational_metrics", raw)
    else:
        for k, v in OPERATIONAL_METRICS_DEFAULTS.items():
            if k not in raw:
                raw[k] = type(v)() if isinstance(v, (int, str)) else (dict(v) if isinstance(v, dict) else v)
        if not isinstance(raw.get("model_turns_by_phase"), dict):
            raw["model_turns_by_phase"] = {}
    return raw


def on_phase_transition_recorded(session: Any, phase: str) -> None:
    """Call from ArtifactSession.record_phase_transition for each persisted step."""
    m = ensure_operational_metrics(session)
    if phase == "ACT":
        m["total_act_phases"] = int(m.get("total_act_phases", 0) or 0) + 1
    elif phase == "VERIFY":
        m["total_verify_phases"] = int(m.get("total_verify_phases", 0) or 0) + 1
    elif phase == "REPAIR":
        m["total_repair_phases"] = int(m.get("total_repair_phases", 0) or 0) + 1


def set_total_loop_iterations(session: Any, n: int) -> None:
    m = ensure_operational_metrics(session)
    m["total_loop_iterations"] = max(0, int(n))


def record_main_model_turn_ok(session: Any, *, prompt_chars: int, phase: Optional[str] = None) -> None:
    """One successful chat/completions turn (explore/act/verify text turns if any)."""
    m = ensure_operational_metrics(session)
    m["total_model_turns"] = int(m.get("total_model_turns", 0) or 0) + 1
    m["prompt_chars_sent"] = int(m.get("prompt_chars_sent", 0) or 0) + max(0, int(prompt_chars))
    if phase:
        byp = m.get("model_turns_by_phase")
        if not isinstance(byp, dict):
            byp = {}
            m["model_turns_by_phase"] = byp
        byp[phase] = int(byp.get(phase, 0) or 0) + 1


def record_repair_specialist_model_turn(session: Any) -> None:
    """One repair-specialist NIM/OpenAI call (REPAIR phase; not counted in total_model_turns)."""
    m = ensure_operational_metrics(session)
    byp = m.get("model_turns_by_phase")
    if not isinstance(byp, dict):
        byp = {}
        m["model_turns_by_phase"] = byp
    byp["REPAIR"] = int(byp.get("REPAIR", 0) or 0) + 1


def record_tool_execution_metrics(session: Any, tool_name: str) -> None:
    """Count each executed inner tool (after policy allows the call)."""
    m = ensure_operational_metrics(session)
    m["total_tool_calls"] = int(m.get("total_tool_calls", 0) or 0) + 1
    if tool_name in _WRITE_TOOLS:
        m["total_write_tool_calls"] = int(m.get("total_write_tool_calls", 0) or 0) + 1
    elif tool_name in _READ_TOOLS:
        m["total_read_tool_calls"] = int(m.get("total_read_tool_calls", 0) or 0) + 1


def record_tool_output_archived(session: Any) -> None:
    m = ensure_operational_metrics(session)
    m["tool_output_archived_count"] = int(m.get("tool_output_archived_count", 0) or 0) + 1


def record_verification_metrics(
    session: Any,
    v_res: Mapping[str, Any],
    *,
    reused: bool,
) -> None:
    """
    Update pass/fail/reuse tallies. When ``reused`` is True, only ``verification_reuse_count``
    increments (finalize path reusing stored verification).
    """
    m = ensure_operational_metrics(session)
    if reused:
        m["verification_reuse_count"] = int(m.get("verification_reuse_count", 0) or 0) + 1
        return
    status = str(v_res.get("status") or "")
    steps = int(v_res.get("steps_executed_count") or 0)
    if status == "skipped" or (bool(v_res.get("skip_execution")) and steps == 0):
        return
    if status == "success":
        m["verification_pass_count"] = int(m.get("verification_pass_count", 0) or 0) + 1
        return
    if status in ("failed", "blocked"):
        m["verification_fail_count"] = int(m.get("verification_fail_count", 0) or 0) + 1
        return
    if steps > 0:
        m["verification_fail_count"] = int(m.get("verification_fail_count", 0) or 0) + 1


def record_repair_success(session: Any) -> None:
    m = ensure_operational_metrics(session)
    m["repair_success_count"] = int(m.get("repair_success_count", 0) or 0) + 1


def set_final_aborted_reason(session: Any, reason: Optional[str]) -> None:
    m = ensure_operational_metrics(session)
    m["final_aborted_reason"] = str(reason or "")


def refresh_operational_metrics_snapshot(session: Any) -> Dict[str, Any]:
    """
    Align derived counters with authoritative session fields before JSON/markdown export.
    """
    m = ensure_operational_metrics(session)
    m["repair_attempts"] = int(getattr(session, "repair_attempt_count", 0) or 0)
    m["repair_prompt_chars_sent"] = int(getattr(session, "repair_specialist_prompt_chars", 0) or 0)
    if not isinstance(m.get("model_turns_by_phase"), dict):
        m["model_turns_by_phase"] = {}
    return m


def format_operational_metrics_markdown(metrics: Mapping[str, Any]) -> str:
    """Compact human-readable block for artifact markdown."""
    lines = [
        "## 📈 Operational metrics",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]
    keys = list(OPERATIONAL_METRICS_DEFAULTS.keys())
    for k in keys:
        v = metrics.get(k, OPERATIONAL_METRICS_DEFAULTS[k])
        if k == "model_turns_by_phase" and isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False, sort_keys=True)
        lines.append(f"| `{k}` | {v} |")
    return "\n".join(lines) + "\n\n"
