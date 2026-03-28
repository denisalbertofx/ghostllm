from __future__ import annotations

import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from apps.cli.assistant import CodexAssistant
from apps.cli.runtime.session_phase import LOOP_ABORT_STAGNATION


class TestCliPlanModeBehavior(unittest.TestCase):
    def test_broad_plan_mode_detects_plan_payload_without_literal_slash(self) -> None:
        assistant = CodexAssistant("http://localhost:11434", "key", "coder")
        assistant.mode = "Chat"
        assistant.current_intent = SimpleNamespace(task="encuentra los bugs mas importantes")
        assistant.artifact_manager.current_session = SimpleNamespace(
            task_contract={"intent": {"is_slash_command": True, "mode": "plan"}}
        )
        self.assertTrue(assistant._is_broad_plan_mode_task())

    def test_capture_readonly_plan_payload_only_for_final_no_tool_turn(self) -> None:
        assistant = CodexAssistant("http://localhost:11434", "key", "coder")
        self.assertFalse(
            assistant._should_capture_readonly_plan_payload(
                "Conclusion: partial",
                [{"id": "tool_1", "type": "function"}],
            )
        )
        self.assertTrue(
            assistant._should_capture_readonly_plan_payload(
                "Conclusion: final",
                [],
            )
        )

    def test_broad_plan_stagnation_runs_final_synthesis_without_checkpoint(self) -> None:
        assistant = CodexAssistant("http://localhost:11434", "key", "coder")
        assistant._loop_abort_reason = LOOP_ABORT_STAGNATION
        assistant._tools_executed_this_session = True
        assistant._final_synthesis_done = False
        assistant.history = []
        assistant.artifact_manager.current_session = SimpleNamespace(events=[], task_intent="analysis")
        assistant._is_broad_plan_mode_task = lambda: True  # type: ignore[method-assign]
        assistant.renderer.session_status = MagicMock(return_value=nullcontext(SimpleNamespace()))  # type: ignore[method-assign]
        assistant.memory.add_message = MagicMock()  # type: ignore[method-assign]

        with patch.object(
            assistant,
            "_stream_completion",
            return_value={"content": "Conclusion: ok\nFindings:\n1. x\nSteps:\n1. y", "tool_calls": None},
        ) as mock_stream, patch.object(assistant.console, "print"):
            ok = assistant._maybe_stagnation_readonly_synthesis()

        self.assertTrue(ok)
        mock_stream.assert_called_once()
        self.assertTrue(assistant._final_synthesis_done)
        self.assertTrue(
            any(evt.get("event") == "broad_plan_stagnation_synthesis" for evt in assistant.artifact_manager.current_session.events)
        )

    def test_broad_plan_churn_runs_final_synthesis(self) -> None:
        assistant = CodexAssistant("http://localhost:11434", "key", "coder")
        assistant._tools_executed_this_session = True
        assistant._final_synthesis_done = False
        assistant.history = []
        assistant.artifact_manager.current_session = SimpleNamespace(
            events=[{"event": "explore_v2_churn_abort", "reason": "No new read_file hit in last 5 explore steps"}],
            task_intent="analysis",
        )
        assistant._is_broad_plan_mode_task = lambda: True  # type: ignore[method-assign]
        assistant.renderer.session_status = MagicMock(return_value=nullcontext(SimpleNamespace()))  # type: ignore[method-assign]
        assistant.memory.add_message = MagicMock()  # type: ignore[method-assign]

        with patch.object(
            assistant,
            "_stream_completion",
            return_value={"content": "Conclusion: ok\nFindings:\n1. x\nSteps:\n1. y", "tool_calls": None},
        ) as mock_stream, patch.object(assistant.console, "print"):
            ok = assistant._maybe_churn_readonly_synthesis()

        self.assertTrue(ok)
        mock_stream.assert_called_once()
        self.assertTrue(assistant._final_synthesis_done)
        self.assertTrue(
            any(evt.get("event") == "broad_plan_churn_synthesis" for evt in assistant.artifact_manager.current_session.events)
        )


if __name__ == "__main__":
    unittest.main()
