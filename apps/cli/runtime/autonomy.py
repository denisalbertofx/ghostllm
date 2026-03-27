import os
import re
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

from .paths import PathComposer

READ_ONLY_BUDGET_TOOLS = frozenset({"ls", "read_file", "summarize_repo", "git_diff", "local_inspection"})


def stagnation_tool_detail(tool_name: str, args: Optional[Dict[str, Any]] = None) -> str:
    """
    Detail string for StagnationDetector: read_file includes start_line/max_lines so
    consecutive reads of different slices are not treated as identical loops.
    """
    args = args or {}
    t = (tool_name or "").strip().lower()
    if t == "read_file":
        p = str(args.get("path") or "").strip()
        if not p:
            return ""
        sl = args.get("start_line")
        ml = args.get("max_lines")
        if sl is not None or ml is not None:
            return f"{p}@{sl}:{ml}"
        return p
    if t == "ls":
        return str(args.get("path") or ".").strip() or "."
    return str(args.get("path") or args.get("command") or "")


def _stagnation_read_path_base(detail: str) -> str:
    d = (detail or "").strip()
    if "@" in d:
        return d.split("@", 1)[0]
    return d


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

    Adaptive ceiling: after verifiable progress (successful verification), total_budget
    may grow up to ``initial_total_budget * GHOST_BUDGET_ADAPTIVE_MULT`` (default 2.5).
    Repeated reads of the same path without a successful write accrue churn multiplier
    (hard cap on redundant I/O).

    Contract spec ``budget_policy`` (``task_contract.spec``) caps tool invocations
    when set via ``reset_contract_spec_tool_limits``.

    Deprecated aliases: ``reset_taskspec_tool_limits``, ``taskspec_allows_tool``,
    ``record_taskspec_tool_invocation`` (same methods).
    """
    def __init__(self, total_budget: int = 100):
        self.initial_total_budget = max(1, int(total_budget))
        self.total_budget = self.initial_total_budget
        self.used_budget = 0
        self._adaptive_cap = self._compute_adaptive_cap(self.initial_total_budget)
        # path_key -> consecutive read_file consumes without successful write to that path
        self._reads_since_write_per_path: Dict[str, int] = {}
        self._contract_spec_max_tools: Optional[int] = None
        self._contract_spec_max_shell: Optional[int] = None
        self._contract_spec_reserved_write_tools = 0
        self._contract_spec_soft_read_only_tools = 0
        self._contract_spec_tool_count = 0
        self._contract_spec_shell_count = 0
        self._contract_spec_postcap_write_count = 0
        self._contract_spec_postcap_read_count = 0

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

    @staticmethod
    def _compute_adaptive_cap(initial: int) -> int:
        try:
            mult = float(os.environ.get("GHOST_BUDGET_ADAPTIVE_MULT", "2.5"))
        except (TypeError, ValueError):
            mult = 2.5
        mult = max(1.0, min(6.0, mult))
        env_cap = os.environ.get("GHOST_AUTONOMY_BUDGET_MAX", "").strip()
        cap_from_mult = int(initial * mult)
        if env_cap:
            try:
                cap_from_env = int(env_cap)
            except ValueError:
                cap_from_env = cap_from_mult
            return max(initial, cap_from_mult, cap_from_env)
        return max(initial, cap_from_mult)

    def reset_session_budget_state(self) -> None:
        """New Ghost session: spending resets; adaptive total returns to initial."""
        self.used_budget = 0
        self.total_budget = self.initial_total_budget
        self._adaptive_cap = self._compute_adaptive_cap(self.initial_total_budget)
        self._reads_since_write_per_path.clear()

    def note_successful_write_path(self, rel_path: str) -> None:
        """Clears read-churn counters for this file (and normalized variants)."""
        key = self._budget_path_key(rel_path)
        if not key:
            return
        drop = [k for k in self._reads_since_write_per_path if k == key or k.startswith(key + "/")]
        for k in drop:
            self._reads_since_write_per_path.pop(k, None)

    def grant_extension_for_verified_progress(self, *, steps_executed: int = 1) -> int:
        """
        Extend total_budget when verification produced real executed checks that passed.
        Returns points added (0 if at adaptive cap).
        """
        try:
            base_grant = int(os.environ.get("GHOST_BUDGET_VERIFY_GRANT", "20"))
        except (TypeError, ValueError):
            base_grant = 20
        base_grant = max(0, min(80, base_grant))
        extra = max(0, int(steps_executed)) * 2
        room = self._adaptive_cap - self.total_budget
        if room <= 0:
            return 0
        add = min(room, base_grant + extra)
        if add > 0:
            self.total_budget += add
        return add

    def max_read_churn_depth(self) -> int:
        """Max consecutive read_file count per path without a successful write to that path."""
        if not self._reads_since_write_per_path:
            return 0
        return max(int(v) for v in self._reads_since_write_per_path.values())

    def grant_extension_for_operational_progress(
        self,
        *,
        reason: str,
        amount: Optional[int] = None,
        skip_if_read_churn: bool = True,
    ) -> Tuple[int, str]:
        """
        Smaller adaptive grants for non-verify progress (e.g. verify failure subset, repair parse drift).
        Call sites must enforce causal/structural policy before invoking; this only applies churn/cap rules.
        Returns (points_added, skip_reason_empty_if_applied).
        """
        if skip_if_read_churn:
            try:
                churn_after = int(os.environ.get("GHOST_BUDGET_READ_CHURN_AFTER", "4"))
            except (TypeError, ValueError):
                churn_after = 4
            churn_after = max(2, min(12, churn_after))
            if self.max_read_churn_depth() >= churn_after:
                return 0, f"read_churn_depth>={churn_after}"
        try:
            base_grant = int(os.environ.get("GHOST_BUDGET_PROGRESS_GRANT", "12"))
        except (TypeError, ValueError):
            base_grant = 12
        base_grant = max(0, min(50, base_grant))
        if amount is not None:
            try:
                base_grant = max(0, min(50, int(amount)))
            except (TypeError, ValueError):
                pass
        room = self._adaptive_cap - self.total_budget
        if room <= 0:
            return 0, "at_adaptive_cap"
        add = min(room, base_grant)
        if add > 0:
            self.total_budget += add
        return add, ""

    @staticmethod
    def _budget_path_key(rel_path: str) -> str:
        if not rel_path:
            return ""
        try:
            p = PathComposer.normalize_path_segments(str(rel_path).strip())
        except Exception:
            p = str(rel_path).replace("\\", "/").strip()
        p = os.path.normcase(p)
        return p

    def get_cost(self, action: str) -> int:
        if not action: return 3
        return self.costs.get(action.lower(), 3) # Default 3 for unknown actions

    def consume(self, action: str, *, path: str = "") -> int:
        cost = self.get_cost(action)
        act = (action or "").lower()
        if act == "read_file" and path:
            key = self._budget_path_key(path)
            if key:
                n = self._reads_since_write_per_path.get(key, 0) + 1
                self._reads_since_write_per_path[key] = n
                try:
                    churn_after = int(os.environ.get("GHOST_BUDGET_READ_CHURN_AFTER", "4"))
                except (TypeError, ValueError):
                    churn_after = 4
                churn_after = max(2, min(12, churn_after))
                if n >= churn_after:
                    try:
                        mult = int(os.environ.get("GHOST_BUDGET_CHURN_MULT", "2"))
                    except (TypeError, ValueError):
                        mult = 2
                    mult = max(2, min(4, mult))
                    cost *= mult
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
        self._contract_spec_postcap_write_count = 0
        self._contract_spec_postcap_read_count = 0
        if not budget_policy:
            self._contract_spec_max_tools = None
            self._contract_spec_max_shell = None
            self._contract_spec_reserved_write_tools = 0
            self._contract_spec_soft_read_only_tools = 0
            return
        try:
            self._contract_spec_max_tools = int(budget_policy.get("max_tool_calls", 999999))
        except (TypeError, ValueError):
            self._contract_spec_max_tools = None
        try:
            self._contract_spec_max_shell = int(budget_policy.get("max_shell_calls", 999999))
        except (TypeError, ValueError):
            self._contract_spec_max_shell = None
        try:
            self._contract_spec_reserved_write_tools = max(
                0,
                int(budget_policy.get("reserved_write_tool_calls", 0)),
            )
        except (TypeError, ValueError):
            self._contract_spec_reserved_write_tools = 0
        try:
            self._contract_spec_soft_read_only_tools = max(
                0,
                int(budget_policy.get("soft_read_only_tool_calls", 0)),
            )
        except (TypeError, ValueError):
            self._contract_spec_soft_read_only_tools = 0

    @staticmethod
    def _is_write_tool(tool_name: str) -> bool:
        return tool_name in ("write_file", "edit_file", "patch_file", "delete_file", "merge_apply")

    def contract_spec_allows_tool(self, tool_name: str) -> Tuple[bool, str]:
        """Enforce max_tool_calls / max_shell_calls from contract spec before weighted consume."""
        if self._contract_spec_max_tools is None:
            return True, ""
        if self._contract_spec_tool_count >= self._contract_spec_max_tools:
            if self._is_write_tool(tool_name) and (
                self._contract_spec_postcap_write_count < self._contract_spec_reserved_write_tools
            ):
                return True, ""
            if tool_name in READ_ONLY_BUDGET_TOOLS and (
                self._contract_spec_postcap_read_count < self._contract_spec_soft_read_only_tools
            ):
                return True, ""
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
        if self._is_write_tool(tool_name) and self._contract_spec_tool_count >= self._contract_spec_max_tools:
            self._contract_spec_postcap_write_count += 1
        elif tool_name in READ_ONLY_BUDGET_TOOLS and self._contract_spec_tool_count >= self._contract_spec_max_tools:
            self._contract_spec_postcap_read_count += 1
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

    def __init__(
        self,
        max_same_action_same_target: int = 3,
        max_idle_reads: int = 12,
        max_failed_edits_same_target: int = 2,
    ):
        self.history: List[Tuple[str, str]] = []
        self.max_same_action_same_target = max_same_action_same_target  # same tool + same path
        self.max_idle_reads = max_idle_reads  # consecutive reads on few targets
        self.max_failed_edits_same_target = max_failed_edits_same_target
        self.consecutive_reads = 0

    def reset(self) -> None:
        """Nueva tarea / mensaje: no arrastrar repeticiones de la sesión anterior (p. ej. /do continua)."""
        self.history.clear()
        self.consecutive_reads = 0

    def reset_after_verified_progress(self) -> None:
        """Clear stagnation counters when verification succeeds (real forward evidence)."""
        self.history.clear()
        self.consecutive_reads = 0

    def add_action(self, action: str, detail: str):
        self.history.append((action, detail))
        if len(self.history) > 24:
            self.history.pop(0)

        is_read = action and action.lower() in self.READ_ONLY_ACTIONS
        if is_read:
            self.consecutive_reads += 1
        else:
            self.consecutive_reads = 0

    def add_failed_edit_attempt(self, path: str) -> None:
        """Record edit_file miss/error for the same file to break blind patch loops."""
        p = (path or "").strip().replace("\\", "/")
        self.add_action("edit_file_failed", p)

    def check_stagnation(self) -> Tuple[bool, str]:
        # 0. Repeated failed edit_file on the same path (blind patch loop)
        if len(self.history) >= self.max_failed_edits_same_target:
            w = self.history[-self.max_failed_edits_same_target :]
            if all(
                a == "edit_file_failed" and d == w[0][1] and d
                for a, d in w
            ):
                return (
                    True,
                    f"edit_file falló {self.max_failed_edits_same_target} veces seguidas en {w[0][1]}: "
                    "lee el bloque exacto con read_file y usa old_str literal.",
                )

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
            paths = [_stagnation_read_path_base(d) for _, d in read_window]
            unique_paths = len(set(p for p in paths if p))
            if unique_paths <= 2:
                return True, f"Estancamiento: {self.consecutive_reads} lecturas en pocos targets sin avance."
            # 3+ distinct paths = legitimate exploration, don't trigger

        return False, ""
