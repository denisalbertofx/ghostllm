import os
import re
import typer
from dataclasses import dataclass
from enum import Enum
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from typing import Dict, Any, Optional, List


class ApprovalLevel(Enum):
    """How tool calls are authorized before execution (bundled UI vs per-call policy)."""

    AUTO = "auto"  # read-only / safe: no bundled prompt
    BUNDLE = "batch"  # file writes/edits: one bundled approval for the round
    MANUAL = "manual"  # shell etc.: falls through to check_permission / per-call confirm


_APPROVAL_AUTO_TOOLS = frozenset({"ls", "read_file", "summarize_repo", "search_code"})
_APPROVAL_BUNDLE_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})


@dataclass
class PolicyResult:
    is_allowed: bool
    reason: str
    requires_confirmation: bool = True
    severity: str = "low" # low, medium, high, critical

class PolicyGate:
    """
    Enforces safety policies before execution.
    GEP-2: Policy Gate implementation.
    """
    def __init__(self, console: Console, auto_approve: bool = False):
        self.console = console
        self.auto_approve = auto_approve
        
        # Robust Unix-isms patterns (Regex with word boundaries)
        self.unsafe_unix_patterns = [
            r"\bsed\b", r"\bgrep\b", r"\btouch\b", r"\bhead\b", r"\btail\b",
            r"\brm\b", r"\bcp\b", r"\bmv\b", r"\bcat\b", r"\bsh\b", r"\bbash\b"
        ]
        
        # Destructive patterns
        self.destructive_patterns = [
            r"rm\s+-rf", r"del\s+/s\s+/q", r"rd\s+/s\s+/q", r"format\s+"
        ]

    def check_permission(self, action_type: str, detail: str, current_mode: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        Main entry point for security checks. 
        Returns True if allowed, False if denied.
        """
        metadata = metadata or {}
        task_type = metadata.get("task_type", "ask")
        
        # 1. Mode Protections
        if current_mode in ["Plan", "Review"] and action_type in ["Write File", "Patch File", "Execute Shell", "Delete File"]:
            self._deny(action_type, f"Action forbidden in {current_mode} mode.")
            return False
        
        # 1.1 Swarm Worker Protections (v0)
        if metadata.get("is_swarm_worker") and action_type == "Execute Shell":
            self._deny(action_type, "Shell execution is prohibited for Swarm Workers v0.")
            return False

        # 2. Deletion Protection (GEP-2.1) - Evidence Based
        if action_type == "Delete File" or (action_type == "Execute Shell" and any(re.search(r"\b(rm|del|rd)\b", x) for x in detail.split())):
            # Evidence must be structured: either explicit 'fix' intent or specific safety flag
            is_fixing = task_type in ["fix", "debug"]
            has_safety_flag = metadata.get("safety_override", False)
            
            if not (is_fixing or has_safety_flag):
                self._deny(action_type, "Deletion denied: Not in a fix/debug context and no safety override provided.")
                return False

        # 3. Installation Protection (GEP-2.2)
        if action_type == "Execute Shell" and "npm install" in detail.lower():
            # Must be a research or debug task resolving a missing dependency
            is_authorized_install = task_type in ["research", "debug"] and metadata.get("missing_dependency", False)
            if not is_authorized_install:
                self._deny(action_type, "Automatic 'npm install' blocked. Use a targeted /debug session or manual install.")
                return False

        # 4. Windows Compatibility (GEP-3) - Using word boundaries
        if action_type == "Execute Shell":
            for pattern in self.unsafe_unix_patterns:
                if re.search(pattern, detail.lower()):
                    # Special check: is it inside a string or path? 
                    # We look for the command at the start of the line or after a pipe/ampersand
                    if re.search(fr"(^|[|&;])\s*{pattern}", detail.lower()):
                        self._deny(action_type, f"Shell command uses Unix-ism '{pattern}'. Use PowerShell native equivalents.")
                        return False
            
            for pattern in self.destructive_patterns:
                if re.search(pattern, detail.lower()):
                    self._deny(action_type, "Shell command contains forbidden destructive patterns.")
                    return False

        # 5. Multi-file Protection
        if action_type in ["Write File", "Patch File"]:
            files_changed = metadata.get("files_in_batch", 0)
            if files_changed > 3:
                self._warn(action_type, f"Mass modification detected ({files_changed} files). Risks include breaking context limits.")

        # 6. User Authorization (Final Gate)
        if self.auto_approve:
            self.console.print(f" [bold green]✓[/bold green] [dim]Auto-authorized ({current_mode}): {action_type}[/dim]")
            return True

        # 7. Manual Confirmation Prompt
        return self._manual_confirm(action_type, detail)

    def get_approval_level(self, tool_name: str, detail: str = "") -> ApprovalLevel:
        """
        Classify a native tool for the assistant dispatch path (bundled vs auto vs per-call).

        ``detail`` is reserved for future heuristics (e.g. riskier shell commands).
        """
        _ = detail  # noqa: ARG002 — kept for API stability with assistant / scripts
        name = (tool_name or "").strip()
        if name in _APPROVAL_AUTO_TOOLS:
            return ApprovalLevel.AUTO
        if name in _APPROVAL_BUNDLE_TOOLS:
            return ApprovalLevel.BUNDLE
        if name == "run_shell":
            return ApprovalLevel.MANUAL
        return ApprovalLevel.MANUAL

    def _deny(self, action: str, reason: str):
        self.console.print(f"\n [bold red]✘ POLICY VIOLATION:[/bold red]\n [red]Action:[/red] {action}\n [red]Reason:[/red] {reason}\n")

    def _warn(self, action: str, message: str):
        self.console.print(f" [bold yellow]⚠ WARNING:[/bold yellow] {message}")

    def _manual_confirm(self, action: str, detail: str) -> bool:
        auth_panel = Panel(
            Text.from_markup(
                f"[bold white]Ghost solicita autorización para:[/bold white]\n"
                f"[cyan]{action}[/cyan]: [dim]{detail}[/dim]\n\n"
                f"[bold green]Enter[/bold green] Aprobar | [bold red]n[/bold red] Denegar"
            ),
            title="[bold yellow]Δ Autorización Requerida[/bold yellow]",
            border_style="yellow",
            expand=False
        )
        self.console.print(auth_panel)
        confirm = typer.prompt("¿Proceder?", default="y")
        return confirm.lower() in ["y", "yes", ""]

    def set_auto_approve(self, value: bool):
        self.auto_approve = value
