"""
Runtime architecture invariants for session phase / terminal resolution.

Used by ``tests/test_runtime_architecture_invariants.py`` and optionally by
diagnostics. Keeps checks pure and file-path based where possible.

Human-readable rules and integration guidance: docs/RUNTIME_EXTENSION_BOUNDARIES.md
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from apps.cli.runtime.session_phase import TERMINAL_PHASES, SessionPhase

TERMINAL_PHASE_VALUES = frozenset(p.value for p in TERMINAL_PHASES)
REPAIR_ALLOWED_SUCCESSORS = frozenset({SessionPhase.ACT.value, SessionPhase.CLOSING.value})

# Assistant must not apply literal DONE/ABORTED except via ``tr.phase`` from resolve_terminal_result.
_FORBIDDEN_ASSISTANT_LITERAL_TERMINAL = re.compile(
    r"_apply_session_phase\(\s*SessionPhase\.(DONE|ABORTED)\b"
)
_FORBIDDEN_ASSISTANT_DIRECT_ASSIGN = re.compile(
    r"self\.session_phase\s*=\s*SessionPhase\.(DONE|ABORTED)\b"
)


def assistant_source_must_not_apply_literal_terminal_phases(assistant_py_text: str) -> None:
    """
    Invariant: only :meth:`CodexAssistant._apply_terminal_resolution` may move the
    live session into DONE/ABORTED (via ``_apply_session_phase(tr.phase, ...)``).
    """
    m = _FORBIDDEN_ASSISTANT_LITERAL_TERMINAL.search(assistant_py_text)
    if m:
        raise AssertionError(
            "assistant.py must not call _apply_session_phase(SessionPhase.DONE|ABORTED, ...); "
            f"found: {m.group(0)!r}"
        )
    m2 = _FORBIDDEN_ASSISTANT_DIRECT_ASSIGN.search(assistant_py_text)
    if m2:
        raise AssertionError(
            "assistant.py must not assign self.session_phase to DONE/ABORTED directly; "
            f"found: {m2.group(0)!r}"
        )


def load_assistant_source(repo_root: Optional[Path] = None) -> str:
    root = repo_root or Path(__file__).resolve().parents[3]
    path = root / "apps" / "cli" / "assistant.py"
    return path.read_text(encoding="utf-8")


_TASK_OUTCOME_ASSIGN = re.compile(r"\btask_outcome\s*=")


def cli_production_code_task_outcome_assignments(repo_root: Path) -> List[Tuple[Path, int, str]]:
    """
    Invariant 2: only terminal resolution should persist ``task_outcome`` on the artifact session.
    Returns (path, line_no, stripped_line) for each assignment in apps/cli excluding test modules.
    """
    hits: List[Tuple[Path, int, str]] = []
    cli = repo_root / "apps" / "cli"
    if not cli.is_dir():
        return hits
    for path in sorted(cli.rglob("*.py")):
        name = path.name
        if name.startswith("test_") or name in ("conftest.py",):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _TASK_OUTCOME_ASSIGN.search(line) and not line.lstrip().startswith("#"):
                hits.append((path, i, line.strip()))
    return hits


def phase_edges_from_transitions(
    transitions: Sequence[Mapping[str, Any]],
) -> List[Tuple[str, str]]:
    """``(from_phase, to_phase)`` per row; empty strings preserved as recorded."""
    out: List[Tuple[str, str]] = []
    for row in transitions or ():
        if not isinstance(row, Mapping):
            continue
        fr = str(row.get("from") or "")
        to = str(row.get("to") or "")
        out.append((fr, to))
    return out


def assert_repair_outgoing_edges_valid(edges: Iterable[Tuple[str, str]]) -> None:
    """
    Invariants 4–5: from REPAIR only ACT or CLOSING; never REPAIR → DONE/ABORTED.
    """
    for fr, to in edges:
        if fr != SessionPhase.REPAIR.value:
            continue
        if to in TERMINAL_PHASE_VALUES:
            raise AssertionError(f"illegal REPAIR → {to} (terminal must go through finalize)")
        if to not in REPAIR_ALLOWED_SUCCESSORS:
            raise AssertionError(
                f"REPAIR must transition only to {sorted(REPAIR_ALLOWED_SUCCESSORS)}; got REPAIR → {to}"
            )


def assert_diff_requires_verify_phase_visited(
    *,
    diff_summary: Sequence[Any],
    phase_transitions: Sequence[Mapping[str, Any]],
    task_contract: Optional[Mapping[str, Any]],
) -> None:
    """
    Invariant 3 (relaxed): if there are recorded file changes and ``skip_verify`` is
    not set on the contract, the session must have entered VERIFY at least once
    (loop or finalize records the same phase value).
    """
    if not diff_summary:
        return
    tc = task_contract if isinstance(task_contract, Mapping) else None
    if tc and bool(tc.get("skip_verify")):
        return
    to_phases = {str(r.get("to") or "") for r in phase_transitions if isinstance(r, Mapping)}
    if SessionPhase.VERIFY.value not in to_phases:
        raise AssertionError(
            "session has diff_summary but never entered VERIFY while skip_verify is false — "
            "write path must run verification (loop or finalize)"
        )


def assert_phase_transition_chain_coherent(
    transitions: Sequence[Mapping[str, Any]],
) -> None:
    """
    Invariant 8 (partial): each transition's ``from`` should match the previous ``to``
    when both are non-empty (ArtifactSession recording convention).
    """
    edges = phase_edges_from_transitions(transitions)
    for i in range(1, len(edges)):
        prev_to = edges[i - 1][1]
        cur_from = edges[i][0]
        if not cur_from or not prev_to:
            continue
        if cur_from != prev_to:
            raise AssertionError(
                f"phase_transitions chain break at index {i}: "
                f"previous to={prev_to!r} but current from={cur_from!r}"
            )


def assert_terminal_record_matches_session_phase(session: Any) -> None:
    """When ``terminal_resolution_record`` is populated, it agrees with ``session.phase``."""
    rec = getattr(session, "terminal_resolution_record", None)
    if not isinstance(rec, dict) or not rec:
        return
    tp = rec.get("terminal_phase")
    ph = getattr(session, "phase", None)
    if ph is None:
        return
    if str(tp) != str(ph):
        raise AssertionError(
            f"terminal_resolution_record.terminal_phase={tp!r} != session.phase={ph!r}"
        )


def assert_phase_history_matches_transitions(
    session: Any,
) -> None:
    """Invariant 8: ``phase_history`` and ``phase_transitions`` list the same phase steps."""
    hist = getattr(session, "phase_history", None) or []
    trans = getattr(session, "phase_transitions", None) or []
    if len(hist) != len(trans):
        raise AssertionError(
            f"phase_history len {len(hist)} != phase_transitions len {len(trans)}"
        )
    for i, (h, t) in enumerate(zip(hist, trans)):
        if not isinstance(h, dict) or not isinstance(t, dict):
            continue
        hp = str(h.get("phase") or "")
        tp = str(t.get("to") or "")
        if hp != tp:
            raise AssertionError(f"row {i}: phase_history.phase={hp!r} != phase_transitions.to={tp!r}")


def simulate_phase_path(transitions: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
    """Build minimal ``phase_transitions``-shaped rows for tests (from/to only)."""
    rows: List[Dict[str, Any]] = []
    for fr, to in transitions:
        rows.append({"from": fr, "to": to, "reason": "test", "ts": "", "iteration": None})
    return rows
