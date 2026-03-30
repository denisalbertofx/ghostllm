from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from apps.cli.assistant import (
    CodexAssistant,
    LOOP_ABORT_POLICY,
    _TOOL_CALL_CONTRACT_NATIVE,
)


def _bare_assistant() -> CodexAssistant:
    assistant = CodexAssistant.__new__(CodexAssistant)
    assistant.model = "coder"
    assistant._active_request_model = "coder"
    assistant._tool_call_contract_mode = _TOOL_CALL_CONTRACT_NATIVE
    assistant._tool_call_contract_received = ""
    assistant.console = SimpleNamespace(print=MagicMock())
    assistant.artifact_manager = SimpleNamespace(
        current_session=SimpleNamespace(
            events=[],
            expected_tool_call_contract="",
            received_tool_call_contract="",
            tool_call_contract_mismatch=False,
        )
    )
    assistant.session_phase = SimpleNamespace(value="EXPLORE")
    assistant.auto_approve = True
    assistant._trace_mgr = SimpleNamespace(record_runtime_event=MagicMock())
    return assistant


def test_native_contract_fails_fast_on_legacy_inline_tool_markup() -> None:
    assistant = _bare_assistant()

    kind, executed = assistant._dispatch_model_tool_round(
        {"content": '<tool_call>{"name":"read_file","arguments":{"path":"x.py"}}</tool_call>', "tool_calls": None},
        status=None,
        task_obj=None,
    )

    assert kind == "terminal"
    assert executed is None
    assert assistant._loop_abort_reason == LOOP_ABORT_POLICY
    assert assistant.console.print.called
    session = assistant.artifact_manager.current_session
    assert session.expected_tool_call_contract == _TOOL_CALL_CONTRACT_NATIVE
    assert session.received_tool_call_contract == "legacy_inline_markup"
    assert session.tool_call_contract_mismatch is True


def test_model_without_native_tool_support_fails_before_execution() -> None:
    assistant = _bare_assistant()
    assistant._active_request_native_tool_capable = False

    ok = assistant._ensure_native_tool_calling_ready("planner")

    assert ok is False
    assert assistant._loop_abort_reason == LOOP_ABORT_POLICY
    assert assistant._tool_call_contract_received == "model_without_native_tool_calling"
    session = assistant.artifact_manager.current_session
    assert session.expected_tool_call_contract == _TOOL_CALL_CONTRACT_NATIVE
    assert session.received_tool_call_contract == "model_without_native_tool_calling"
    assert session.tool_call_contract_mismatch is True
