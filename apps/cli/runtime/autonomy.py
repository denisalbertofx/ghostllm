import os
import re
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

from .paths import PathComposer


class ExplorationMemory:
    """
    Tracks explored paths to prevent redundant run_shell for directory listing.
    When run_shell would execute Get-ChildItem/dir/ls on a path we've already ls'd, return cached result.
    """
    LIST_DIR_PATTERNS = [
        re.compile(r"Get-ChildItem\s+(?:-Path\s+)?[\"']?([^\"'|\s]+)[\"']?", re.I),
        re.compile(r"dir\s+([^\s/][^\s]*)(?:\s+|$)", re.I),
        re.compile(r"ls\s+([^\s]+)", re.I),
    ]

    def __init__(self):
        self._ls_cache: Dict[str, List[str]] = {}  # normalized_path -> list of files

    def add_ls(self, path: str, files: List[str], base: str) -> None:
        key = self._norm_key(path, base)
        if key:
            self._ls_cache[key] = list(files)

    def get_list_dir_path(self, cmd: str) -> Optional[str]:
        """Extract target path from a list-directory command, or None if not a list command."""
        cmd = cmd.strip()
        for pat in self.LIST_DIR_PATTERNS:
            m = pat.search(cmd)
            if m:
                return m.group(1).strip("'\"")
        return None

    def is_list_dir_command(self, cmd: str) -> bool:
        return self.get_list_dir_path(cmd) is not None

    def get_cached_ls(self, path: str, base: str) -> Optional[List[str]]:
        key = self._norm_key(path, base)
        return self._ls_cache.get(key)

    def _norm_key(self, path: str, base: str) -> str:
        if not path: return ""
        full = PathComposer.compose(base, path)
        return os.path.normcase(os.path.normpath(full))

    def clear(self) -> None:
        self._ls_cache.clear()


@dataclass
class AutonomyCheckpoint:
    work_done: List[str]
    pending_work: List[str]
    stop_reason: str # budget_exhausted, stagnation_detected
    recommendation: str

class BudgetManager:
    """
    Manages a weighted budget for autonomous operations.
    GEP-4: Autonomy Budget implementation.
    Cost model: read_file/ls low; edit_file/write_file medium; run_shell high;
    build-type run_shell effectively very high (run_shell=15 + chain of actions).
    Verification plan uses budget_remaining to prefer typecheck before build.

    Contract spec ``budget_policy`` (``task_contract.spec``) caps tool invocations
    when set via ``reset_contract_spec_tool_limits``.

    Deprecated aliases: ``reset_taskspec_tool_limits``, ``taskspec_allows_tool``,
    ``record_taskspec_tool_invocation`` (same methods).
    """
    def __init__(self, total_budget: int = 100):
        self.total_budget = total_budget
        self.used_budget = 0
        self._contract_spec_max_tools: Optional[int] = None
        self._contract_spec_max_shell: Optional[int] = None
        self._contract_spec_tool_count = 0
        self._contract_spec_shell_count = 0

        # Cost mapping (low: 1-2, medium: 5-10, high: 15+)
        self.costs = {
            "read_file": 2,
            "ls": 2,
            "summarize_repo": 2,
            "git_diff": 2,
            "local_inspection": 1,
            "write_file": 5,
            "edit_file": 5,
            "patch_file": 5,
            "run_shell": 15,
            "delete_file": 10,
            "merge_apply": 15
        }

    def get_cost(self, action: str) -> int:
        if not action: return 3
        return self.costs.get(action.lower(), 3) # Default 3 for unknown actions
    def consume(self, action: str) -> int:
        cost = self.get_cost(action)
        self.used_budget += cost
        return cost

    def is_exhausted(self) -> bool:
        return self.used_budget >= self.total_budget

    @property
    def remaining(self) -> int:
        return max(0, self.total_budget - self.used_budget)

    @property
    def progress_percent(self) -> float:
        return (self.used_budget / self.total_budget) * 100

    def reset_contract_spec_tool_limits(self, budget_policy: Optional[Dict[str, Any]]) -> None:
        """Apply contract spec budget_policy caps for the current session (None = no extra caps)."""
        self._contract_spec_tool_count = 0
        self._contract_spec_shell_count = 0
        if not budget_policy:
            self._contract_spec_max_tools = None
            self._contract_spec_max_shell = None
            return
        try:
            self._contract_spec_max_tools = int(budget_policy.get("max_tool_calls", 999999))
        except (TypeError, ValueError):
            self._contract_spec_max_tools = None
        try:
            self._contract_spec_max_shell = int(budget_policy.get("max_shell_calls", 999999))
        except (TypeError, ValueError):
            self._contract_spec_max_shell = None

    def contract_spec_allows_tool(self, tool_name: str) -> Tuple[bool, str]:
        """Enforce max_tool_calls / max_shell_calls from contract spec before weighted consume."""
        if self._contract_spec_max_tools is None:
            return True, ""
        if self._contract_spec_tool_count >= self._contract_spec_max_tools:
            return False, (
                f"Contract spec budget_policy: max_tool_calls ({self._contract_spec_max_tools}) reached"
            )
        if tool_name == "run_shell" and self._contract_spec_max_shell is not None:
            if self._contract_spec_shell_count >= self._contract_spec_max_shell:
                return False, (
                    f"Contract spec budget_policy: max_shell_calls ({self._contract_spec_max_shell}) reached"
                )
        return True, ""

    def record_contract_spec_tool_invocation(self, tool_name: str) -> None:
        if self._contract_spec_max_tools is None:
            return
        self._contract_spec_tool_count += 1
        if tool_name == "run_shell":
            self._contract_spec_shell_count += 1


# Deprecated method names (same callables; kept for external callers).
BudgetManager.reset_taskspec_tool_limits = BudgetManager.reset_contract_spec_tool_limits
BudgetManager.taskspec_allows_tool = BudgetManager.contract_spec_allows_tool
BudgetManager.record_taskspec_tool_invocation = BudgetManager.record_contract_spec_tool_invocation


class StagnationDetector:
    """
    Detects genuinely repetitive low-progress behavior. Does NOT trigger on normal
    repo exploration (several distinct reads before editing).
    """
    READ_ONLY_ACTIONS = frozenset(["read_file", "ls", "summarize_repo", "git_diff"])

    def __init__(self, max_same_action_same_target: int = 3, max_idle_reads: int = 12):
        self.history: List[Tuple[str, str]] = []
        self.max_same_action_same_target = max_same_action_same_target  # same tool + same path
        self.max_idle_reads = max_idle_reads  # consecutive reads on few targets
        self.consecutive_reads = 0

    def reset(self) -> None:
        """Nueva tarea / mensaje: no arrastrar repeticiones de la sesión anterior (p. ej. /do continua)."""
        self.history.clear()
        self.consecutive_reads = 0

    def add_action(self, action: str, detail: str):
        self.history.append((action, detail))
        if len(self.history) > 20:
            self.history.pop(0)

        is_read = action and action.lower() in self.READ_ONLY_ACTIONS
        if is_read:
            self.consecutive_reads += 1
        else:
            self.consecutive_reads = 0

    def check_stagnation(self) -> Tuple[bool, str]:
        # 1. Same tool + same target repeated (genuine loop)
        if len(self.history) >= self.max_same_action_same_target:
            window = self.history[-self.max_same_action_same_target:]
            first = window[0]
            if all(a == first[0] and d == first[1] for a, d in window):
                return True, f"Repetición detectada: {first[0]} en {first[1]} ejecutado {self.max_same_action_same_target} veces seguidas."

        # 2. Identical tool sequence repeated (e.g. read A, read B, read A, read B...)
        if len(self.history) >= 6:
            last_6 = [t for t in self.history[-6:]]
            first_3 = last_6[:3]
            next_3 = last_6[3:]
            if first_3 == next_3:
                return True, "Secuencia de acciones idéntica repetida (posible bucle)."

        # 3. Many consecutive reads only if SAME/FEW paths (not exploration of distinct files)
        # Reading 4–6 distinct files is normal; trigger only when same 1–2 targets repeatedly
        if self.consecutive_reads >= self.max_idle_reads:
            read_window = [(a, d) for a, d in self.history[-self.max_idle_reads:] if a and a.lower() in self.READ_ONLY_ACTIONS]
            paths = [d for _, d in read_window]
            unique_paths = len(set(p for p in paths if p))
            if unique_paths <= 2:
                return True, f"Estancamiento: {self.consecutive_reads} lecturas en pocos targets sin avance."
            # 3+ distinct paths = legitimate exploration, don't trigger

        return False, ""
