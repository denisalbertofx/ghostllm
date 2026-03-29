from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from apps.cli.assistant import CodexAssistant


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

    def run(self, task: str, *, keep_open: bool = False, mode: Optional[str] = None) -> None:
        assistant = self._assistant(mode=mode)
        assistant.run(task, keep_open=keep_open)

    def chat(self, task: str) -> None:
        self.run(task, keep_open=False, mode="Chat")

    def plan(self, task: str) -> None:
        self.run(f"/plan {task}", keep_open=False, mode="Plan")

    def do(self, task: str) -> None:
        self.run(f"/do {task}", keep_open=False, mode="Execute")

    def fix(self, task: str) -> None:
        self.run(f"/fix {task}", keep_open=False, mode="Execute")
