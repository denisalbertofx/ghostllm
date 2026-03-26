"""
Unified session state machine for assistant loop, TaskManager, and ArtifactSession.

Authority: this enum and TERMINAL_PHASES / LOOP_HALT_PHASES are the single vocabulary
for session phases. New phases require CodexAssistant._chat_loop + phase_invariants tests.
Terminal mapping for DONE/ABORTED lives in terminal_resolution (not here beyond helpers).

Extension guide: docs/RUNTIME_EXTENSION_BOUNDARIES.md
"""
from __future__ import annotations

from enum import Enum
from typing import FrozenSet, Optional


class SessionPhase(str, Enum):
    IDLE = "IDLE"
    INTAKE = "INTAKE"
    EXPLORE = "EXPLORE"
    ACT = "ACT"
    VERIFY = "VERIFY"
    REPAIR = "REPAIR"
    CLOSING = "CLOSING"  # loop ended; final DONE/ABORTED applied only at finalize
    DONE = "DONE"
    ABORTED = "ABORTED"


TERMINAL_PHASES: FrozenSet[SessionPhase] = frozenset({SessionPhase.DONE, SessionPhase.ABORTED})
LOOP_HALT_PHASES: FrozenSet[SessionPhase] = frozenset(
    {SessionPhase.DONE, SessionPhase.ABORTED, SessionPhase.CLOSING}
)

# Abort reasons recorded on artifact / logs (not phases)
LOOP_ABORT_STAGNATION = "stagnation"
LOOP_ABORT_POLICY = "policy_block"
LOOP_ABORT_PROVIDER = "provider_error"
LOOP_ABORT_MAX_ITERATIONS = "max_iterations"


def compute_terminal_session_phase(
    *,
    budget_exhausted: bool,
    session_failed: bool,
    outcome: str,
    loop_abort_reason: Optional[str] = None,
    evidence_score: int = 0,
    has_final_nl_response: bool = False,
) -> SessionPhase:
    """
    Final session phase when the task session ends. Delegates to terminal_resolution
    (kept for tests and callers that only have outcome + legacy flags).
    """
    from apps.cli.runtime.terminal_resolution import terminal_phase_from_signals

    return terminal_phase_from_signals(
        budget_exhausted=budget_exhausted,
        session_failed=session_failed,
        outcome=outcome,
        loop_abort_reason=loop_abort_reason,
        provider_ok=True,
        policy_status="ok",
        evidence_score=evidence_score,
        has_final_nl_response=has_final_nl_response,
    )
