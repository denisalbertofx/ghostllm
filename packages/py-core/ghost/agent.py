from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from apps.cli.assistant import CodexAssistant


@dataclass(frozen=True)
class AgentRunResult:
    session_id: str = ""
    task_outcome: str = ""
    primary_output_path: str = ""
    review_packet_path: str = ""
    artifact_json_path: str = ""
    artifact_markdown_path: str = ""
    persisted_plan_path: str = ""
    persisted_handoff_path: str = ""
    raw_artifact: Dict[str, Any] = field(default_factory=dict)
    captured_stdout: str = ""
    captured_stderr: str = ""

    @classmethod
    def from_artifact(
        cls,
        artifact: Optional[Dict[str, Any]],
        *,
        captured_stdout: str = "",
        captured_stderr: str = "",
    ) -> "AgentRunResult":
        data = dict(artifact or {})
        return cls(
            session_id=str(data.get("session_id") or ""),
            task_outcome=str(data.get("task_outcome") or ""),
            primary_output_path=str(data.get("primary_output_path") or ""),
            review_packet_path=str(data.get("review_packet_path") or ""),
            artifact_json_path=str(data.get("artifact_json_path") or ""),
            artifact_markdown_path=str(data.get("artifact_markdown_path") or ""),
            persisted_plan_path=str(data.get("persisted_plan_path") or ""),
            persisted_handoff_path=str(data.get("persisted_handoff_path") or ""),
            raw_artifact=data,
            captured_stdout=captured_stdout,
            captured_stderr=captured_stderr,
        )


@dataclass
class Agent:
    server_url: str = ""
    api_key: str = ""
    model: str = "coder"
    mode: str = "Chat"
    auto_approve: bool = False
    profile: str = "coder"
    project_name: str = "GhostLLM"
    cwd: Optional[str] = None
    silent: bool = True

    def __post_init__(self) -> None:
        if not self.server_url:
            self.server_url = os.environ.get("GHOST_SERVER_URL", "http://127.0.0.1:8000")
        if not self.api_key:
            self.api_key = os.environ.get("GHOST_API_KEY", "")

    def _assistant(self, *, mode: Optional[str] = None) -> CodexAssistant:
        return CodexAssistant(
            self.server_url,
            self.api_key,
            self.model,
            mode=mode or self.mode,
            auto_approve=self.auto_approve,
            profile=self.profile,
            project_name=self.project_name,
            cwd=self.cwd,
        )

    def run(self, task: str, *, keep_open: bool = False, mode: Optional[str] = None) -> AgentRunResult:
        out = io.StringIO()
        err = io.StringIO()
        if self.silent:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                assistant = self._assistant(mode=mode)
                artifact = assistant.run(task, keep_open=keep_open)
        else:
            assistant = self._assistant(mode=mode)
            artifact = assistant.run(task, keep_open=keep_open)
        return AgentRunResult.from_artifact(
            artifact,
            captured_stdout=out.getvalue(),
            captured_stderr=err.getvalue(),
        )

    def chat(self, task: str) -> AgentRunResult:
        return self.run(task, keep_open=False, mode="Chat")

    def plan(self, task: str) -> AgentRunResult:
        return self.run(f"/plan {task}", keep_open=False, mode="Plan")

    def do(self, task: str) -> AgentRunResult:
        return self.run(f"/do {task}", keep_open=False, mode="Execute")

    def fix(self, task: str) -> AgentRunResult:
        return self.run(f"/fix {task}", keep_open=False, mode="Execute")
