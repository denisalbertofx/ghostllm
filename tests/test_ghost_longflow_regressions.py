"""
Regresiones ligeras para sesiones largas / CI: contrato UI, continuidad, outcomes, cierre.

No sustituyen E2E con LLM real; fijan invariantes de producto.
"""
from __future__ import annotations

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.closure_artifact_summary import derive_completion_style
from apps.cli.runtime.outcome_engine import determine_task_outcome
from apps.cli.ui import ui_contract


class _SessAbort:
    phase = "ABORTED"
    closure_reason = "verification_failed_low_confidence"
    terminal_resolution_record: dict = {}
    task_confidence_signals: dict = {}


def test_ui_contract_stable_tokens_nonempty() -> None:
    assert len(ui_contract.PANEL_TITLE_REVIEW.strip()) >= 3
    assert "lote" in ui_contract.TOOL_TABLE_TITLE_MARKER
    assert ui_contract.MSG_APPROVAL_SAME_BUNDLE.strip()


def test_readonly_analysis_next_action_not_already_impl_wording() -> None:
    """Cierre /plan: no mezclar copy de 'already_implemented' / no changes needed."""
    s = ArtifactSession("sid", "revisar foo", task_id="t1")
    s.task_intent = "analysis"
    s.change_expectation = "should_not_write"
    s.diff_summary = []
    msgs = [
        {"role": "user", "content": "audita"},
        {"role": "assistant", "content": "Hallazgos preliminares en el módulo."},
    ]
    r = determine_task_outcome(s, msgs, {})
    assert r.outcome == "read_only"
    low = (r.recommended_next_action or "").lower()
    assert "no changes needed" not in low
    assert "ninguna acción de implementación" in low or "archivo" in low or "check" in low


def test_aborted_after_verify_failure_completion_style() -> None:
    s = _SessAbort()
    style, expl = derive_completion_style(s)
    assert style == "aborted"
    assert "ABORTED" in expl or "abort" in expl.lower()
