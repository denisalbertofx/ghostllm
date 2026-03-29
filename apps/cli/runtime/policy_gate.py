import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from apps.cli.runtime.project_policy import load_and_validate_project_policy


class ApprovalLevel(Enum):
    """How tool calls are authorized before execution."""

    AUTO = "auto"
    BUNDLE = "batch"
    MANUAL = "manual"


_APPROVAL_AUTO_TOOLS = frozenset({"ls", "read_file", "summarize_repo", "search_code"})
_APPROVAL_BUNDLE_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})
_LOCAL_PACKAGE_INSTALL_RE = re.compile(r"\b(npm|pnpm|yarn)\s+install\b", re.IGNORECASE)
_NETWORK_SHELL_RE = re.compile(
    r"\b(curl|wget|invoke-webrequest|iwr|irm|git\s+clone|docker\s+pull|pip\s+install|uv\s+pip\s+install|npm\s+install|pnpm\s+install|yarn\s+add)\b|https?://",
    re.IGNORECASE,
)


@dataclass
class PolicyResult:
    is_allowed: bool
    reason: str
    requires_confirmation: bool = True
    severity: str = "low"


class PolicyGate:
    """
    Enforces safety policies before execution.
    GEP-2: Policy Gate implementation.
    """

    def __init__(self, console: Console, auto_approve: bool = False, cwd: Optional[str] = None):
        self.console = console
        self.auto_approve = auto_approve
        self.cwd = cwd

        self.unsafe_unix_patterns = [
            r"\bsed\b",
            r"\bgrep\b",
            r"\btouch\b",
            r"\bhead\b",
            r"\btail\b",
            r"\brm\b",
            r"\bcp\b",
            r"\bmv\b",
            r"\bcat\b",
            r"\bsh\b",
            r"\bbash\b",
        ]

        self.destructive_patterns = [
            r"rm\s+-rf",
            r"del\s+/s\s+/q",
            r"rd\s+/s\s+/q",
            r"format\s+",
        ]

    @staticmethod
    def _capability_for_action(action_type: str) -> str:
        if action_type in {"List Directory", "Read File"}:
            return "read"
        if action_type in {"Write File", "Patch File"}:
            return "write"
        if action_type == "Delete File":
            return "delete"
        if action_type == "Execute Shell":
            return "shell_exec"
        return ""

    @staticmethod
    def _tool_to_action(tool_name: str) -> str:
        return {
            "ls": "List Directory",
            "read_file": "Read File",
            "write_file": "Write File",
            "edit_file": "Patch File",
            "delete_file": "Delete File",
            "run_shell": "Execute Shell",
        }.get((tool_name or "").strip(), "")

    @staticmethod
    def _shell_uses_network(detail: str) -> bool:
        return bool(_NETWORK_SHELL_RE.search(str(detail or "")))

    def _load_project_policy(self):
        if not self.cwd:
            return None
        return load_and_validate_project_policy(self.cwd)

    def check_permission(
        self,
        action_type: str,
        detail: str,
        current_mode: str,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        original_command: Optional[str] = None,
        batch_preapproved: bool = False,
    ) -> bool:
        metadata = dict(metadata or {})
        if original_command:
            metadata.setdefault("original_command", original_command)
        task_type = metadata.get("task_type", "ask")
        forced_manual = False

        project_policy = self._load_project_policy()
        if project_policy and project_policy.exists:
            if not project_policy.valid:
                self._deny(action_type, f"Invalid project policy: {'; '.join(project_policy.errors[:3])}")
                return False
            sandbox = dict(project_policy.data.get("sandbox") or {})
            allow = set(sandbox.get("allow") or [])
            deny = set(sandbox.get("deny") or [])
            require_approval = set(sandbox.get("require_approval") or [])
            capabilities = set()
            base_capability = self._capability_for_action(action_type)
            if base_capability:
                capabilities.add(base_capability)
            if action_type == "Execute Shell" and self._shell_uses_network(detail):
                capabilities.add("network")
            for capability in sorted(capabilities):
                if capability in deny:
                    self._deny(action_type, f"Project policy denies capability '{capability}'.")
                    return False
                if allow and capability not in allow:
                    self._deny(action_type, f"Project policy does not allow capability '{capability}'.")
                    return False
                if capability in require_approval:
                    forced_manual = True

        if current_mode in ["Plan", "Review"] and action_type in [
            "Write File",
            "Patch File",
            "Execute Shell",
            "Delete File",
        ]:
            self._deny(action_type, f"Action forbidden in {current_mode} mode.")
            return False

        if metadata.get("is_swarm_worker") and action_type == "Execute Shell":
            self._deny(action_type, "Shell execution is prohibited for Swarm Workers v0.")
            return False

        delete_shell = action_type == "Execute Shell" and any(
            re.search(r"\b(rm|del|rd)\b", x) for x in detail.split()
        )
        if action_type == "Delete File" or delete_shell:
            is_fixing = task_type in ["fix", "debug"]
            has_safety_flag = metadata.get("safety_override", False)
            if not (is_fixing or has_safety_flag):
                self._deny(
                    action_type,
                    "Deletion denied: Not in a fix/debug context and no safety override provided.",
                )
                return False

        if action_type == "Execute Shell" and _LOCAL_PACKAGE_INSTALL_RE.search(detail):
            lowered_detail = detail.lower()
            is_global_install = bool(
                re.search(r"(^|\s)-(g|global)(\s|$)", lowered_detail)
                or " npm install -g" in f" {lowered_detail}"
                or " pnpm add -g" in f" {lowered_detail}"
                or " yarn global " in f" {lowered_detail}"
            )
            local_install_task_types = {
                "fix",
                "scaffold",
                "implementation",
                "code",
                "direct_edit",
                "repair",
            }
            is_authorized_install = (
                (task_type in ["research", "debug"] and metadata.get("missing_dependency", False))
                or (task_type in local_install_task_types and not is_global_install)
            )
            if not is_authorized_install:
                self._deny(
                    action_type,
                    "Automatic 'npm install' blocked. Use a targeted /debug session or manual install.",
                )
                return False

        if action_type == "Execute Shell":
            lowered = detail.lower()
            for pattern in self.unsafe_unix_patterns:
                if re.search(pattern, lowered) and re.search(fr"(^|[|&;])\s*{pattern}", lowered):
                    self._deny(
                        action_type,
                        f"Shell command uses Unix-ism '{pattern}'. Use PowerShell native equivalents.",
                    )
                    return False
            for pattern in self.destructive_patterns:
                if re.search(pattern, lowered):
                    self._deny(action_type, "Shell command contains forbidden destructive patterns.")
                    return False

        if action_type in ["Write File", "Patch File"]:
            files_changed = metadata.get("files_in_batch", 0)
            if files_changed > 3:
                self._warn(
                    action_type,
                    f"Mass modification detected ({files_changed} files). Risks include breaking context limits.",
                )

        if batch_preapproved and not forced_manual:
            self.console.print(
                f" [bold green]OK[/bold green] [dim]Batch-preapproved ({current_mode}): {action_type}[/dim]"
            )
            return True

        if self.auto_approve and not forced_manual:
            self.console.print(
                f" [bold green]OK[/bold green] [dim]Auto-authorized ({current_mode}): {action_type}[/dim]"
            )
            return True

        return self._manual_confirm(action_type, detail, original_command=original_command)

    def get_approval_level(self, tool_name: str, detail: str = "") -> ApprovalLevel:
        project_policy = self._load_project_policy()
        if project_policy and project_policy.exists and project_policy.valid:
            sandbox = dict(project_policy.data.get("sandbox") or {})
            require_approval = set(sandbox.get("require_approval") or [])
            capability = self._capability_for_action(self._tool_to_action(tool_name))
            if capability and capability in require_approval:
                return ApprovalLevel.MANUAL
            if (tool_name or "").strip() == "run_shell" and self._shell_uses_network(detail):
                if "network" in require_approval:
                    return ApprovalLevel.MANUAL

        name = (tool_name or "").strip()
        if name in _APPROVAL_AUTO_TOOLS:
            return ApprovalLevel.AUTO
        if name in _APPROVAL_BUNDLE_TOOLS:
            return ApprovalLevel.BUNDLE
        if name == "run_shell":
            return ApprovalLevel.MANUAL
        return ApprovalLevel.MANUAL

    def _deny(self, action: str, reason: str):
        self.console.print(
            f"\n [bold red]POLICY VIOLATION:[/bold red]\n [red]Action:[/red] {action}\n [red]Reason:[/red] {reason}\n"
        )

    def _warn(self, action: str, message: str):
        self.console.print(f" [bold yellow]WARNING:[/bold yellow] {message}")

    def _manual_confirm(
        self,
        action: str,
        detail: str,
        original_command: Optional[str] = None,
    ) -> bool:
        original_block = ""
        if original_command and original_command != detail:
            original_block = f"\n[dim]Original:[/dim] {original_command}"
        auth_panel = Panel(
            Text.from_markup(
                f"[bold white]Ghost solicita autorizacion para:[/bold white]\n"
                f"[cyan]{action}[/cyan]: [dim]{detail}[/dim]{original_block}\n\n"
                f"[bold green]Enter[/bold green] Aprobar | [bold red]n[/bold red] Denegar"
            ),
            title="[bold yellow]Autorizacion requerida[/bold yellow]",
            border_style="yellow",
            expand=False,
        )
        self.console.print(auth_panel)
        confirm = typer.prompt("Proceder?", default="y")
        return confirm.lower() in ["y", "yes", ""]

    def confirm_action(self, action: str, detail: str) -> bool:
        if self.auto_approve:
            return True
        return self._manual_confirm(action, detail)

    def set_auto_approve(self, value: bool):
        self.auto_approve = value
