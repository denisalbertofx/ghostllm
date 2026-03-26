"""
Synthetic end-state builders + validation helpers for Ghost v1 runtime regression.

Mirrors **final artifact shape** after a real assistant loop (phases, metrics, runtime_health,
forensic fields). Does not call the model or CodexAssistant.

Consumed by ``tests/test_ghost_v1_runtime_regression.py`` (release gate).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from apps.cli.runtime.phase_invariants import (
    assert_diff_requires_verify_phase_visited,
    assert_phase_history_matches_transitions,
    assert_phase_transition_chain_coherent,
    assert_repair_outgoing_edges_valid,
    assert_terminal_record_matches_session_phase,
    phase_edges_from_transitions,
)
from apps.cli.runtime.runtime_health import RUNTIME_HEALTH_SCHEMA_VERSION, compute_runtime_health
from apps.cli.runtime.session_metrics import refresh_operational_metrics_snapshot
from apps.cli.runtime.session_phase import SessionPhase

# Edges the live assistant loop may emit (superset for regression scenarios).
GHOST_V1_ALLOWED_PHASE_EDGES: frozenset[Tuple[str, str]] = frozenset(
    {
        ("", "INTAKE"),
        ("INTAKE", "EXPLORE"),
        ("INTAKE", "ACT"),
        ("INTAKE", "CLOSING"),
        ("EXPLORE", "ACT"),
        ("EXPLORE", "EXPLORE"),
        ("EXPLORE", "VERIFY"),
        ("EXPLORE", "CLOSING"),
        ("ACT", "EXPLORE"),
        ("ACT", "VERIFY"),
        ("ACT", "CLOSING"),
        ("VERIFY", "REPAIR"),
        ("VERIFY", "CLOSING"),
        ("REPAIR", "ACT"),
        ("REPAIR", "CLOSING"),
        ("CLOSING", "DONE"),
        ("CLOSING", "ABORTED"),
    }
)


def replay_phase_transitions(
    session: Any,
    pairs: Sequence[Tuple[str, str]],
    *,
    detail: str = "regression_fixture",
) -> None:
    """Append coherent phase_history / phase_transitions (see ArtifactSession.record_phase_transition)."""
    for fr, to in pairs:
        session.record_phase_transition(to, detail=detail, from_phase=fr)


def assert_phase_edges_allowed_v1(session: Any) -> None:
    """Fail if any recorded transition uses an edge not in GHOST_V1_ALLOWED_PHASE_EDGES."""
    edges = phase_edges_from_transitions(getattr(session, "phase_transitions", None) or [])
    for fr, to in edges:
        if (fr, to) not in GHOST_V1_ALLOWED_PHASE_EDGES:
            raise AssertionError(
                f"phase transition not in GHOST_V1_ALLOWED_PHASE_EDGES: {fr!r} -> {to!r}"
            )


@dataclass
class RuntimeHealthExpectation:
    """Subset of compute_runtime_health fields to assert."""

    health_status: str
    terminal_status: str
    verify_status: str
    repair_status: str
    exploration_status: str


@dataclass
class GhostV1RegressionExpectation:
    """Expected final state for a synthetic completed session."""

    final_phase: str
    task_outcome: str
    health: RuntimeHealthExpectation
    min_operational_metrics: Dict[str, Any] = field(default_factory=dict)
    require_risk_flags: Tuple[str, ...] = ()
    forbid_risk_flags: Tuple[str, ...] = ()
    assert_diff_requires_verify: bool = True
    min_promotion_decisions: int = 0
    min_repair_forensic_keys: int = 0


def validate_ghost_v1_regression_session(
    session: Any,
    exp: GhostV1RegressionExpectation,
) -> Dict[str, Any]:
    """
    Run architecture + health checks. Returns runtime_health dict for extra assertions.

    Raises AssertionError on illegal REPAIR edges, broken transition chains, or health mismatch.
    """
    session.phase = exp.final_phase
    # task_outcome must be set by the scenario builder (not here — single writer invariant in apps/cli).
    got_outcome = str(getattr(session, "task_outcome", "") or "")
    if got_outcome != exp.task_outcome:
        raise AssertionError(
            f"artifact outcome mismatch: got {got_outcome!r}, expected {exp.task_outcome!r}"
        )

    assert_phase_edges_allowed_v1(session)
    assert_phase_transition_chain_coherent(session.phase_transitions)
    assert_phase_history_matches_transitions(session)
    assert_repair_outgoing_edges_valid(phase_edges_from_transitions(session.phase_transitions))

    tc = getattr(session, "task_contract", None)
    tc_map = tc if isinstance(tc, Mapping) else None
    if exp.assert_diff_requires_verify:
        assert_diff_requires_verify_phase_visited(
            diff_summary=getattr(session, "diff_summary", None) or [],
            phase_transitions=session.phase_transitions,
            task_contract=tc_map,
        )

    if exp.min_promotion_decisions > 0:
        n = len(getattr(session, "promotion_decisions", None) or [])
        if n < exp.min_promotion_decisions:
            raise AssertionError(
                f"expected at least {exp.min_promotion_decisions} promotion_decisions, got {n}"
            )

    session.refresh_repair_forensic_summary()
    rf = getattr(session, "repair_forensic_summary", None) or {}
    if exp.min_repair_forensic_keys > 0:
        if not isinstance(rf, dict) or len(rf) < exp.min_repair_forensic_keys:
            raise AssertionError(
                f"expected repair_forensic_summary with at least {exp.min_repair_forensic_keys} keys, got {rf!r}"
            )

    if not isinstance(getattr(session, "terminal_resolution_record", None), dict):
        raise AssertionError("terminal_resolution_record must be a populated dict")
    assert_terminal_record_matches_session_phase(session)

    om = refresh_operational_metrics_snapshot(session)
    for k, v in exp.min_operational_metrics.items():
        got = om.get(k)
        if isinstance(v, bool):
            if bool(got) is not v:
                raise AssertionError(f"operational_metrics[{k!r}] expected {v}, got {got!r}")
        elif isinstance(v, int):
            if int(got or 0) < v:
                raise AssertionError(f"operational_metrics[{k!r}] expected >= {v}, got {got!r}")
        else:
            if got != v:
                raise AssertionError(f"operational_metrics[{k!r}] expected {v!r}, got {got!r}")

    rh = compute_runtime_health(session, operational_metrics=om)
    if rh.get("schema_version") != RUNTIME_HEALTH_SCHEMA_VERSION:
        raise AssertionError(f"unexpected runtime_health schema: {rh.get('schema_version')}")

    h = exp.health
    checks = [
        ("health_status", h.health_status),
        ("terminal_status", h.terminal_status),
        ("verify_status", h.verify_status),
        ("repair_status", h.repair_status),
        ("exploration_status", h.exploration_status),
    ]
    for key, want in checks:
        got = rh.get(key)
        if got != want:
            raise AssertionError(f"runtime_health[{key!r}] expected {want!r}, got {got!r}")

    flags = list(rh.get("main_risk_flags") or [])
    for f in exp.require_risk_flags:
        if f not in flags:
            raise AssertionError(f"expected risk flag {f!r} in {flags!r}")
    for f in exp.forbid_risk_flags:
        if f in flags:
            raise AssertionError(f"forbidden risk flag {f!r} present in {flags!r}")

    return rh


def terminal_record_done(*, outcome: str = "implemented") -> Dict[str, Any]:
    return {
        "terminal_phase": SessionPhase.DONE.value,
        "outcome": outcome,
        "loop_abort_reason": None,
        "resolution_sources": {
            "budget_exhausted": False,
            "budget_status": "ok",
            "provider_status": "ok",
            "policy_status": "ok",
            "session_failed": False,
            "loop_abort_reason_raw": None,
        },
    }


def terminal_record_aborted(
    *,
    outcome: str = "no_op",
    loop_abort_reason: str,
    resolution_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    src: Dict[str, Any] = {
        "budget_exhausted": "budget" in loop_abort_reason.lower(),
        "budget_status": "exhausted" if "budget" in loop_abort_reason.lower() else "ok",
        "provider_status": "ok",
        "policy_status": "ok",
        "session_failed": True,
        "loop_abort_reason_raw": loop_abort_reason,
    }
    if resolution_extra:
        src.update(resolution_extra)
    return {
        "terminal_phase": SessionPhase.ABORTED.value,
        "outcome": outcome,
        "loop_abort_reason": loop_abort_reason,
        "resolution_sources": src,
    }
