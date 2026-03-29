from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from apps.cli.assistant import (
    CodexAssistant,
    LOOP_ABORT_POLICY,
    _TOOL_CALL_CONTRACT_LEGACY,
    _TOOL_CALL_CONTRACT_NATIVE,
)


def _bare_assistant(mode: str) -> CodexAssistant:
    assistant = CodexAssistant.__new__(CodexAssistant)
    assistant.model = "coder"
    assistant._tool_call_contract_mode = mode
    assistant.console = SimpleNamespace(print=MagicMock())
    assistant.artifact_manager = SimpleNamespace(current_session=SimpleNamespace(events=[]))
    assistant.session_phase = SimpleNamespace(value="EXPLORE")
    assistant.auto_approve = True
    return assistant


def test_native_contract_fails_fast_on_legacy_inline_tool_markup() -> None:
    assistant = _bare_assistant(_TOOL_CALL_CONTRACT_NATIVE)

    kind, executed = assistant._dispatch_model_tool_round(
        {"content": '<tool_call>{"name":"read_file","arguments":{"path":"x.py"}}</tool_call>', "tool_calls": None},
        status=None,
        task_obj=None,
    )

    assert kind == "terminal"
    assert executed is None
    assert assistant._loop_abort_reason == LOOP_ABORT_POLICY
    assert assistant.console.print.called


def test_legacy_adapter_mode_detects_legacy_markup() -> None:
    assistant = _bare_assistant(_TOOL_CALL_CONTRACT_LEGACY)
    assert assistant._content_looks_like_legacy_tool_call(
        '<tool_call>{"name":"read_file","arguments":{"path":"x.py"}}</tool_call>'
    )
