import os
import sys
import json
import hashlib
import uuid
import re
import time
import random
import logging
import subprocess
import difflib
from collections.abc import Mapping
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import requests
from dataclasses import asdict
from rich.live import Live
from rich.status import Status
from rich.markup import escape
from rich.table import Table
from rich import box

from apps.cli.ui.renderer import format_tool_live_hint_from_prepared
from apps.cli.ui.ui_contract import MSG_ACTIVE_WORKSET_PLAIN_PREFIX, MSG_APPROVAL_SAME_BUNDLE
from apps.cli.runtime.slash_execute_contract import apply_slash_execute_write_overrides
from apps.cli.runtime.task_contract import (
    Intent,
    append_budget_extension_record,
    append_repair_run_record,
    contract_has_operational_spec,
    ensure_task_contract_foundation,
    get_task_contract,
    infer_work_task_type,
    route_intake_intent,
    seal_task_contract_after_intake,
)
from apps.cli.runtime.adapters.contract_intake import intake_fill_task_contract_spec
from .runtime.policy_gate import PolicyGate, ApprovalLevel
from .runtime.verification import VerificationManager, parse_pytest_focus_targets
from .runtime.verification_coordinator import VerificationCoordinator, format_integrity_verify_preamble
from .runtime.artifacts import ArtifactManager, append_compacted_session_event
from .runtime.hooks import HookManager, HookEvents
from .runtime.tasks import TaskManager
from .runtime.swarm import SwarmManager
from .runtime.review_coordinator import ReviewCoordinator
from .runtime.merge_assistant import MergeAssistant
from .runtime.batch import BatchManager
from apps.cli.runtime.autonomy import (
    AutonomyCheckpoint,
    BudgetManager,
    ExplorationMemory,
    StagnationDetector,
    stagnation_tool_detail,
)
from apps.cli.runtime.paths import PathComposer, WorkingDirectoryGuard

logger = logging.getLogger(__name__)
from apps.cli.runtime.shell_normalizer import ShellNormalizer
from apps.cli.runtime.repo_profile import scan_repo_profile
from apps.cli.runtime.decision_planner import (
    apply_decision_planner_to_session,
    build_decision_planner_prompt_block,
)
from apps.cli.runtime.execution_agent import (
    build_execution_agent_prompt_block,
    resolve_model_role_map,
    run_execution_agent_for_session,
)
from apps.cli.runtime.ghost_profile import maybe_coerce_intent_for_operational_mode
from apps.cli.runtime.pipeline_preamble import (
    parallel_preamble_eligible,
    run_planner_and_retrieval_parallel,
)
from apps.cli.runtime.repo_retrieval import build_retrieval_prompt_block, run_retrieval_for_session
from apps.cli.runtime.exploration_planner import path_under_ui_roots
from apps.cli.runtime.taskspec_adapter import (
    build_contract_prompt_blocks,
    contract_spec_dict_from_session,
    effective_max_repair_attempts,
    verification_skip_respects_contract_spec,
    write_blocked_by_contract_spec,
)
from apps.cli.runtime import batch_executor as batch_exec
from apps.cli.runtime.session_trace import (
    BatchExecutionTrace,
    ModelCallTrace,
    RepairTrace,
    TRACE_STATUS_BLOCKED,
    TRACE_STATUS_COMPLETED,
    TRACE_STATUS_DENIED,
    TRACE_STATUS_FAILED,
    attach_trace_manager,
    detach_trace_manager,
    finish_session_trace,
    start_session_trace,
    trace_timestamp_iso,
    ToolCallTrace,
    verification_trace_from_result,
)
from apps.cli.runtime.harness_bundle import (
    build_agents_project_prompt_block,
    hydrate_agents_on_session,
    readonly_analysis_truthfulness_prompt_section,
)
from apps.cli.runtime.analysis_grounding import (
    enforce_readonly_assistant_message,
    readonly_analysis_grounding_enabled,
    record_read_file_ledger,
)
from apps.cli.runtime.analysis_output_policy import finalize_readonly_editorial_reply
from apps.cli.runtime.outcome_engine import compute_findings_evidence_tier, normalize_tool_history
from apps.cli.runtime.intent_classifier import user_explicitly_requests_verify_shell
from apps.cli.runtime.explore_promotion import (
    ExploreTurnResult,
    ExplorationMetrics,
    REASON_FOCUSED_WRITE_CONTEXT_READY,
    REASON_REPEATED_TARGET_READ,
    REASON_SIMPLE_WRITE_MIN_CONTEXT,
    should_promote_explore_to_act,
)
from apps.cli.runtime.repo_search import (
    SEARCH_MODE_FILENAME,
    SEARCH_MODE_PATH_EXISTS,
    SEARCH_MODE_SYMBOL,
    SEARCH_MODE_TEXT,
    search_code as _repo_search_code,
)
from apps.cli.runtime.repo_mismatch import (
    detect_repo_mismatch,
    inject_mismatch_into_session,
    repo_mismatch_detection_enabled,
)
from apps.cli.runtime.explore_engine_v2 import (
    EXPLORE_CLASS_PATH_LISTING,
    EXPLORE_CLASS_SYMBOL_LOOKUP,
    build_graceful_blocked_closure_message,
    build_listing_paths_absent_operator_message,
    churn_nudge_message,
    classify_explore_task,
    detect_exploration_churn,
    explore_listing_targets_all_absent,
    explore_v2_enabled,
    history_has_tool_call,
    should_abort_explore_as_unproductive,
    symbol_omit_ls_first_turn_enabled,
    tool_ranking_nudge,
)
from apps.cli.runtime.answer_now_policy import (
    answer_now_policy_enabled,
    bounded_code_task_kind,
    decide_answer_now_after_tools_with_details,
)
from apps.cli.runtime.evidence_sufficiency import should_run_immediate_synthesis_after_sufficiency
from apps.cli.runtime.micro_task_mode import (
    detect_micro_task_kind,
    micro_task_early_pressure_max_iter,
    micro_task_mode_enabled,
    micro_task_skip_intake_heavy_enabled,
    micro_task_system_prompt_section,
    resolve_task_mode,
)
from apps.cli.runtime.planning_task import (
    detect_strategy_plan_request,
    strategy_plan_system_prompt_section,
)
from apps.cli.runtime.explore_planning_governor import (
    assistant_text_suggests_protracted_planning,
    explore_planning_governor_enabled,
    ls_target_already_listed_successfully,
    normalize_ls_path_key,
    session_is_small_explore_scope,
    small_scope_planning_text_threshold,
)
from apps.cli.runtime.iteration_budget import (
    adapt_synthesis_reserve_iterations,
    apply_iteration_budget_to_session,
    global_iteration_cap,
)
from apps.cli.runtime.narrow_factual_chat import (
    factual_explore_iterations_remaining,
    suppress_tools_on_last_factual_iteration,
    synthesis_reserve_iterations,
)
from apps.cli.runtime.session_phase import (
    LOOP_ABORT_MAX_ITERATIONS,
    LOOP_ABORT_POLICY,
    LOOP_ABORT_PROVIDER,
    LOOP_ABORT_STAGNATION,
    LOOP_HALT_PHASES,
    SessionPhase,
)
from apps.cli.runtime.outcome_engine import (
    OUTCOME_ALREADY_IMPLEMENTED,
    _has_final_nl_response,
    determine_task_outcome,
    verification_integrity_stale,
)
from apps.cli.runtime.task_confidence_engine import merge_iteration_limit_summary_with_confidence
from apps.cli.runtime.verification import VerificationManager
from apps.cli.runtime.terminal_resolution import (
    PolicyTerminalStatus,
    ProviderTerminalStatus,
    TerminalResolution,
    iteration_limit_operator_assessment,
    resolve_terminal_result,
)
from apps.cli.runtime.session_metrics import (
    record_main_model_turn_ok,
    record_repair_specialist_model_turn,
    record_repair_success,
    record_tool_execution_metrics,
    record_tool_output_archived,
    record_verification_metrics,
    set_final_aborted_reason,
    set_total_loop_iterations,
)
from apps.cli.runtime.session_phase_timing import (
    append_intake_timing_step,
    ensure_runtime_breakdown_list,
    log_phase_timing_row,
)
from apps.cli.runtime.chat_prompt import (
    build_sliding_window_history,
    shrink_tool_result_for_prompt,
    sliding_window_enabled,
)
from apps.cli.runtime.repair_safety import (
    failed_checks_indicate_structural_python_failure,
    parse_error_fingerprint,
    parse_python_file_errors,
    validate_edited_python_parse,
)
from apps.cli.runtime.policy_tool_dedup import canonical_tool_invocation_key
from apps.cli.runtime.repair_specialist import (
    REPAIR_JSON_SYSTEM,
    REPAIR_OUTCOME_APPLIED_PATCH,
    REPAIR_OUTCOME_INVALID_PATCH,
    REPAIR_OUTCOME_NO_EFFECTIVE_PATCH,
    REPAIR_OUTCOME_OUT_OF_ALLOWLIST,
    REPAIR_OUTCOME_PARSE_BLOCKED,
    REPAIR_OUTCOME_PROVIDER_FAILURE,
    build_repair_allowlist,
    classify_error_signature_delta,
    classify_failure_set_delta,
    classify_failure_set_delta_meta,
    classify_structured_repair_outcome,
    compute_causal_error_signature,
    snapshot_failed_verification,
    repair_context_char_budget,
    repair_specialist_enabled,
    summarize_primary_failure,
    run_repair_specialist_llm,
)
from apps.cli.runtime.repair_budget_nonpy import resolve_repair_operational_budget
from .indexer import RepoIndexer
from .ui.renderer import GhostRenderer
from .ui.theme import ghost_panel, make_ghost_console
from ghostllm_core.memory import MemoryStore


def _sha256_utf8(s: str) -> str:
    """Helper to get sha256 hex digest of a string encoded as utf-8."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _positive_int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_newlines_with_index_map(text: str) -> Tuple[str, List[int]]:
    normalized: List[str] = []
    index_map: List[int] = []
    i = 0
    while i < len(text):
        index_map.append(i)
        ch = text[i]
        if ch == "\r":
            normalized.append("\n")
            if i + 1 < len(text) and text[i + 1] == "\n":
                i += 2
            else:
                i += 1
            continue
        normalized.append(ch)
        i += 1
    index_map.append(len(text))
    return "".join(normalized), index_map


def _dominant_newline(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    if "\r" in text:
        return "\r"
    return os.linesep


def _coerce_newlines_like_reference(text: str, reference: str) -> str:
    target_newline = _dominant_newline(reference)
    normalized, _ = _normalize_newlines_with_index_map(text)
    return normalized.replace("\n", target_newline)


def _line_number_from_offset(text: str, offset: int) -> int:
    if offset <= 0:
        return 1
    current = 0
    for idx, line in enumerate(text.splitlines(keepends=True), start=1):
        current += len(line)
        if offset < current:
            return idx
    return max(1, len(text.splitlines()) or 1)


def _suggest_recovery_read_window(path: str, content: str, old_str: str) -> Optional[Dict[str, Any]]:
    content_lines = content.splitlines()
    if not content_lines:
        return None
    candidates = [line.strip() for line in old_str.splitlines() if line.strip()]
    for needle in candidates:
        if len(needle) < 6:
            continue
        for idx, line in enumerate(content_lines, start=1):
            if needle in line:
                start_line = max(1, idx - 3)
                max_lines = min(12, len(content_lines) - start_line + 1)
                return {
                    "path": path,
                    "start_line": start_line,
                    "max_lines": max_lines,
                }
    return None


DISCOVERY_ACTIONS = frozenset(["summarize_repo", "ls", "read_file"])
WRITE_ACTIONS = frozenset(["write_file", "edit_file", "delete_file"])
DISCOVERY_CAP = int(os.getenv("GHOST_DISCOVERY_CAP", 10))


class CodexAssistant:
    SYSTEM_PROMPT = """
# GHOST CORE | Integrity First
You are Ghost, the high-performance evolution of the AXI framework.
Your primary goal is to safely and efficiently maintain project integrity.

# Build-Fix Safe Mode
1. **INTEGRITY FIRST**: Before any repair or multi-file edit, you MUST verify the current state.
2. **SURGICAL EDITS**: Avoid rewriting entire files. Use `edit_file` whenever possible to minimize risk.
3. **POLICY BOUNDARIES**: You already have a Policy Gate active. If a tool call is safe and within intent, proceed.
4. **WINDOWS NATIVE**: Always use PowerShell-compatible commands. Never use Unix-only pipes like `head`, `tail`, `grep`, `sed`, `awk` in run_shell.
5. **PLATFORM SHELL**: Ghost runs in PowerShell. Use `Select-Object -First N` instead of `head -n N`, `Select-Object -Last N` instead of `tail -n N`, `Select-String -Pattern` instead of `grep`. Example: `npx tsc --noEmit 2>&1 | Select-Object -First 20` not `| head -20`.
6. **ERROR CLASSIFICATION**: Before fixing, classify the error as (SYNTAX_ERROR, DEPENDENCY_MISSING, LOGIC_BUG, ASSET_MISSING).

# Mode Boundaries
- **Plan**: Research only. No `write_file`, `edit_file`, or `run_shell`.
- **Code/Fix/Debug**: Active execution permitted.

# Tool Call Format
Use JSON within <tool_call> tags. Example:
<tool_call>{{"name": "read_file", "arguments": {{"path": "main.py"}}}}</tool_call>

# Automatic Integrity Check
- Ghost performs a verification cycle (Build/Lint/Tests) automatically at the end of every task.
- Do NOT run manual verification tools unless you specifically need to debug a verification failure.

# Shell Minimization & Platform
- Prefer `ls`, `read_file`, `summarize_repo` over `run_shell` for exploration. Use `run_shell` only for: builds, migrations, tests, or when no other tool can do the job.
- In PowerShell: use `Select-Object -First N` (not head), `Select-Object -Last N` (not tail), `Select-String` (not grep). Unix pipes with head/tail/grep are blocked by policy.
- **Verification commands**: Use standard commands only: `npm run build`, `npx tsc --noEmit`, `npm run lint`. Do NOT add pipelines (e.g. `2>&1 | Select-Object`) - the runtime handles truncation. This ensures consistent error detection.
- For listing directories: use `ls` with path. Never use `run_shell` with Get-ChildItem, dir, or ls - redundant and wastes budget.
- When the user says "no uses shell", "do not use shell", or "shell only when necessary": use `run_shell` only as last resort (e.g. db:migrate, npm run build). Use `ls`/`read_file` for all exploration.

# Path & File Handling
- Directories: use `ls` with the path. If you pass a directory to `read_file`, Ghost returns the listing automatically.
- Files: use `read_file` only for file paths (e.g. app/api/issues/route.ts). Paths are normalized (no double slashes).
- For surgical context, prefer `read_file(path, start_line=..., max_lines=...)` over shell snippets.
- **Documentation / agent files:** Do NOT use `write_file` to replace `AGENTS.md`, `README.md`, `CONTRIBUTING.md`, or `LICENSE` with the user's request text, a one-line "plan", or a dump of the prompt. Those files define project or agent rules. For UI/branding work, edit `app/`, `components/`, styles, and `public/` unless the user explicitly names a doc file.

# Typed Validation (Zod/TypeScript)
- When validating request params with Zod (e.g. safeParse, parse), ALWAYS use the parsed/validated value for downstream typed operations.
- Do NOT use the raw untyped input (e.g. searchParams.get) after validation if a typed parsed value exists.
- Example: if you have parseResult = schema.safeParse(raw), use parseResult.data (the validated enum/literal) for eq(column, value), not the raw string. Raw string stays as string; schema.output is properly typed.

# Implementation Progression
- For implementation tasks (API, schema, endpoints): after discovering the code exists (schema, routes, build passes), conclude with "Already implemented" and list evidence. Do not stay in read-only exploration indefinitely.

{fast_session_nudge}
{agents_project_block}
# Session phases (MANDATORY)
You MUST follow this loop. Never stay in EXPLORE forever.

PHASE EXPLORE
- **Symbol / definition questions (where is X defined, which file contains X, what does X do):** use `search_code(mode=symbol)` as the FIRST tool. Then `read_file` the top match and answer. Do NOT open exploration with `ls` unless the user explicitly asked to list a directory.
- **Filename / path questions:** use `search_code(mode=filename)` or `search_code(mode=path_exists)` to check existence. Use ls only if you need a directory listing.
- **Architecture / overview:** start with `summarize_repo`, then targeted `read_file`.
- **Write tasks:** read only what is strictly needed; avoid repeating `ls` on already-listed directories.
- Classify: New implementation | Modification | Bug fix | Already implemented
- The runtime (Exploration Engine v2) injects tool-ranking hints and detects churn. If a [SYSTEM EXPLORE v2] message appears, follow its strategy guidance.
- Do NOT re-list the same directory more than once. If you see the **same file list again** for the same path (cached/shadow `ls`), stop listing — use `read_file`, `search_code`, or synthesize.
- Do NOT repeat a tool call that already returned an error.

PHASE ACT
- write/edit/delete files as needed
- If already implemented: produce final answer and stop tools

PHASE VERIFY (runtime-driven)
- Ghost runs integrity checks after edits; you may read changed files while they run

PHASE REPAIR (runtime-driven)
- If verification fails, Ghost injects a repair turn; fix failures with focused edits

Terminal phases IDLE/INTAKE/DONE/ABORTED are managed by the runtime.

{contract_spec_block}{repo_profile_block}{decision_planner_block}{retrieval_block}{execution_agent_block}{exploration_block}
[CURRENT TASK INTENT]
Intent: {task_intent}
Scope: {task_scope}
Change expectation: {change_expectation}
Confidence: {intent_confidence:.0%}

[CURRENT SESSION PHASE]
Phase: {session_phase}
Discovery actions this session: {discovery_count}
{state_nudge}
"""

    NATIVE_TOOLS = [
        {"name": "ls", "description": "List files in a directory", "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}},
        {
            "name": "read_file",
            "description": "Read file content. Optional line slicing: start_line + max_lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "max_lines": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "search_code",
            "description": (
                "Search for symbols, filenames, file paths, or text patterns inside the repository. "
                "PREFERRED over ls for symbol lookups and 'where is X defined' questions. "
                "Parameters: query (required) — the symbol name, filename fragment, or text pattern; "
                "mode (optional) — one of: 'symbol' (default, word-boundary match), 'filename' (file name contains), "
                "'path_exists' (check if path exists), 'text' (raw regex search); "
                "max_results (optional, default 20)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "mode": {"type": "string", "enum": ["symbol", "filename", "path_exists", "text"], "default": "symbol"},
                    "max_results": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
        },
        {"name": "write_file", "description": "Write entire file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
        {"name": "edit_file", "description": "Surgical string replacement", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}}, "required": ["path", "old_str", "new_str"]}},
        {"name": "run_shell", "description": "Execute PowerShell command. For verification use standard commands only: npm run build, npx tsc --noEmit, npm run lint. Do NOT use custom pipelines (e.g. 2>&1 | Select-Object) - the runtime handles output truncation.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "delete_file", "description": "Delete a file with mandatory evidence", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["path", "evidence"]}},
        {"name": "summarize_repo", "description": "Get high-level project summary", "parameters": {"type": "object", "properties": {}}}
    ]

    READ_ONLY_TOOL_NAMES = frozenset({"ls", "read_file", "summarize_repo", "search_code"})
    WRITE_OR_SHELL_TOOL_NAMES = frozenset({"write_file", "edit_file", "delete_file", "run_shell"})

    def _tool_defs_for_session_phase(self) -> List[Dict[str, Any]]:
        if self.session_phase == SessionPhase.EXPLORE:
            return [t for t in self.NATIVE_TOOLS if t["name"] in self.READ_ONLY_TOOL_NAMES]
        return list(self.NATIVE_TOOLS)

    def _phase_blocked_tool_message(self, tool_name: str) -> Optional[str]:
        if self.session_phase != SessionPhase.EXPLORE:
            return None
        if tool_name in self.WRITE_OR_SHELL_TOOL_NAMES:
            return (
                f"Phase EXPLORE is read-only (allowed: {', '.join(sorted(self.READ_ONLY_TOOL_NAMES))}). "
                "This write/shell attempt was rejected. The runtime may promote you to ACT on the next loop "
                "when policy allows (see explore_promotion in session events)."
            )
        return None

    def __init__(
        self,
        server_url: str,
        api_key: str,
        model: str,
        mode: str = "Chat",
        auto_approve: bool = False,
        profile: str = "coder",
        project_name: str = "GhostLLM",
        role: str = "main",
        is_swarm_worker: bool = False,
        command_mode: str = "dev",
        runtime_prep: Optional[Any] = None,
        gateway_preflight: Optional[Any] = None,
    ):
        self.role = role
        self.is_swarm_worker = is_swarm_worker
        self.command_mode = command_mode
        
        # Override mode based on role
        if role == "architect": mode = "Plan"
        elif role == "reviewer": mode = "Review"
        
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self._gateway_preflight = gateway_preflight
        self.model = model
        self.mode = mode
        self._bundle_approval_fp: Optional[str] = None
        self._bundle_approval_ok: Optional[bool] = None
        self.auto_approve = auto_approve
        self.profile = profile
        self.project_name = project_name
        self.history = []
        self.session_id = f"ghost_{int(time.time())}"
        self.cwd = os.path.abspath(os.getcwd())
        if runtime_prep is None:
            from apps.cli.runtime.runtime_env import prepare_runtime

            runtime_prep = prepare_runtime(self.cwd)
        self._runtime_prep = runtime_prep
        self.memory = MemoryStore()
        
        # UI & Runtime
        self.console = make_ghost_console()
        self.renderer = GhostRenderer(self.console)
        gw_pf = getattr(self, "_gateway_preflight", None)
        self.renderer.render_cli_startup_summary(
            command_mode=self.command_mode,
            assistant_mode=self.mode,
            model=self.model,
            prep=self._runtime_prep,
            cwd_same_as_repo=True,
            role_models=resolve_model_role_map(),
            llm_gateway_url=self.server_url,
            llm_gateway_reachable=getattr(gw_pf, "ok", None) if gw_pf is not None else None,
            llm_gateway_checked=getattr(gw_pf, "checked_url", "") if gw_pf is not None else "",
            provider_backend_label="openai_compatible",
        )
        self.policy_gate = PolicyGate(self.console, self.auto_approve)
        self.verification_manager = VerificationManager(self.cwd)
        self.verification_coordinator = VerificationCoordinator(self.verification_manager)
        self.artifact_manager = ArtifactManager(self.cwd)
        self.task_manager = TaskManager(self.cwd)
        self.swarm_manager = SwarmManager(self.cwd)
        self.review_coordinator = ReviewCoordinator(self.cwd, self.swarm_manager)
        self.merge_assistant = MergeAssistant(self.cwd, self.swarm_manager, self.review_coordinator)
        self.indexer = RepoIndexer(self.cwd)
        self.budget_manager = BudgetManager(int(os.getenv("GHOST_AUTONOMY_BUDGET", 100)))
        self.stagnation_detector = StagnationDetector()
        self.exploration_memory = ExplorationMemory()
        self.batch_manager = BatchManager(self.cwd)
        self.hook_manager = HookManager()
        self._register_default_hooks()
        
        self.current_intent = Intent(mode="Chat", task="", is_slash_command=False, task_type="ask")
        self.stop_loop = False
        self.session_phase = SessionPhase.IDLE
        self._phase_wall_start_mono: Optional[float] = None
        self._phase_segment_model_turns: int = 0
        self._loop_abort_reason: Optional[str] = None
        self._discovery_action_count = 0
        self._explore_seen_read_paths: set = set()
        self._explore_read_path_counts: Dict[str, int] = {}
        self._explore_text_only_streak = 0
        self._explore_ls_paths_this_dispatch: set = set()
        self._small_scope_planning_nudge_sent = False
        self._last_explore_assistant_text = ""
        self._explore_rejected_writes_this_round: List[str] = []
        self._trace_mgr = None
        self._trace_ctx_token = None
        self._last_api_error_hint: str = ""
        self._repair_act_bridge: bool = False
        self._last_main_turn_prompt_chars: int = 0
        self._history_rollup: str = ""
        self._tool_output_seq: int = 0
        self._http_session = requests.Session()
        self._answer_now_nudge_pending: bool = False
        self._answer_now_synthesis_context: Optional[str] = None
        self._answer_now_pressure_injected_key: Optional[Tuple[int, int]] = None
        self._final_synthesis_done: bool = False
        # Exploration Engine v2
        self._explore_v2_mismatch_checked: bool = False
        self._explore_v2_listing_path_checked: bool = False
        self._explore_v2_churn_nudge_injected: bool = False
        self._explore_v2_tool_ranking_nudge_injected: bool = False
        self._last_tool_denial_key: Optional[Tuple[str, str]] = None

    @staticmethod
    def _api_failure_user_hint(status_code: int, err_msg: Any) -> str:
        """Actionable hint when chat/completions fails (not 'rephrase your prompt')."""
        em = str(err_msg or "").lower()
        if status_code == 404:
            return (
                "Upstream 404 (modelo o ruta API): revisa configs/models.yaml, variables GHOST_*_MODEL, "
                "y que upstream.base_url termine en /v1. No suele solucionarse cambiando el texto del prompt."
            )
        if status_code == 403:
            return (
                "Modelo no permitido en el registro Ghost (403): habilita el alias en configs/models.yaml "
                "o usa un id listado en GET /v1/models. Si usaste `ghost plan` con un CLI antiguo, "
                "el string literal «planner» no es válido: actualiza Ghost o pasa `--model kimi` (o el upstream del planner)."
            )
        if status_code == 429 or "rate" in em or "429" in em:
            return "Límite de tasa o cuota: espera y reintenta, o cambia de modelo/perfil."
        if status_code >= 500 or "timeout" in em or "upstream" in em:
            return "Fallo de servidor o proveedor: ghost doctor, revisa ghost.log y el estado de NVIDIA NIM."
        return ""

    def _trace_phase_start(self, name: str) -> None:
        m = getattr(self, "_trace_mgr", None)
        if m:
            try:
                m.start_phase(name)
            except Exception:
                pass

    def _trace_phase_end(self, name: str, status: str = "ok", notes: str = "") -> None:
        m = getattr(self, "_trace_mgr", None)
        if m:
            try:
                m.end_phase(name, status=status, notes=notes)
            except Exception:
                pass

    def _finalize_session_trace(self) -> None:
        mgr = getattr(self, "_trace_mgr", None)
        if not mgr:
            return
        try:
            sess = self.artifact_manager.current_session
            if sess:
                try:
                    if mgr.batch_executor_used:
                        sess.batch_executor_used = True
                    if mgr.approval_compression_used:
                        sess.approval_compression_used = True
                    sess.compressed_approval_count = max(
                        int(getattr(sess, "compressed_approval_count", 0) or 0),
                        int(mgr.compressed_approval_count or 0),
                    )
                    sess.batched_read_count = max(
                        int(getattr(sess, "batched_read_count", 0) or 0),
                        int(mgr.batched_read_count or 0),
                    )
                    sess.batched_verification_count = max(
                        int(getattr(sess, "batched_verification_count", 0) or 0),
                        int(mgr.batched_verification_count or 0),
                    )
                except Exception:
                    pass
            path, short, detail_md = finish_session_trace(mgr, self.cwd, sess)
            if sess and path:
                sess.trace_path = path
                sess.trace_summary_short = short or ""
                sess.trace_detail_md = detail_md or ""
                try:
                    self.artifact_manager.persist()
                except Exception:
                    pass
        except Exception as e:
            logger.warning("session trace finalize failed (non-fatal): %s", e)
        finally:
            self._trace_mgr = None

    def _flush_completed_phase_wall_clock(
        self,
        completed: SessionPhase,
        *,
        entering_detail: str,
    ) -> None:
        """Record wall duration + model turns for the phase we are leaving (best-effort)."""
        if completed == SessionPhase.IDLE:
            return
        start = getattr(self, "_phase_wall_start_mono", None)
        if start is None:
            return
        dur_ms = (time.monotonic() - float(start)) * 1000.0
        sess = self.artifact_manager.current_session
        if not sess:
            return
        pv = completed.value
        row = {
            "phase": pv,
            "duration_ms": round(dur_ms, 2),
            "model_turns_segment": int(getattr(self, "_phase_segment_model_turns", 0) or 0),
            "iteration_at_transition": getattr(self, "_session_loop_iteration", None),
            "detail_entering_next": (entering_detail or "")[:220],
        }
        ensure_runtime_breakdown_list(sess).append(row)
        log_phase_timing_row(row)

    def _apply_session_phase(
        self,
        phase: SessionPhase,
        *,
        detail: str = "",
        sync_task: bool = True,
    ) -> None:
        prev = getattr(self, "session_phase", None)
        if prev != phase:
            if prev is not None and prev != SessionPhase.IDLE:
                self._flush_completed_phase_wall_clock(
                    prev,
                    entering_detail=detail,
                )
            self._phase_wall_start_mono = time.monotonic()
            self._phase_segment_model_turns = 0
            logger.info(
                "session phase: %s -> %s%s",
                getattr(prev, "value", prev),
                phase.value,
                f" — {detail}" if detail else "",
            )
        if phase == SessionPhase.EXPLORE and prev != SessionPhase.EXPLORE:
            self._explore_seen_read_paths = set()
            self._explore_read_path_counts = {}
            self._explore_text_only_streak = 0
            self._small_scope_planning_nudge_sent = False
            self._last_explore_assistant_text = ""
            self._answer_now_nudge_pending = False
            self._answer_now_synthesis_context = None
            self._answer_now_pressure_injected_key = None
        self.session_phase = phase
        sess = self.artifact_manager.current_session
        if sess:
            prev_s = getattr(prev, "value", prev) if prev is not None else ""
            sess.record_phase_transition(
                phase.value,
                detail=detail,
                from_phase=str(prev_s) if prev_s is not None else "",
                iteration=getattr(self, "_session_loop_iteration", None),
            )
            if prev != phase and phase != SessionPhase.IDLE:
                self._append_phase_checkpoint(sess, phase, detail)
        if sync_task and self.task_manager.current_task:
            ct = self.task_manager.current_task
            if ct.status != phase.value or detail:
                self.task_manager.update_status(
                    phase.value,
                    detail=detail or None,
                    event="phase",
                )

    def _abort_session_policy_block(self) -> None:
        """Policy terminal tool stop: halt loop as CLOSING; finalize applies ABORTED once."""
        self._loop_abort_reason = LOOP_ABORT_POLICY
        self._session_failed = True
        self._apply_session_phase(SessionPhase.CLOSING, detail="policy_block", sync_task=True)

    def _apply_terminal_resolution(self, tr: TerminalResolution, session: Any) -> None:
        """Apply single authoritative terminal outcome + phase (finalize only)."""
        session.task_outcome = tr.outcome
        session.outcome_confidence = tr.outcome_confidence
        session.evidence_score = tr.evidence_score
        session.evidence_lines = list(tr.evidence_lines)
        session.root_cause = tr.root_cause
        session.next_action = tr.next_action
        session.task_confidence_level = getattr(tr, "task_confidence_level", "medium")
        session.task_completion_confidence = float(getattr(tr, "task_completion_confidence", 0.0) or 0.0)
        session.closure_reason = getattr(tr, "closure_reason_code", "") or ""
        session.closure_reason_summary_es = getattr(tr, "closure_reason_summary_es", "") or ""
        session.closure_posture_es = getattr(tr, "closure_posture_es", "") or ""
        session.task_confidence_signals = dict(getattr(tr, "task_confidence_signals", {}) or {})
        session.findings_evidence_tier = str(getattr(tr, "findings_evidence_tier", "") or "")
        self._loop_abort_reason = tr.loop_abort_reason
        self._apply_session_phase(tr.phase, detail="session complete", sync_task=True)

    @staticmethod
    def _env_truthy(name: str) -> bool:
        return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")

    def _verify_batch_approval_callback(self):
        if not self._env_truthy("GHOST_USE_APPROVAL_COMPRESSION") or self.auto_approve:
            return None

        def _fn(cmds: List[str]) -> bool:
            if len(cmds) <= 1:
                return True
            t0 = time.monotonic()
            ok = self.renderer.render_verify_batch_approval_request(cmds)
            _mgr = getattr(self, "_trace_mgr", None)
            if _mgr:
                try:
                    _mgr.record_batch_execution(
                        BatchExecutionTrace(
                            batch_id=f"vm_{uuid.uuid4().hex[:10]}",
                            batch_type="verify_batch",
                            step_count=len(cmds),
                            approval_class="shell_verify",
                            compressed_approval_used=True,
                            total_batch_duration_ms=(time.monotonic() - t0) * 1000.0,
                            ok=ok,
                            notes="verification_manager",
                        )
                    )
                except Exception:
                    pass
            return ok

        return _fn

    def _apply_verify_shell_batch_annotations(self, prepared_calls: List[Dict[str, Any]]) -> None:
        if not self._env_truthy("GHOST_USE_BATCH_EXECUTOR"):
            return
        segs = batch_exec.segment_prepared_call_indices(prepared_calls)
        compress = self._env_truthy("GHOST_USE_APPROVAL_COMPRESSION")
        for sk, idxs in segs:
            if sk != "verify_batch":
                continue
            plan = batch_exec.build_batch_plan_for_segment(sk, idxs, prepared_calls)
            if not plan:
                continue
            if batch_exec.should_compress_verify_approval(
                sk, len(idxs), compression_enabled=compress
            ) and not self.auto_approve:
                req = batch_exec.build_verify_batch_approval_request(plan)
                ok = self.renderer.render_verify_batch_approval_request(req.shell_commands)
                for ix in idxs:
                    prepared_calls[ix]["shell_batch_preapproved"] = ok

    def _append_batch_plan_artifact(self, plan: batch_exec.BatchPlan) -> None:
        sess = self.artifact_manager.current_session
        if not sess:
            return
        try:
            sess.batch_executor_used = True
            sess.batch_plans.append(plan.model_dump(mode="json"))
        except Exception:
            pass

    def _append_batch_result_artifact(
        self,
        plan: batch_exec.BatchPlan,
        idxs: List[int],
        dur_ms: float,
        seg_ok: bool,
        compress_used: bool,
    ) -> None:
        sess = self.artifact_manager.current_session
        if not sess:
            return
        try:
            br = batch_exec.BatchExecutionResult(
                batch_id=plan.batch_id,
                ok=seg_ok,
                duration_ms=dur_ms,
                summary=f"type={plan.batch_type} steps={len(idxs)} compressed={compress_used}",
            )
            sess.batch_results.append(br.model_dump(mode="json"))
        except Exception:
            pass

    def _record_batch_segment_trace(
        self,
        plan: batch_exec.BatchPlan,
        idxs: List[int],
        dur_ms: float,
        compress_used: bool,
        seg_ok: bool,
    ) -> None:
        mgr = getattr(self, "_trace_mgr", None)
        if not mgr:
            return
        try:
            mgr.record_batch_execution(
                BatchExecutionTrace(
                    batch_id=plan.batch_id,
                    batch_type=plan.batch_type,
                    step_count=len(idxs),
                    approval_class=plan.approval_scope or "",
                    compressed_approval_used=bool(
                        compress_used and plan.batch_type == "verify_batch"
                    ),
                    total_batch_duration_ms=float(dur_ms),
                    ok=seg_ok,
                    notes="",
                )
            )
        except Exception:
            pass

    def _run_segmented_prepared_calls(
        self,
        prepared_calls: List[Dict[str, Any]],
        status,
        *,
        legacy: bool = False,
    ) -> Tuple[List[str], str]:
        """Returns (executed_tool_names, exit_reason) exit_reason: '' | 'terminal' | 'stagnation'."""
        self._apply_verify_shell_batch_annotations(prepared_calls)
        use_batch = self._env_truthy("GHOST_USE_BATCH_EXECUTOR")
        if use_batch:
            segs = batch_exec.segment_prepared_call_indices(prepared_calls)
        else:
            segs = [("single", [i]) for i in range(len(prepared_calls))]
        executed_names: List[str] = []
        mem_kind = "user" if legacy else "tool"
        self.renderer.begin_tool_segment()
        try:
            exit_reason = self._run_segmented_inner(
                segs, prepared_calls, status, legacy, mem_kind, executed_names
            )
            return executed_names, exit_reason
        finally:
            self.renderer.flush_tool_segment()

    def _run_segmented_inner(
        self,
        segs: List[Tuple[str, List[int]]],
        prepared_calls: List[Dict[str, Any]],
        status,
        legacy: bool,
        mem_kind: str,
        executed_names: List[str],
    ) -> str:
        use_batch = self._env_truthy("GHOST_USE_BATCH_EXECUTOR")
        for sk, idxs in segs:
            t_seg = time.monotonic()
            seg_ok = True
            plan = None
            compress_used = (
                sk == "verify_batch"
                and len(idxs) >= 2
                and self._env_truthy("GHOST_USE_APPROVAL_COMPRESSION")
                and not self.auto_approve
            )
            if use_batch and sk in ("read_batch", "verify_batch"):
                plan = batch_exec.build_batch_plan_for_segment(sk, idxs, prepared_calls)
                if plan:
                    self._append_batch_plan_artifact(plan)
            for idx in idxs:
                pc = prepared_calls[idx]
                tc_name = pc["name"]
                tc_args = pc["args"]
                if legacy:
                    call = pc.get("call")
                    tc_id = None
                else:
                    tc = pc["tc"]
                    tc_id = tc.get("id")
                phase_err = self._phase_blocked_tool_message(tc_name)
                if phase_err:
                    if (
                        self.session_phase == SessionPhase.EXPLORE
                        and tc_name in self.WRITE_OR_SHELL_TOOL_NAMES
                    ):
                        self._explore_rejected_writes_this_round.append(tc_name)
                    self.renderer.append_tool_trace(tc_name, phase_err[:160], False)
                    tool_result = {"error": phase_err}
                    res_content = self._json_for_tool_prompt(tc_name, tool_result)
                    if legacy:
                        res_content = f"TOOL_RESULT: {res_content}"
                        self.history.append({"role": "user", "content": res_content})
                    else:
                        self.history.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "name": tc_name,
                                "content": res_content,
                            }
                        )
                    self.memory.add_message(self.session_id, mem_kind, res_content)
                    seg_ok = False
                    continue
                allow, deny_reason = self._contract_spec_allow_tool(tc_name, tc_args)
                if not allow:
                    try:
                        _dk = canonical_tool_invocation_key(tc_name, tc_args if isinstance(tc_args, dict) else {})
                    except Exception:
                        try:
                            _ak = json.dumps(tc_args, sort_keys=True, default=str)[:1200]
                        except (TypeError, ValueError):
                            _ak = str(tc_args)[:1200]
                        _dk = (str(tc_name or "").lower(), _ak)
                    if _dk == getattr(self, "_last_tool_denial_key", None):
                        deny_reason = f"{deny_reason} [repeated identical blocked call — do not retry this tool until policy changes]"
                        _sess_d = self.artifact_manager.current_session
                        if _sess_d is not None and getattr(_sess_d, "events", None) is not None:
                            _sess_d.events.append(
                                {
                                    "event": "redundant_tool_deny",
                                    "tool": tc_name,
                                    "key_tail": (_dk[1][:120] if len(_dk) > 1 else ""),
                                }
                            )
                    self._last_tool_denial_key = _dk
                    _d = tc_args.get("path") or tc_args.get("command") or ""
                    _lbl = f"{_d} — bloqueado: {deny_reason}" if _d else str(deny_reason or "bloqueado")
                    self.renderer.append_tool_trace(tc_name, _lbl, False)
                    tool_result = {"error": deny_reason}
                    res_content = self._json_for_tool_prompt(tc_name, tool_result)
                    if legacy:
                        res_content = f"TOOL_RESULT: {res_content}"
                        self.history.append({"role": "user", "content": res_content})
                    else:
                        self.history.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "name": tc_name,
                                "content": res_content,
                            }
                        )
                    self.memory.add_message(self.session_id, mem_kind, res_content)
                    seg_ok = False
                    continue
                self._last_tool_denial_key = None

                if tc_name == "ls":
                    from apps.cli.runtime.exploration_redundancy import get_previous_ls_result_payload
                    lp = str(tc_args.get("path") or ".").strip() or "."
                    cached_payload = get_previous_ls_result_payload(self.history, lp)
                    
                    if cached_payload:
                        self.renderer.append_tool_trace(
                            tc_name, f"{lp} — shadow listing (cache injected)", True
                        )
                        tool_result = cached_payload
                        res_content = self._json_for_tool_prompt(tc_name, tool_result)
                        sess_ls = self.artifact_manager.current_session
                        if sess_ls and getattr(sess_ls, "events", None) is not None:
                            sess_ls.events.append(
                                {
                                    "event": "explore_shadow_ls_injected",
                                    "path": lp,
                                }
                            )
                        logger.info("explore_shadow_ls_injected path=%s", lp)
                        if legacy:
                            res_content = f"TOOL_RESULT: {res_content}"
                            self.history.append({"role": "user", "content": res_content})
                        else:
                            self.history.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc_id,
                                    "name": tc_name,
                                    "content": res_content,
                                }
                            )
                        self.memory.add_message(self.session_id, mem_kind, res_content)
                        executed_names.append(tc_name)
                        self._after_explore_tool_result("ls", tc_args, tool_result, legacy)
                        continue
                _bt_path = ""
                if tc_name == "read_file":
                    _bt_path = str(tc_args.get("path") or "").strip()
                elif tc_name in ("edit_file", "write_file", "patch_file"):
                    _bt_path = str(tc_args.get("path") or "").strip()
                self.budget_manager.consume(tc_name, path=_bt_path)
                self.budget_manager.record_contract_spec_tool_invocation(tc_name)
                self.stagnation_detector.add_action(
                    tc_name, stagnation_tool_detail(tc_name, tc_args if isinstance(tc_args, dict) else {})
                )
                self._tools_executed_this_session = True
                if tc_name == "run_shell" and pc.get("shell_batch_preapproved") is False:
                    _cmd = tc_args.get("command") or "run_shell"
                    self.renderer.append_tool_trace(tc_name, f"{_cmd} (lote de verificación denegado)", False)
                    tool_result = {"error": "Verification batch denied by user."}
                else:
                    is_auth = bool(pc.get("is_authorized"))
                    if pc.get("shell_batch_preapproved") is True:
                        is_auth = True
                    if legacy:
                        tool_result = self._execute_tool(
                            call if call is not None else {"name": tc_name, "arguments": tc_args},
                            is_authorized=is_auth,
                        )
                    else:
                        tool_result = self._execute_tool(
                            {"name": tc_name, "arguments": tc_args},
                            is_authorized=is_auth,
                        )
                if (
                    self.session_phase == SessionPhase.EXPLORE
                    and tc_name == "read_file"
                    and not tool_result.get("error")
                ):
                    _rp = (tc_args.get("path") or "").strip()
                    if _rp:
                        _composed = PathComposer.compose(self.cwd, _rp)
                        _norm_read = os.path.normcase(os.path.normpath(_composed))
                        self._explore_seen_read_paths.add(_norm_read)
                        self._explore_read_path_counts[_norm_read] = (
                            int(self._explore_read_path_counts.get(_norm_read, 0) or 0) + 1
                        )
                if (
                    self.session_phase == SessionPhase.EXPLORE
                    and tc_name == "ls"
                    and isinstance(tool_result, dict)
                    and not tool_result.get("error")
                    and isinstance(tool_result.get("files"), list)
                ):
                    _lp = str(tc_args.get("path") or ".").strip() or "."
                    self._explore_ls_paths_this_dispatch.add(normalize_ls_path_key(_lp))
                executed_names.append(tc_name)
                res_content = self._json_for_tool_prompt(tc_name, tool_result)
                if legacy:
                    res_content = f"TOOL_RESULT: {res_content}"
                    self.history.append({"role": "user", "content": res_content})
                else:
                    self.history.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "name": tc_name,
                            "content": res_content,
                        }
                    )
                self.memory.add_message(self.session_id, mem_kind, res_content)
                if (
                    tc_name == "read_file"
                    and isinstance(tool_result, dict)
                    and not tool_result.get("error")
                ):
                    _sess_ledger = self.artifact_manager.current_session
                    _lpath = str(tc_args.get("path") or "").strip()
                    _lcontent = tool_result.get("content")
                    if _sess_ledger and _lpath and isinstance(_lcontent, str) and _lcontent.strip():
                        record_read_file_ledger(_sess_ledger, _lpath, _lcontent)
                if tc_name in ("write_file", "edit_file") and isinstance(tool_result, dict) and not tool_result.get(
                    "error"
                ):
                    self.budget_manager.note_successful_write_path(str(tc_args.get("path") or ""))
                if tc_name == "edit_file" and isinstance(tool_result, dict) and tool_result.get("error"):
                    _ee = str(tool_result.get("error") or "").lower()
                    if "old_str" in _ee or "target string" in _ee or "not found" in _ee:
                        self.stagnation_detector.add_failed_edit_attempt(str(tc_args.get("path") or ""))
                self._after_explore_tool_result(tc_name, tc_args, tool_result, legacy)
                if tool_result.get("terminal"):
                    if status:
                        status.stop()
                    self.budget_manager.consume("terminal_block")
                    return "terminal"
                st_is_stagnant, st_reason = self.stagnation_detector.check_stagnation()
                if st_is_stagnant:
                    if status:
                        status.stop()
                    checkpoint = self._create_autonomy_checkpoint("stagnation_detected", st_reason)
                    self.renderer.render_autonomy_checkpoint(checkpoint)
                    return "stagnation"
                if tool_result.get("error"):
                    seg_ok = False
            if use_batch and sk in ("read_batch", "verify_batch") and plan:
                dur = (time.monotonic() - t_seg) * 1000.0
                self._record_batch_segment_trace(plan, idxs, dur, compress_used, seg_ok)
                self._append_batch_result_artifact(plan, idxs, dur, seg_ok, compress_used)
        return ""

    def _record_tool_call_trace(
        self,
        name: str,
        target: str,
        started_iso: str,
        duration_ms: float,
        result: Dict[str, Any],
    ) -> None:
        mgr = getattr(self, "_trace_mgr", None)
        if not mgr:
            return
        try:
            err = result.get("error") if isinstance(result, dict) else None
            ok = err is None
            err_s = str(err or "").lower()
            if ok:
                tstatus = TRACE_STATUS_COMPLETED
            elif "denied" in err_s or "access denied" in err_s:
                tstatus = TRACE_STATUS_DENIED
            elif "blocked" in err_s or "norm block" in err_s:
                tstatus = TRACE_STATUS_BLOCKED
            else:
                tstatus = TRACE_STATUS_FAILED
            summary = ""
            if isinstance(result, dict):
                if "content" in result:
                    summary = f"content_len={len(str(result.get('content') or ''))}"
                elif "files" in result:
                    summary = f"files={len(result.get('files') or [])}"
                elif "stdout" in result:
                    summary = f"exit={result.get('exit_code')} out_len={len(str(result.get('stdout') or ''))}"
                elif "summary" in result:
                    summary = f"summary_len={len(str(result.get('summary') or ''))}"
                else:
                    summary = str(list(result.keys())[:6])
            mgr.record_tool_call(
                ToolCallTrace(
                    tool_name=name,
                    target=(target or "")[:2000],
                    started_at=started_iso,
                    ended_at=trace_timestamp_iso(),
                    duration_ms=duration_ms,
                    status=tstatus,
                    ok=ok,
                    error_message=str(err)[:2000] if err else "",
                    result_summary=summary[:500],
                )
            )
        except Exception:
            pass

    @staticmethod
    def _looks_like_test_path(path: str) -> bool:
        norm = str(path or "").replace("\\", "/").lower()
        return norm.startswith("tests/") or "/test_" in norm or norm.startswith("test_")

    def _workset_target_files(self) -> List[str]:
        ts = self._session_contract_spec_dict()
        if not isinstance(ts, dict):
            return []
        out: List[str] = []
        for item in ts.get("target_files") or []:
            if isinstance(item, str) and item.strip():
                out.append(item.replace("\\", "/"))
        return out[:8]

    def _update_session_workset_from_tool(
        self,
        name: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        sess = self.artifact_manager.current_session
        if not sess or not isinstance(result, dict) or result.get("error"):
            return
        try:
            targets = self._workset_target_files()
            if name == "read_file":
                path = str(result.get("path") or args.get("path") or "")
                if path and path != ".":
                    sess.note_workset_read(path)
                    if self._looks_like_test_path(path):
                        sess.note_related_test(path)
                    for target in targets:
                        if target and target != path:
                            sess.note_dependency_edge(target, path, "read_context")
                    sess.set_workset_focus_reason(f"read_file:{path}")
            elif name == "search_code":
                mode = str(args.get("mode") or "")
                query = str(args.get("query") or "")
                sess.note_search_query(mode, query)
                for match in (result.get("matches") or [])[:10]:
                    if not isinstance(match, dict):
                        continue
                    path = str(match.get("path") or "")
                    if not path:
                        continue
                    sess.note_workset_candidate(path)
                    if self._looks_like_test_path(path):
                        sess.note_related_test(path)
                    for target in targets:
                        if target and target != path:
                            sess.note_dependency_edge(target, path, f"search_{mode or 'code'}")
                if query:
                    sess.set_workset_focus_reason(f"search_code:{mode or 'symbol'}:{query}")
            elif name == "ls":
                path = str(args.get("path") or "")
                if path and path not in (".", "./"):
                    sess.note_workset_candidate(path)
                    sess.set_workset_focus_reason(f"ls:{path}")
            elif name in ("write_file", "edit_file", "delete_file"):
                path = str(args.get("path") or result.get("file") or "")
                op = "edit"
                if name == "write_file":
                    op = "write"
                elif name == "delete_file":
                    op = "delete"
                if path:
                    sess.note_workset_edit(path, op)
                    if self._looks_like_test_path(path):
                        sess.note_related_test(path)
                    sess.set_workset_focus_reason(f"{name}:{path}")
            elif name == "run_shell":
                cmd = str(args.get("command") or "")
                if cmd:
                    sess.note_shell_command(cmd)
                    for match in re.findall(r"((?:tests|apps|packages)/[\\w./-]+\\.py)", cmd.replace("\\", "/")):
                        sess.note_related_test(match)
                        for target in targets:
                            if target and target != match:
                                sess.note_dependency_edge(target, match, "shell_verify")
                    sess.set_workset_focus_reason("run_shell")
        except Exception:
            pass

    @staticmethod
    def _phase_checkpoint_next_step(phase: SessionPhase) -> str:
        if phase == SessionPhase.INTAKE:
            return "Bound the workset and prepare focused exploration."
        if phase == SessionPhase.EXPLORE:
            return "Read only the files needed to reach an implementation or no-op decision."
        if phase == SessionPhase.ACT:
            return "Apply the next coherent batch of edits on the active workset."
        if phase == SessionPhase.VERIFY:
            return "Run incremental verification against the files changed so far."
        if phase == SessionPhase.REPAIR:
            return "Patch only the root-cause files from the failed verification batch."
        if phase == SessionPhase.CLOSING:
            return "Summarize outcome, residual risks, and the next engineering action."
        return ""

    def _phase_checkpoint_open_risks(self, sess: Any) -> List[str]:
        risks: List[str] = []
        if getattr(self, "_pending_edit_recovery", None):
            risks.append("pending_edit_recovery")
        if bool(getattr(sess, "reverification_pending", False)):
            risks.append("reverification_pending")
        if bool(getattr(sess, "repo_mismatch_detected", False)):
            risks.append("repo_mismatch_detected")
        ver = getattr(sess, "verification", {}) or {}
        if isinstance(ver, dict) and ver.get("status") == "failed":
            risks.append("verification_failed")
        if int(getattr(sess, "repair_attempt_count", 0) or 0) > 0:
            risks.append("repair_in_progress")
        return risks

    def _append_phase_checkpoint(self, sess: Any, phase: SessionPhase, detail: str = "") -> None:
        try:
            sess.set_workset_focus_reason(detail or phase.value)
            sess.append_phase_checkpoint(
                phase=phase.value,
                reason=detail,
                iteration=getattr(self, "_session_loop_iteration", None),
                task_summary=getattr(sess, "plan", "") or getattr(sess, "task", ""),
                focus_files=sess.workset_focus_files(),
                open_risks=self._phase_checkpoint_open_risks(sess),
                planned_next_step=self._phase_checkpoint_next_step(phase),
            )
        except Exception:
            pass

    def _workset_focus_files(self, sess: Any, limit: int = 6) -> List[str]:
        try:
            return list(sess.workset_focus_files(limit=limit))
        except Exception:
            return []

    def _repair_focus_files(self, sess: Any, failed_checks: List[Dict[str, Any]]) -> List[str]:
        focus: List[str] = []
        ws = getattr(sess, "active_workset", {}) or {}
        for key in ("edited_files", "written_files"):
            for item in ws.get(key, []) or []:
                if item and item not in focus:
                    focus.append(str(item))
        for item in self._workset_target_files():
            if item and item not in focus:
                focus.append(item)
        names = {str(c.get("name") or "") for c in failed_checks if isinstance(c, dict)}
        if "Tests" in names:
            for item in ws.get("related_tests", []) or []:
                if item and item not in focus:
                    focus.append(str(item))
        return focus[:8]

    def _persist_repair_causal_digest(self, sess: Any, failed_checks: List[Dict[str, Any]]) -> None:
        if not sess or not failed_checks:
            return
        _, digest = compute_causal_error_signature(failed_checks)
        if digest:
            sess.last_repair_causal_digest = digest

    def _filter_diff_summary_for_repair(
        self,
        sess: Any,
        failed_checks: List[Dict[str, Any]],
        diff_summary: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if len(diff_summary) <= 1:
            return diff_summary
        focus = set(self._repair_focus_files(sess, failed_checks))
        if not focus:
            return diff_summary
        narrowed = [d for d in diff_summary if str(d.get("file") or "") in focus]
        return narrowed or diff_summary

    def _workset_runtime_nudge(self, state: Any) -> str:
        sess = self.artifact_manager.current_session
        if not sess:
            return ""
        parts: List[str] = []
        focus = self._workset_focus_files(sess, limit=6)
        if focus:
            parts.append(f"{MSG_ACTIVE_WORKSET_PLAIN_PREFIX} {', '.join(focus[:4])}.")
        ivs = getattr(sess, "incremental_verify_state", {}) or {}
        batches = int(ivs.get("batches_run") or 0)
        if batches > 0 and state in (SessionPhase.ACT, SessionPhase.REPAIR, SessionPhase.VERIFY):
            covered = ", ".join((ivs.get("files_covered") or [])[:3]) or "n/a"
            last_status = str(ivs.get("last_status") or "n/a")
            parts.append(f"Incremental verify: batches={batches}, last={last_status}, covered={covered}.")
        if len(focus) > 1 and state in (SessionPhase.ACT, SessionPhase.REPAIR):
            parts.append("Edit one coherent batch, then hand off to VERIFY before expanding scope.")
        return " ".join(parts)

    def _record_main_model_trace(
        self,
        *,
        started_iso: str,
        start_mono: float,
        first_mono: Optional[float],
        first_iso: Optional[str],
        ok: bool,
        err: str,
        prompt_chars: int,
        output_chars: int,
    ) -> None:
        mgr = getattr(self, "_trace_mgr", None)
        if not mgr:
            return
        try:
            end_mono = time.monotonic()
            duration_ms = (end_mono - start_mono) * 1000.0
            ttft_ms = None
            if first_mono is not None:
                ttft_ms = (first_mono - start_mono) * 1000.0
            role = "planner" if getattr(self, "profile", "") == "planner" else "general"
            mstatus = (
                TRACE_STATUS_COMPLETED if (ok and not err) else TRACE_STATUS_FAILED
            )
            mgr.record_model_call(
                ModelCallTrace(
                    role=role,
                    model=str(self.model),
                    provider_backend="openai_compatible",
                    started_at=started_iso,
                    first_token_at=first_iso,
                    ended_at=trace_timestamp_iso(),
                    ttft_ms=ttft_ms,
                    duration_ms=duration_ms,
                    status=mstatus,
                    ok=ok,
                    error_message=(err or "")[:2000],
                    prompt_chars=int(prompt_chars),
                    output_chars=int(output_chars),
                )
            )
        except Exception:
            pass

    def run(self, initial_task: Optional[str] = None):
        if initial_task:
            self._process_input(initial_task)
        
        while not self.stop_loop:
            try:
                text = self.renderer.read_input()
                if not text or text.lower() in ["exit", "quit", "q"]: 
                    break
                self._process_input(text)
            except KeyboardInterrupt:
                break
        
        self.console.print("\n[dim]Ghost session terminated. Stay secure.[/dim]")

    def _process_input(self, text: str):
        task = None
        is_resumed = False
        
        # 1. Handle Internal Task Commands
        if text.startswith("/tasks"):
            tasks = self.task_manager.list_tasks()
            self.renderer.render_task_list(tasks)
            return

        if text.startswith("/board"):
            self.swarm_manager.refresh_worker_statuses()
            workers = self.swarm_manager.list_workers()
            self.renderer.render_swarm_board(workers)
            return

        if text.startswith("/swarm cleanup"):
            count = self.swarm_manager.cleanup_workers()
            self.console.print(f"[bold green]✓ Swarm Cleanup Complete:[/bold green] {count} dead workers pruned.")
            return

        if text.startswith("/swarm"):
            self.swarm_manager.refresh_worker_statuses()
            self.console.print("[bold yellow]⚠ Ghost Swarm v0 Initialized.[/bold yellow]")
            self.console.print("[dim]Worktree management and coordination layer active.[/dim]")
            return

        if text.startswith("/batch start"):
            try:
                # Format: /batch start "Task 1", "Task 2"
                content = text.replace("/batch start", "").strip()
                if content.startswith("[") and content.endswith("]"):
                    step_titles = json.loads(content)
                else:
                    step_titles = [s.strip().strip('"') for s in content.split(",") if s.strip()]
                
                batch = self.batch_manager.create_batch(step_titles)
                self.console.print(f"[bold green]✓ Batch initialized:[/bold green] {batch.batch_id}")
                self.console.print(f"[dim]Executing {len(step_titles)} steps...[/dim]")
                
                batch.status = "running"
                self.batch_manager.save(batch)
                
                # Auto-inject first step as input
                self._process_input(batch.steps[0].title)
                return
            except Exception as e:
                self.console.print(f"[bold red]✘ Batch start failed:[/bold red] {str(e)}")
                return

        if text.startswith("/batch status"):
            batch = self.batch_manager.current_batch
            if not batch:
                self.console.print("[yellow]No active batch found.[/yellow]")
                return
            status_text = self.batch_manager.get_status(batch)
            self.console.print(f"[bold cyan]Batch Tracker:[/bold cyan] {status_text}")
            return

        if text.startswith("/batch stop"):
            batch = self.batch_manager.current_batch
            if batch and self.batch_manager.stop_batch(batch):
                self.console.print(f"[bold yellow]⚠ Batch {batch.batch_id} stopped manually.[/bold yellow]")
            else:
                self.console.print("[yellow]No running batch to stop.[/yellow]")
            return
        if text.startswith("/worker spawn"):
            try:
                parts = text.split(" ")
                role = parts[2]
                task_id = "unknown"
                if "--task" in parts:
                    idx = parts.index("--task")
                    task_id = parts[idx+1]
                
                worker = self.swarm_manager.spawn_worker(role, task_id)
                self.console.print(f"[bold green]✓ Worker spawned:[/bold green] {worker.worker_id} ({role})")
                return
            except Exception as e:
                self.console.print(f"[bold red]✘ Spawn failed:[/bold red] {str(e)}")
                return

        if text.startswith("/merge preview"):
            parts = text.split()
            if len(parts) < 3:
                self.console.print("[bold red]✘ Usage:[/bold red] /merge preview <task_id>")
                return
            task_id = parts[2]
            try:
                bundle = self.merge_assistant.get_merge_preview(task_id)
                self.renderer.render_merge_preview(bundle)
            except Exception as e:
                self.console.print(f"[bold red]✘ Preview failed:[/bold red] {str(e)}")
            return

        if text.startswith("/merge apply"):
            parts = text.split()
            if len(parts) < 3:
                self.console.print("[bold red]✘ Usage:[/bold red] /merge apply <task_id>")
                return
            task_id = parts[2]
            
            try:
                # 1. Preview check (Risk classification)
                bundle = self.merge_assistant.get_merge_preview(task_id)
                if not bundle.can_apply:
                    self.console.print(f"[bold red]✘ Merge Blocked:[/bold red] {bundle.conflict_risk}")
                    self.console.print("[yellow]Please rebase worktree or clean main repository before merging.[/yellow]")
                    return

                if bundle.conflict_risk != "LOW":
                    self.console.print(f"[bold yellow]⚠ Warning:[/bold yellow] Risk is {bundle.conflict_risk}")

                confirm = self.renderer.console.input(f"[bold yellow]⚠ Are you sure you want to apply merge for {task_id}? (y/n): [/bold yellow]")
                if confirm.lower() != 'y':
                    self.console.print("[yellow]Merge cancelled by user.[/yellow]")
                    return

                # 2. Execute Merge & Verification
                result = self.merge_assistant.apply_merge(task_id)
                self.renderer.render_merge_success(task_id, result["files"])
                
                # 3. Show Verification Results
                v = result["verification"]
                v_style = "green" if v["status"] == "success" else "red"
                self.console.print(f"\n[bold {v_style}]Post-Merge Verification: {v['status'].upper()}[/bold {v_style}]")
                for check in v["checks"]:
                    res_icon = "✓" if check["result"] == "passed" else "✘"
                    res_color = "green" if check["result"] == "passed" else "red"
                    self.console.print(f"  [{res_color}]{res_icon} {check['name']}[/{res_color}]")

            except Exception as e:
                self.console.print(f"[bold red]✘ Merge failed:[/bold red] {str(e)}")
            return

        if text.startswith("/review"):
            parts = text.split()
            if len(parts) < 2:
                self.console.print("[bold red]✘ Usage:[/bold red] /review <task_id>")
                return
            task_id = parts[1]
            try:
                bundle = self.review_coordinator.get_review_bundle(task_id)
                self.renderer.render_review_bundle(bundle)
            except Exception as e:
                self.console.print(f"[bold red]✘ Review failed:[/bold red] {str(e)}")
            return

        if text.startswith("/approve") or text.startswith("/reject"):
            parts = text.split()
            if len(parts) < 2:
                self.console.print(f"[bold red]✘ Usage:[/bold red] {parts[0]} <task_id> [comment]")
                return
            task_id = parts[1]
            decision = "approved" if parts[0] == "/approve" else "rejected"
            comment = " ".join(parts[2:]) if len(parts) > 2 else ""
            try:
                self.review_coordinator.save_decision(task_id, decision, comment)
                self.renderer.render_review_decision(task_id, decision)
            except Exception as e:
                self.console.print(f"[bold red]✘ Decision failed:[/bold red] {str(e)}")
            return

        # Determine Task Object
        task_obj = None
        is_resumed = False
        if text.startswith("/resume "):
            task_id_val = text.split(" ")[1].strip()
            task_obj = self.task_manager.resume_task(task_id_val)
            if task_obj:
                self.console.print(f"[bold green]✓ Resuming Task:[/bold green] {task_obj.task_id} - {task_obj.title}")
                is_resumed = True
            else:
                self.console.print(f"[bold red]✘ Task not found:[/bold red] {task_id_val}")
                return

        self.current_intent = route_intake_intent(text)
        intent = self.current_intent
        maybe_coerce_intent_for_operational_mode(intent)
        if intent.is_slash_command:
            self.mode = intent.mode
        self._bundle_approval_fp = None
        self._bundle_approval_ok = None

        if (
            not is_resumed
            and not self.task_manager.current_task
        ):
            from apps.cli.runtime.workflow_continuity import (
                is_continuation_user_message,
                load_latest_handoff_any,
            )

            if is_continuation_user_message(text):
                ho, _hn = load_latest_handoff_any(self.cwd)
                tid = str((ho or {}).get("task_id") or "").strip()
                if tid:
                    r = self.task_manager.resume_task(tid)
                    if r:
                        self.console.print(
                            f"[dim]Continuidad: reanudando tarea `{tid}` desde handoff reciente.[/dim]"
                        )

        # 0. Preflight Working Directory Guard (Hard-Block enforcement)
        bootstrap_keywords = ["desde cero", "nuevo", "inicializa", "re-inicializa", "bootstrap", "clean start"]
        is_bootstrap_intent = intent.scaffold_type == "bootstrap" or any(k in intent.task.lower() for k in bootstrap_keywords)
        
        if is_bootstrap_intent:
            action, msg, options = WorkingDirectoryGuard.validate_scaffold(self.cwd, intent.target_folder)
            if action == "HARD_BLOCK":
                self.console.print(f"\n[bold red]⚠ CWD GUARD CONFLICT:[/bold red] {msg}")
                self.console.print("\n[bold yellow]Opciones válidas:[/bold yellow]")
                for opt in options:
                    self.console.print(f"  • {opt}")
                self.console.print(f"\n[dim]Intento detectado: {intent.scaffold_type} | Directorio: {self.cwd}[/dim]\n")
                self._session_failed = True # Mark session as failed
                return # TERMINAL PREFLIGHT BLOCK
            elif action == "WARN":
                self.console.print(f"\n[bold yellow]⚠ AVISO DE CONTEXTO:[/bold yellow] {msg}")
        # Start Persistent Task (if not resumed)
        if not is_resumed:
            if not self.task_manager.current_task:
                # 1. Infer type from text
                inferred_type = infer_work_task_type(text)
                
                # 2. Extract manual override if provided via --type
                task_type_val = inferred_type
                type_source_val = "inferred"
                clean_text = text
                if "--type" in text:
                    parts = text.split("--type")
                    clean_text = parts[0].strip()
                    task_type_val = parts[1].split()[0].strip()
                    type_source_val = "manual"
                
                task_obj = self.task_manager.create_task(
                    clean_text, 
                    intent.mode, 
                    owner="denis", 
                    task_type=task_type_val,
                    inferred_type=inferred_type,
                    type_source=type_source_val,
                    batch_id=getattr(self.batch_manager.current_batch, "batch_id", None)
                )
            else:
                task_obj = self.task_manager.current_task

        if not task_obj:
            task_obj = self.task_manager.current_task

        if not task_obj:
            # Fallback for search/chat intents that should not reach execution loop
            return
        
        if is_resumed:
            task_obj.title = f"{task_obj.title} (Resumed: {text})"

        # --- BATCH TRACEABILITY ---
        step_id_val = None
        if self.batch_manager.current_batch and self.batch_manager.current_batch.status == "running":
            step_id_val = self.batch_manager.current_batch.current_step_index

        self._session_failed = False
        self._last_api_error_hint = ""
        self._blocked_shell_commands: List[tuple] = []  # (command, error) for run_shell blocked by policy
        self._pending_edit_recovery: Optional[Dict[str, Any]] = None
        self._already_implemented_answer_pending: Optional[Dict[str, Any]] = None
        self._stream_interrupted_this_session = False  # for strict verification provenance (no fake success)
        self._verification_completed_this_session = False  # True when we ran verification without skip_execution
        self._finalize_skip_integrity_re_verify = False  # loop VERIFY already ran full coordinator
        self.exploration_memory.clear()
        self.stagnation_detector.reset()
        self.session_phase = SessionPhase.INTAKE
        self._loop_abort_reason = None
        self._discovery_action_count = 0
        self._micro_task_early_pressure_injected = False
        self._conclusion_nudge_injected = False
        self._explore_v2_mismatch_checked = False
        self._explore_v2_listing_path_checked = False
        self._explore_v2_churn_nudge_injected = False
        self._explore_v2_tool_ranking_nudge_injected = False
        self._explore_empty_symbol_search_nudged = False
        self._explore_ls_before_search_nudged = False
        self.artifact_manager.start_session(
            text, 
            task_id=task_obj.task_id, 
            task_type=task_obj.task_type, 
            type_source=task_obj.type_source,
            batch_id=task_obj.batch_id,
            step_id=step_id_val
        )
        session = self.artifact_manager.current_session
        session.role_operator = (
            os.getenv("USER") or os.getenv("USERNAME") or session.role_operator or "human_operator"
        )
        session.role_author = session.role_author or "ghost_model"
        session.provider_backend = "openai_compatible"
        session.llm_gateway_url = self.server_url
        gw_pf0 = getattr(self, "_gateway_preflight", None)
        if gw_pf0 is not None:
            session.llm_gateway_preflight_ok = bool(getattr(gw_pf0, "ok", False))
            session.llm_gateway_preflight_path = str(getattr(gw_pf0, "checked_url", "") or "")
        rp0 = self._runtime_prep
        if rp0:
            session.config_source = rp0.config_source
            session.autoload_env_used = rp0.autoload_env_used
            session.env_file_loaded = rp0.env_file_loaded or ""
            session.active_feature_flags = dict(rp0.active_feature_flags)
            session.startup_warnings = list(rp0.startup_warnings)
            session.effective_repo_root = rp0.repo_root_effective
            session.cli_command_mode = self.command_mode
        self._trace_mgr = start_session_trace(session.session_id, self.cwd, self.command_mode)
        self._trace_ctx_token = attach_trace_manager(self._trace_mgr)
        try:
            self._chat_loop(task_obj, text, intent, is_resumed)
        finally:
            self._finalize_session_trace()
            if getattr(self, "_trace_ctx_token", None) is not None:
                detach_trace_manager(self._trace_ctx_token)
                self._trace_ctx_token = None

    def _handle_intent(self, intent: Intent) -> bool:
        if intent.task_type == "plan":
            self.console.print("[ghost.info]Ghost Designer Mode Active. Formulating plan...[/ghost.info]")
            return False # Continue to LLM for planning
        elif intent.task_type == "ask" and intent.task == "help":
            self.renderer.print_help()
            return True

    def _session_has_diff(self) -> bool:
        sess = self.artifact_manager.current_session
        return bool(sess and getattr(sess, "diff_summary", None))

    def _last_tool_round_had_edit_old_str_miss(self) -> bool:
        """True if latest assistant message is text-only and preceding tool batch includes edit_file old_str miss."""
        if len(self.history) < 3:
            return False
        last = self.history[-1]
        if last.get("role") != "assistant":
            return False
        if last.get("tool_calls"):
            return False
        found = False
        for m in reversed(self.history[:-1]):
            if m.get("role") == "assistant":
                break
            if m.get("role") != "tool" or m.get("name") != "edit_file":
                continue
            try:
                raw = m.get("content", "")
                if not isinstance(raw, str) or not raw.strip().startswith("{"):
                    continue
                res = json.loads(raw)
                err = str(res.get("error", "")).lower()
                if "target string" in err or ("old_str" in err and "not found" in err):
                    found = True
            except Exception:
                continue
        return found

    @staticmethod
    def _edit_miss_nudge_enabled() -> bool:
        v = os.getenv("GHOST_EDIT_MISS_NUDGE", "1").strip().lower()
        return v not in ("0", "false", "no", "off")

    def _fast_path_eligible(self, spec: Optional[Dict[str, Any]] = None) -> bool:
        if isinstance(spec, Mapping):
            ts: Mapping[str, Any] = spec
        else:
            current = self._session_contract_spec_dict() or {}
            if not isinstance(current, Mapping):
                return False
            ts = current
        ce = str(ts.get("change_expectation") or "").strip().lower()
        if ce not in ("must_write", "may_write"):
            return False
        targets = [str(x).strip() for x in (ts.get("target_files") or []) if str(x).strip()]
        return 1 <= len(targets) <= 2

    def _mark_fast_path_intake_state(self, session: Any) -> None:
        spec = contract_spec_dict_from_session(session) if session else None
        eligible = self._fast_path_eligible(spec)
        session.fast_path_eligible = bool(eligible)
        session.fast_path_used = False
        session.first_edit_turn = 0
        session.verification_scope = ""
        session.planned_check_cwds = {}
        session.fallback_reason = ""

    def _maybe_inject_fast_path_nudge(self) -> None:
        if getattr(self, "_fast_path_nudge_sent", False):
            return
        sess = self.artifact_manager.current_session
        if not sess or not getattr(sess, "fast_path_eligible", False):
            return
        spec = self._session_contract_spec_dict() or {}
        targets = [str(x).strip() for x in (spec.get("target_files") or []) if str(x).strip()]
        if not targets:
            return
        self._fast_path_nudge_sent = True
        target_text = ", ".join(targets[:2])
        msg = (
            "[SYSTEM] Focused write fast path active. This task already has explicit target_files. "
            f"Read only the minimum context around {target_text}, avoid repo-wide exploration, "
            "use read_file(start_line,max_lines) before edit_file, and move to the first patch within this turn or the next. "
            "Only inspect an extra file if it is a direct dependency of the target."
        )
        self.history.append({"role": "user", "content": msg})
        self.memory.add_message(self.session_id, "user", msg)
        sess.events.append({"event": "fast_path_nudge", "targets": targets[:2]})

    def _run_task_contract_intake(self, session: Any, text: str, intent: Intent) -> None:
        """TaskContract shell + TaskSpec / legacy spec, planner, retrieval, execution agent."""
        session.intake_timing_ms = {}
        self._fast_path_nudge_sent = False
        ensure_task_contract_foundation(session, intent)
        _t_rp = time.monotonic()
        self._trace_phase_start("repo_profile")
        repo_profile = scan_repo_profile(Path(self.cwd))
        vc_map: Dict[str, Any] = {}
        if repo_profile.v2 is not None:
            vcmd = repo_profile.v2.verification_commands
            vc_map = {
                "typecheck": vcmd.typecheck,
                "build": vcmd.build,
                "lint": vcmd.lint,
                "tests": vcmd.tests,
            }
        self.verification_manager.set_repo_verification_commands(vc_map)
        self._trace_phase_end("repo_profile")
        append_intake_timing_step(session, "repo_profile", (time.monotonic() - _t_rp) * 1000.0)
        if repo_profile.root:
            session.effective_repo_root = str(repo_profile.root.resolve())

        _t_cs = time.monotonic()
        self._trace_phase_start("contract_spec")
        source, budget_policy = intake_fill_task_contract_spec(session, text, repo_profile)
        # Early repo-mismatch probe (artifact + events) before heavy planner/retrieval
        if repo_mismatch_detection_enabled() and (text or "").strip():
            try:
                mm0 = detect_repo_mismatch(text, self.cwd, session, history=None)
                inject_mismatch_into_session(mm0, session)
                if mm0.detected:
                    session.events.append(
                        {
                            "event": "repo_mismatch_detected_intake",
                            "confidence": mm0.confidence,
                            "reasons": list(mm0.reasons),
                            "missing_paths": list(mm0.missing_paths or [])[:12],
                        }
                    )
            except Exception:
                logger.debug("intake repo_mismatch probe failed", exc_info=True)
        self.budget_manager.reset_contract_spec_tool_limits(budget_policy)
        _agents_root = str(getattr(session, "effective_repo_root", "") or "").strip() or self.cwd
        hydrate_agents_on_session(session, _agents_root, text or "")
        if source == "legacy_engine_off":
            self.console.print("[dim]Runtime contract: legacy (GHOST_USE_TASKSPEC disabled)[/dim]")
        elif source == "legacy_invalid_engine":
            self.console.print(
                "[yellow]⚠ Ghost: TaskSpec invalid — legacy intent contract active. "
                "Set GHOST_USE_TASKSPEC=0 to silence this path.[/yellow]"
            )
            self.console.print(
                f"[dim]Runtime contract: {session.runtime_contract_source} (engine validation failed)[/dim]"
            )
        else:
            vs = (
                getattr(session, "contract_spec_validation_status", None)
                or getattr(session, "taskspec_validation_status", None)
                or ""
            )
            self.console.print(
                f"[dim]Runtime contract: {session.runtime_contract_source}"
                + (f" (validation {vs})" if vs else "")
                + "[/dim]"
            )
        self._trace_phase_end("contract_spec")
        append_intake_timing_step(session, "contract_spec", (time.monotonic() - _t_cs) * 1000.0)

        self._apply_micro_task_detection(session, text)
        skip_intake_heavy = bool(
            getattr(session, "micro_task_kind", None) and micro_task_skip_intake_heavy_enabled()
        )
        if skip_intake_heavy:
            session.planner_used = False
            logger.info("intake: micro_task skip planner+retrieval (kind=%s)", session.micro_task_kind)

        if not skip_intake_heavy and parallel_preamble_eligible(session):
            _t_pp = time.monotonic()
            self._trace_phase_start("parallel_preamble")
            _r_iso = trace_timestamp_iso()
            try:
                pt = run_planner_and_retrieval_parallel(
                    Path(self.cwd),
                    session,
                    text,
                    budget_remaining=self.budget_manager.remaining,
                    apply_decision_planner=apply_decision_planner_to_session,
                )
                notes = (
                    f"planner_ms={pt.planner_ms:.1f};index_ms={pt.index_ms:.1f};apply_ms={pt.apply_ms:.1f};"
                    f"wall_ms={pt.wall_ms:.1f};serialized_est_ms={pt.serialized_would_ms:.1f};"
                    f"overlap_saved_ms={pt.overlap_saved_ms:.1f}"
                )
                self._trace_phase_end("parallel_preamble", notes=notes)
                append_intake_timing_step(session, "parallel_preamble", (time.monotonic() - _t_pp) * 1000.0)
                if pt.overlap_saved_ms >= 25:
                    self.console.print(
                        f"[dim]Pipeline paralelo (planner+índice): ~{pt.overlap_saved_ms:.0f} ms menos vs serie estimada[/dim]"
                    )
                _r_dt = pt.index_ms + pt.apply_ms
            except Exception as e:
                logger.warning("parallel_preamble failed, sequential fallback: %s", e)
                self._trace_phase_end(
                    "parallel_preamble",
                    status=TRACE_STATUS_FAILED,
                    notes=str(e)[:500],
                )
                append_intake_timing_step(session, "parallel_preamble", (time.monotonic() - _t_pp) * 1000.0)
                _t_dp = time.monotonic()
                self._trace_phase_start("decision_planner")
                apply_decision_planner_to_session(
                    session, budget_remaining=self.budget_manager.remaining
                )
                self._trace_phase_end("decision_planner")
                append_intake_timing_step(session, "decision_planner", (time.monotonic() - _t_dp) * 1000.0)
                self._trace_phase_start("retrieval")
                _r_iso = trace_timestamp_iso()
                _r_t0 = time.monotonic()
                run_retrieval_for_session(Path(self.cwd), session, text)
                _r_dt = (time.monotonic() - _r_t0) * 1000.0
                self._trace_phase_end("retrieval")
                append_intake_timing_step(session, "retrieval", (time.monotonic() - _r_t0) * 1000.0)
            _tm = getattr(self, "_trace_mgr", None)
            if _tm and getattr(session, "retrieval_enabled", False):
                try:
                    ci = session.retrieval_cache_info or {}
                    _tm.record_tool_call(
                        ToolCallTrace(
                            tool_name="retrieval_index",
                            target="",
                            started_at=_r_iso,
                            ended_at=trace_timestamp_iso(),
                            duration_ms=_r_dt,
                            status=TRACE_STATUS_COMPLETED,
                            ok=True,
                            error_message="",
                            result_summary=f"rebuilt={ci.get('rebuilt')} incremental={ci.get('incremental')} parallel_preamble=1",
                        )
                    )
                except Exception:
                    pass
        elif not skip_intake_heavy:
            _t_dp = time.monotonic()
            self._trace_phase_start("decision_planner")
            apply_decision_planner_to_session(
                session, budget_remaining=self.budget_manager.remaining
            )
            self._trace_phase_end("decision_planner")
            append_intake_timing_step(session, "decision_planner", (time.monotonic() - _t_dp) * 1000.0)

            self._trace_phase_start("retrieval")
            _r_iso = trace_timestamp_iso()
            _r_t0 = time.monotonic()
            run_retrieval_for_session(Path(self.cwd), session, text)
            _r_dt = (time.monotonic() - _r_t0) * 1000.0
            self._trace_phase_end("retrieval")
            append_intake_timing_step(session, "retrieval", (time.monotonic() - _r_t0) * 1000.0)
            _tm = getattr(self, "_trace_mgr", None)
            if _tm and getattr(session, "retrieval_enabled", False):
                try:
                    ci = session.retrieval_cache_info or {}
                    _tm.record_tool_call(
                        ToolCallTrace(
                            tool_name="retrieval_index",
                            target="",
                            started_at=_r_iso,
                            ended_at=trace_timestamp_iso(),
                            duration_ms=_r_dt,
                            status=TRACE_STATUS_COMPLETED,
                            ok=True,
                            error_message="",
                            result_summary=f"rebuilt={ci.get('rebuilt')} incremental={ci.get('incremental')}",
                        )
                    )
                except Exception:
                    pass

        _t_ea = time.monotonic()
        self._trace_phase_start("execution_agent")
        if skip_intake_heavy:
            self._trace_phase_end("execution_agent", notes="skipped_micro_task")
        else:
            run_execution_agent_for_session(session)
            self._trace_phase_end("execution_agent")
        append_intake_timing_step(session, "execution_agent", (time.monotonic() - _t_ea) * 1000.0)

        it_ms = getattr(session, "intake_timing_ms", None) or {}
        if isinstance(it_ms, dict):
            pp = float(it_ms.get("parallel_preamble", 0) or 0)
            session.intake_planning_wall_ms = round(
                pp
                if pp > 0
                else float(it_ms.get("decision_planner", 0) or 0) + float(it_ms.get("retrieval", 0) or 0),
                2,
            )
        try:
            logger.info(
                "runtime_intake_timing_ms planning_wall_ms=%s %s",
                getattr(session, "intake_planning_wall_ms", 0),
                json.dumps(getattr(session, "intake_timing_ms", {}) or {}, ensure_ascii=False),
            )
        except Exception:
            pass

        apply_slash_execute_write_overrides(session, intent)
        seal_task_contract_after_intake(session)
        apply_iteration_budget_to_session(session, task_text=(text or "").strip(), intent=intent)
        self._mark_fast_path_intake_state(session)

    def _effective_iteration_cap(self) -> int:
        """Main loop, ACT nudges, and bounded-task pressure use this (never above GHOST_MAX_ITERATIONS)."""
        g = global_iteration_cap()
        v = getattr(self, "_session_effective_iteration_cap", None)
        if isinstance(v, int) and v > 0:
            return max(4, min(v, g))
        return g

    def _maybe_inject_small_scope_planning_discipline_nudge(self) -> None:
        """
        After several text-only EXPLORE turns that look like extended planning, nudge one-shot:
        prefer one discovery tool then answer (small-scope sessions only).
        """
        if not explore_planning_governor_enabled():
            return
        sess = self.artifact_manager.current_session
        if not sess or not session_is_small_explore_scope(sess):
            return
        if getattr(self, "_small_scope_planning_nudge_sent", False):
            return
        thr = small_scope_planning_text_threshold()
        if self._explore_text_only_streak < thr:
            return
        last = getattr(self, "_last_explore_assistant_text", "") or ""
        if not assistant_text_suggests_protracted_planning(last):
            return
        self._small_scope_planning_nudge_sent = True
        msg = (
            "[SYSTEM] Small-scope task: avoid multi-turn exploration plans. "
            "Use at most one `ls` or `read_file` if you still lack facts, then give the direct final answer "
            "using tool output already in this thread. Pattern: brief plan → one tool if needed → answer."
        )
        self.history.append({"role": "user", "content": msg})
        self.memory.add_message(self.session_id, "user", msg)
        sess.events.append({"event": "explore_small_scope_planning_nudge", "streak": self._explore_text_only_streak})
        logger.info("explore_small_scope_planning_nudge streak=%s", self._explore_text_only_streak)

    def _apply_micro_task_detection(self, session: Any, task_text: str) -> None:
        """Mark trivial bounded read-only tasks right after contract spec (before planner/retrieval)."""
        if not micro_task_mode_enabled():
            session.micro_task_kind = None
            session.task_mode = resolve_task_mode(micro_task_kind=None, mode_enabled=False)
            return
        explore_blocked = not self._explore_act_policy_allows()
        spec = self._session_contract_spec_dict()
        k = detect_micro_task_kind(
            task_text, explore_act_blocked=explore_blocked, contract_spec=spec
        )
        session.micro_task_kind = k
        session.task_mode = resolve_task_mode(micro_task_kind=k, mode_enabled=True)
        if k:
            session.events.append(
                {"event": "micro_task_detected", "kind": k, "task_mode": session.task_mode}
            )
            logger.info("task_mode=%s micro_task_kind=%s", session.task_mode, k)
        else:
            logger.info("task_mode=%s (micro-task mode on, no shallow/factual match)", session.task_mode)

    def _try_evidence_sufficient_immediate_synthesis(self, dec: Any, _executed_names: List[str]) -> bool:
        """
        After tools, one no-tools model turn when evidence sufficiency + env allow it;
        return True if assistant produced non-empty text (caller then promotes EXPLORE → CLOSING).
        """
        sess = self.artifact_manager.current_session
        if not sess or not should_run_immediate_synthesis_after_sufficiency(sess):
            return False
        if self._final_synthesis_done:
            return False
        mtk = getattr(sess, "micro_task_kind", None)
        if mtk:
            nudge = (
                "[SYSTEM] Micro-task: tool output already answers the user. "
                "Reply with the final answer only (match the user's format). Do not call tools."
            )
            dim_line = "\n[dim]Micro-task: síntesis inmediata (sin más herramientas)[/dim]"
        else:
            nudge = (
                "[SYSTEM] You already have enough evidence from tools for this bounded question. "
                "Reply with the final answer only (match the user's format). Do not call tools."
            )
            dim_line = "\n[dim]Evidencia suficiente: cierre en un turno (sin herramientas)[/dim]"
        self.history.append({"role": "user", "content": nudge})
        self.memory.add_message(self.session_id, "user", nudge)
        self.console.print(dim_line)
        self._final_synthesis_done = True
        try:
            with self.renderer.session_status(
                self.session_phase.value,
                initial="thinking",
                rail_profile=self._live_rail_profile(),
            ) as status:
                msg = self._stream_completion(status, show_turn_brand=False, tools=[])
        except Exception as e:
            logger.warning("evidence_sufficient immediate synthesis failed: %s", e)
            self._final_synthesis_done = False
            return False
        ok_text = bool(msg and str(msg.get("content") or "").strip())
        sess.events.append(
            {
                "event": "evidence_sufficient_immediate_synthesis",
                "trigger": "micro_task" if mtk else "bounded_evidence",
                "kind": dec.kind,
                "why": dec.evidence_reason,
                "had_tool_calls": bool(msg.get("tool_calls")) if msg else False,
                "content_len": len(str((msg or {}).get("content") or "").strip()),
                "ok_text": ok_text,
            }
        )
        return ok_text

    def _phase_intake(self, session: Any, text: str, intent: Intent, is_resumed: bool) -> None:
        """INTAKE: build runtime TaskContract on the artifact session, then move to EXPLORE."""
        logger.info("phase INTAKE: building task contract")
        self._apply_session_phase(SessionPhase.INTAKE, detail="intake start", sync_task=not is_resumed)
        self._trace_mgr.set_task_text(text)
        session.plan = intent.task or text
        from apps.cli.runtime.workflow_continuity import (
            ContinuityLoadResult,
            _clean_identifier,
            build_continuity_system_block,
            is_continuation_user_message,
            load_continuity_for_task,
            load_latest_handoff_any,
            strip_yaml_frontmatter,
        )

        if is_continuation_user_message(text):
            tid = _clean_identifier(getattr(session, "task_id", None))
            handoff: Dict[str, Any] = {}
            if tid:
                ctx = load_continuity_for_task(self.cwd, tid)
                handoff = dict(ctx.handoff) if ctx.handoff else {}
                if not handoff:
                    ho2, _ = load_latest_handoff_any(self.cwd)
                    if ho2:
                        handoff = dict(ho2)
            else:
                ho_fb, _ = load_latest_handoff_any(self.cwd)
                handoff = dict(ho_fb) if ho_fb else {}
                tid_resolved = _clean_identifier(handoff.get("task_id")) if handoff else ""
                if tid_resolved:
                    ctx = load_continuity_for_task(self.cwd, tid_resolved)
                    if ctx.handoff:
                        handoff = dict(ctx.handoff)
                else:
                    ctx = ContinuityLoadResult(
                        plan_path="",
                        plan_body="",
                        handoff={},
                        handoff_warnings=[],
                        handoff_source="",
                    )
            plan_excerpt = ""
            if ctx.plan_body.strip():
                plan_excerpt = strip_yaml_frontmatter(ctx.plan_body)
            elif handoff.get("plan_excerpt"):
                plan_excerpt = str(handoff.get("plan_excerpt") or "").strip()
            base_plan = (session.plan or text or "").strip()
            if plan_excerpt:
                session.plan = f"{plan_excerpt}\n\n---\nContinuación del operador: {base_plan}"
            session.continuity_injected_block = build_continuity_system_block(
                plan_excerpt or (session.plan or "")[:4000],
                handoff,
            )
            session.continuity_loaded_handoff = handoff
            try:
                session.events.append(
                    {
                        "event": "continuity_loaded",
                        "had_persisted_plan_file": bool(ctx.plan_path),
                        "handoff_task_id": handoff.get("task_id"),
                    }
                )
            except Exception:
                pass
        self._run_task_contract_intake(session, text, intent)
        session.explore_class_hint = classify_explore_task(session.plan or text, session)
        _sess_ts = self.artifact_manager.current_session
        self.hook_manager.trigger(
            HookEvents.TASK_START,
            task=text,
            intent=asdict(intent),
            task_mode=getattr(_sess_ts, "task_mode", "standard") if _sess_ts else "standard",
            micro_task_kind=getattr(_sess_ts, "micro_task_kind", None) if _sess_ts else None,
        )
        logger.info("phase transition: INTAKE -> EXPLORE (contract ready)")
        self._apply_session_phase(SessionPhase.EXPLORE, detail="post-intake", sync_task=True)

    def _bump_discovery_from_tools(self, tool_names: List[str]) -> None:
        for name in tool_names:
            if name in DISCOVERY_ACTIONS:
                self._discovery_action_count += 1

    def _effective_prompt_mode(self) -> str:
        """Slash del turno (`/do`, `/plan`, …) prima sobre el modo del constructor (REPL)."""
        sess = self.artifact_manager.current_session if self.artifact_manager else None
        if sess:
            tc = getattr(sess, "task_contract", None)
            if isinstance(tc, dict):
                id_rec = tc.get("intent")
                if isinstance(id_rec, dict) and id_rec.get("is_slash_command"):
                    m = str(id_rec.get("mode") or "").strip()
                    if m:
                        return m
        return (self.mode or "Chat").strip() or "Chat"

    def _live_rail_profile(self) -> str:
        """Perfil del rail vivo del spinner: /plan → solo lectura (3 pasos), resto → implementación."""
        from apps.cli.ui import ui_contract as _uic

        if (self._effective_prompt_mode() or "").strip().lower() == "plan":
            return _uic.LIVE_RAIL_PROFILE_READONLY
        return _uic.LIVE_RAIL_PROFILE_IMPLEMENT

    def _slash_execute_routing_active(self) -> bool:
        sess = self.artifact_manager.current_session if self.artifact_manager else None
        if not sess:
            return False
        tc = getattr(sess, "task_contract", None)
        if not isinstance(tc, dict):
            return False
        id_rec = tc.get("intent")
        if not isinstance(id_rec, dict) or not id_rec.get("is_slash_command"):
            return False
        return str(id_rec.get("mode") or "").strip() in ("Execute", "Patch", "Fix")

    def _inject_readonly_truthfulness(self, session: Any) -> bool:
        if self._slash_execute_routing_active():
            return False
        spec_rd = self._session_contract_spec_dict() or {}
        intent_rd = str(spec_rd.get("intent") or getattr(session, "task_intent", "") or "").strip().lower()
        ce_rd = str(spec_rd.get("change_expectation") or getattr(session, "change_expectation", "") or "").strip().lower()
        mode_l = (self._effective_prompt_mode() or "").strip().lower()
        return bool(intent_rd in ("analysis", "review") or ce_rd == "should_not_write" or mode_l == "plan")

    @staticmethod
    def _bundled_actions_fingerprint(actions: List[Dict[str, Any]]) -> str:
        if not actions:
            return ""
        parts: List[str] = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            nm = str(a.get("name") or "")
            ar = a.get("args")
            if not isinstance(ar, dict):
                ar = {}
            try:
                blob = json.dumps(ar, sort_keys=True, ensure_ascii=False)
            except (TypeError, ValueError):
                blob = str(ar)
            parts.append(f"{nm}|{blob}")
        base = "\n".join(sorted(parts))
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]

    def _explore_act_policy_allows(self) -> bool:
        if (self._effective_prompt_mode() or "").strip().lower() == "plan":
            return False
        sess = self.artifact_manager.current_session
        if sess and (getattr(sess, "change_expectation", "") or "").strip().lower() == "should_not_write":
            return False
        return True

    def _explore_promotion_target_paths(self) -> List[str]:
        """Small set of concrete write targets that justify an early EXPLORE -> ACT handoff."""
        sess = self.artifact_manager.current_session
        spec = self._session_contract_spec_dict() or {}
        spec_candidates: List[str] = []
        if isinstance(spec, dict):
            for p in spec.get("target_files") or []:
                if isinstance(p, str) and p.strip():
                    spec_candidates.append(p)
        candidates: List[str] = list(spec_candidates)
        if not (1 <= len(spec_candidates) <= 2) and sess:
            for p in getattr(sess, "selected_targets", []) or []:
                if isinstance(p, str) and p.strip():
                    candidates.append(p)
        out: List[str] = []
        seen: set[str] = set()
        for raw in candidates:
            composed = PathComposer.compose(self.cwd, str(raw))
            norm = os.path.normcase(os.path.normpath(composed))
            if not norm or norm in seen:
                continue
            seen.add(norm)
            out.append(norm)
            if len(out) >= 6:
                break
        return out

    def _count_relevant_read_hits_for_promotion(self, target_paths: List[str]) -> int:
        if not target_paths:
            return 0
        seen_reads = set(getattr(self, "_explore_seen_read_paths", set()) or set())
        return sum(1 for p in target_paths if p in seen_reads)

    def _max_target_read_count_for_promotion(self, target_paths: List[str]) -> int:
        if not target_paths:
            return 0
        read_counts = getattr(self, "_explore_read_path_counts", {}) or {}
        return max(int(read_counts.get(p, 0) or 0) for p in target_paths)

    def _build_exploration_metrics_for_promotion(self) -> ExplorationMetrics:
        sess = self.artifact_manager.current_session
        spec = self._session_contract_spec_dict() or {}
        target_paths = self._explore_promotion_target_paths()
        target_file_count = len(target_paths)
        relevant_read_hits = self._count_relevant_read_hits_for_promotion(target_paths)
        change_expectation = ""
        if sess:
            change_expectation = str(getattr(sess, "change_expectation", "") or "")
        elif isinstance(spec, dict):
            change_expectation = str(spec.get("change_expectation") or "")
        ib = getattr(sess, "iteration_budget", None) or {}
        ib_category = str(ib.get("category") or "") if isinstance(ib, dict) else ""
        focused_write_task = (
            self._explore_act_policy_allows()
            and change_expectation.strip().lower() in ("must_write", "may_write")
            and 1 <= target_file_count <= 2
        )
        simple_write_task = ib_category == "simple_write" or (
            focused_write_task and len(self._primary_user_task_text()) <= 380
        )
        return ExplorationMetrics(
            discovery_action_count=int(getattr(self, "_discovery_action_count", 0)),
            discovery_cap=DISCOVERY_CAP,
            budget_remaining=int(self.budget_manager.remaining),
            budget_exhausted=bool(self.budget_manager.is_exhausted()),
            explore_text_only_streak=int(getattr(self, "_explore_text_only_streak", 0)),
            unique_read_paths=len(getattr(self, "_explore_seen_read_paths", set())),
            target_file_count=target_file_count,
            relevant_read_hits=relevant_read_hits,
            max_target_read_count=self._max_target_read_count_for_promotion(target_paths),
            focused_write_task=focused_write_task,
            simple_write_task=simple_write_task,
            policy_allows_act=self._explore_act_policy_allows(),
            min_unique_reads_for_context=int(os.getenv("GHOST_EXPLORE_MIN_UNIQUE_READS", "3")),
            max_text_only_streak=int(os.getenv("GHOST_EXPLORE_MAX_TEXT_ONLY_STREAK", "2")),
            min_discovery_for_context=int(os.getenv("GHOST_EXPLORE_MIN_DISCOVERY_FOR_CONTEXT", "2")),
        )

    def _log_explore_promotion(self, reason_code: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"event": "explore_promote_act", "reason_code": reason_code}
        if extra:
            payload.update(extra)
        logger.info("explore_promotion %s", json.dumps(payload, ensure_ascii=False, default=str))
        sess = self.artifact_manager.current_session
        if sess:
            sess.events.append(dict(payload))

    def _record_phase_promotion(
        self,
        kind: str,
        reason_code: str,
        from_phase: SessionPhase,
        to_phase: SessionPhase,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        sess = self.artifact_manager.current_session
        if not sess:
            return
        sess.append_promotion_decision(
            kind=kind,
            reason_code=reason_code,
            from_phase=from_phase.value,
            to_phase=to_phase.value,
            iteration=getattr(self, "_session_loop_iteration", None),
            extra=extra,
        )

    def _promote_explore_to_act(self, reason_code: str, extra: Optional[Dict[str, Any]] = None) -> None:
        sess = self.artifact_manager.current_session
        if sess and getattr(sess, "fast_path_eligible", False):
            if reason_code in (
                REASON_FOCUSED_WRITE_CONTEXT_READY,
                REASON_REPEATED_TARGET_READ,
                REASON_SIMPLE_WRITE_MIN_CONTEXT,
            ):
                sess.fast_path_used = True
            elif not getattr(sess, "fallback_reason", ""):
                sess.fallback_reason = f"fast_path_fallback:{reason_code}"
        self.stagnation_detector.reset()
        self._log_explore_promotion(reason_code, extra)
        logger.info("phase transition: EXPLORE -> ACT (explore_promote:%s)", reason_code)
        self._record_phase_promotion(
            "explore_to_act",
            reason_code,
            SessionPhase.EXPLORE,
            SessionPhase.ACT,
            extra=extra,
        )
        self._apply_session_phase(
            SessionPhase.ACT,
            detail=f"explore_promote:{reason_code}",
            sync_task=True,
        )

    def _primary_user_task_text(self) -> str:
        """First substantive user message (slash payload or chat), ignoring system nudges."""
        t = (getattr(self.current_intent, "task", None) or "").strip()
        if t:
            return t
        for m in self.history:
            if m.get("role") != "user":
                continue
            c = str(m.get("content") or "")
            if c.startswith("[SYSTEM]"):
                continue
            c = c.strip()
            if c:
                return c
        return ""

    def _promote_explore_to_closing(
        self,
        reason_code: str,
        extra: Optional[Dict[str, Any]] = None,
        *,
        synthesis_context: str = "readonly",
    ) -> None:
        """EXPLORE → CLOSING when read-only listing or narrow factual answer is ready (no ACT path)."""
        self.stagnation_detector.reset()
        self._answer_now_nudge_pending = False
        self._answer_now_synthesis_context = None
        if synthesis_context == "factual":
            event = "explore_to_closing_factual"
            kind = "explore_to_closing_factual"
            detail = f"explore_factual:{reason_code}"
        else:
            event = "explore_to_closing_readonly"
            kind = "explore_to_closing_readonly"
            detail = f"explore_readonly:{reason_code}"
        payload: Dict[str, Any] = {"event": event, "reason_code": reason_code, "context": synthesis_context}
        if extra:
            payload.update(extra)
        logger.info("explore_to_closing %s", json.dumps(payload, ensure_ascii=False, default=str))
        sess = self.artifact_manager.current_session
        if sess:
            sess.events.append(dict(payload))
        self._record_phase_promotion(
            kind,
            reason_code,
            SessionPhase.EXPLORE,
            SessionPhase.CLOSING,
            extra=extra,
        )
        self._apply_session_phase(SessionPhase.CLOSING, detail=detail, sync_task=True)

    def _promote_explore_to_closing_readonly(self, reason_code: str, extra: Optional[Dict[str, Any]] = None) -> None:
        self._promote_explore_to_closing(reason_code, extra, synthesis_context="readonly")

    def _detect_already_implemented_fast_path(self) -> Optional[Dict[str, Any]]:
        sess = self.artifact_manager.current_session
        if not sess or self._session_has_diff():
            return None
        spec = self._session_contract_spec_dict() or {}
        change_expectation = str(spec.get("change_expectation") or "").strip().lower()
        if change_expectation not in ("must_write", "may_write"):
            return None
        raw_targets = spec.get("target_files") or []
        target_files = [str(p).replace("\\", "/").strip() for p in raw_targets if isinstance(p, str) and p.strip()]
        if not (1 <= len(target_files) <= 2):
            return None

        probe_verification = {"status": "skipped", "checks": [], "steps_executed_count": 0}
        outcome = determine_task_outcome(sess, self.history, probe_verification)
        if outcome.outcome != OUTCOME_ALREADY_IMPLEMENTED:
            return None

        repo_v2 = self._session_repo_v2_dict() or {}
        repo_vc = repo_v2.get("verification_commands") if isinstance(repo_v2, dict) else None
        manager = VerificationManager(self.cwd, repo_verification_commands=repo_vc if isinstance(repo_vc, dict) else None)
        checks = manager.get_applicable_checks(files_changed=[{"file": path} for path in target_files])
        targeted_tests = [
            str(check.get("command") or "")
            for check in checks
            if str(check.get("kind") or "") == "tests"
            and "targeted" in str(check.get("name") or "").lower()
            and str(check.get("command") or "").strip()
        ]
        if not targeted_tests:
            return None
        return {
            "summary": str(outcome.summary or ""),
            "evidence_lines": list(outcome.evidence_lines or []),
            "target_files": target_files,
            "targeted_tests": targeted_tests,
            "next_action": str(outcome.recommended_next_action or ""),
        }

    def _queue_already_implemented_completion(self, signal: Dict[str, Any]) -> None:
        self._already_implemented_answer_pending = dict(signal)
        tests_summary = "; ".join(str(x) for x in (signal.get("targeted_tests") or [])[:2])
        nudge = (
            "[SYSTEM] The requested change is already implemented in the focused target and nearby tests already cover it. "
            "Reply with the final answer only: say that no code changes are needed, cite the target file and nearby test evidence briefly, "
            "and do not call more tools."
        )
        if tests_summary:
            nudge += f" Nearby test command: {tests_summary}."
        self.history.append({"role": "user", "content": nudge})
        self.memory.add_message(self.session_id, "user", nudge)
        sess = self.artifact_manager.current_session
        if sess:
            sess.events.append(
                {
                    "event": "already_implemented_fast_path",
                    "target_files": list(signal.get("target_files") or []),
                    "targeted_tests": list(signal.get("targeted_tests") or []),
                }
            )

    def _assistant_text_claims_already_implemented(self, content: Any) -> bool:
        text = str(content or "").strip().lower()
        if not text:
            return False
        needles = (
            "already implemented",
            "already exists",
            "no changes are needed",
            "no changes needed",
            "ya está implementado",
            "ya esta implementado",
            "ya existe",
            "no se requieren cambios",
            "no requiere cambios",
        )
        return any(needle in text for needle in needles)

    def _explore_tools_for_turn(self, iterations: int) -> List[Dict[str, Any]]:
        """EXPLORE tool defs; may drop tools on last iteration for bounded answer-now tasks."""
        if getattr(self, "_already_implemented_answer_pending", None):
            return []
        defs = self._tool_defs_for_session_phase()
        if (
            explore_v2_enabled()
            and symbol_omit_ls_first_turn_enabled()
            and iterations == 1
            and self.session_phase == SessionPhase.EXPLORE
            and self._resolve_explore_task_class() == EXPLORE_CLASS_SYMBOL_LOOKUP
        ):
            defs = [t for t in defs if t.get("name") != "ls"]
        if not answer_now_policy_enabled():
            return defs
        if not bounded_code_task_kind(self._primary_user_task_text()):
            return defs
        max_iter = self._effective_iteration_cap()
        rem = factual_explore_iterations_remaining(iterations, max_iter)
        if (
            rem == 0
            and suppress_tools_on_last_factual_iteration()
            and int(getattr(self, "_discovery_action_count", 0)) >= 1
        ):
            return []
        return defs

    def _maybe_inject_answer_now_pressure_nudge(self, iterations: int) -> None:
        """Reserve end of iteration budget for synthesis on bounded code questions (RO-2 / RO-3)."""
        if not answer_now_policy_enabled():
            return
        sess_mt = self.artifact_manager.current_session
        mtk = getattr(sess_mt, "micro_task_kind", None) if sess_mt else None
        if (
            mtk
            and micro_task_mode_enabled()
            and iterations <= micro_task_early_pressure_max_iter()
            and not getattr(self, "_micro_task_early_pressure_injected", False)
        ):
            self._micro_task_early_pressure_injected = True
            msg = (
                "[SYSTEM] Micro-task: use at most 1–2 discovery tools (`ls` / `read_file`), "
                "then give the final answer. Do not draft a long multi-step exploration plan."
            )
            self.history.append({"role": "user", "content": msg})
            self.memory.add_message(self.session_id, "user", msg)
            return
        if not bounded_code_task_kind(self._primary_user_task_text()):
            return
        max_iter = self._effective_iteration_cap()
        reserve = adapt_synthesis_reserve_iterations(max_iter, synthesis_reserve_iterations())
        rem = factual_explore_iterations_remaining(iterations, max_iter)
        if rem > reserve:
            return
        key = (iterations, rem)
        if getattr(self, "_answer_now_pressure_injected_key", None) == key:
            return
        self._answer_now_pressure_injected_key = key
        if rem == 0:
            msg = (
                f"[SYSTEM] Final iteration ({iterations}/{max_iter}). Produce the complete concise answer "
                "from tool results already in this conversation. No tools."
            )
        else:
            msg = (
                f"[SYSTEM] Bounded code question: only {rem} iteration(s) left including this one "
                f"({iterations}/{max_iter}). Synthesize from existing tool output; "
                "avoid new tools unless strictly necessary."
            )
        self.history.append({"role": "user", "content": msg})
        self.memory.add_message(self.session_id, "user", msg)

    def _handle_answer_now_after_tools(self, executed_names: List[str]) -> None:
        """When evidence satisfies a tiny scoped task, nudge final answer or close on redundant tools."""
        if not executed_names:
            return
        user_task = self._primary_user_task_text()
        spec = self._session_contract_spec_dict()
        dec, suff = decide_answer_now_after_tools_with_details(
            task_text=user_task,
            history=self.history,
            contract_spec=spec,
            discovery_action_count=int(getattr(self, "_discovery_action_count", 0)),
            explore_act_blocked=not self._explore_act_policy_allows(),
        )
        if dec is None:
            return
        ctx = dec.synthesis_context
        if ctx not in ("readonly", "factual"):
            ctx = "readonly"
        sess = self.artifact_manager.current_session
        if sess:
            ev_payload: Dict[str, Any] = {
                "event": "evidence_sufficiency_eval",
                "sufficient": suff.sufficient,
                "reason": suff.reason,
                "confidence": suff.confidence,
                "task_family": suff.task_family,
                "synthesis_context": suff.synthesis_context,
                "unlikely_more_exploration_helps": suff.unlikely_more_exploration_helps,
            }
            sess.events.append(ev_payload)
            logger.info(
                "evidence_sufficiency_eval %s",
                json.dumps(ev_payload, ensure_ascii=False, default=str),
            )
        if sess and should_run_immediate_synthesis_after_sufficiency(sess):
            if self._try_evidence_sufficient_immediate_synthesis(dec, executed_names):
                self._answer_now_nudge_pending = False
                self._answer_now_synthesis_context = None
                self._promote_explore_to_closing(
                    "evidence_sufficient_immediate_synthesis",
                    extra={
                        "kind": dec.kind,
                        "why": dec.evidence_reason,
                        "executed": list(executed_names)[:12],
                    },
                    synthesis_context=ctx,
                )
                return
        if self._answer_now_nudge_pending:
            self._promote_explore_to_closing(
                f"answer_now_duplicate_tools_{dec.kind}",
                extra={"why": dec.evidence_reason, "executed": list(executed_names)[:12]},
                synthesis_context=ctx,
            )
            return
        self._answer_now_nudge_pending = True
        self._answer_now_synthesis_context = ctx
        payload = {
            "event": "explore_answer_now_nudge",
            "kind": dec.kind,
            "why": dec.evidence_reason,
            "synthesis_context": ctx,
        }
        logger.info("explore_answer_now_nudge %s", json.dumps(payload, ensure_ascii=False, default=str))
        if sess:
            sess.events.append(dict(payload))
        if dec.kind == "shallow_list":
            nudge = (
                "[SYSTEM] The last tool output already satisfies this narrow listing request. "
                "Reply with the final answer only (match the user's format, e.g. one file name per line). "
                "Do not call more tools."
            )
        elif dec.kind == "repo_overview":
            nudge = (
                "[SYSTEM] Tool output already contains the repo overview you need. "
                "Reply with the final concise answer: stack, main modules, and structure requested by the user. "
                "No more tools."
            )
        else:
            nudge = (
                "[SYSTEM] Tool output already contains what you need for this narrow factual question. "
                "Reply with your final concise answer (what it does, file path if asked). No more tools."
            )
        self.history.append({"role": "user", "content": nudge})
        self.memory.add_message(self.session_id, "user", nudge)

    def _resolve_verify_skip_flags(self, task_obj: Any) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """Returns (skip_exec, skip_reason, contract_spec dict from task_contract.spec)."""
        tsd = self._session_contract_spec_dict()
        skip_stream = getattr(self, "_stream_interrupted_this_session", False) and not getattr(
            self, "_verification_completed_this_session", False
        )
        has_run_shell = any(m.get("name") == "run_shell" for m in self.history if m.get("role") == "tool")
        skip_manual_only = not has_run_shell
        skip_manual_only = verification_skip_respects_contract_spec(tsd, skip_manual_only)
        skip_exec = skip_stream or skip_manual_only
        skip_reason = "manual_only" if skip_manual_only else ("stream_interrupted" if skip_stream else None)
        return skip_exec, skip_reason, tsd

    def _stagnation_should_halt(self, status) -> bool:
        is_stagnant, reason = self.stagnation_detector.check_stagnation()
        if not is_stagnant:
            return False
        if status:
            status.stop()
        self._loop_abort_reason = LOOP_ABORT_STAGNATION
        checkpoint = self._create_autonomy_checkpoint("stagnation_detected", reason)
        self.renderer.render_autonomy_checkpoint(checkpoint)
        return True

    def _chat_loop(self, task_obj: Any, text: str, intent: Intent, is_resumed: bool):
        """
        Explicit phase dispatcher (state machine).

        INTAKE → EXPLORE ⇄ (text-only) → ACT → (diff) → VERIFY → DONE
                                        ↑              ↘ fail+repair cap → REPAIR → ACT → VERIFY …
                                        └ no diff, text-only → DONE
        Terminal: DONE | ABORTED (verify exhausted, policy, provider, stagnation, max iter, budget).

        Extension / invariants: docs/RUNTIME_EXTENSION_BOUNDARIES.md ; tests/test_runtime_architecture_invariants.py
        """
        session = self.artifact_manager.current_session
        self.budget_manager.reset_session_budget_state()
        self._last_tool_denial_key = None
        self._tools_executed_this_session = False
        self._edit_miss_nudge_count = 0
        self._conclusion_nudge_injected = False
        self._small_scope_planning_nudge_sent = False
        self._last_explore_assistant_text = ""

        self._phase_intake(session, text, intent, is_resumed)

        if intent.is_slash_command and self._handle_intent(intent):
            return

        self.history.append({"role": "user", "content": text})
        self.memory.add_message(self.session_id, "user", text)

        iterations = 0
        gcap = global_iteration_cap()
        sess_loop = self.artifact_manager.current_session
        ib = getattr(sess_loop, "iteration_budget", None) if sess_loop else None
        if isinstance(ib, dict) and ib.get("effective_max") is not None:
            try:
                max_iter_loop = max(4, min(int(ib["effective_max"]), gcap))
            except (TypeError, ValueError):
                max_iter_loop = gcap
        else:
            max_iter_loop = gcap
        self._session_effective_iteration_cap = max_iter_loop
        logger.info(
            "ghost iteration cap effective=%s global=%s category=%s",
            max_iter_loop,
            gcap,
            (ib or {}).get("category", "") if isinstance(ib, dict) else "",
        )

        while (
            self.session_phase not in LOOP_HALT_PHASES
            and not self.budget_manager.is_exhausted()
            and iterations < max_iter_loop
        ):
            iterations += 1
            self._session_loop_iteration = iterations
            logger.info("ghost phase dispatch iter=%s phase=%s", iterations, self.session_phase.value)

            if self.session_phase == SessionPhase.EXPLORE:
                br = self._phase_explore(task_obj, iterations)
                if br == "halt":
                    break
                continue
            if self.session_phase == SessionPhase.ACT:
                br = self._phase_act(task_obj, iterations)
                if br == "halt":
                    break
                continue
            if self.session_phase == SessionPhase.VERIFY:
                self._phase_verify(task_obj, f"iter_{iterations}")
                continue
            if self.session_phase == SessionPhase.REPAIR:
                self._phase_repair(task_obj)
                continue
            break

        if iterations >= max_iter_loop and self.session_phase not in LOOP_HALT_PHASES:
            logger.warning("phase loop: max iterations (%s) — closing for finalize", max_iter_loop)
            self._loop_abort_reason = LOOP_ABORT_MAX_ITERATIONS
            self._apply_session_phase(SessionPhase.CLOSING, detail="max_iterations", sync_task=True)

        if self.budget_manager.is_exhausted() and self.session_phase not in LOOP_HALT_PHASES:
            checkpoint = self._create_autonomy_checkpoint("budget_exhausted", "El presupuesto de autonomía se ha agotado.")
            self.renderer.render_autonomy_checkpoint(checkpoint)

        if session:
            set_total_loop_iterations(session, iterations)

        # --- legacy tail: post-loop (provider / terminal / finalize) ---
        self._chat_loop_postamble(task_obj)

    def _maybe_max_iteration_readonly_synthesis(self, _task_obj: Any) -> None:
        """
        After hitting GHOST_MAX_ITERATIONS on read-only / bounded-factual style tasks, one no-tools
        model turn to surface an answer from existing tool output (operator UX).
        """
        v = os.getenv("GHOST_MAX_ITER_READONLY_SYNTHESIS", "1").strip().lower()
        if v in ("0", "false", "no", "off"):
            return
        if getattr(self, "_loop_abort_reason", None) != LOOP_ABORT_MAX_ITERATIONS:
            return
        if _has_final_nl_response(self.history):
            return
        if self._final_synthesis_done:
            return
        if not getattr(self, "_tools_executed_this_session", False):
            return
        sess = self.artifact_manager.current_session
        if not sess:
            return
        task_text = self._primary_user_task_text()
        intent = str(getattr(sess, "task_intent", "") or "")
        if bounded_code_task_kind(task_text) is None and intent not in (
            "analysis",
            "review",
            "ask",
            "research",
        ):
            return
        nudge = (
            "[SYSTEM] Iteration budget (GHOST_MAX_ITERATIONS) was reached. "
            "Write the best final answer using only tool output already in this conversation. "
            "Plain text only — do not call tools."
        )
        self.history.append({"role": "user", "content": nudge})
        self.memory.add_message(self.session_id, "user", nudge)
        self.console.print(
            "\n[dim]Límite de iteraciones: generando síntesis final sin herramientas a partir del historial…[/dim]"
        )
        try:
            with self.renderer.session_status(
                SessionPhase.CLOSING.value,
                initial="closing",
                rail_profile=self._live_rail_profile(),
            ) as status:
                msg = self._stream_completion(status, show_turn_brand=False, tools=[])
            if msg and getattr(sess, "events", None) is not None:
                sess.events.append(
                    {
                        "event": "iteration_limit_closing_synthesis",
                        "had_tool_calls": bool(msg.get("tool_calls")),
                        "content_len": len(str(msg.get("content") or "").strip()),
                    }
                )
        except Exception as e:
            logger.warning("iteration limit closing synthesis failed: %s", e)

    def _chat_loop_postamble(self, task_obj: Any) -> None:
        """Verification (if needed) + single resolve_terminal_result + persist (batch hooks)."""
        policy_status: PolicyTerminalStatus = "ok"
        if any(
            msg.get("role") == "user" and "Terminal stop triggered" in msg.get("content", "")
            for msg in self.history[-3:]
        ):
            self._loop_abort_reason = LOOP_ABORT_POLICY
            self._session_failed = True
            policy_status = "terminal_stop"

        provider_status: ProviderTerminalStatus = "ok"
        if getattr(self, "_fatal_provider_error", False):
            self._loop_abort_reason = LOOP_ABORT_PROVIDER
            self._session_failed = True
            provider_status = "fatal_error"

        if not self.artifact_manager.current_session:
            return

        sess = self.artifact_manager.current_session
        hard_abort = policy_status != "ok" or provider_status != "ok"

        if not hard_abort:
            self._maybe_max_iteration_readonly_synthesis(task_obj)

        if self.budget_manager.is_exhausted():
            last_fail = getattr(sess, "last_failed_check", "") or ""
            repair_sum = getattr(sess, "repair_summary", "") or ""
            diff = getattr(sess, "diff_summary", []) or []
            unique_files = list(dict.fromkeys(d.get("file", "") for d in diff))[:3]
            parts = ["Budget exhausted before task completion."]
            if last_fail:
                parts.append(f"Last failed check: {last_fail}.")
            if repair_sum and getattr(sess, "repair_attempt_count", 0) > 0:
                sess.reverification_pending = True
                sess.last_repair_applied = repair_sum
                if unique_files:
                    parts.append(
                        f"Repair was applied to {', '.join(unique_files)}, but final verification could not complete."
                    )
                else:
                    parts.append("Repair was attempted, but final verification could not complete.")
            sess.budget_exhausted_reason = " ".join(parts)

        made_changes = bool(sess.diff_summary)
        tools_executed = getattr(self, "_tools_executed_this_session", False)
        has_final_nl_response = self._has_final_nl_response_from_history()
        is_no_op = not made_changes and not tools_executed and not has_final_nl_response

        if not hard_abort:
            if is_no_op:
                self._session_failed = True
                sess.verification = {"status": "skipped", "checks": []}
                self.console.print(
                    "\n[bold yellow]No execution or response.[/bold yellow] [dim]Task will be marked ABORTED in artifact. See details.[/dim]"
                )
            elif made_changes:
                if getattr(self, "_finalize_skip_integrity_re_verify", False):
                    pass
                else:
                    skip_exec, skip_reason, tsd_fin = self._resolve_verify_skip_flags(task_obj)
                    repo_v2_fin = self._session_repo_v2_dict()
                    prev_v = getattr(sess, "verification", {}) or {}
                    failed_names_fin = (
                        [
                            c.get("name")
                            for c in prev_v.get("checks", [])
                            if c.get("status") in ("failed", "error")
                        ]
                        if getattr(sess, "repair_attempt_count", 0) > 0 and prev_v
                        else None
                    )
                    tc_fin = get_task_contract(sess)
                    v_reused = self.verification_coordinator.try_reuse_stored_verification(
                        sess,
                        getattr(sess, "diff_summary", []) or [],
                        verification_completed=getattr(
                            self, "_verification_completed_this_session", False
                        ),
                    )
                    if v_reused is not None:
                        sess.verification = v_reused
                        sess.verification_justification = v_reused.get("justification", "")
                        sess.verification_scope = str(v_reused.get("verification_scope") or "")
                        sess.planned_check_cwds = dict(v_reused.get("planned_check_cwds") or {})
                        record_verification_metrics(sess, v_reused, reused=True)
                    else:
                        if not skip_exec:
                            check_names = self.verification_coordinator.planned_check_labels(
                                task_type=task_obj.task_type,
                                scope=getattr(sess, "task_scope", "") or "unknown",
                                files_changed=getattr(sess, "diff_summary", []) or [],
                                budget_remaining=getattr(self.budget_manager, "remaining", 100),
                                contract_spec=tsd_fin,
                                repo_v2=repo_v2_fin,
                                failed_check_names=failed_names_fin,
                            )
                            _pytest_ff = list(getattr(sess, "pytest_focus_targets", None) or [])
                            _vf_b = format_integrity_verify_preamble(
                                check_names, pytest_focus_n=len(_pytest_ff)
                            )
                            self.console.print(f"\n[dim]⚙ Running integrity verification: {_vf_b}[/dim]")
                            sess.events.append(
                                {
                                    "event": "verify_preamble",
                                    "text": _vf_b[:500],
                                    "context_label": "finalize",
                                }
                            )
                        self.hook_manager.trigger(HookEvents.PRE_VERIFICATION)
                        self._apply_session_phase(
                            SessionPhase.VERIFY,
                            detail="VerificationCoordinator (finalize)"
                            + ("; skip_exec" if skip_exec else ""),
                        )
                        self._trace_phase_start("verification")
                        _v0f = time.monotonic()
                        v_res = self.verification_coordinator.run(
                            diff_summary=getattr(sess, "diff_summary", []) or [],
                            task_contract=tc_fin,
                            task_type=task_obj.task_type,
                            scope=getattr(sess, "task_scope", "") or "unknown",
                            budget_remaining=getattr(self.budget_manager, "remaining", 100),
                            history=self.history,
                            blocked_commands=getattr(self, "_blocked_shell_commands", []),
                            skip_execution=skip_exec,
                            skip_reason=skip_reason,
                            repo_v2=repo_v2_fin,
                            verify_batch_approval_fn=self._verify_batch_approval_callback(),
                            session=sess,
                            reuse_if_verified=False,
                            failed_check_names=failed_names_fin,
                            pytest_focus_paths=list(getattr(sess, "pytest_focus_targets", None) or [])
                            or None,
                        )
                        _mgr_vf = getattr(self, "_trace_mgr", None)
                        if _mgr_vf:
                            try:
                                _mgr_vf.record_verification_trace(
                                    verification_trace_from_result(
                                        v_res, (time.monotonic() - _v0f) * 1000.0
                                    )
                                )
                            except Exception:
                                pass
                        self._trace_phase_end("verification")
                        self.hook_manager.trigger(HookEvents.POST_VERIFICATION, results=v_res)
                        self.renderer.render_verification_results(
                            v_res,
                            contract_spec=self._session_contract_spec_dict(),
                        )
                        sess.verification = v_res
                        sess.verification_justification = v_res.get("justification", "")
                        record_verification_metrics(sess, v_res, reused=False)
                        if str(v_res.get("status") or "") == "success":
                            sess.last_verify_failure_snapshot = {}
                            sess.pytest_focus_targets = []
                            sess.last_repair_causal_digest = ""
                            if not skip_exec:
                                sess.last_integrity_ok_write_epoch = int(getattr(sess, "write_epoch", 0) or 0)
                            _g = self.budget_manager.grant_extension_for_verified_progress(
                                steps_executed=int(v_res.get("steps_executed_count") or 0),
                            )
                            if _g:
                                logger.info("adaptive budget: +%s after finalize verify ok", _g)
                                append_budget_extension_record(
                                    sess,
                                    {
                                        "points": _g,
                                        "reason": "verify_executed_ok",
                                        "detail": "finalize",
                                        "skipped": "",
                                    },
                                )
                            self.stagnation_detector.reset_after_verified_progress()
            else:
                sess.verification = {"status": "skipped", "checks": []}
        else:
            ver = getattr(sess, "verification", None)
            if not isinstance(ver, dict) or not ver:
                sess.verification = {"status": "skipped", "checks": []}

        ver_final = sess.verification if isinstance(getattr(sess, "verification", None), dict) else {}

        self._trace_phase_start("conclusion")
        pre_resolve_loop_abort = getattr(self, "_loop_abort_reason", None)
        tr = resolve_terminal_result(
            sess,
            ver_final,
            history=self.history,
            budget_status="exhausted" if self.budget_manager.is_exhausted() else "ok",
            provider_status=provider_status,
            policy_status=policy_status,
            session_failed=getattr(self, "_session_failed", False),
            loop_abort_reason=pre_resolve_loop_abort,
        )
        if tr.phase == SessionPhase.ABORTED:
            set_final_aborted_reason(sess, tr.loop_abort_reason or "aborted")
        else:
            set_final_aborted_reason(sess, "")
        rec: Dict[str, Any] = {
            "ts": trace_timestamp_iso(),
            "terminal_phase": tr.phase.value,
            "outcome": tr.outcome,
            "outcome_confidence": tr.outcome_confidence,
            "evidence_score": tr.evidence_score,
            "loop_abort_reason": tr.loop_abort_reason,
            "root_cause_excerpt": (tr.root_cause or "")[:2000],
            "next_action_excerpt": (tr.next_action or "")[:1200],
            "evidence_lines": list(tr.evidence_lines[:24]),
            "task_confidence": {
                "level": tr.task_confidence_level,
                "completion_confidence": tr.task_completion_confidence,
                "closure_reason": tr.closure_reason_code,
                "closure_reason_summary_es": tr.closure_reason_summary_es,
                "closure_posture_es": tr.closure_posture_es,
                "signals": dict(tr.task_confidence_signals or {}),
            },
            "resolution_sources": {
                "budget_exhausted": self.budget_manager.is_exhausted(),
                "budget_status": "exhausted" if self.budget_manager.is_exhausted() else "ok",
                "provider_status": provider_status,
                "policy_status": policy_status,
                "session_failed": getattr(self, "_session_failed", False),
                "loop_abort_reason_raw": pre_resolve_loop_abort,
            },
        }
        if pre_resolve_loop_abort == LOOP_ABORT_MAX_ITERATIONS:
            rec["loop_stop_signal_raw"] = pre_resolve_loop_abort
            ila = iteration_limit_operator_assessment(
                terminal_phase_value=tr.phase.value,
                outcome=tr.outcome,
                evidence_score=tr.evidence_score,
                has_final_nl=_has_final_nl_response(self.history),
                session_failed=getattr(self, "_session_failed", False),
                loop_stop_raw=pre_resolve_loop_abort,
            )
            rec["iteration_limit_assessment"] = merge_iteration_limit_summary_with_confidence(
                ila,
                task_confidence_level=tr.task_confidence_level,
                task_completion_confidence=tr.task_completion_confidence,
                closure_reason_summary_es=tr.closure_reason_summary_es,
            )
        sess.terminal_resolution_record = rec
        self._apply_terminal_resolution(tr, sess)
        self._trace_phase_end("conclusion")

        if is_no_op:
            if getattr(self, "_last_api_error_hint", ""):
                sess.root_cause = (
                    "No execution or answer produced (fallo de API o respuesta vacía). "
                    + self._last_api_error_hint
                )
                sess.next_action = self._last_api_error_hint
            else:
                sess.root_cause = (
                    "No execution or answer produced. The model may not have returned tool calls. "
                    "If there was no API error above, try rephrasing or use /do with explicit file paths."
                )
                sess.next_action = (
                    "Retry with a clearer prompt, or use /do with explicit file paths."
                )

        if tr.phase == SessionPhase.DONE:
            self.hook_manager.trigger(HookEvents.TASK_COMPLETE, changes=made_changes)

        batch = self.batch_manager.current_batch
        if batch and batch.status == "running":
            current_idx = batch.current_step_index

            if tr.phase == SessionPhase.DONE:
                batch.steps[current_idx].status = "success"
                batch.steps[current_idx].task_id = task_obj.task_id
                batch.steps[current_idx].artifact_id = self.artifact_manager.current_session.session_id

                if current_idx + 1 < len(batch.steps):
                    batch.current_step_index += 1
                    self.batch_manager.save(batch)
                    next_step = batch.steps[batch.current_step_index]
                    self.console.print(
                        f"\n[bold cyan]▶ Advancing to Batch Step {batch.current_step_index + 1}: {next_step.title}[/bold cyan]"
                    )

                    self.task_manager.current_task = None
                    self.artifact_manager.current_session = None

                    self._process_input(next_step.title)
                else:
                    batch.status = "completed"
                    batch.current_step_index = len(batch.steps)
                    self.batch_manager.save(batch)
                    self.console.print("\n[bold green]🏁 Batch Sequence Completed Successfully.[/bold green]")
            else:
                if batch.steps[current_idx].retry_count < 1:
                    batch.steps[current_idx].retry_count += 1
                    batch.steps[current_idx].status = "retrying"
                    self.batch_manager.save(batch)
                    self.console.print(
                        f"\n[bold yellow]↻ Retrying Batch Step {current_idx + 1} (Attempt 1/1)...[/bold yellow]"
                    )

                    self.task_manager.current_task = None
                    self.artifact_manager.current_session = None

                    self._process_input(batch.steps[current_idx].title)
                else:
                    batch.status = "failed"
                    batch.steps[current_idx].status = "failed"
                    self.batch_manager.save(batch)
                    self.console.print(
                        f"\n[bold red]✘ Batch Halted at Step {current_idx + 1}: {batch.steps[current_idx].title}[/bold red]"
                    )

        self._finalize_session_trace()
        try:
            from apps.cli.runtime.workflow_continuity import persist_plan_and_handoff

            persist_plan_and_handoff(self.cwd, self.artifact_manager.current_session)
        except Exception as e:
            logger.warning("workflow continuity persist failed (non-fatal): %s", e)
        self.task_manager.add_artifact(self.artifact_manager.current_session.session_id)
        self.artifact_manager.persist()
        self.renderer.render_artifact_summary(self.artifact_manager.current_session.to_dict())

    def _dispatch_model_tool_round(
        self, msg: Dict[str, Any], status, task_obj: Any
    ) -> Tuple[str, Optional[List[str]]]:
        """
        Execute tool calls from one assistant message (native or legacy XML).
        Returns (kind, executed_names): kind in text_only | tools | terminal | stagnation.
        """
        _tm_loop = getattr(self, "_trace_mgr", None)
        if _tm_loop:
            try:
                _tm_loop.record_chat_model_turn()
            except Exception:
                pass

        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            if _tm_loop:
                try:
                    _tm_loop.record_tool_round()
                except Exception:
                    pass
            if status:
                status.stop()
            if self.session_phase == SessionPhase.EXPLORE:
                self._explore_ls_paths_this_dispatch = set()
            bundled_actions = []
            prepared_calls = []
            for tc in tool_calls:
                func_obj = tc.get("function")
                if func_obj:
                    tc_name = func_obj.get("name")
                    args_raw = func_obj.get("arguments", "{}")
                    tc_args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                else:
                    tc_name = tc.get("name")
                    tc_args = tc.get("arguments", {})
                detail = tc_args.get("path") or tc_args.get("command") or ""
                level = self.policy_gate.get_approval_level(tc_name, detail)
                is_authorized = False
                if level == ApprovalLevel.AUTO:
                    is_authorized = True
                elif level == ApprovalLevel.BUNDLE:
                    bundled_actions.append({"name": tc_name, "detail": detail, "tc": tc, "args": tc_args})
                prepared_calls.append({
                    "tc": tc,
                    "name": tc_name,
                    "args": tc_args,
                    "level": level,
                    "is_authorized": is_authorized,
                })
            if bundled_actions and not self.auto_approve:
                if status:
                    status.stop()
                fp = self._bundled_actions_fingerprint(bundled_actions)
                if fp and fp == self._bundle_approval_fp:
                    authorized = bool(self._bundle_approval_ok)
                    if not authorized:
                        self.console.print(f"[dim]{MSG_APPROVAL_SAME_BUNDLE}[/dim]")
                else:
                    authorized = self.renderer.render_bundled_approval_request(bundled_actions)
                    self._bundle_approval_fp = fp
                    self._bundle_approval_ok = authorized
                if authorized:
                    for pc in prepared_calls:
                        if pc["level"] == ApprovalLevel.BUNDLE:
                            pc["is_authorized"] = True
                if status:
                    status.start()
            if status:
                status.stop()
            _live_hint = format_tool_live_hint_from_prepared(prepared_calls)
            self.renderer.update_status(
                status,
                "tool_exec",
                phase=self.session_phase.value,
                live_detail=_live_hint,
                rail_profile=self._live_rail_profile(),
            )
            executed_names, exit_reason = self._run_segmented_prepared_calls(
                prepared_calls, status, legacy=False
            )
            if exit_reason == "terminal":
                self._abort_session_policy_block()
                return "terminal", None
            if exit_reason == "stagnation":
                self._loop_abort_reason = LOOP_ABORT_STAGNATION
                return "stagnation", None
            return "tools", executed_names
        legacy_calls = self._extract_tool_calls(content)
        if legacy_calls:
            if _tm_loop:
                try:
                    _tm_loop.record_tool_round()
                except Exception:
                    pass
            if status:
                status.stop()
            if self.session_phase == SessionPhase.EXPLORE:
                self._explore_ls_paths_this_dispatch = set()
            bundled_actions = []
            prepared_calls = []
            for call in legacy_calls:
                tc_name = call.get("name")
                tc_args = call.get("arguments", {})
                detail = tc_args.get("path") or tc_args.get("command") or ""
                level = self.policy_gate.get_approval_level(tc_name, detail)
                is_authorized = False
                if level == ApprovalLevel.AUTO:
                    is_authorized = True
                elif level == ApprovalLevel.BUNDLE:
                    bundled_actions.append({"name": tc_name, "detail": detail, "tc": call, "args": tc_args})
                prepared_calls.append({
                    "call": call,
                    "name": tc_name,
                    "args": tc_args,
                    "level": level,
                    "is_authorized": is_authorized,
                })
            if bundled_actions and not self.auto_approve:
                if status:
                    status.stop()
                fp = self._bundled_actions_fingerprint(bundled_actions)
                if fp and fp == self._bundle_approval_fp:
                    authorized = bool(self._bundle_approval_ok)
                    if not authorized:
                        self.console.print(f"[dim]{MSG_APPROVAL_SAME_BUNDLE}[/dim]")
                else:
                    authorized = self.renderer.render_bundled_approval_request(bundled_actions)
                    self._bundle_approval_fp = fp
                    self._bundle_approval_ok = authorized
                if authorized:
                    for pc in prepared_calls:
                        if pc["level"] == ApprovalLevel.BUNDLE:
                            pc["is_authorized"] = True
                if status:
                    status.start()
            if status:
                status.stop()
            _live_hint_l = format_tool_live_hint_from_prepared(prepared_calls)
            self.renderer.update_status(
                status,
                "tool_exec",
                phase=self.session_phase.value,
                live_detail=_live_hint_l,
                rail_profile=self._live_rail_profile(),
            )
            legacy_executed, exit_reason = self._run_segmented_prepared_calls(
                prepared_calls, status, legacy=True
            )
            if exit_reason == "terminal":
                self._abort_session_policy_block()
                return "terminal", None
            if exit_reason == "stagnation":
                self._loop_abort_reason = LOOP_ABORT_STAGNATION
                return "stagnation", None
            return "tools", legacy_executed
        if status:
            status.stop()
        return "text_only", None

    def _resolve_explore_task_class(self) -> str:
        """INTAKE hint (explore_class_hint) or live classify; persists hint when missing."""
        sess = self.artifact_manager.current_session
        text = self._primary_user_task_text()
        if sess:
            hint = (getattr(sess, "explore_class_hint", None) or "").strip()
            if hint:
                return hint
        tc = classify_explore_task(text, sess)
        if sess:
            sess.explore_class_hint = tc
        return tc

    def _after_explore_tool_result(
        self,
        tc_name: str,
        tc_args: Dict[str, Any],
        tool_result: Dict[str, Any],
        legacy: bool,
    ) -> None:
        """Search-first corrections after tool results in EXPLORE (symbol questions)."""
        if legacy or self.session_phase != SessionPhase.EXPLORE or not explore_v2_enabled():
            return
        if not isinstance(tool_result, dict) or tool_result.get("error"):
            return
        sess = self.artifact_manager.current_session
        if not sess:
            return
        if self._resolve_explore_task_class() != EXPLORE_CLASS_SYMBOL_LOOKUP:
            return

        if tc_name == "search_code":
            mode = str(tc_args.get("mode") or SEARCH_MODE_SYMBOL)
            if mode == SEARCH_MODE_SYMBOL:
                if not tool_result.get("found"):
                    if not getattr(self, "_explore_empty_symbol_search_nudged", False):
                        self._explore_empty_symbol_search_nudged = True
                        q = tc_args.get("query") or ""
                        self.history.append({
                            "role": "user",
                            "content": (
                                "[SYSTEM] search_code(mode=symbol) returned no matches in this repository. "
                                "State clearly that this name was not found under the repo root "
                                f"(query={q!r}). Do NOT run repeated `ls` to locate it — either try a different "
                                "spelling, use search_code(mode=text) if appropriate, or conclude the symbol is "
                                "absent from this repo."
                            ),
                        })
                    if getattr(sess, "events", None) is not None:
                        sess.events.append({
                            "event": "explore_symbol_search_no_match",
                            "query": tc_args.get("query"),
                        })

        if tc_name == "ls":
            if not history_has_tool_call(self.history, "search_code"):
                if not getattr(self, "_explore_ls_before_search_nudged", False):
                    self._explore_ls_before_search_nudged = True
                    self.history.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM] Symbol/definition task: you used `ls` before `search_code`. "
                            "Run search_code(mode=symbol) now with the identifier from the user message as `query`, "
                            "then read_file on the best match. Do not list more directories first."
                        ),
                    })

    def _explore_v2_precheck(self, iterations: int) -> Optional[str]:
        """Exploration Engine v2 pre-checks: mismatch, churn, tool-ranking hints."""
        if not explore_v2_enabled():
            return None
        task_text = self._primary_user_task_text()
        sess = self.artifact_manager.current_session
        task_class = self._resolve_explore_task_class()
        # Inject tool ranking nudge on first EXPLORE turn
        if iterations == 1 and not self._explore_v2_tool_ranking_nudge_injected:
            self._explore_v2_tool_ranking_nudge_injected = True
            nudge = tool_ranking_nudge(task_class)
            if nudge:
                self.history.append({"role": "user", "content": nudge})
        # Repo mismatch check (once per session)
        if not self._explore_v2_mismatch_checked:
            self._explore_v2_mismatch_checked = True
            if repo_mismatch_detection_enabled() and task_text:
                mismatch = detect_repo_mismatch(task_text, self.cwd, sess, history=self.history)
                if sess:
                    inject_mismatch_into_session(mismatch, sess)
                if mismatch.detected:
                    logger.info(
                        "explore_v2_mismatch_detected confidence=%.2f reasons=%s",
                        mismatch.confidence, mismatch.reasons,
                    )
                    self.history.append({"role": "user", "content": (
                        "[SYSTEM] Repo mismatch detected. "
                        + (mismatch.operator_message_en or mismatch.operator_message_es)
                        + " Provide a concise explanation to the operator and stop."
                    )})
                    if sess:
                        sess.events.append({
                            "event": "explore_v2_mismatch_close",
                            "confidence": mismatch.confidence,
                            "reasons": mismatch.reasons,
                            "missing_paths": mismatch.missing_paths,
                        })
                    self._promote_explore_to_closing(
                        "repo_mismatch_detected",
                        extra={"mismatch_confidence": mismatch.confidence},
                        synthesis_context="readonly",
                    )
                    return "ok"
        # Listing task: all explicit directory targets absent → explain, do not ls-loop
        if (
            explore_v2_enabled()
            and not self._explore_v2_listing_path_checked
            and task_class == EXPLORE_CLASS_PATH_LISTING
            and task_text
            and self.cwd
        ):
            self._explore_v2_listing_path_checked = True
            all_absent, absent_paths = explore_listing_targets_all_absent(task_text, self.cwd)
            if all_absent and absent_paths:
                msg_en, msg_es = build_listing_paths_absent_operator_message(absent_paths, self.cwd)
                self.history.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM] Listing targets are not present in this repository. "
                        + msg_en
                        + " "
                        + msg_es
                    ),
                })
                if sess:
                    sess.events.append({
                        "event": "explore_explicit_paths_absent",
                        "paths": absent_paths,
                        "cwd": self.cwd,
                    })
                self._promote_explore_to_closing(
                    "explore_explicit_paths_absent",
                    extra={"absent_paths": absent_paths},
                    synthesis_context="readonly",
                )
                return "ok"
        # Exploration churn: symbol_lookup checks from iter 2 (faster escape from ls loops)
        min_churn_iter = 2 if task_class == EXPLORE_CLASS_SYMBOL_LOOKUP else 3
        if iterations >= min_churn_iter:
            abort_flag, abort_reason = should_abort_explore_as_unproductive(
                self.history, sess,
                repo_mismatch_detected=bool(getattr(sess, "repo_mismatch_detected", False)),
                explore_task_class=task_class,
            )
            if abort_flag:
                logger.info("explore_v2_churn_abort reason=%s iter=%d", abort_reason, iterations)
                if sess:
                    sess.events.append({
                        "event": "explore_v2_churn_abort",
                        "reason": abort_reason,
                        "iteration": iterations,
                    })
                self.history.append({"role": "user", "content": (
                    "[SYSTEM] Exploration detected as unproductive (churn). "
                    + build_graceful_blocked_closure_message(abort_reason, task_text)
                    + " Summarize findings and stop."
                )})
                self._promote_explore_to_closing(
                    "explore_churn_abort",
                    extra={"churn_reason": abort_reason, "iteration": iterations},
                    synthesis_context="readonly",
                )
                return "ok"
            if not self._explore_v2_churn_nudge_injected:
                churn = detect_exploration_churn(self.history, sess, explore_task_class=task_class)
                if churn.detected:
                    self._explore_v2_churn_nudge_injected = True
                    nudge_msg = churn_nudge_message(churn)
                    if nudge_msg:
                        self.history.append({"role": "user", "content": nudge_msg})
        return None

    def _phase_explore(self, task_obj: Any, iterations: int) -> str:
        """EXPLORE: read-only tools; transition to ACT via explicit promotion rules."""
        self._maybe_inject_fast_path_nudge()
        if self._should_force_conclusion_after_discovery() and not self._conclusion_nudge_injected:
            self._conclusion_nudge_injected = True
            self._apply_session_phase(SessionPhase.EXPLORE, detail="discovery cap nudge")
            self.history.append({
                "role": "user",
                "content": "[SYSTEM] Discovery cap reached. Output your CONCLUSION now as text: Plan, Diagnostic, Actions taken, Evidence, Final status. No more tool calls.",
            })
        self._explore_rejected_writes_this_round = []
        show_turn_brand = iterations == 1 or self._env_truthy("GHOST_SHOW_BRAND_EVERY_TURN")
        self._maybe_inject_answer_now_pressure_nudge(iterations)
        self._maybe_inject_small_scope_planning_discipline_nudge()
        # Exploration Engine v2: mismatch / churn / tool-ranking checks
        v2_result = self._explore_v2_precheck(iterations)
        if v2_result is not None:
            return v2_result
        with self.renderer.session_status(
            self.session_phase.value,
            rail_profile=self._live_rail_profile(),
        ) as status:
            self.renderer.render_budget_status(self.budget_manager.remaining, self.budget_manager.total_budget)
            sess0 = self.artifact_manager.current_session
            st_is, st_reason = self.stagnation_detector.check_stagnation()
            if st_is:
                metrics0 = self._build_exploration_metrics_for_promotion()
                turn0 = ExploreTurnResult(
                    kind="precheck_stagnation",
                    stagnation_reason=st_reason,
                    rejected_write_tool_names=tuple(self._explore_rejected_writes_this_round),
                )
                if should_promote_explore_to_act(sess0, turn0, metrics0):
                    self._promote_explore_to_act(
                        metrics0.last_promotion_reason,
                        {"stagnation_reason": st_reason, "when": "precheck"},
                    )
                    return "ok"
                if status:
                    status.stop()
                self._loop_abort_reason = LOOP_ABORT_STAGNATION
                checkpoint = self._create_autonomy_checkpoint("stagnation_detected", st_reason)
                self.renderer.render_autonomy_checkpoint(checkpoint)
                return "halt"
            msg = self._stream_completion(
                status,
                show_turn_brand=show_turn_brand,
                tools=self._explore_tools_for_turn(iterations),
            )
            if not msg:
                return "halt"
            kind, executed = self._dispatch_model_tool_round(msg, status, task_obj)
            if kind == "terminal":
                return "ok"
            if kind == "stagnation":
                sess = self.artifact_manager.current_session
                _, st_reason2 = self.stagnation_detector.check_stagnation()
                metrics_s = self._build_exploration_metrics_for_promotion()
                turn_s = ExploreTurnResult(
                    kind="stagnation_mid_tools",
                    executed_tool_names=tuple(executed or ()),
                    rejected_write_tool_names=tuple(self._explore_rejected_writes_this_round),
                    assistant_text=str(msg.get("content") or ""),
                    stagnation_reason=st_reason2,
                )
                if should_promote_explore_to_act(sess, turn_s, metrics_s):
                    self._loop_abort_reason = None
                    self._promote_explore_to_act(
                        metrics_s.last_promotion_reason,
                        {"stagnation_reason": st_reason2, "when": "mid_tools"},
                    )
                    return "ok"
                return "halt"
            content = str(msg.get("content") or "")
            sess = self.artifact_manager.current_session
            if kind == "tools":
                self._bump_discovery_from_tools(executed or [])
                self._explore_text_only_streak = 0
                self._small_scope_planning_nudge_sent = False
                metrics = self._build_exploration_metrics_for_promotion()
                turn = ExploreTurnResult(
                    kind="tools",
                    executed_tool_names=tuple(executed or ()),
                    rejected_write_tool_names=tuple(self._explore_rejected_writes_this_round),
                    assistant_text=content,
                )
                if should_promote_explore_to_act(sess, turn, metrics):
                    self._promote_explore_to_act(
                        metrics.last_promotion_reason,
                        {"executed_tools": list(turn.executed_tool_names), "when": "post_tools"},
                    )
                else:
                    already_impl = self._detect_already_implemented_fast_path()
                    if already_impl:
                        self._queue_already_implemented_completion(already_impl)
                    else:
                        self._handle_answer_now_after_tools(list(executed or []))
                return "ok"
            self._last_explore_assistant_text = content
            self._explore_text_only_streak += 1
            already_impl = self._detect_already_implemented_fast_path()
            if already_impl and self._assistant_text_claims_already_implemented(content):
                self._promote_explore_to_closing(
                    "already_implemented_text_only",
                    extra={
                        "content_len": len(str(content).strip()),
                        "target_files": list(already_impl.get("target_files") or []),
                    },
                    synthesis_context="readonly",
                )
                return "ok"
            if getattr(self, "_already_implemented_answer_pending", None) and (content or "").strip():
                signal = dict(self._already_implemented_answer_pending or {})
                self._already_implemented_answer_pending = None
                self._promote_explore_to_closing(
                    "already_implemented_after_nudge",
                    extra={
                        "content_len": len(str(content).strip()),
                        "target_files": list(signal.get("target_files") or []),
                    },
                    synthesis_context="readonly",
                )
                return "ok"
            if self._answer_now_nudge_pending and (content or "").strip():
                actx = self._answer_now_synthesis_context or "readonly"
                if actx not in ("readonly", "factual"):
                    actx = "readonly"
                self._promote_explore_to_closing(
                    "answer_now_after_nudge",
                    extra={"content_len": len(str(content).strip()), "context": actx},
                    synthesis_context=actx,
                )
                return "ok"
            metrics = self._build_exploration_metrics_for_promotion()
            turn = ExploreTurnResult(
                kind="text_only",
                assistant_text=content,
                rejected_write_tool_names=tuple(self._explore_rejected_writes_this_round),
            )
            if should_promote_explore_to_act(sess, turn, metrics):
                self._promote_explore_to_act(
                    metrics.last_promotion_reason,
                    {"when": "text_only"},
                )
            return "ok"

    def _phase_act(self, task_obj: Any, iterations: int) -> str:
        """ACT: write tools; diff -> VERIFY; text-only without diff -> DONE."""
        if getattr(self, "_repair_act_bridge", False):
            self._repair_act_bridge = False
            logger.info("phase transition: ACT (repair bridge, no LLM) -> VERIFY")
            self._record_phase_promotion(
                "act_to_verify",
                "post_structured_repair",
                SessionPhase.ACT,
                SessionPhase.VERIFY,
            )
            self._apply_session_phase(SessionPhase.VERIFY, detail="post_structured_repair", sync_task=True)
            return "ok"
        max_iter_act = self._effective_iteration_cap()
        show_turn_brand = iterations == 1 or self._env_truthy("GHOST_SHOW_BRAND_EVERY_TURN")
        with self.renderer.session_status(
            self.session_phase.value,
            rail_profile=self._live_rail_profile(),
        ) as status:
            self.renderer.render_budget_status(self.budget_manager.remaining, self.budget_manager.total_budget)
            if self._stagnation_should_halt(status):
                return "halt"
            msg = self._stream_completion(
                status,
                show_turn_brand=show_turn_brand,
                tools=self._tool_defs_for_session_phase(),
            )
            if not msg:
                return "halt"
            content = msg.get("content", "")
            kind, executed = self._dispatch_model_tool_round(msg, status, task_obj)
            if kind == "terminal":
                return "ok"
            if kind == "stagnation":
                return "halt"
            if kind == "tools":
                self._bump_discovery_from_tools(executed or [])
                if self._session_has_diff():
                    logger.info("phase transition: ACT -> VERIFY (diff after tools)")
                    self._record_phase_promotion(
                        "act_to_verify",
                        "diff_after_act",
                        SessionPhase.ACT,
                        SessionPhase.VERIFY,
                    )
                    self._apply_session_phase(SessionPhase.VERIFY, detail="diff after ACT", sync_task=True)
                return "ok"
            plan_mode = (self._effective_prompt_mode() or "").strip().lower() == "plan"
            cap_nudge = int(os.getenv("GHOST_EDIT_MISS_MAX_NUDGES", "1"))
            if (
                self._edit_miss_nudge_enabled()
                and not plan_mode
                and not self._session_has_diff()
                and self._last_tool_round_had_edit_old_str_miss()
                and self._edit_miss_nudge_count < cap_nudge
                and iterations < max_iter_act
                and not self.budget_manager.is_exhausted()
            ):
                self._edit_miss_nudge_count += 1
                self._apply_session_phase(SessionPhase.ACT, detail="edit_file old_str miss", sync_task=True)
                recovery = getattr(self, "_pending_edit_recovery", None) or {}
                rec_args = dict(recovery.get("arguments") or {})
                rec_path = str(rec_args.get("path") or "").strip()
                rec_start = rec_args.get("start_line")
                rec_max = rec_args.get("max_lines")
                if rec_path:
                    if rec_start and rec_max:
                        one_step = (
                            f"read_file(path='{rec_path}', start_line={rec_start}, max_lines={rec_max})"
                        )
                    else:
                        one_step = f"read_file(path='{rec_path}')"
                else:
                    one_step = "read_file de la ruta que intentabas editar"
                nudge_txt = (
                    "[SYSTEM] edit_file falló: old_str no coincide con el disco. "
                    f"PASO ÚNICO: {one_step} → copia old_str literal desde esa salida → vuelve a edit_file "
                    "(o write_file si sustituyes casi todo el fichero). "
                    "No cierres solo con texto; no uses run_shell para leer el archivo."
                )
                self.history.append({"role": "user", "content": nudge_txt})
                self.memory.add_message(self.session_id, "user", nudge_txt)
                self.console.print(
                    "\n[dim yellow]↪ Reintento guiado:[/dim yellow] "
                    "[dim]fallo de old_str — un turno más con herramientas[/dim]"
                )
                return "ok"
            if self._session_has_diff():
                logger.info("phase transition: ACT -> VERIFY (text turn with diff)")
                self._record_phase_promotion(
                    "act_to_verify",
                    "text_turn_with_diff",
                    SessionPhase.ACT,
                    SessionPhase.VERIFY,
                )
                self._apply_session_phase(SessionPhase.VERIFY, detail="text turn + diff", sync_task=True)
                return "ok"
            logger.info("phase transition: ACT -> CLOSING (no diff, text-only; terminal at finalize)")
            self._apply_session_phase(SessionPhase.CLOSING, detail="no write scope", sync_task=True)
            already_impl_signal = self._detect_already_implemented_fast_path()
            if (
                content
                and content.strip()
                and hasattr(self, "current_intent")
                and self.current_intent.mode != "Chat"
                and not already_impl_signal
            ):
                self.console.print(f"\n[dim]💡 Siguiente Mejor Acción: [bold]ghost review[/bold][/dim]")
            elif not content or not str(content).strip():
                tool_summary = self._format_tool_execution_summary()
                if tool_summary:
                    self.console.print(f"\n[dim]{tool_summary}[/dim]")
                elif (
                    hasattr(self, "current_intent")
                    and self.current_intent.mode != "Chat"
                    and not already_impl_signal
                ):
                    self.console.print(f"\n[dim]💡 Siguiente Mejor Acción: [bold]ghost review[/bold][/dim]")
            return "ok"

    @staticmethod
    def _failed_checks_blob(failed_checks: List[Dict[str, Any]]) -> str:
        rows: List[str] = []
        for c in failed_checks or []:
            if not isinstance(c, dict):
                continue
            rows.append(
                "\n".join(
                    str(x or "")
                    for x in (
                        c.get("stderr"),
                        c.get("stdout"),
                        c.get("error"),
                        c.get("output_summary"),
                    )
                )
            )
        return "\n".join(rows)

    def _bump_session_write_epoch(self) -> None:
        sess = self.artifact_manager.current_session
        if sess:
            sess.write_epoch = int(getattr(sess, "write_epoch", 0) or 0) + 1

    def _phase_verify(self, task_obj: Any, context_label: str) -> None:
        """VERIFY: run verify_change only (no LLM). CLOSING defers definitive DONE/ABORTED to finalize."""
        sess = self.artifact_manager.current_session
        if not sess:
            return
        repair_count = getattr(sess, "repair_attempt_count", 0)
        tsd_loop = self._session_contract_spec_dict()
        skip_exec, skip_reason, _tsd = self._resolve_verify_skip_flags(task_obj)
        tc_sess = get_task_contract(sess)
        if not skip_exec and not self._session_has_diff():
            logger.info("phase transition: VERIFY skipped (no diff) -> CLOSING")
            sess.record_incremental_verification(
                {
                    "status": "skipped",
                    "verification_scope": "",
                    "checks": [],
                    "steps_executed_count": 0,
                },
                diff_summary=getattr(sess, "diff_summary", []) or [],
                context_label=f"{context_label}:skip_no_diff",
            )
            self._apply_session_phase(SessionPhase.CLOSING, detail="verify skipped no diff", sync_task=True)
            return
        prev_verification = getattr(sess, "verification", {}) or {}
        failed_names = (
            [c.get("name") for c in prev_verification.get("checks", []) if c.get("status") in ("failed", "error")]
            if repair_count > 0 and prev_verification
            else None
        )
        pytest_focus = list(getattr(sess, "pytest_focus_targets", None) or [])
        if not skip_exec:
            check_names = self.verification_coordinator.planned_check_labels(
                task_type=task_obj.task_type,
                scope=getattr(sess, "task_scope", "") or "unknown",
                files_changed=getattr(sess, "diff_summary", []) or [],
                budget_remaining=getattr(self.budget_manager, "remaining", 100),
                contract_spec=tsd_loop,
                repo_v2=self._session_repo_v2_dict(),
                failed_check_names=failed_names,
            )
            _v_banner = format_integrity_verify_preamble(check_names, pytest_focus_n=len(pytest_focus))
            self.console.print(f"\n[dim]⚙ Running integrity verification: {_v_banner}[/dim]")
            sess.events.append(
                {"event": "verify_preamble", "text": _v_banner[:500], "context_label": context_label}
            )
        self.hook_manager.trigger(HookEvents.PRE_VERIFICATION)
        self._apply_session_phase(
            SessionPhase.VERIFY,
            detail=f"VerificationCoordinator ({context_label})" + ("; skip_exec" if skip_exec else ""),
            sync_task=True,
        )
        self._trace_phase_start("verification")
        _v0 = time.monotonic()
        v_res = self.verification_coordinator.run(
            diff_summary=getattr(sess, "diff_summary", []) or [],
            task_contract=tc_sess,
            task_type=task_obj.task_type,
            scope=getattr(sess, "task_scope", "") or "unknown",
            budget_remaining=getattr(self.budget_manager, "remaining", 100),
            history=self.history,
            blocked_commands=getattr(self, "_blocked_shell_commands", []),
            skip_execution=skip_exec,
            skip_reason=skip_reason,
            repo_v2=self._session_repo_v2_dict(),
            verify_batch_approval_fn=self._verify_batch_approval_callback(),
            session=sess,
            reuse_if_verified=False,
            failed_check_names=failed_names,
            pytest_focus_paths=pytest_focus if pytest_focus else None,
        )
        _mgr_v = getattr(self, "_trace_mgr", None)
        if _mgr_v:
            try:
                _mgr_v.record_verification_trace(
                    verification_trace_from_result(v_res, (time.monotonic() - _v0) * 1000.0)
                )
            except Exception:
                pass
        self._trace_phase_end("verification")
        self.hook_manager.trigger(HookEvents.POST_VERIFICATION, results=v_res)
        if not skip_exec:
            self._verification_completed_this_session = True
            self._finalize_skip_integrity_re_verify = True
        self.renderer.render_verification_results(
            v_res,
            contract_spec=self._session_contract_spec_dict(),
        )
        sess.verification = v_res
        sess.verification_justification = v_res.get("justification", "")
        sess.verification_scope = str(v_res.get("verification_scope") or "")
        sess.planned_check_cwds = dict(v_res.get("planned_check_cwds") or {})
        sess.record_incremental_verification(
            v_res,
            diff_summary=getattr(sess, "diff_summary", []) or [],
            context_label=context_label,
        )
        record_verification_metrics(sess, v_res, reused=False)
        if v_res.get("status") != "failed":
            sess.last_verify_failure_snapshot = {}
            sess.reverification_pending = False
            sess.pytest_focus_targets = []
            sess.last_repair_causal_digest = ""
            if not skip_exec:
                sess.last_integrity_ok_write_epoch = int(getattr(sess, "write_epoch", 0) or 0)
            added = self.budget_manager.grant_extension_for_verified_progress(
                steps_executed=int(v_res.get("steps_executed_count") or 0),
            )
            if added:
                logger.info("adaptive budget: +%s after verify ok (%s)", added, context_label)
                append_budget_extension_record(
                    sess,
                    {
                        "points": added,
                        "reason": "verify_executed_ok",
                        "detail": context_label,
                        "skipped": "",
                    },
                )
            self.stagnation_detector.reset_after_verified_progress()
            logger.info("phase transition: VERIFY -> CLOSING (%s)", context_label)
            self._apply_session_phase(SessionPhase.CLOSING, detail=f"verify ok ({context_label})", sync_task=True)
            return
        checks = v_res.get("checks", [])
        failed_checks = [c for c in checks if c.get("status") in ("failed", "error")]
        _prev_vsnap = dict(getattr(sess, "last_verify_failure_snapshot", None) or {})
        _cur_vsnap = snapshot_failed_verification(failed_checks)
        _fd_prev = (
            _prev_vsnap
            if (str(_prev_vsnap.get("digest") or "").strip() or _prev_vsnap.get("names"))
            else None
        )
        _failure_set_delta, _fd_meta = classify_failure_set_delta_meta(
            _fd_prev,
            failed_checks,
            cur=_cur_vsnap,
        )
        _failed_blob = self._failed_checks_blob(failed_checks)
        _extra_causal = bool(getattr(sess, "last_repair_extra_causal_attempt", False))
        MAX_REPAIR = effective_max_repair_attempts(
            tsd_loop,
            failed_checks_blob=_failed_blob,
            extra_causal_attempt=_extra_causal,
            default=1,
        )
        sess.last_repair_extra_causal_attempt = False
        if _failure_set_delta == "failure_reduced":
            _op_add, _op_sk = self.budget_manager.grant_extension_for_operational_progress(
                reason="verify_failure_progress:failure_reduced",
                skip_if_read_churn=True,
            )
            if _op_add:
                logger.info(
                    "adaptive budget: +%s after %s (%s)",
                    _op_add,
                    _failure_set_delta,
                    context_label,
                )
                append_budget_extension_record(
                    sess,
                    {
                        "points": _op_add,
                        "reason": "verify_failure_progress:failure_reduced",
                        "detail": context_label,
                        "skipped": _op_sk,
                    },
                )
            elif _op_sk:
                sess.events.append(
                    {
                        "event": "budget_extension_skipped",
                        "reason": _failure_set_delta,
                        "detail": _op_sk,
                    }
                )
        elif _failure_set_delta in (
            "same_failure_unchanged",
            "failure_expanded",
            "failure_signature_changed",
        ):
            sess.events.append(
                {
                    "event": "verify_failure_no_budget_extension",
                    "delta": _failure_set_delta,
                    "context_label": context_label,
                    "note": "conservative_no_grant"
                    if _failure_set_delta == "failure_signature_changed"
                    else "",
                }
            )
        _sig_after_txt, _sig_after_dig = compute_causal_error_signature(failed_checks)
        sess.events.append(
            {
                "event": "verify_failure_set_delta",
                "delta": _failure_set_delta,
                "failed_names": [c.get("name") for c in failed_checks[:6]],
                "causal_digest_before": str(_prev_vsnap.get("digest") or "")[:32],
                "causal_digest_after": str(_sig_after_dig or "")[:32],
                "structural_digest_before": str(_prev_vsnap.get("structural_digest") or "")[:32],
                "structural_digest_after": str(_cur_vsnap.get("structural_digest") or "")[:32],
                "budget_extended": _failure_set_delta == "failure_reduced",
                "failure_delta_confidence_gate": str(_fd_meta.get("confidence_gate") or ""),
                "prev_all_low_confidence": bool(_fd_meta.get("prev_all_low_confidence")),
            }
        )
        sess.last_verify_failure_snapshot = _cur_vsnap
        for fc in failed_checks:
            kn = str(fc.get("kind") or "").lower()
            nm = str(fc.get("name") or "")
            if kn == "tests" or "pytest" in nm.lower() or "python tests" in nm.lower():
                focused = parse_pytest_focus_targets(
                    stdout=str(fc.get("stdout") or ""),
                    stderr=str(fc.get("stderr") or ""),
                )
                if focused:
                    sess.pytest_focus_targets = focused
                break
        coord = v_res.get("coordinator") if isinstance(v_res.get("coordinator"), dict) else {}
        actionable = coord.get("reparable")
        if actionable is None:
            actionable = any(c.get("name") in ("Build", "TypeCheck", "Lint") for c in failed_checks)
        if actionable and repair_count < MAX_REPAIR:
            err_summary = "; ".join([f"{c.get('name')}: failed" for c in failed_checks[:3]])
            failed_names_list = [c.get("name") for c in failed_checks]
            repair_focus_files = self._repair_focus_files(sess, failed_checks)
            self._trace_phase_start("repair")
            _rep_t0 = time.monotonic()
            sess.repair_attempt_count = repair_count + 1
            focus_hint = f" Focus files: {', '.join(repair_focus_files[:6])}." if repair_focus_files else ""
            sess.repair_summary = f"Verification failed ({err_summary}). Repair attempted.{focus_hint}"
            sess.last_failed_check = ", ".join(failed_names_list[:3])
            sess.reverification_pending = True
            _sig_txt, _sig_dig = compute_causal_error_signature(failed_checks)
            _delta_tr = classify_error_signature_delta(
                getattr(sess, "last_repair_causal_digest", "") or "",
                _sig_dig,
            )
            _mgr_r = getattr(self, "_trace_mgr", None)
            if _mgr_r:
                try:
                    _rs = trace_timestamp_iso()
                    _rep_ms = (time.monotonic() - _rep_t0) * 1000.0
                    _mgr_r.record_repair_trace(
                        RepairTrace(
                            attempt_number=repair_count + 1,
                            started_at=_rs,
                            ended_at=trace_timestamp_iso(),
                            duration_ms=_rep_ms,
                            failed_check=", ".join(failed_names_list[:3]),
                            repair_summary=err_summary[:500],
                            causal_error_signature=(_sig_txt or "")[:600],
                            error_signature_delta=_delta_tr,
                            reverify_attempted=False,
                            result="repair_nudge_pending",
                            status=TRACE_STATUS_COMPLETED,
                        )
                    )
                except Exception:
                    pass
            self._trace_phase_end("repair")
            logger.info("phase transition: VERIFY -> REPAIR (%s)", context_label)
            self._record_phase_promotion(
                "verify_to_repair",
                "verification_failed_reparable",
                SessionPhase.VERIFY,
                SessionPhase.REPAIR,
                extra={
                    "context_label": context_label,
                    "failed_checks": failed_names_list[:5],
                    "attempt": repair_count + 1,
                    "failure_set_delta": _failure_set_delta,
                    "max_repair": MAX_REPAIR,
                },
            )
            self._apply_session_phase(SessionPhase.REPAIR, detail=err_summary[:200], sync_task=True)
            return
        logger.info("phase transition: VERIFY -> CLOSING (%s, verification failed)", context_label)
        sess.reverification_pending = False
        self._apply_session_phase(SessionPhase.CLOSING, detail="verification failed", sync_task=True)

    def _phase_repair(self, task_obj: Any) -> None:
        """REPAIR: structured RepairSpecialist (compact context + repair model) or legacy nudge -> ACT -> VERIFY."""
        sess = self.artifact_manager.current_session
        summary = (getattr(sess, "repair_summary", "") or "").replace("Verification failed (", "").replace("). Repair attempted.", "")
        if len(summary) > 220:
            summary = summary[:220] + "…"

        use_specialist = repair_specialist_enabled()
        v = getattr(sess, "verification", {}) or {}
        checks = list(v.get("checks") or [])
        coord_v = v.get("coordinator") if isinstance(v.get("coordinator"), dict) else {}
        fc_coord = coord_v.get("failed_checks")
        if isinstance(fc_coord, list) and fc_coord:
            failed_checks = list(fc_coord)
        else:
            failed_checks = [c for c in checks if c.get("status") in ("failed", "error")]
        diff_summary = list(getattr(sess, "diff_summary", []) or [])
        diff_summary_for_repair = self._filter_diff_summary_for_repair(sess, failed_checks, diff_summary)
        repair_focus_files = self._repair_focus_files(sess, failed_checks)
        primary_failure = summarize_primary_failure(failed_checks)
        prev_raw = getattr(sess, "repair_specialist_last_raw", "") or ""
        causal_sig_text, causal_sig_digest = compute_causal_error_signature(failed_checks)
        err_sig_delta = classify_error_signature_delta(
            getattr(sess, "last_repair_causal_digest", "") or "",
            causal_sig_digest,
        )

        repair_allow = build_repair_allowlist(failed_checks, diff_summary_for_repair) if failed_checks else []
        if use_specialist and failed_checks and repair_allow:
            roles = resolve_model_role_map()
            repair_model = roles.get("repair") or ""
            if sess:
                sess.repair_model_resolved = repair_model
            budget_ref = max(self._last_main_turn_prompt_chars, 1)
            cap_total = repair_context_char_budget(last_main_turn_prompt_chars=self._last_main_turn_prompt_chars)
            self.console.print(
                f"\n[dim]Repair Specialist[/dim] [bold yellow]model={repair_model}[/bold yellow] "
                f"[dim](repair prompt cap ≈{cap_total} chars vs last main turn ≈{budget_ref})[/dim]"
            )
            res = run_repair_specialist_llm(
                failed_checks=failed_checks,
                diff_summary=diff_summary_for_repair,
                previous_patch_text=prev_raw,
                last_main_turn_prompt_chars=self._last_main_turn_prompt_chars,
                ghost_server_url=self.server_url,
                ghost_api_key=self.api_key,
                causal_signature_text=causal_sig_text,
                error_signature_delta=err_sig_delta,
            )
            if sess:
                record_repair_specialist_model_turn(sess)
                self._phase_segment_model_turns = int(
                    getattr(self, "_phase_segment_model_turns", 0) or 0
                ) + 1
                sess.repair_specialist_prompt_chars = res.prompt_user_chars + len(REPAIR_JSON_SYSTEM)
                sess.repair_specialist_usage_tokens = res.usage_total_tokens
                sess.repair_specialist_last_raw = res.raw_model_text or ""
            applied = 0
            apply_errs: List[str] = []
            structural = failed_checks_indicate_structural_python_failure(failed_checks)
            pre_parse_by_path: Dict[str, Any] = {}
            if structural and res.provider_ok and res.parse_ok and res.edits_to_apply:
                for ed in res.edits_to_apply:
                    rel = str(ed.get("path") or "").replace("\\", "/").strip()
                    if rel.lower().endswith(".py"):
                        pre_parse_by_path[rel] = parse_python_file_errors(self.cwd, rel)
            if res.provider_ok and res.parse_ok and res.edits_to_apply:
                metadata: Dict[str, Any] = {}
                for ed in res.edits_to_apply:
                    r = self._inner_execute_tool(
                        "edit_file",
                        {"path": ed["path"], "old_str": ed["old_str"], "new_str": ed["new_str"]},
                        metadata,
                        is_authorized=True,
                    )
                    if r.get("error"):
                        apply_errs.append(f"{ed['path']}: {r.get('error')}")
                    else:
                        applied += 1
                res.edits_applied = applied
            edited_py = [
                str(ed["path"]).replace("\\", "/")
                for ed in res.edits_to_apply
                if str(ed.get("path") or "").lower().endswith(".py")
            ]
            if structural and applied > 0 and edited_py:
                ok_parse, parse_reason, post_errs, same_sig = validate_edited_python_parse(
                    self.cwd, edited_py, pre_errors=pre_parse_by_path
                )
                res.parse_validation_ok = ok_parse
                res.parse_validation_reason = parse_reason
                res.same_parse_signature_after_patch = same_sig
                for rel in edited_py:
                    pre_e = pre_parse_by_path.get(rel)
                    post_e = post_errs.get(rel) if isinstance(post_errs, dict) else None
                    res.pre_parse_fingerprints[rel] = parse_error_fingerprint(pre_e)
                    res.post_parse_fingerprints[rel] = parse_error_fingerprint(post_e)
            outcome = classify_structured_repair_outcome(res, applied)
            if sess:
                append_repair_run_record(
                    sess,
                    {
                        "source": "repair_specialist",
                        "outcome": outcome,
                        "edits_applied": applied,
                        "edits_allowlisted": len(res.edits_to_apply),
                        "model_id": res.model_id,
                        "causal_signature_text": (causal_sig_text or "")[:500],
                        "causal_signature_digest": causal_sig_digest,
                        "error_signature_delta": err_sig_delta,
                        "parse_validation_ok": bool(getattr(res, "parse_validation_ok", True)),
                        "parse_validation_reason": str(getattr(res, "parse_validation_reason", "") or "")[:500],
                        "pre_parse_fingerprints": dict(getattr(res, "pre_parse_fingerprints", {}) or {}),
                        "post_parse_fingerprints": dict(getattr(res, "post_parse_fingerprints", {}) or {}),
                        "same_parse_signature_after_patch": bool(
                            getattr(res, "same_parse_signature_after_patch", False)
                        ),
                    },
                )
            ro_detail: Dict[str, Any] = {
                "outcome": outcome,
                "edits_proposed": res.edits_proposed,
                "edits_allowlisted": len(res.edits_to_apply),
                "edits_applied": applied,
                "parse_ok": res.parse_ok,
                "provider_ok": res.provider_ok,
                "all_proposals_outside_allowlist": res.all_proposals_outside_allowlist,
                "repair_attempt_count": int(getattr(sess, "repair_attempt_count", 0) or 0),
                "model_id": res.model_id,
                "parse_validation_ok": bool(getattr(res, "parse_validation_ok", True)),
                "parse_validation_reason": str(getattr(res, "parse_validation_reason", "") or "")[:300],
                "pre_parse_fingerprints": dict(getattr(res, "pre_parse_fingerprints", {}) or {}),
                "post_parse_fingerprints": dict(getattr(res, "post_parse_fingerprints", {}) or {}),
                "same_parse_signature_after_patch": bool(
                    getattr(res, "same_parse_signature_after_patch", False)
                ),
            }
            if apply_errs:
                ro_detail["apply_errors_sample"] = apply_errs[:8]
            if sess:
                sess.repair_specialist_outcome = outcome
                sess.repair_specialist_outcome_detail = dict(ro_detail)
                sess.repair_outcome_log.append(dict(ro_detail))
                parts = [
                    f"repair_specialist model={res.model_id}",
                    f"outcome={outcome}",
                    f"prompt_chars≈{res.prompt_user_chars} budget={res.prompt_budget}",
                    f"tokens={res.usage_total_tokens}",
                    res.root_cause or "",
                    f"edits_applied={applied}/{len(res.edits_to_apply)}",
                ]
                if apply_errs:
                    parts.append("apply_errors=" + "; ".join(apply_errs[:3]))
                sess.last_repair_applied = " | ".join(p for p in parts if p)

            if outcome == REPAIR_OUTCOME_APPLIED_PATCH:
                record_repair_success(sess)
                _rp_deserves, _rp_detail, _rp_conf, _rp_skip = resolve_repair_operational_budget(
                    cwd=self.cwd,
                    structural_python_failure=bool(structural),
                    applied=int(applied),
                    res=res,
                    failed_checks=failed_checks,
                    session=sess,
                )
                if _rp_deserves:
                    _rp_add, _rp_sk = self.budget_manager.grant_extension_for_operational_progress(
                        reason="repair_specialist_structured_progress",
                        skip_if_read_churn=True,
                    )
                else:
                    _rp_add, _rp_sk = 0, _rp_skip
                    if sess is not None:
                        append_compacted_session_event(
                            sess,
                            {
                                "event": "budget_extension_skipped",
                                "reason": "repair_no_structured_progress",
                                "detail": _rp_skip,
                                "confidence": _rp_conf,
                            },
                        )
                if _rp_add:
                    append_budget_extension_record(
                        sess,
                        {
                            "points": _rp_add,
                            "reason": "repair_structured_progress",
                            "detail": _rp_detail,
                            "skipped": _rp_sk,
                            "confidence": _rp_conf,
                        },
                    )
                elif _rp_deserves and _rp_sk:
                    if sess is not None:
                        append_compacted_session_event(
                            sess,
                            {
                                "event": "budget_extension_skipped",
                                "reason": "repair_structured_progress_churn",
                                "detail": _rp_sk,
                                "confidence": _rp_conf,
                            },
                        )
                sess.last_repair_extra_causal_attempt = False
                self.console.print(
                    f"[dim]Repair Specialist:[/dim] {res.root_cause or 'applied minimal edits'} "
                    f"[dim]({applied} edit(s))[/dim]"
                )
                self._repair_act_bridge = True
                logger.info("phase transition: REPAIR -> ACT (bridge) -> VERIFY structured repair")
                self._record_phase_promotion(
                    "repair_to_act",
                    "repair_specialist_applied_patch",
                    SessionPhase.REPAIR,
                    SessionPhase.ACT,
                    extra={"outcome": outcome, "edits_applied": applied},
                )
                self._apply_session_phase(SessionPhase.ACT, detail="repair_specialist_applied", sync_task=True)
                self._persist_repair_causal_digest(sess, failed_checks)
                return

            if sess:
                sess.last_repair_extra_causal_attempt = bool(
                    applied > 0
                    and outcome == REPAIR_OUTCOME_PARSE_BLOCKED
                    and not getattr(res, "same_parse_signature_after_patch", True)
                )
                if sess.last_repair_extra_causal_attempt:
                    sess.events.append(
                        {
                            "event": "repair_extra_attempt_deserved",
                            "outcome": outcome,
                            "edits_applied": applied,
                            "err_sig_delta": err_sig_delta,
                        }
                    )

            _rep_blob = self._failed_checks_blob(failed_checks)
            max_rep = effective_max_repair_attempts(
                self._session_contract_spec_dict(),
                failed_checks_blob=_rep_blob,
                default=1,
            )
            attempt_n = int(getattr(sess, "repair_attempt_count", 0) or 0)
            if outcome != REPAIR_OUTCOME_APPLIED_PATCH and attempt_n >= max_rep:
                logger.info(
                    "phase REPAIR outcome=%s, repair budget exhausted (attempt %s >= max %s) -> CLOSING",
                    outcome,
                    attempt_n,
                    max_rep,
                )
                self._record_phase_promotion(
                    "repair_to_closing",
                    "repair_budget_exhausted",
                    SessionPhase.REPAIR,
                    SessionPhase.CLOSING,
                    extra={"outcome": outcome, "attempt": attempt_n, "max_repair": max_rep},
                )
                self._apply_session_phase(SessionPhase.CLOSING, detail=f"repair_{outcome}", sync_task=True)
                self._persist_repair_causal_digest(sess, failed_checks)
                if sess:
                    sess.last_repair_extra_causal_attempt = False
                return

            if outcome == REPAIR_OUTCOME_PROVIDER_FAILURE:
                self.console.print(f"[yellow]Repair Specialist: proveedor falló ({res.error}); reparación conversacional.[/yellow]")
            elif outcome == REPAIR_OUTCOME_OUT_OF_ALLOWLIST:
                self.console.print("[yellow]Repair Specialist: propuestas fuera de allowlist; reparación conversacional.[/yellow]")
            elif outcome == REPAIR_OUTCOME_INVALID_PATCH:
                self.console.print("[yellow]Repair Specialist: JSON inválido; reparación conversacional.[/yellow]")
            elif outcome == REPAIR_OUTCOME_NO_EFFECTIVE_PATCH:
                self.console.print(
                    "[yellow]Repair Specialist: ningún cambio aplicado (0 edits efectivos); reparación conversacional.[/yellow]"
                )
            elif outcome == REPAIR_OUTCOME_PARSE_BLOCKED:
                self.console.print(
                    "[yellow]Repair Specialist: el parche no supera ast.parse / firma causal en .py; "
                    "no se considera progreso estructural.[/yellow]"
                )

        if sess:
            append_repair_run_record(
                sess,
                {
                    "source": "legacy_repair_nudge",
                    "summary_excerpt": summary[:120] if summary else "",
                    "causal_signature_text": (causal_sig_text or "")[:500],
                    "causal_signature_digest": causal_sig_digest,
                    "error_signature_delta": err_sig_delta,
                },
            )
        self._record_phase_promotion(
            "repair_to_act",
            "legacy_repair_nudge",
            SessionPhase.REPAIR,
            SessionPhase.ACT,
        )
        repair_nudge = (
            f"[REPAIR] Verification failed: {summary or 'see integrity output'}. "
            f"{('Fix this exact active failure first: ' + primary_failure + '. ') if primary_failure else ''}"
            f"{('Focus only on: ' + ', '.join(repair_focus_files[:6]) + '. ') if repair_focus_files else ''}"
            "Do not refactor unrelated logic until the exact active failure disappears. "
            "If the failure is syntax, import, attribute, or startup related, patch only the root-cause line or block. "
            "Fix the errors in the changed files. One focused repair pass. Use edit_file on the files you changed."
        )
        self.history.append({"role": "user", "content": repair_nudge})
        self.memory.add_message(self.session_id, "user", repair_nudge)
        self.console.print("\n[bold yellow]⚠ Verification failed. Repair pass — edit and save changes.[/bold yellow]")
        logger.info("phase transition: REPAIR -> ACT (legacy nudge)")
        self._apply_session_phase(SessionPhase.ACT, detail="after repair nudge", sync_task=True)
        self._persist_repair_causal_digest(sess, failed_checks)

    _TOOL_CALL_XML_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)

    def _strip_tool_calls_for_display(self, text: str) -> str:
        """Remove tool-call XML fragments so they never reach the user."""
        if not text or not isinstance(text, str):
            return text or ""
        return self._TOOL_CALL_XML_RE.sub("", text).strip()

    def _is_tool_call_chunk(self, chunk: str, in_tool_call: bool) -> bool:
        """True if chunk should be suppressed from display (tool-call content)."""
        if not chunk:
            return in_tool_call
        if "<tool_call>" in chunk or "</tool_call>" in chunk:
            return True
        if re.search(r'\{\s*"name"\s*:', chunk):
            return True
        return in_tool_call

    def _compute_execution_context(self) -> Dict[str, Any]:
        """Compute session phase string for the prompt, discovery count, and nudge."""
        discovery_count = 0
        for m in self.history:
            if m.get("role") != "tool":
                continue
            name = m.get("name", "")
            if name in DISCOVERY_ACTIONS:
                discovery_count += 1
        state = getattr(self, "session_phase", SessionPhase.EXPLORE)
        state_s = state.value if isinstance(state, SessionPhase) else str(state)
        count = getattr(self, "_discovery_action_count", discovery_count)
        count = max(count, discovery_count)
        nudge = ""
        if state == SessionPhase.EXPLORE:
            if count >= DISCOVERY_CAP:
                nudge = (
                    f"MANDATORY: Exploration cap reached ({count} actions). Classify: New implementation | "
                    "Modification | Bug fix | Already implemented. Then ACT (edits) or finish with evidence."
                )
            elif count >= DISCOVERY_CAP - 2:
                nudge = f"Exploration almost complete ({count}/{DISCOVERY_CAP}). Prepare to classify and act or conclude."
        elif state == SessionPhase.ACT:
            nudge = "Phase ACT: apply edits; runtime will run VERIFY when you finish with a text turn."
        elif state == SessionPhase.VERIFY:
            nudge = "Phase VERIFY: integrity checks are running or have completed; address failures in REPAIR if needed."
        elif state == SessionPhase.REPAIR:
            nudge = "Phase REPAIR: fix verification failures with focused edits, then continue."
        workset_nudge = self._workset_runtime_nudge(state)
        if workset_nudge:
            nudge = f"{nudge} {workset_nudge}".strip()
        sess_ctx = self.artifact_manager.current_session
        if sess_ctx and verification_integrity_stale(sess_ctx):
            nudge = (
                f"{nudge} Do not claim that tests passed or that verification succeeded unless Ghost VERIFY "
                "has completed successfully after your latest write_file/edit_file; prior run_shell output is stale."
            ).strip()
        pending_rec = getattr(self, "_pending_edit_recovery", None)
        if pending_rec and state in (SessionPhase.ACT, SessionPhase.REPAIR):
            ra = dict(pending_rec.get("arguments") or {})
            rp = str(ra.get("path") or "?")
            sl, ml = ra.get("start_line"), ra.get("max_lines")
            if sl is not None and ml is not None:
                slice_h = f"read_file(path='{rp}', start_line={sl}, max_lines={ml})"
            else:
                slice_h = f"read_file(path='{rp}')"
            nudge = (
                f"{nudge} MANDATORY next step: {slice_h}, then edit_file with old_str copied literally from that "
                "slice—do not call run_shell or write_file until the edit applies."
            ).strip()
        return {"state": state_s, "discovery_count": count, "nudge": nudge}

    def _should_force_conclusion_after_discovery(self) -> bool:
        """True when discovery cap exceeded and no writes - force conclusion next turn."""
        return (
            self._discovery_action_count >= DISCOVERY_CAP
            and self.session_phase == SessionPhase.EXPLORE
            and not any(
                m.get("name") in WRITE_ACTIONS
                for m in self.history
                if m.get("role") == "tool"
            )
        )

    def _format_tool_execution_summary(self) -> str:
        """Compact summary of tool executions from recent history (read-only visibility)."""
        tool_msgs = [m for m in self.history[-15:] if m.get("role") == "tool"]
        if not tool_msgs:
            return ""
        counts: Dict[str, int] = {}
        for m in tool_msgs:
            name = m.get("name", "?")
            if name not in counts:
                counts[name] = 0
            counts[name] += 1
        parts = [f"{name}({count})" for name, count in counts.items()]
        return "Executed: " + ", ".join(parts)

    def _has_final_nl_response_from_history(self) -> bool:
        """True if the last assistant message has non-empty text (final answer).
        Ensures we do not mark as no_op/skipped when the model produced a concluding response.
        """
        for m in reversed(self.history):
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            if content is None:
                continue
            if isinstance(content, str):
                if content.strip():
                    return True
                continue
            if isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        if (p.get("text") or "").strip():
                            return True
            break
        return False

    def _is_implementation_task(self, task_text: str, task_type: str) -> bool:
        """True if the task asks for implementation (API, schema, endpoints, etc.)."""
        if not task_text:
            return False
        t = task_text.lower()
        impl_keywords = ["implement", "implementa", "conecta", "connect", "endpoint", "api", "schema", "drizzle", "sqlite", "route", "ruta"]
        if any(k in t for k in impl_keywords):
            return True
        return task_type in ["direct_edit", "code", "fix"]

    def _infer_already_implemented_evidence(self) -> Optional[str]:
        """Scan tool history for evidence that implementation exists (build, typecheck, routes, schema)."""
        has_reads = False
        has_successful_shell = False
        for m in self.history[-20:]:
            if m.get("role") != "tool":
                continue
            name = m.get("name", "")
            try:
                raw = m.get("content", "")
                res = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else {}
            except Exception:
                res = {}
            if name == "read_file":
                has_reads = True
            if name == "run_shell" and res.get("exit_code") == 0:
                has_successful_shell = True
        parts = []
        if has_reads:
            parts.append("files inspected")
        if has_successful_shell:
            parts.append("shell check succeeded")  # Do NOT say "verification passed" - that requires VerificationManager provenance
        if parts:
            return "Evidence: " + ", ".join(parts) + ". No code changes needed."
        return None

    def _extract_tool_calls_json(self, text: str) -> List[Dict[str, Any]]:
        """Fallback: extract JSON tool-call objects without XML tags."""
        calls = []
        for m in re.finditer(r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"arguments"\s*:\s*', text):
            start = m.start()
            depth = 1
            for i in range(m.end(), len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(text[start:i + 1])
                            if isinstance(obj, dict) and "name" in obj and "arguments" in obj:
                                calls.append(obj)
                        except Exception:
                            pass
                        break
        return calls

    def _extract_tool_calls(self, text: str) -> List[Dict[str, Any]]:
        calls = []
        matches = re.findall(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)
        for m in matches:
            try:
                calls.append(json.loads(m.strip()))
            except Exception:
                try:
                    inner_match = re.search(r"\{.*\}", m, re.DOTALL)
                    if inner_match:
                        calls.append(json.loads(inner_match.group(0)))
                except Exception:
                    pass
        if not calls:
            calls = self._extract_tool_calls_json(text)
        return calls

    PROVIDER_NOT_INITIALIZED = "NVIDIA Provider not initialized"

    def _render_provider_recovery(self, detail: str = ""):
        """Show clear recovery message when NVIDIA provider is not initialized."""
        self.console.print(ghost_panel(
            "[bold red]✘ NVIDIA Provider Not Initialized[/bold red]\n\n"
            "[white]What failed:[/white] The Ghost server is running but the NVIDIA NIM provider could not be initialized.\n\n"
            "[white]Likely cause:[/white]\n"
            "  • configs/default.yaml missing or invalid\n"
            "  • configs/models.yaml missing or invalid\n"
            "  • nvidia_api_key missing, invalid, or expired\n"
            "  • NVIDIA NIM base URL unreachable\n\n"
            "[white]Next action:[/white]\n"
            "  1. Run [bold cyan]ghost doctor[/bold cyan] to check server connectivity\n"
            "  2. Verify configs/default.yaml has valid upstream.nvidia_api_key\n"
            "  3. Restart the daemon: [bold cyan]ghost stop[/bold cyan] then [bold cyan]ghost start[/bold cyan]\n\n"
            + (f"[dim]Detail: {detail}[/dim]" if detail else ""),
            title="[ghost.brand]Ghost[/ghost.brand] [dim]· recuperación[/dim]",
            border_style="red",
            expand=False,
        ))

    def _json_for_tool_prompt(self, name: str, tool_result: Dict[str, Any]) -> str:
        """JSON for chat history / model prompt; large payloads truncated with full copy in artifact."""
        max_c = int(os.getenv("GHOST_TOOL_OUTPUT_PROMPT_MAX_CHARS", "10000"))
        try:
            full_json = json.dumps(tool_result, ensure_ascii=False)
        except (TypeError, ValueError):
            return json.dumps({"error": "non_serializable_tool_result", "tool": name})
        if len(full_json) <= max_c:
            return full_json
        sess = self.artifact_manager.current_session
        ref = f"to_{self._tool_output_seq}"
        self._tool_output_seq += 1
        approx = len(full_json)
        if sess is not None:
            sess.tool_output_archive.append(
                {"ref": ref, "tool": name, "full": tool_result, "approx_chars": approx}
            )
            record_tool_output_archived(sess)
            if len(sess.tool_output_archive) > 250:
                sess.tool_output_archive = sess.tool_output_archive[-200:]
        shrunk = shrink_tool_result_for_prompt(name, tool_result, max_c)
        if not isinstance(shrunk, dict):
            shrunk = {"preview": str(shrunk)[: max_c - 200]}
        else:
            shrunk = dict(shrunk)
        shrunk["_ghost_full_output_ref"] = ref
        shrunk["_ghost_approx_chars"] = approx
        try:
            return json.dumps(shrunk, ensure_ascii=False)
        except (TypeError, ValueError):
            return json.dumps(
                {
                    "_ghost_full_output_ref": ref,
                    "_ghost_approx_chars": approx,
                    "preview": full_json[: max_c - 120],
                }
            )

    def _post_chat_completions(
        self,
        *,
        json_payload: Dict[str, Any],
        stream: bool,
        connect_t: int,
        read_t: int,
    ):
        """Single chat/completions POST with keep-alive session + exponential backoff + jitter."""
        url = f"{self.server_url}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        max_retries = max(1, int(os.getenv("GHOST_HTTP_MAX_RETRIES", "3")))
        base = float(os.getenv("GHOST_HTTP_RETRY_BASE_SEC", "0.55"))
        max_wait = float(os.getenv("GHOST_HTTP_RETRY_MAX_WAIT_SEC", "32"))
        jitter = float(os.getenv("GHOST_HTTP_RETRY_JITTER_SEC", "0.35"))
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                r = requests.post(
                    url,
                    headers=headers,
                    json=json_payload,
                    stream=stream,
                    timeout=(connect_t, read_t),
                )
                if r.status_code in (429, 502, 503, 504) and attempt < max_retries - 1:
                    try:
                        r.close()
                    except Exception:
                        pass
                    delay = min(
                        max_wait,
                        base * (2**attempt) + random.uniform(0, jitter * (attempt + 1)),
                    )
                    time.sleep(delay)
                    continue
                return r
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_exc = e
                if isinstance(e, requests.exceptions.ConnectionError):
                    err_s = str(e).lower()
                    if (
                        "connection refused" in err_s
                        or "actively refused" in err_s
                        or "10061" in err_s
                        or "deneg" in err_s
                        or "nodename nor servname" in err_s
                    ):
                        raise
                if attempt >= max_retries - 1:
                    raise
                delay = min(
                    max_wait,
                    base * (2**attempt) + random.uniform(0, jitter * (attempt + 1)),
                )
                time.sleep(delay)
        if last_exc:
            raise last_exc
        raise RuntimeError("GHOST: HTTP retry exhausted")

    def _stream_completion(
        self,
        parent_status=None,
        *,
        show_turn_brand: bool = True,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Wrapper for _execute_stream_requests with retry logic."""
        self._fatal_provider_error = False
        for attempt in range(2):
            try:
                result = self._execute_stream_request(
                    parent_status,
                    prefer_non_stream=(attempt > 0),
                    show_turn_brand=show_turn_brand and attempt == 0,
                    tools=tools,
                )
                if getattr(self, "_fatal_provider_error", False):
                    return {}
                return result
            except (requests.exceptions.RequestException, Exception) as e:
                err_str = str(e).lower()
                if self.PROVIDER_NOT_INITIALIZED.lower() in err_str:
                    self._fatal_provider_error = True
                    self._render_provider_recovery(err_str)
                    if parent_status:
                        parent_status.stop()
                    return {}
                retryable_msgs = ["429", "prematurely", "timeout", "broken pipe", "connection reset"]
                is_retryable = any(m in err_str for m in retryable_msgs) or "ChunkedEncodingError" in str(
                    type(e)
                )

                if attempt == 0 and is_retryable:
                    self.console.print(
                        f"[yellow]⚠ Stream interrupted ({type(e).__name__}), "
                        f"reintentando sin streaming (1/2)…[/yellow]"
                    )
                    time.sleep(2)
                    continue
                self.console.print(f"[bold red]✘ System Error:[/bold red] {e}")
                if parent_status:
                    parent_status.stop()
                self._stream_interrupted_this_session = True
                return {}
        self._stream_interrupted_this_session = True
        return {}

    def _execute_stream_request(
        self,
        parent_status=None,
        *,
        prefer_non_stream: bool = False,
        show_turn_brand: bool = True,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        tree = self.indexer.get_project_tree(max_depth=1)
        git = self._get_git_info()
        branch_info = f"{git['branch']} ({git['dirty']})"

        max_iter = global_iteration_cap()
        session = self.artifact_manager.current_session
        if session:
            ib = getattr(session, "iteration_budget", None)
            if isinstance(ib, dict) and ib.get("effective_max") is not None:
                try:
                    max_iter = max(4, min(int(ib["effective_max"]), global_iteration_cap()))
                except (TypeError, ValueError):
                    max_iter = global_iteration_cap()
        auth_status = "ACTIVE (High-Trust)" if self.auto_approve else "DISABLED (Manual Confirmation Required)"
        ctx = self._compute_execution_context()
        if session:
            task_intent = getattr(session, "task_intent", "") or "modification"
            task_scope = getattr(session, "task_scope", "") or "unknown"
            change_expectation = getattr(session, "change_expectation", "") or "may_write"
            intent_confidence = getattr(session, "intent_confidence", 0.0)
        else:
            task_intent, task_scope, change_expectation, intent_confidence = "modification", "unknown", "may_write", 0.0
        agents_project_block = ""
        if session:
            agents_project_block = build_agents_project_prompt_block(session)
        if session:
            ts_block, rp_block, exp_block = build_contract_prompt_blocks(session)
        else:
            ts_block, rp_block, exp_block = "", "", ""
        if ts_block:
            ts_block = ts_block + "\n"
        if rp_block:
            rp_block = rp_block + "\n"
        if exp_block:
            exp_block = exp_block + "\n"
        dp_block = ""
        if session:
            dp_block = build_decision_planner_prompt_block(session)
        if dp_block:
            dp_block = dp_block + "\n"
        ret_block = ""
        if session:
            ret_block = build_retrieval_prompt_block(session)
        if ret_block:
            ret_block = ret_block + "\n"
        exec_block = ""
        if session:
            exec_block = build_execution_agent_prompt_block(session)
        if exec_block:
            exec_block = exec_block + "\n"
        fast_nudge = ""
        if getattr(self, "command_mode", "") in ("fast", "turbo"):
            fast_nudge = (
                "\n# Fast profile (ghost fast / turbo)\n"
                "- Primary model is **fast**: antes del primer `edit_file` haz **read_file** (o `ls`) del path objetivo y copia `old_str` textualmente del contenido leído.\n"
                "- Tareas de **branding / UI / estética**: prioriza `app/`, `components/`, estilos globales; no edites rutas API salvo que el usuario pida cambios de backend.\n"
                "- **No uses `write_file` en `AGENTS.md` / `README.md`** para pegar el enunciado del usuario; eso rompe reglas del repo y no arregla la UI.\n"
                "- Si `edit_file` devuelve que no encontró `old_str`: vuelve a leer el fichero, ajusta el fragmento y **sigue con más herramientas** en el mismo turno o en el siguiente; no cierres la tarea solo con un párrafo explicativo.\n"
            )
        full_system = self.SYSTEM_PROMPT.format(
            mode=self._effective_prompt_mode() if hasattr(self, "mode") else "Chat",
            project=self.project_name if hasattr(self, "project_name") else "GhostLLM",
            cwd=self.cwd,
            tree=tree,
            profile=self.profile if hasattr(self, "profile") else "coder",
            branch_info=branch_info,
            auto_approve=auth_status,
            max_iter=max_iter,
            task_intent=task_intent,
            task_scope=task_scope,
            change_expectation=change_expectation,
            intent_confidence=intent_confidence,
            session_phase=ctx["state"],
            discovery_count=ctx["discovery_count"],
            state_nudge=ctx["nudge"],
            fast_session_nudge=fast_nudge,
            agents_project_block=agents_project_block,
            contract_spec_block=ts_block,
            repo_profile_block=rp_block,
            decision_planner_block=dp_block,
            retrieval_block=ret_block,
            execution_agent_block=exec_block,
            exploration_block=exp_block,
        )
        mtk = getattr(session, "micro_task_kind", None) if session else None
        if mtk and micro_task_mode_enabled():
            full_system = full_system + micro_task_system_prompt_section(mtk)
        elif detect_strategy_plan_request(self._primary_user_task_text()):
            full_system = full_system + strategy_plan_system_prompt_section()

        if session and self._inject_readonly_truthfulness(session):
            full_system = full_system + readonly_analysis_truthfulness_prompt_section()

        _cont = str(getattr(session, "continuity_injected_block", "") or "").strip() if session else ""
        if _cont:
            full_system = full_system + "\n\n# Continuidad de sesión anterior\n\n" + _cont

        latest_user_content = next((msg["content"] for msg in reversed(self.history) if msg["role"] == "user"), "")
        history_for_prompt = self.history
        if sliding_window_enabled():
            tail = int(os.getenv("GHOST_PROMPT_HISTORY_TAIL_MESSAGES", "28"))
            rollup_max = int(os.getenv("GHOST_PROMPT_ROLLUP_MAX_CHARS", "2200"))
            keep_u = os.getenv("GHOST_PROMPT_KEEP_FIRST_USER", "1").strip().lower() not in ("0", "false", "no", "off")
            history_for_prompt, self._history_rollup = build_sliding_window_history(
                self.history,
                self._history_rollup,
                tail_messages=tail,
                rollup_max_chars=rollup_max,
                keep_first_user=keep_u,
            )
        active_messages = [{"role": "system", "content": full_system}] + history_for_prompt
        tr_prompt_chars = sum(len(str(m.get("content") or "")) for m in active_messages)
        self._last_main_turn_prompt_chars = tr_prompt_chars
        use_stream = self._should_use_stream(latest_user_content, prompt_chars_est=tr_prompt_chars)
        if prefer_non_stream:
            use_stream = False

        sess_enforce = self.artifact_manager.current_session
        defer_analysis_grounding_print = readonly_analysis_grounding_enabled(
            sess_enforce, getattr(self, "mode", "") or ""
        )

        tr_iso = trace_timestamp_iso()
        tr_mono = time.monotonic()
        tr_first_mono: Optional[float] = None
        tr_first_iso: Optional[str] = None
        tr_out = 0
        tr_ok = False
        tr_err = ""
            
        tool_list = tools if tools is not None else self.NATIVE_TOOLS
        payload = {
            "model": self.model, "messages": active_messages, "stream": use_stream,
            "temperature": 0.1, "tools": tool_list, "tool_choice": "auto",
        }

        connect_t = int(os.getenv("GHOST_HTTP_CONNECT_TIMEOUT", "8"))
        read_t = int(os.getenv("GHOST_HTTP_READ_TIMEOUT", "300"))
        if tr_prompt_chars > 80_000:
            read_t = max(read_t, int(os.getenv("GHOST_HTTP_READ_TIMEOUT_LARGE", "600")))

        response_text = ""
        tool_calls_buffer = {}
        status = parent_status
        if show_turn_brand:
            self.console.print(
                f"\n[ghost.brand]GHOST[/ghost.brand] [dim]·[/dim] "
                f"[white]{getattr(self, 'project_name', 'GhostLLM')}[/white]"
            )
        if not use_stream and tr_prompt_chars > 50_000:
            self.console.print(
                "[dim]Modo sin streaming (prompt grande); timeout de lectura extendido si aplica.[/dim]"
            )

        try:
            if not use_stream:
                r = self._post_chat_completions(
                    json_payload=payload,
                    stream=False,
                    connect_t=connect_t,
                    read_t=read_t,
                )
                if r.status_code != 200:
                    err_msg = r.text
                    try:
                        j = r.json()
                        err_msg = j.get("detail")
                        if err_msg is None and isinstance(j.get("error"), dict):
                            err_msg = j["error"].get("message", r.text)
                        if err_msg is None:
                            err_msg = r.text
                    except Exception:
                        pass
                    if r.status_code == 429: raise Exception(f"429: {err_msg}")
                    if r.status_code == 500 and self.PROVIDER_NOT_INITIALIZED.lower() in str(err_msg).lower():
                        self._fatal_provider_error = True
                        self._render_provider_recovery(err_msg)
                        if status: status.stop()
                        tr_err = str(err_msg or "provider_init")
                        return {}
                    hint = self._api_failure_user_hint(r.status_code, err_msg)
                    self._last_api_error_hint = hint
                    self.console.print(f"[bold red]✘ API Error ({r.status_code}):[/bold red] {err_msg}")
                    if hint:
                        self.console.print(f"[dim]{hint}[/dim]")
                    tr_err = str(err_msg or "api_error")
                    return {}
                data = r.json()
                choice = data.get("choices", [{}])[0]
                msg_data = choice.get("message", {})
                response_text = msg_data.get("content") or ""
                if response_text:
                    cleaned = self._strip_tool_calls_for_display(response_text)
                    if cleaned and not defer_analysis_grounding_print:
                        self.console.print(f"{escape(cleaned)}")
                
                tcs_raw = msg_data.get("tool_calls", [])
                for idx, tc in enumerate(tcs_raw):
                    tool_calls_buffer[idx] = {"id": tc.get("id"), "function": {"name": tc.get("function", {}).get("name", ""), "arguments": tc.get("function", {}).get("arguments", "")}, "type": "function"}
            else:
                with self._post_chat_completions(
                    json_payload=payload,
                    stream=True,
                    connect_t=connect_t,
                    read_t=read_t,
                ) as r:
                    if r.status_code != 200:
                        err_msg = r.text
                        try:
                            j = r.json()
                            err_msg = j.get("detail")
                            if err_msg is None and isinstance(j.get("error"), dict):
                                err_msg = j["error"].get("message", r.text)
                            if err_msg is None:
                                err_msg = r.text
                        except Exception:
                            pass
                        if r.status_code == 429: raise Exception(f"429: {err_msg}")
                        if r.status_code == 500 and self.PROVIDER_NOT_INITIALIZED.lower() in str(err_msg).lower():
                            self._fatal_provider_error = True
                            self._render_provider_recovery(err_msg)
                            if status: status.stop()
                            tr_err = str(err_msg or "provider_init")
                            return {}
                        hint = self._api_failure_user_hint(r.status_code, err_msg)
                        self._last_api_error_hint = hint
                        self.console.print(f"[bold red]✘ API Error ({r.status_code}):[/bold red] {err_msg}")
                        if hint:
                            self.console.print(f"[dim]{hint}[/dim]")
                        tr_err = str(err_msg or "api_error")
                        return {}
                    in_xml_tool_call = False
                    try:
                        for line in r.iter_lines():
                            if not line: continue
                            line_str = line.decode("utf-8").strip()
                            if line_str.startswith("data: "):
                                data_content = line_str[6:].strip()
                                if data_content == "[DONE]": break
                                try:
                                    data = json.loads(data_content)
                                    if "error" in data:
                                        if "429" in data["error"] or "retry" in data["error"].lower():
                                            raise Exception(f"429: {data['error']}")
                                        self.console.print(f"[bold red]✘ Stream Error:[/bold red] {data['error']}")
                                        tr_err = str(data["error"])
                                        break
                                    choice = data["choices"][0]
                                    delta = choice.get("delta", {})
                                    
                                    content = delta.get("content", "")
                                    if content:
                                        if tr_first_mono is None:
                                            tr_first_mono = time.monotonic()
                                            tr_first_iso = trace_timestamp_iso()
                                        if status:
                                            self.renderer.update_status(
                                                status, "building", phase=self.session_phase.value
                                            )
                                            status.stop()
                                            status = None
                                        response_text += content
                                        if "<tool_call>" in content or re.search(r'\{\s*"name"\s*:', content):
                                            in_xml_tool_call = True
                                        if "</tool_call>" in content:
                                            in_xml_tool_call = False
                                        if not defer_analysis_grounding_print and not self._is_tool_call_chunk(
                                            content, in_xml_tool_call
                                        ):
                                            self.console.print(escape(content), end="")
                                    
                                    tcs = delta.get("tool_calls", [])
                                    for tc in tcs:
                                        if status:
                                            self.renderer.update_status(
                                                status, "tool_exec", phase=self.session_phase.value
                                            )
                                        idx = tc.get("index", 0)
                                        if idx not in tool_calls_buffer: tool_calls_buffer[idx] = {"id": tc.get("id"), "function": {"name": "", "arguments": ""}, "type": "function"}
                                        if tc.get("id"): tool_calls_buffer[idx]["id"] = tc.get("id")
                                        fn = tc.get("function", {})
                                        if fn.get("name"): tool_calls_buffer[idx]["function"]["name"] += fn.get("name")
                                        if fn.get("arguments"): tool_calls_buffer[idx]["function"]["arguments"] += fn.get("arguments")
                                except Exception as e:
                                    if "429" in str(e): raise
                                    pass
                    except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError) as e:
                        raise Exception(f"Stream interrupted prematurely: {e}")
                if use_stream and not defer_analysis_grounding_print:
                    print("\n")
        
            final_tool_calls = [tool_calls_buffer[i] for i in sorted(tool_calls_buffer.keys())]
            safe_content = response_text if response_text else ("" if final_tool_calls else "...")
            sess_g = self.artifact_manager.current_session
            if readonly_analysis_grounding_enabled(sess_g, getattr(self, "mode", "") or "") and str(safe_content).strip():
                _gr = enforce_readonly_assistant_message(str(safe_content), sess_g)
                safe_content = _gr.text
                _th = normalize_tool_history(self.history)
                _ver = getattr(sess_g, "verification", None) or {}
                _tier_now = compute_findings_evidence_tier(_th, _ver if isinstance(_ver, dict) else {})
                safe_content = finalize_readonly_editorial_reply(
                    safe_content,
                    tier=_tier_now,
                    grounding=_gr,
                    cli_mode=getattr(self, "mode", "") or "",
                )
                response_text = safe_content
            if defer_analysis_grounding_print:
                _cl = self._strip_tool_calls_for_display(safe_content)
                if _cl:
                    self.console.print(f"{escape(_cl)}")
                if use_stream:
                    print("\n")
            msg = {"role": "assistant", "content": safe_content, "tool_calls": final_tool_calls if final_tool_calls else None}
            
            self.history.append(msg)
            self.memory.add_message(self.session_id, "assistant", response_text or "")
            tr_ok = True
            tr_out = len(response_text or "")
            tr_err = ""
            return msg
        except Exception as e:
            if not tr_err:
                tr_err = str(e)
            raise
        finally:
            self._record_main_model_trace(
                started_iso=tr_iso,
                start_mono=tr_mono,
                first_mono=tr_first_mono,
                first_iso=tr_first_iso,
                ok=tr_ok and not tr_err,
                err=tr_err,
                prompt_chars=tr_prompt_chars,
                output_chars=tr_out if tr_ok else len(response_text or ""),
            )
            _sess_m = self.artifact_manager.current_session
            if _sess_m and tr_ok and not tr_err:
                _ph = getattr(self, "session_phase", None)
                _pv = _ph.value if _ph is not None and hasattr(_ph, "value") else ""
                record_main_model_turn_ok(
                    _sess_m, prompt_chars=tr_prompt_chars, phase=_pv or None
                )
                self._phase_segment_model_turns = int(
                    getattr(self, "_phase_segment_model_turns", 0) or 0
                ) + 1

    def _session_contract_spec_dict(self) -> Optional[Dict[str, Any]]:
        sess = self.artifact_manager.current_session
        return contract_spec_dict_from_session(sess) if sess else None

    def _session_repo_v2_dict(self) -> Optional[Dict[str, Any]]:
        sess = self.artifact_manager.current_session
        if not sess:
            return None
        rp = getattr(sess, "repo_profile", None) or {}
        if not isinstance(rp, dict):
            return None
        v2 = rp.get("profile_v2")
        return v2 if isinstance(v2, dict) else None

    def _should_force_surgical_edit(self, path: str) -> bool:
        rel = PathComposer.normalize_path_segments(str(path or ""))
        if not rel:
            return False
        full_path = PathComposer.compose(self.cwd, rel)
        if not os.path.isfile(full_path):
            return False
        task = self._primary_user_task_text().lower()
        structural_markers = (
            "indent",
            "indentación",
            "sintaxis",
            "syntax",
            "import",
            "typo",
            "structural",
            "mínim",
            "minimum",
            "no cambies",
            "corrige sólo",
            "correct only",
            "repair only",
        )
        if not any(marker in task for marker in structural_markers):
            return False
        ts = self._session_contract_spec_dict() or {}
        targets = [str(x).strip() for x in (ts.get("target_files") or []) if str(x).strip()]
        if targets and rel not in [PathComposer.normalize_path_segments(x) for x in targets]:
            return False
        return True

    def _should_force_surgical_edit_v2(self, path: str) -> bool:
        rel = PathComposer.normalize_path_segments(str(path or ""))
        if not rel:
            return False
        full_path = PathComposer.compose(self.cwd, rel)
        if not os.path.isfile(full_path):
            return False
        task = self._primary_user_task_text().lower()
        structural_markers = (
            "indent",
            "indentacion",
            "sintaxis",
            "syntax",
            "import",
            "typo",
            "structural",
            "minim",
            "minimum",
            "minimal",
            "smallest",
            "surgical",
            "quirurg",
            "exact block",
            "exact line",
            "single file",
            "no cambies",
            "corrige solo",
            "cambio minimo",
            "correct only",
            "repair only",
        )
        ts = self._session_contract_spec_dict() or {}
        target_files = ts.get("target_files") or []
        targets = [str(x).strip() for x in target_files if str(x).strip()]
        normalized_targets = [PathComposer.normalize_path_segments(x) for x in targets]
        target_match = not normalized_targets or rel in normalized_targets
        if normalized_targets and not target_match:
            return False
        if any(marker in task for marker in structural_markers):
            return True
        intent = str(ts.get("intent") or "").strip().lower()
        rewrite_markers = ("rewrite", "reescribe", "replace whole file", "regenerate file")
        if (
            intent == "bugfix"
            and target_match
            and len(normalized_targets) <= 3
            and not any(marker in task for marker in rewrite_markers)
        ):
            return True
        return False

    def _contract_spec_allow_tool(self, name: str, args: Dict[str, Any]) -> Tuple[bool, str]:
        """Contract spec budget caps + forbidden layers + should_not_write."""
        sess = self.artifact_manager.current_session
        if not sess or not contract_has_operational_spec(sess):
            return True, ""
        if name == "run_shell":
            cmd0 = str(args.get("command") or "").strip()
            ts0 = contract_spec_dict_from_session(sess) or {}
            if cmd0 and batch_exec.is_verification_shell_command(cmd0):
                _ig = str(ts0.get("intent") or "").strip().lower()
                _ce = str(ts0.get("change_expectation") or "").strip().lower()
                task_txt = str(getattr(sess, "task", "") or "")
                if (_ce == "should_not_write" or _ig in ("analysis", "review")) and not user_explicitly_requests_verify_shell(
                    task_txt
                ):
                    return (
                        False,
                        "Modo inspección/analysis (solo lectura): lint/typecheck/build/test vía run_shell están "
                        "desactivados por defecto. Pide explícitamente ejecutar ese comando (p. ej. «ejecuta pytest») "
                        "o usa una tarea de implementación si quieres verificación automática.",
                    )
        ok, reason = self.budget_manager.contract_spec_allows_tool(name)
        if not ok:
            return False, reason
        if name in ("write_file", "edit_file", "delete_file"):
            path = args.get("path") or ""
            if path:
                path = PathComposer.normalize_path_segments(path)
            blocked, br = write_blocked_by_contract_spec(path, contract_spec_dict_from_session(sess))
            if blocked:
                return False, br
        if name == "write_file":
            path = args.get("path") or ""
            if path and self._should_force_surgical_edit_v2(path):
                return (
                    False,
                    "Existing file + structural/syntax repair task: use read_file on the exact block and apply edit_file instead of write_file.",
                )
        if name == "run_shell":
            pending = getattr(self, "_pending_edit_recovery", None)
            cmd = str(args.get("command") or "").strip()
            if pending and cmd and not batch_exec.is_verification_shell_command(cmd):
                rec_args = dict(pending.get("arguments") or {})
                rec_path = rec_args.get("path") or "(unknown)"
                rec_start = rec_args.get("start_line")
                rec_max = rec_args.get("max_lines")
                slice_hint = f"{rec_path}:{rec_start}+{rec_max}" if rec_start and rec_max else rec_path
                return (
                    False,
                    "Pending edit_file recovery: use read_file instead of run_shell "
                    f"for exact file inspection ({slice_hint}).",
                )
            if (
                sess
                and getattr(sess, "reverification_pending", False)
                and verification_integrity_stale(sess)
                and cmd
                and batch_exec.is_verification_shell_command(cmd)
            ):
                return (
                    False,
                    "Re-verification pending after edits: use read_file → edit_file on the failure site; "
                    "Ghost VERIFY will run the real test command—avoid duplicate shell verification here.",
                )
            already_impl = self._detect_already_implemented_fast_path()
            if already_impl and cmd and batch_exec.is_verification_shell_command(cmd):
                return (
                    False,
                    "Already implemented fast-path: nearby tests already cover this; "
                    "answer without manual run_shell verification.",
                )
        ts = contract_spec_dict_from_session(sess)
        if ts and "ui" in (ts.get("forbidden_layers") or []) and name in ("read_file", "ls"):
            rpd = getattr(sess, "repo_profile", None) or {}
            ui_roots: List[str] = []
            if isinstance(rpd, dict):
                v2 = rpd.get("profile_v2")
                if isinstance(v2, dict):
                    ep = v2.get("entrypoints") or {}
                    if isinstance(ep, dict):
                        ui_roots = [str(x) for x in (ep.get("ui_roots") or []) if x]
            if ui_roots:
                check_path = args.get("path")
                if name == "ls":
                    check_path = check_path if check_path not in (None, "") else "."
                else:
                    check_path = check_path or ""
                if check_path and path_under_ui_roots(str(check_path), ui_roots):
                    return (
                        False,
                        "TaskSpec forbids UI layer; path is under RepoProfile ui_roots (read/list blocked).",
                    )
        return True, ""

    def _tool_trace_target(self, name: str, args: Dict[str, Any]) -> str:
        if name == "read_file":
            path = str(args.get("path") or "")
            if not path:
                return ""
            start_line = _positive_int_or_none(args.get("start_line"))
            max_lines = _positive_int_or_none(args.get("max_lines"))
            if start_line is None and max_lines is None:
                return path
            start = start_line or 1
            if max_lines is None:
                return f"{path}:{start}"
            end = start + max_lines - 1
            return f"{path}:{start}" if end <= start else f"{path}:{start}-{end}"
        return str(args.get("path") or args.get("command") or "")

    def _execute_tool(self, call: Dict[str, Any], is_authorized: bool = False) -> Dict[str, Any]:
        """Defensive Tool Execution Engine."""
        name, args = call.get("name"), call.get("arguments", {})
        target = self._tool_trace_target(name, args)

        # Trigger PreToolUse Hook
        self.hook_manager.trigger(HookEvents.PRE_TOOL_USE, name=name, arguments=args)
        
        metadata = {
            "task_type": self.current_intent.task_type if hasattr(self, "current_intent") else "ask",
            "mode": getattr(self, "mode", "Chat"),
            "role": self.role,
            "is_swarm_worker": self.is_swarm_worker
        }
        
        t_tool_iso = trace_timestamp_iso()
        t_tool_mono = time.monotonic()
        try:
            result = self._inner_execute_tool(name, args, metadata, is_authorized=is_authorized)
            self._update_session_workset_from_tool(name, args, result)
            _sess_tool = self.artifact_manager.current_session
            if _sess_tool and name:
                record_tool_execution_metrics(_sess_tool, name)
            # Track blocked run_shell for verification truthfulness (blocked != success)
            if name == "run_shell" and result.get("error"):
                blocked = getattr(self, "_blocked_shell_commands", [])
                if not isinstance(blocked, list):
                    blocked = []
                blocked.append((args.get("command", ""), result.get("error", "")))
                self._blocked_shell_commands = blocked
            # Trigger PostToolUse Hook
            self.hook_manager.trigger(HookEvents.POST_TOOL_USE, name=name, result=result)
            dur_ms = (time.monotonic() - t_tool_mono) * 1000.0
            self._record_tool_call_trace(
                name,
                target,
                t_tool_iso,
                dur_ms,
                result,
            )
            self.renderer.append_tool_trace(
                name,
                target,
                result.get("error") is None,
                auto_approved=bool(self.auto_approve or is_authorized),
                duration_ms=dur_ms,
            )
            return result
        except Exception as e:
            self._session_failed = True
            self.hook_manager.trigger(HookEvents.TASK_FAILED, error=str(e))
            err_result = {"error": str(e)}
            dur_ms = (time.monotonic() - t_tool_mono) * 1000.0
            self._record_tool_call_trace(
                name,
                target,
                t_tool_iso,
                dur_ms,
                err_result,
            )
            self.renderer.append_tool_trace(
                name,
                target,
                False,
                auto_approved=bool(self.auto_approve or is_authorized),
                duration_ms=dur_ms,
            )
            return err_result

    def _inner_execute_tool(self, name: str, args: Dict[str, Any], metadata: Dict[str, Any], is_authorized: bool = False) -> Dict[str, Any]:
        path = args.get("path")
        if name == "ls":
            path = args.get("path", ".")
            path = PathComposer.normalize_path_segments(path)
            if not is_authorized and not self.policy_gate.check_permission("List Directory", path, getattr(self, "mode", "Chat"), metadata):
                return {"error": "Access denied by policy."}
            full_path = PathComposer.compose(self.cwd, path)
            if not os.path.exists(full_path): return {"error": f"Path not found: {path}"}
            if not os.path.isdir(full_path): return {"error": f"'{path}' is not a directory. Use 'ls' instead."}
            files = os.listdir(full_path)
            self.exploration_memory.add_ls(path, files, self.cwd)
            return {"files": files}
            
        elif name == "read_file":
            if not path: return {"error": "Missing 'path' argument."}
            path = PathComposer.normalize_path_segments(path)
            if not is_authorized and not self.policy_gate.check_permission("Read File", path, getattr(self, "mode", "Chat"), metadata):
                return {"error": "Access denied by policy."}
            full_path = PathComposer.compose(self.cwd, path)
            if not os.path.exists(full_path): return {"error": f"Path not found: {path}"}
            if os.path.isdir(full_path):
                files = os.listdir(full_path)
                self.exploration_memory.add_ls(path, files, self.cwd)
                listing = "\n".join(files)
                return {"content": f"[Directory: {path}]\nContents:\n{listing}\n\nUse ls for directories; read_file for files."}
            with open(full_path, "r", encoding="utf-8-sig", newline="") as f: content = f.read()
            start_line = _positive_int_or_none(args.get("start_line"))
            max_lines = _positive_int_or_none(args.get("max_lines"))
            if start_line is None and max_lines is None:
                return {"content": content}
            lines = content.splitlines(keepends=True)
            total_lines = len(lines) or 1
            start = start_line or 1
            start = max(1, min(start, total_lines))
            span = max_lines or total_lines
            end = min(total_lines, start + span - 1)
            slice_content = "".join(lines[start - 1:end])
            return {
                "content": slice_content,
                "path": path,
                "start_line": start,
                "end_line": end,
                "total_lines": total_lines,
                "sliced": True,
            }
            
        elif name == "write_file":
            content = args.get("content")
            if not path or content is None: return {"error": "Missing 'path' or 'content' arguments."}
            path = PathComposer.normalize_path_segments(path)
            if not is_authorized and not self.policy_gate.check_permission("Write File", path, getattr(self, "mode", "Chat"), metadata):
                return {"error": "Action denied by user or policy."}
            full_path = PathComposer.compose(self.cwd, path)
            old_content = ""
            if os.path.exists(full_path):
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f: old_content = f.read()
            
            target_dir = os.path.dirname(full_path)
            if target_dir: os.makedirs(target_dir, exist_ok=True)
            
            with open(full_path, "w", encoding="utf-8") as f: f.write(content)
            self.renderer.render_diff(path, "".join(difflib.unified_diff(old_content.splitlines(keepends=True), content.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}")))
            self.artifact_manager.add_diff(
                path, "Write File", content_sha256=_sha256_utf8(content)
            )
            sess = self.artifact_manager.current_session
            if sess:
                if not int(getattr(sess, "first_edit_turn", 0) or 0):
                    sess.first_edit_turn = int(getattr(self, "_session_loop_iteration", 0) or 0)
                if getattr(sess, "fast_path_eligible", False):
                    sess.fast_path_used = True
            self._pending_edit_recovery = None
            self.indexer.invalidate_project_tree_cache()
            self._bump_session_write_epoch()
            return {"status": "success", "bytes": len(content)}
            
        elif name == "edit_file":
            old_str, new_str = args.get("old_str"), args.get("new_str")
            if not all([path, old_str is not None, new_str is not None]): return {"error": "Missing required arguments."}
            path = PathComposer.normalize_path_segments(path)
            if not is_authorized and not self.policy_gate.check_permission("Patch File", path, getattr(self, "mode", "Chat"), metadata):
                return {"error": "Action denied by user or policy."}
            full_path = PathComposer.compose(self.cwd, path)
            if not os.path.exists(full_path): return {"error": f"File not found: {path}"}
            with open(full_path, "r", encoding="utf-8-sig", newline="") as f: content = f.read()
            match_mode = "exact"
            if old_str in content:
                replacement = new_str
                new_content = content.replace(old_str, replacement, 1)
            else:
                normalized_content, index_map = _normalize_newlines_with_index_map(content)
                normalized_old, _ = _normalize_newlines_with_index_map(str(old_str))
                match_at = normalized_content.find(normalized_old)
                if match_at >= 0:
                    raw_start = index_map[match_at]
                    raw_end = index_map[match_at + len(normalized_old)]
                    raw_old = content[raw_start:raw_end]
                    replacement = _coerce_newlines_like_reference(str(new_str), raw_old)
                    new_content = content[:raw_start] + replacement + content[raw_end:]
                    match_mode = "normalized_newlines"
                else:
                    error = {"error": "Target string (old_str) not found."}
                    self._pending_edit_recovery = None
                    recovery = _suggest_recovery_read_window(path, content, str(old_str))
                    if recovery:
                        self._pending_edit_recovery = {
                            "next_tool": "read_file",
                            "arguments": dict(recovery),
                        }
                        error["recovery"] = {
                            "next_tool": "read_file",
                            "arguments": recovery,
                            "hint": "Read the exact slice from disk and retry edit_file with a literal old_str.",
                        }
                    return error
            with open(full_path, "w", encoding="utf-8", newline="") as f: f.write(new_content)
            self.renderer.render_diff(path, "".join(difflib.unified_diff(content.splitlines(keepends=True), new_content.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}")))
            self.artifact_manager.add_diff(
                path, "Edit File", content_sha256=_sha256_utf8(new_content)
            )
            sess = self.artifact_manager.current_session
            if sess:
                if not int(getattr(sess, "first_edit_turn", 0) or 0):
                    sess.first_edit_turn = int(getattr(self, "_session_loop_iteration", 0) or 0)
                if getattr(sess, "fast_path_eligible", False):
                    sess.fast_path_used = True
            self._pending_edit_recovery = None
            self.indexer.invalidate_project_tree_cache()
            self._bump_session_write_epoch()
            return {"status": "success", "file": path, "match_mode": match_mode}
            
        elif name == "run_shell":
            cmd = args.get("command")
            if not cmd: return {"error": "Missing 'command' argument."}

            # Shell minimization: list-dir commands -> use ls (avoid redundant exploration)
            list_path = self.exploration_memory.get_list_dir_path(cmd)
            if list_path:
                cached = self.exploration_memory.get_cached_ls(list_path, self.cwd)
                if cached is not None:
                    self.console.print(f" [dim]Using ls cache for {list_path} (redundant shell blocked)[/dim]")
                    return {"stdout": "\n".join(cached), "stderr": "", "exit_code": 0}
                full_path = PathComposer.compose(self.cwd, list_path)
                if os.path.exists(full_path) and os.path.isdir(full_path):
                    files = os.listdir(full_path)
                    self.exploration_memory.add_ls(list_path, files, self.cwd)
                    self.console.print(f" [dim]Using ls instead of shell for {list_path}[/dim]")
                    return {"stdout": "\n".join(files), "stderr": "", "exit_code": 0}

            # Windows Shell Normalization v1
            norm_cmd, was_changed, explanation = ShellNormalizer.normalize(cmd)
            if explanation:
                self.console.print(f"\n [bold red]✘ NORM BLOCK:[/bold red] {explanation}")
                return {"error": f"Command blocked by normalizer: {explanation}"}
            
            original_context = cmd if was_changed else None
            effective_cmd = norm_cmd if was_changed else cmd
            
            if was_changed:
                self.console.print(f" [dim]⚡ Auto-normalized Unix command to PowerShell: [bold white]{norm_cmd}[/bold white][/dim]")
            
            if not self.policy_gate.check_permission(
                "Execute Shell",
                effective_cmd,
                getattr(self, "mode", "Chat"),
                metadata,
                original_command=original_context,
                batch_preapproved=bool(is_authorized),
            ):
                return {"error": "Action denied by user or policy."}
            
            res = subprocess.run(effective_cmd, shell=True, capture_output=True, text=True, env=os.environ.copy(), cwd=self.cwd)
            
            # GEP-3.1: Terminal Failure for Exact Commands
            is_exact = getattr(self.current_intent, "is_exact_command", False)
            if res.returncode != 0 and is_exact:
                self.console.print(f"\n [bold red]TERMINAL FAIL:[/bold red] Exact command failed with exit code {res.returncode}.")
                return {
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                    "exit_code": res.returncode,
                    "terminal": True,
                    "error": "Exact command execution failed. Terminal stop triggered."
                }
                
            return {"stdout": res.stdout, "stderr": res.stderr, "exit_code": res.returncode}
            
        elif name == "delete_file":
            evidence = args.get("evidence", "")
            if not path: return {"error": "Missing 'path' argument."}
            path = PathComposer.normalize_path_segments(path)
            metadata["evidence"] = evidence
            if not self.policy_gate.check_permission("Delete File", path, getattr(self, "mode", "Chat"), metadata):
                return {"error": "Action denied by policy."}
            full_path = PathComposer.compose(self.cwd, path)
            if not os.path.exists(full_path): return {"error": f"File not found: {path}"}
            if self.auto_approve or self.policy_gate.confirm_action("Delete File", f"Evidence: {evidence}"):
                os.remove(full_path)
                self.artifact_manager.add_diff(
                    path,
                    "Delete File",
                    content_sha256=_sha256_utf8(f"DELETE:{path}"),
                )
                sess = self.artifact_manager.current_session
                if sess:
                    if not int(getattr(sess, "first_edit_turn", 0) or 0):
                        sess.first_edit_turn = int(getattr(self, "_session_loop_iteration", 0) or 0)
                    if getattr(sess, "fast_path_eligible", False):
                        sess.fast_path_used = True
                self._pending_edit_recovery = None
                self.indexer.invalidate_project_tree_cache()
                return {"status": "success", "file": path}
            return {"status": "aborted"}
            
        elif name == "search_code":
            query = args.get("query")
            if not query:
                return {"error": "Missing 'query' argument."}
            mode = str(args.get("mode") or SEARCH_MODE_SYMBOL)
            if mode not in (SEARCH_MODE_SYMBOL, SEARCH_MODE_FILENAME, SEARCH_MODE_PATH_EXISTS, SEARCH_MODE_TEXT):
                mode = SEARCH_MODE_SYMBOL
            try:
                max_results = max(1, min(int(args.get("max_results") or 20), 50))
            except (TypeError, ValueError):
                max_results = 20
            result = _repo_search_code(query=query, repo_root=self.cwd, mode=mode, max_results=max_results)
            return result.to_tool_payload()

        elif name == "summarize_repo":
            return {"summary": self.indexer.get_project_summary()}
            
        return {"error": f"Tool '{name}' not found."}

    def _audit_hook(self, event_name: str, **kwargs):
        """Standard audit log implementation for artifacts."""
        if self.artifact_manager.current_session:
            details = ""
            if event_name == HookEvents.TASK_START:
                mode = kwargs.get("intent", {}).get("mode", "Unknown")
                tm = kwargs.get("task_mode", "standard")
                mtk = kwargs.get("micro_task_kind")
                extra = f" | task_mode={tm}"
                if mtk:
                    extra += f" | micro_task_kind={mtk}"
                details = f"Mode: {mode} | Task: {kwargs.get('task')}{extra}"
            elif event_name == HookEvents.PRE_TOOL_USE:
                details = f"Initializing tool: {kwargs.get('name')}"
            elif event_name == HookEvents.POST_TOOL_USE:
                res = kwargs.get("result", {})
                status = "success" if "error" not in res else "failed"
                details = f"Completed: {kwargs.get('name')} ({status})"
            elif event_name == HookEvents.PRE_VERIFICATION:
                details = "Starting integrity verification cycle"
            elif event_name == HookEvents.POST_VERIFICATION:
                res = kwargs.get("results", {})
                status = res.get("status", "unknown").upper()
                details = f"Verification finished with status: {status}"
            elif event_name == HookEvents.TASK_COMPLETE:
                made_changes = "Changes detected" if kwargs.get("changes") else "No changes made"
                details = f"Session finalized successfully. {made_changes}."
            elif event_name == HookEvents.TASK_FAILED:
                details = f"Critical Error: {kwargs.get('error')}"
            
            entry = {
                "event": event_name,
                "timestamp": time.time(),
                "details": details
            }
            self.artifact_manager.current_session.events.append(entry)

    def _register_default_hooks(self):
        """Registers system-level hooks for observability."""
        # Register audit_log for key events
        for event in [HookEvents.TASK_START, HookEvents.PRE_TOOL_USE, HookEvents.POST_TOOL_USE, HookEvents.PRE_VERIFICATION, HookEvents.POST_VERIFICATION, HookEvents.TASK_COMPLETE, HookEvents.TASK_FAILED]:
            self.hook_manager.register(event, lambda e=event, **kw: self._audit_hook(e, **kw))

    def _get_git_info(self) -> Dict[str, str]:
        try:
            branch = subprocess.check_output("git rev-parse --abbrev-ref HEAD", shell=True, text=True, stderr=subprocess.DEVNULL).strip()
            dirty = "dirty" if subprocess.run("git diff --quiet", shell=True, stderr=subprocess.DEVNULL).returncode != 0 else "clean"
            return {"branch": branch, "dirty": dirty}
        except:
            return {"branch": "unknown", "dirty": "unknown"}

    def _should_use_stream(self, text: str, *, prompt_chars_est: int = 0) -> bool:
        """
        Streaming falla a menudo con prompts enormes (ChunkedEncodingError / cierre prematuro).
        Desactivar stream por tamaño del último mensaje o del contexto total.
        """
        max_user = int(os.getenv("GHOST_STREAM_MAX_USER_CHARS", "4000"))
        max_ctx = int(os.getenv("GHOST_STREAM_MAX_CONTEXT_CHARS", "95000"))
        if prompt_chars_est >= max_ctx:
            return False
        if len(text) > max_user:
            return False
        if not text:
            return True
        if self.current_intent.mode == "Plan" and len(text) > 2000:
            return False
        if self.current_intent.mode == "Chat":
            return len(text) <= max_user
        return len(text) <= max_user

    def _create_autonomy_checkpoint(self, reason: str, details: str) -> AutonomyCheckpoint:
        """Analyze session history and create a smart checkpoint."""
        work_done = []
        # Extract unique recent actions
        seen_actions = set()
        for msg in reversed(self.history):
            if msg["role"] == "tool" and len(work_done) < 5:
                action_name = msg.get("name", "unknown")
                if action_name not in seen_actions:
                    work_done.append(action_name)
                    seen_actions.add(action_name)
        
        pending_work = ["Finalizar objetivos pendientes del prompt original"]
        recommendation = "Revisa los logs y usa 'continua' si el progreso es satisfactorio."
        
        if reason == "stagnation_detected":
            recommendation = (
                "Se repitió la misma acción sobre el mismo objetivo. En la siguiente vuelta el detector se reinicia; "
                "si fue edit_file, haz read_file del archivo y usa un old_str copiado literalmente del fichero, o indica la ruta con /do."
            )
            pending_work = [f"Resolver bloqueo: {details}"]
        elif reason == "budget_exhausted":
            recommendation = "Presupuesto agotado. Usa 'continua' para asignar más unidades o /clear si deseas empezar de cero."

        return AutonomyCheckpoint(
            work_done=list(reversed(work_done)),
            pending_work=pending_work,
            stop_reason=reason,
            recommendation=recommendation
        )
