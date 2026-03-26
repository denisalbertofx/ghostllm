"""
Execution Agent v1 — model-router-ready, NVIDIA NIM-oriented, advisory-first.
Produces context bundles, model selection (default Devstral), and structured edit proposals.
Does not replace the existing write path; GHOST_EXECUTION_AGENT_WRITES gates future automation.
"""
from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field

from apps.cli.runtime.exploration_planner import path_under_ui_roots
from apps.cli.runtime.task_contract import (
    contract_has_operational_spec,
    get_task_contract_decision_plan,
    get_task_contract_spec,
)
from apps.cli.runtime.nim_provider import (
    ProviderRequest,
    ProviderResponse,
    nim_chat_complete,
)

# --- Env ---------------------------------------------------------------------

ENV_USE_EXECUTION_AGENT = "GHOST_USE_EXECUTION_AGENT"
ENV_EXECUTION_AGENT_WRITES = "GHOST_EXECUTION_AGENT_WRITES"
ENV_EXECUTION_MODEL = "GHOST_EXECUTION_MODEL"
ENV_PLANNER_MODEL = "GHOST_PLANNER_MODEL"
ENV_REPAIR_MODEL = "GHOST_REPAIR_MODEL"
ENV_EXECUTION_FALLBACK_MODEL = "GHOST_EXECUTION_FALLBACK_MODEL"
ENV_GENERAL_FALLBACK_MODEL = "GHOST_GENERAL_FALLBACK_MODEL"
ENV_USE_NVIDIA_NIM = "GHOST_USE_NVIDIA_NIM"
ENV_NIM_BASE_URL = "GHOST_NIM_BASE_URL"
ENV_NIM_API_KEY = "GHOST_NIM_API_KEY"
ENV_EXECUTION_AGENT_LIVE = "GHOST_EXECUTION_AGENT_LIVE"

# Apuesta inicial NIM (router-ready; overrides vía GHOST_*_MODEL)
DEFAULT_CODE_WRITER_MODEL = "devstral-2-123b-instruct-2512"
DEFAULT_REPAIR_MODEL = "glm-5"
DEFAULT_PLANNER_MODEL = "kimi-k2.5"
DEFAULT_GENERAL_FALLBACK_MODEL = "deepseek-v3.2"
MAX_CONTEXT_CHARS_DEFAULT = 12_000
MAX_CONTEXT_FILES = 14


def is_execution_agent_enabled() -> bool:
    v = os.environ.get(ENV_USE_EXECUTION_AGENT, "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def execution_agent_writes_enabled() -> bool:
    v = os.environ.get(ENV_EXECUTION_AGENT_WRITES, "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def nim_configured() -> bool:
    if os.environ.get(ENV_USE_NVIDIA_NIM, "0").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    return bool(os.environ.get(ENV_NIM_BASE_URL, "").strip() and os.environ.get(ENV_NIM_API_KEY, "").strip())


def execution_agent_live_enabled() -> bool:
    """Real NIM generation for execution proposals (still no file writes unless WRITES=1)."""
    if not is_execution_agent_enabled() or not nim_configured():
        return False
    v = os.environ.get(ENV_EXECUTION_AGENT_LIVE, "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def general_fallback_model_id() -> str:
    """Fallback general (p. ej. multi-file barato / razonamiento); por defecto deepseek-v3.2."""
    v = os.environ.get(ENV_GENERAL_FALLBACK_MODEL, "").strip()
    return v or DEFAULT_GENERAL_FALLBACK_MODEL


def execution_fallback_model_id() -> str:
    """Fallback de ejecución: GHOST_EXECUTION_FALLBACK_MODEL o, si vacío, el fallback general."""
    v = os.environ.get(ENV_EXECUTION_FALLBACK_MODEL, "").strip()
    return v or general_fallback_model_id()


def resolve_model_role_map() -> Dict[str, str]:
    """Deterministic env-based model IDs per role (router-ready)."""
    return {
        "execution": os.environ.get(ENV_EXECUTION_MODEL, DEFAULT_CODE_WRITER_MODEL).strip()
        or DEFAULT_CODE_WRITER_MODEL,
        "general_fallback": general_fallback_model_id(),
        "execution_fallback": execution_fallback_model_id(),
        "planner": os.environ.get(ENV_PLANNER_MODEL, DEFAULT_PLANNER_MODEL).strip() or DEFAULT_PLANNER_MODEL,
        "repair": os.environ.get(ENV_REPAIR_MODEL, DEFAULT_REPAIR_MODEL).strip() or DEFAULT_REPAIR_MODEL,
        # Transporte del CLI siempre OpenAI-compatible; NIM es ruta opcional del agente de ejecución.
        "provider_backend": "nvidia_nim" if nim_configured() else "openai_compatible",
    }


# --- Pydantic models ---------------------------------------------------------


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    role: Literal["execution", "planner", "repair", "reasoning"] = "execution"
    provider: Literal["nvidia_nim", "ghost_default", "null"] = "ghost_default"
    reason: str = ""


class FileEditAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    action: Literal["edit", "add", "review_only"] = "edit"
    rationale: str = ""


class CodeEditProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    priority: int = Field(ge=0, le=100)
    summary: str = ""
    suggested_actions: List[FileEditAction] = Field(default_factory=list)
    diff_or_patch: Optional[str] = None
    action_type: str = "edit"
    nim_confidence: Optional[float] = None
    parsed_from_model: bool = False


class CandidateEditTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    source: Literal["taskspec", "retrieval", "planner", "repo_profile", "merged"] = "taskspec"
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    reasons: List[str] = Field(default_factory=list)


class ContextChunkRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    start_line: int = 0
    end_line: int = 0
    excerpt: str = ""
    role: str = "unknown"


class ExecutionContextBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: List[CandidateEditTarget] = Field(default_factory=list)
    chunks: List[ContextChunkRef] = Field(default_factory=list)
    planner_paths: List[str] = Field(default_factory=list)
    key_file_refs: List[str] = Field(default_factory=list)
    total_chars_approx: int = 0
    truncated: bool = False


class ExecutionAgentProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "1.0.0"
    advisory_only: bool = True
    writes_enabled_env: bool = False
    nim_flag: bool = False
    context_cap: int = MAX_CONTEXT_CHARS_DEFAULT


class ExecutionAttemptSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = ""
    targets_count: int = 0
    proposals_count: int = 0
    model_id: str = ""


ExecutionMode = Literal["no_op", "single_file_edit", "multi_file_edit", "staged_edit"]


class ExecutionAgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    taskspec: Dict[str, Any] = Field(default_factory=dict)
    repo_profile_v2: Dict[str, Any] = Field(default_factory=dict)
    decision_plan: Dict[str, Any] = Field(default_factory=dict)
    retrieval_result: Optional[Dict[str, Any]] = None
    likely_edit_targets: Optional[List[str]] = None
    likely_edit_reasons: Optional[List[str]] = None
    retrieval_highlight_chunks: Optional[List[Dict[str, Any]]] = None
    merged_candidate_order: Optional[List[Dict[str, Any]]] = None
    session_state: Dict[str, Any] = Field(default_factory=dict)


class ExecutionAgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_model: ModelSelection
    selected_targets: List[str] = Field(default_factory=list)
    context_bundle: ExecutionContextBundle
    proposed_edits: List[CodeEditProposal] = Field(default_factory=list)
    execution_mode: ExecutionMode = "no_op"
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    reasoning_lines: List[str] = Field(default_factory=list)
    provenance: ExecutionAgentProvenance = Field(default_factory=ExecutionAgentProvenance)
    execution_prompt: str = ""
    live_generation_used: bool = False
    provider_ok: bool = True
    provider_error_message: str = ""
    live_raw_proposal: str = ""
    proposal_parsed_ok: bool = False
    provider_latency_ms: float = 0.0
    provider_response: Optional[Dict[str, Any]] = None


# --- Path / role helpers -----------------------------------------------------


def _norm_path(p: str) -> str:
    s = p.replace("\\", "/").strip().strip("./")
    while "//" in s:
        s = s.replace("//", "/")
    return s


def _path_matches_forbidden_layer(norm_path: str, layer: str) -> bool:
    p = "/" + norm_path.replace("\\", "/").lower().lstrip("/")
    if layer == "ui":
        return any(
            x in p
            for x in (
                "/components/",
                "/pages/",
                "/ui/",
                "/src/app/",
                "/apps/web/",
                "components/",
                "pages/",
            )
        )
    if layer == "api":
        return "/api/" in p or "/routes/" in p or "route.ts" in p or "route.js" in p
    if layer == "data":
        return any(x in p for x in ("schema", "drizzle", "prisma", "/db/", "migrations"))
    if layer == "infra":
        return any(x in p for x in ("docker", "terraform", ".github/", "k8s", "helm"))
    return False


def _path_allowed(
    rel: str,
    taskspec: Dict[str, Any],
    repo_v2: Dict[str, Any],
) -> bool:
    forbidden = list(taskspec.get("forbidden_layers") or [])
    if "ui" in forbidden:
        ep = repo_v2.get("entrypoints") or {}
        if isinstance(ep, dict):
            ui_roots = [str(x) for x in (ep.get("ui_roots") or []) if x]
            if ui_roots and path_under_ui_roots(rel, ui_roots):
                return False
    for layer in forbidden:
        if layer == "ui":
            continue
        if _path_matches_forbidden_layer(rel, layer):
            return False
    return True


def infer_file_role(path: str, repo_v2: Dict[str, Any]) -> str:
    p = path.replace("\\", "/").lower()
    if "route" in p or "/api/" in p or p.startswith("app/api"):
        return "api_route"
    if any(x in p for x in ("valid", "zod", "schema.z")) and "drizzle" not in p and "pgtable" not in p:
        if "schema" in p and ("db" in p or "drizzle" in p or "prisma" in p):
            return "db_schema"
        return "validation"
    if any(x in p for x in ("drizzle", "prisma", "schema.ts", "schema.sql", "/db/", "migrations")):
        return "db_schema"
    if any(x in p for x in ("tailwind", "components", "pages/", "src/app", ".tsx", ".vue")):
        return "ui"
    if any(x in p for x in ("config", ".yaml", ".yml", "next.config", "vite.config", "package.json")):
        return "config"
    vf = {str(x).lower() for x in (repo_v2.get("validation_files") or []) if x}
    sf = {str(x).lower() for x in (repo_v2.get("db_schema_files") or []) if x}
    pl = _norm_path(path).lower()
    if pl in vf:
        return "validation"
    if pl in sf:
        return "db_schema"
    for r in repo_v2.get("api_routes") or []:
        if isinstance(r, dict) and str(r.get("file", "")).lower() == pl:
            return "api_route"
    return "service"


def _planner_file_paths(decision_plan: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    ex = decision_plan.get("exploration_plan") or {}
    for c in ex.get("ranked_candidates") or []:
        if isinstance(c, dict) and c.get("kind") == "file":
            p = c.get("path")
            if isinstance(p, str) and p.strip():
                out.append(_norm_path(p))
    ep = decision_plan.get("execution_plan") or {}
    for f in ep.get("expected_files_changed") or []:
        if isinstance(f, str) and f.strip():
            out.append(_norm_path(f))
    seen: Set[str] = set()
    uniq: List[str] = []
    for p in out:
        k = p.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _risk_level(decision_plan: Dict[str, Any]) -> str:
    ep = decision_plan.get("execution_plan") or {}
    r = ep.get("risk") or {}
    if isinstance(r, dict):
        return str(r.get("level") or "low").lower()
    return "low"


def _complexity(decision_plan: Dict[str, Any]) -> str:
    ep = decision_plan.get("execution_plan") or {}
    return str(ep.get("estimated_complexity") or "low").lower()


# --- Model selection ---------------------------------------------------------


def select_execution_model(
    taskspec: Dict[str, Any],
    decision_plan: Dict[str, Any],
    session_state: Dict[str, Any],
    *,
    target_file_count: int = 1,
) -> ModelSelection:
    """
    Deterministic model pick; router-ready. Defaults to Devstral for code writing.
    """
    repair_n = int(session_state.get("repair_attempt_count") or 0)
    nim = nim_configured()
    provider: Literal["nvidia_nim", "ghost_default", "null"] = "nvidia_nim" if nim else "ghost_default"

    repair_model = os.environ.get(ENV_REPAIR_MODEL, DEFAULT_REPAIR_MODEL).strip() or DEFAULT_REPAIR_MODEL
    exec_model = os.environ.get(ENV_EXECUTION_MODEL, DEFAULT_CODE_WRITER_MODEL).strip() or DEFAULT_CODE_WRITER_MODEL
    fallback_exec = execution_fallback_model_id()

    intent = str(taskspec.get("intent") or "").lower()
    risk = _risk_level(decision_plan)
    complexity = _complexity(decision_plan)

    if repair_n >= 2 or (intent == "bugfix" and repair_n >= 1):
        return ModelSelection(
            model_id=repair_model,
            role="repair",
            provider=provider,
            reason="repair_attempts_or_bugfix_with_repair_history",
        )

    if target_file_count >= 3 and fallback_exec and risk == "low" and complexity != "high":
        return ModelSelection(
            model_id=fallback_exec,
            role="execution",
            provider=provider,
            reason="multi_file_low_risk_execution_or_general_fallback",
        )

    return ModelSelection(
        model_id=exec_model,
        role="execution",
        provider=provider,
        reason="default_code_writer_devstral_or_GHOST_EXECUTION_MODEL",
    )


# --- Execution mode ----------------------------------------------------------


def resolve_execution_mode(
    taskspec: Dict[str, Any],
    decision_plan: Dict[str, Any],
    candidate_paths: List[str],
    repo_v2: Dict[str, Any],
) -> Tuple[ExecutionMode, List[str]]:
    reasoning: List[str] = []
    if taskspec.get("change_expectation") == "should_not_write":
        reasoning.append("change_expectation_should_not_write")
        return "no_op", []

    intent = str(taskspec.get("intent") or "").lower()
    if intent in ("review", "analysis"):
        reasoning.append(f"intent_{intent}_no_op")
        return "no_op", []

    if not candidate_paths:
        reasoning.append("no_candidate_paths_after_filters")
        return "no_op", []

    risk = _risk_level(decision_plan)
    if risk in ("medium", "high"):
        reasoning.append(f"planner_risk_{risk}")
        return "staged_edit", candidate_paths

    roles = [infer_file_role(p, repo_v2) for p in candidate_paths[:12]]
    distinct = {r for r in roles if r != "service" and r != "unknown"}
    if len(distinct) >= 2 and distinct.intersection({"api_route", "validation", "db_schema"}):
        reasoning.append("cross_layer_api_validation_schema")
        return "staged_edit", candidate_paths

    if len(candidate_paths) <= 1:
        reasoning.append("single_dominant_target")
        return "single_file_edit", candidate_paths

    reasoning.append("multiple_targets")
    return "multi_file_edit", candidate_paths


# --- Context bundle ----------------------------------------------------------


def build_execution_context_bundle(
    inp: ExecutionAgentInput,
    *,
    max_chars: int = MAX_CONTEXT_CHARS_DEFAULT,
) -> ExecutionContextBundle:
    ts = inp.taskspec
    repo_v2 = inp.repo_profile_v2
    dp = inp.decision_plan
    targets: List[CandidateEditTarget] = []
    seen: Set[str] = set()

    def add_target(
        path: str,
        source: Literal["taskspec", "retrieval", "planner", "repo_profile", "merged"],
        conf: float,
        reason: str,
    ) -> None:
        p = _norm_path(path)
        if not p or not _path_allowed(p, ts, repo_v2):
            return
        k = p.lower()
        if k in seen:
            return
        seen.add(k)
        targets.append(
            CandidateEditTarget(path=p, source=source, confidence=conf, reasons=[reason] if reason else [])
        )

    for tf in ts.get("target_files") or []:
        if isinstance(tf, str) and tf.strip():
            add_target(tf, "taskspec", 1.0, "taskspec_target_files")

    for i, lt in enumerate(inp.likely_edit_targets or []):
        if isinstance(lt, str) and lt.strip():
            rs = ""
            lr = inp.likely_edit_reasons or []
            if i < len(lr):
                rs = str(lr[i])
            add_target(lt, "retrieval", 0.85, rs or "likely_edit_target")

    rr = inp.retrieval_result or {}
    top_files = rr.get("top_files") or []
    for row in top_files:
        if isinstance(row, dict):
            p = row.get("path")
            sc = float(row.get("score") or 0)
            if isinstance(p, str) and p.strip():
                add_target(p, "retrieval", min(1.0, 0.3 + sc / 150.0), "retrieval_top_file")

    for p in _planner_file_paths(dp):
        add_target(p, "planner", 0.6, "planner_candidate")

    for row in inp.merged_candidate_order or []:
        if isinstance(row, dict):
            p = row.get("path")
            if isinstance(p, str) and p.strip():
                add_target(p, "merged", 0.55, str(row.get("source") or "merged"))

    key_needed = len(targets) < 3
    if key_needed:
        for kf in repo_v2.get("key_files") or []:
            if isinstance(kf, str) and kf.strip():
                add_target(kf, "repo_profile", 0.35, "key_file_fill")

    chunks: List[ContextChunkRef] = []
    total = 0
    truncated = False

    def append_excerpt(file_path: str, start: int, end: int, excerpt: str, role: str) -> None:
        nonlocal total, truncated
        block = f"[{role}] {file_path}:{start}-{end}\n{excerpt}\n\n"
        if total + len(block) > max_chars:
            truncated = True
            return
        total += len(block)
        chunks.append(
            ContextChunkRef(
                file_path=file_path,
                start_line=start,
                end_line=end,
                excerpt=excerpt[:2000],
                role=role,
            )
        )

    hit_list = (rr.get("hits") or [])[:10]
    for h in hit_list:
        if not isinstance(h, dict):
            continue
        fp = str(h.get("file_path") or "")
        if not fp or not _path_allowed(fp, ts, repo_v2):
            continue
        sl = int(h.get("start_line") or 0)
        el = int(h.get("end_line") or 0)
        prev = str(h.get("preview") or "")[:1200]
        role = infer_file_role(fp, repo_v2)
        append_excerpt(fp, sl, el, prev, role)

    for ch in (inp.retrieval_highlight_chunks or [])[:8]:
        if not isinstance(ch, dict):
            continue
        fp = str(ch.get("file_path") or "")
        if not fp or not _path_allowed(fp, ts, repo_v2):
            continue
        append_excerpt(
            fp,
            int(ch.get("start_line") or 0),
            int(ch.get("end_line") or 0),
            str(ch.get("reason") or "")[:800],
            infer_file_role(fp, repo_v2),
        )

    planner_paths = _planner_file_paths(dp)[:12]
    key_refs = [str(x) for x in (repo_v2.get("key_files") or [])[:6] if x]

    return ExecutionContextBundle(
        targets=targets[:MAX_CONTEXT_FILES],
        chunks=chunks,
        planner_paths=planner_paths,
        key_file_refs=key_refs,
        total_chars_approx=total,
        truncated=truncated,
    )


# --- Proposals (deterministic, advisory) -------------------------------------


def build_proposed_edits(
    mode: ExecutionMode,
    bundle: ExecutionContextBundle,
    taskspec: Dict[str, Any],
) -> List[CodeEditProposal]:
    if mode == "no_op":
        return []
    proposals: List[CodeEditProposal] = []
    prio = 90
    for t in bundle.targets[:8]:
        actions = [
            FileEditAction(
                path=t.path,
                action="edit",
                rationale="; ".join(t.reasons) or f"source={t.source}",
            )
        ]
        proposals.append(
            CodeEditProposal(
                file_path=t.path,
                priority=prio,
                summary=f"Target from {t.source} (confidence {t.confidence:.2f})",
                suggested_actions=actions,
            )
        )
        prio = max(10, prio - 10)
    return proposals


# --- Prompt ------------------------------------------------------------------


def build_execution_prompt(
    context_bundle: ExecutionContextBundle,
    taskspec: Dict[str, Any],
    decision_plan: Dict[str, Any],
    repo_v2: Dict[str, Any],
) -> str:
    lines = [
        "[EXECUTION AGENT — advisory context for code writing]",
        f"Intent: {taskspec.get('intent', '')} | Scope: {', '.join(taskspec.get('scope') or [])}",
        f"Change expectation: {taskspec.get('change_expectation', '')}",
        f"Forbidden layers: {', '.join(taskspec.get('forbidden_layers') or []) or '(none)'}",
        "",
        "Selected edit targets (prioritized):",
    ]
    for t in context_bundle.targets[:12]:
        role = infer_file_role(t.path, repo_v2)
        lines.append(f"  - {t.path} :: role={role} :: {t.source} :: conf={t.confidence:.2f}")

    if context_bundle.chunks:
        lines.append("")
        lines.append("Relevant chunks (line ranges; apply minimal patches only):")
        for c in context_bundle.chunks[:10]:
            lines.append(f"  - [{c.role}] {c.file_path}:{c.start_line}-{c.end_line}")
            if c.excerpt:
                ex = c.excerpt[:600] + ("…" if len(c.excerpt) > 600 else "")
                lines.append(f"    {ex}")

    vp = (taskspec.get("verification_policy") or {}) if isinstance(taskspec.get("verification_policy"), dict) else {}
    lines.append("")
    lines.append("Verification expectations:")
    lines.append(f"  required={vp.get('required')} typecheck={vp.get('typecheck')} lint={vp.get('lint')} build={vp.get('build')} tests={vp.get('tests')}")

    ep = decision_plan.get("execution_plan") or {}
    if isinstance(ep, dict) and ep.get("guardrails"):
        g = ep["guardrails"]
        if isinstance(g, dict):
            lines.append(f"  guardrails: forbid_layers={g.get('forbid_layers')} max_diff_lines={g.get('max_diff_lines')}")

    lines.append("")
    lines.append(
        "Instructions: Produce minimal, targeted edits; respect forbidden layers; prefer edit_file-style patches over full rewrites; do not invent files."
    )
    lines.append("")
    return "\n".join(lines)


def build_planner_prompt(taskspec: Dict[str, Any], decision_plan: Dict[str, Any]) -> str:
    """Concise planner-only prompt (no code-generation instructions)."""
    ex = decision_plan.get("exploration_plan") or {}
    ep = decision_plan.get("execution_plan") or {}
    lines = [
        "[PLANNER — exploration & execution outline]",
        f"Task intent: {taskspec.get('intent', '')} | Scope: {', '.join(taskspec.get('scope') or [])}",
        f"Acceptance (summary): {str(taskspec.get('acceptance_criteria') or [])[:400]}",
        f"Exploration candidates: {len(ex.get('ranked_candidates') or [])}",
        f"Execution strategy: {ep.get('execution_strategy', '')} | blast: {ep.get('blast_radius', '')}",
        f"Risk: {(ep.get('risk') or {}).get('level', '') if isinstance(ep.get('risk'), dict) else ''}",
        "Reply with a short numbered plan (max 8 bullets). No code blocks.",
    ]
    return "\n".join(lines) + "\n"


def build_repair_prompt(
    taskspec: Dict[str, Any],
    session_state: Dict[str, Any],
    *,
    last_failure: str = "",
) -> str:
    """Repair/debug-oriented prompt; does not request full file rewrites."""
    lines = [
        "[REPAIR — debugging / fix strategy]",
        f"Intent: {taskspec.get('intent', '')} | Scope: {', '.join(taskspec.get('scope') or [])}",
        f"Repair attempts so far: {session_state.get('repair_attempt_count', 0)}",
    ]
    if last_failure:
        lines.append(f"Last failure context: {last_failure[:800]}")
    lines.append("Suggest root cause hypotheses and minimal fix steps. No large code dumps.")
    return "\n".join(lines) + "\n"


def parse_execution_response_to_proposals(
    raw_text: str,
    fallback_paths: List[str],
) -> Tuple[List[CodeEditProposal], bool]:
    """
    Parse model output into CodeEditProposal list.
    Returns (proposals, parsed_ok). If JSON fails, returns ([], False) or a single carry-all proposal with raw text.
    """
    text = raw_text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if m:
        text = m.group(1).strip()

    def _one_raw_carryall() -> List[CodeEditProposal]:
        fp = fallback_paths[0] if fallback_paths else "unknown"
        act: Literal["edit", "add", "review_only"] = "edit"
        return [
            CodeEditProposal(
                file_path=fp,
                priority=50,
                summary="Unparsed model output (raw)",
                diff_or_patch=raw_text[:50_000],
                action_type="edit",
                nim_confidence=0.3,
                parsed_from_model=True,
                suggested_actions=[FileEditAction(path=fp, action=act, rationale="raw_fallback")],
            )
        ]

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if raw_text.strip():
            return _one_raw_carryall(), False
        return [], False

    if isinstance(data, dict):
        if "proposals" in data and isinstance(data["proposals"], list):
            data = data["proposals"]
        else:
            data = [data]

    if not isinstance(data, list):
        return _one_raw_carryall(), False

    out: List[CodeEditProposal] = []
    prio = 90
    for item in data:
        if not isinstance(item, dict):
            continue
        fp = str(item.get("file_path") or item.get("path") or "").strip()
        if not fp:
            continue
        action_t = str(item.get("action_type") or "edit").lower()
        if action_t in ("add", "create"):
            fa = "add"
            act_label = "add"
        elif action_t in ("review", "review_only"):
            fa = "review_only"
            act_label = "review_only"
        else:
            fa = "edit"
            act_label = "edit"
        summary = str(item.get("summary") or item.get("description") or "")[:2000]
        diff = item.get("diff_or_patch") or item.get("patch") or item.get("diff")
        diff_s = str(diff)[:50_000] if diff is not None else None
        try:
            conf = float(item.get("confidence")) if item.get("confidence") is not None else None
        except (TypeError, ValueError):
            conf = None
        out.append(
            CodeEditProposal(
                file_path=fp,
                priority=prio,
                summary=summary or f"{act_label} proposal",
                diff_or_patch=diff_s,
                action_type=act_label,
                nim_confidence=conf,
                parsed_from_model=True,
                suggested_actions=[
                    FileEditAction(path=fp, action=fa, rationale=summary[:500] or act_label)
                ],
            )
        )
        prio = max(10, prio - 10)

    if not out and raw_text.strip():
        return _one_raw_carryall(), False
    return out, bool(out)


# --- Providers ---------------------------------------------------------------


class BaseExecutionProvider(ABC):
    @abstractmethod
    def complete_response(self, *, model_id: str, system: str, user: str) -> ProviderResponse:
        ...

    def complete(self, *, model_id: str, system: str, user: str) -> str:
        r = self.complete_response(model_id=model_id, system=system, user=user)
        return r.raw_text if r.ok else ""


class NullExecutionProvider(BaseExecutionProvider):
    def complete_response(self, *, model_id: str, system: str, user: str) -> ProviderResponse:
        return ProviderResponse(
            model=model_id,
            ok=False,
            error_message="null_provider",
        )


class NvidiaNimExecutionProvider(BaseExecutionProvider):
    """OpenAI-compatible chat completions against NIM base URL."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def complete_response(self, *, model_id: str, system: str, user: str) -> ProviderResponse:
        req = ProviderRequest(model=model_id, system=system, user=user)
        return nim_chat_complete(self.base_url, self.api_key, req)


def get_execution_provider() -> BaseExecutionProvider:
    if nim_configured():
        return NvidiaNimExecutionProvider(
            os.environ.get(ENV_NIM_BASE_URL, "").strip(),
            os.environ.get(ENV_NIM_API_KEY, "").strip(),
        )
    return NullExecutionProvider()


LIVE_JSON_SYSTEM = """You are an expert software engineer. Output VALID JSON ONLY (no markdown outside JSON).
Return a JSON array of objects, each with keys:
- file_path (string, required)
- action_type (string: edit | add | review)
- summary (string)
- diff_or_patch (string, patch or description of change)
- confidence (number 0-1)
Do not include commentary outside the JSON array."""


# --- Main run ----------------------------------------------------------------


def run_execution_agent(inp: ExecutionAgentInput) -> ExecutionAgentOutput:
    bundle = build_execution_context_bundle(inp)
    paths = [t.path for t in bundle.targets]
    mode, selected = resolve_execution_mode(inp.taskspec, inp.decision_plan, paths, inp.repo_profile_v2)
    if mode == "no_op":
        selected = []

    model_sel = select_execution_model(
        inp.taskspec,
        inp.decision_plan,
        inp.session_state,
        target_file_count=max(1, len(selected) or len(paths)),
    )

    proposals = build_proposed_edits(mode, bundle, inp.taskspec)
    reasoning: List[str] = [
        f"execution_mode={mode}",
        f"targets_considered={len(paths)}",
        f"model={model_sel.model_id}",
    ]

    conf = 0.55
    if mode == "no_op":
        conf = 0.9
    elif mode == "single_file_edit":
        conf = 0.72
    elif mode == "staged_edit":
        conf = 0.5

    prov = ExecutionAgentProvenance(
        advisory_only=not execution_agent_writes_enabled(),
        writes_enabled_env=execution_agent_writes_enabled(),
        nim_flag=nim_configured(),
        context_cap=int(os.environ.get("GHOST_EXECUTION_CONTEXT_CAP", str(MAX_CONTEXT_CHARS_DEFAULT))),
    )

    prompt = build_execution_prompt(bundle, inp.taskspec, inp.decision_plan, inp.repo_profile_v2)

    out = ExecutionAgentOutput(
        selected_model=model_sel,
        selected_targets=selected[:12] if selected else paths[:12],
        context_bundle=bundle,
        proposed_edits=proposals,
        execution_mode=mode,
        confidence=conf,
        reasoning_lines=reasoning,
        provenance=prov,
        execution_prompt=prompt,
        live_generation_used=False,
        provider_ok=True,
        provider_error_message="",
        live_raw_proposal="",
        proposal_parsed_ok=False,
        provider_latency_ms=0.0,
        provider_response=None,
    )

    if (
        execution_agent_live_enabled()
        and not execution_agent_writes_enabled()
        and mode != "no_op"
    ):
        out.live_generation_used = True
        provider = get_execution_provider()
        user_msg = (
            prompt
            + "\n\nReturn ONLY a JSON array of edit proposals (see system schema). "
            + "Targets: "
            + ", ".join(out.selected_targets[:8])
        )
        resp = provider.complete_response(
            model_id=model_sel.model_id,
            system=LIVE_JSON_SYSTEM,
            user=user_msg,
        )
        out.provider_response = resp.model_dump(mode="json")
        out.provider_latency_ms = resp.timing.latency_ms
        if not resp.ok:
            out.provider_ok = False
            out.provider_error_message = resp.error_message or "provider_failed"
            reasoning.append(f"nim_live_failed={out.provider_error_message}")
        else:
            out.live_raw_proposal = resp.raw_text
            tgt = out.selected_targets or paths
            parsed, p_ok = parse_execution_response_to_proposals(resp.raw_text, tgt)
            out.proposal_parsed_ok = p_ok and bool(parsed)
            if parsed:
                out.proposed_edits = parsed
            else:
                reasoning.append("nim_parse_fallback_or_carryall")
            reasoning.append(f"nim_latency_ms={out.provider_latency_ms:.1f}")

    out.reasoning_lines = list(reasoning)
    return out


def execution_input_from_session(session: Any) -> Optional[ExecutionAgentInput]:
    if not contract_has_operational_spec(session):
        return None
    ts = get_task_contract_spec(session)
    if not isinstance(ts, Mapping):
        return None
    rpd = getattr(session, "repo_profile", None) or {}
    v2 = rpd.get("profile_v2") if isinstance(rpd, dict) else None
    repo_v2 = v2 if isinstance(v2, dict) else {}
    dp = get_task_contract_decision_plan(session)
    rr = getattr(session, "retrieval_result", None)
    if rr is not None and not isinstance(rr, dict):
        rr = None
    sess_state = {
        "repair_attempt_count": int(getattr(session, "repair_attempt_count", 0) or 0),
        "budget_remaining": int(getattr(session, "budget_remaining", 0) or 0),
    }
    return ExecutionAgentInput(
        taskspec=dict(ts),
        repo_profile_v2=repo_v2,
        decision_plan=dp,
        retrieval_result=rr,
        likely_edit_targets=list(getattr(session, "likely_edit_targets", None) or []),
        likely_edit_reasons=list(getattr(session, "likely_edit_target_reasons", None) or []),
        retrieval_highlight_chunks=list(getattr(session, "retrieval_highlight_chunks", None) or []),
        merged_candidate_order=list(getattr(session, "merged_candidate_order", None) or []),
        session_state=sess_state,
    )


def apply_execution_agent_to_session(session: Any, out: ExecutionAgentOutput) -> None:
    role_map = resolve_model_role_map()
    session.execution_agent_used = True
    session.execution_mode = out.execution_mode
    session.selected_execution_model = out.selected_model.model_id
    session.selected_targets = list(out.selected_targets)
    session.planner_model = role_map.get("planner", "")
    session.execution_model_resolved = role_map.get("execution", "")
    session.repair_model_resolved = role_map.get("repair", "")
    session.general_fallback_model = role_map.get("general_fallback", "")
    session.execution_fallback_model = role_map.get("execution_fallback", "")
    session.provider_backend = role_map.get("provider_backend", "openai_compatible")
    session.execution_context_summary = {
        "targets_count": len(out.context_bundle.targets),
        "chunks_count": len(out.context_bundle.chunks),
        "total_chars_approx": out.context_bundle.total_chars_approx,
        "truncated": out.context_bundle.truncated,
        "planner_paths_sample": out.context_bundle.planner_paths[:8],
    }
    session.proposed_edits = [p.model_dump(mode="json") for p in out.proposed_edits]
    session.execution_confidence = out.confidence
    session.execution_reasoning_lines = list(out.reasoning_lines)
    session.execution_agent_provenance = out.provenance.model_dump(mode="json")
    session.execution_agent_prompt = out.execution_prompt
    session.execution_agent_writes_enabled = execution_agent_writes_enabled()
    session.live_generation_used = bool(out.live_generation_used)
    session.provider_ok = bool(out.provider_ok)
    session.provider_error_message = str(out.provider_error_message or "")
    session.provider_latency_ms = float(out.provider_latency_ms or 0.0)
    session.provider_model = out.selected_model.model_id
    session.proposal_parsed = bool(out.proposal_parsed_ok)
    session.execution_raw_proposal = str(out.live_raw_proposal or "")[:100_000]
    session.provider_response_snapshot = dict(out.provider_response or {})
    if not hasattr(session, "benchmark_results"):
        session.benchmark_results = []


def run_execution_agent_for_session(session: Any) -> None:
    if not is_execution_agent_enabled():
        clear_execution_agent_fields(session)
        return
    inp = execution_input_from_session(session)
    if inp is None:
        clear_execution_agent_fields(session)
        return
    out = run_execution_agent(inp)
    apply_execution_agent_to_session(session, out)


def build_execution_agent_prompt_block(session: Any) -> str:
    if getattr(session, "micro_task_kind", None):
        return ""
    if not getattr(session, "execution_agent_used", False):
        return ""
    raw = getattr(session, "execution_agent_prompt", None) or ""
    if not isinstance(raw, str):
        raw = ""
    meta = (
        f"[EXECUTION AGENT meta] mode={getattr(session, 'execution_mode', '')} "
        f"model={getattr(session, 'selected_execution_model', '')} "
        f"backend={getattr(session, 'provider_backend', '')} "
        f"live={getattr(session, 'live_generation_used', False)} "
        f"provider_ok={getattr(session, 'provider_ok', True)} "
        f"parsed={getattr(session, 'proposal_parsed', False)} "
        f"latency_ms={getattr(session, 'provider_latency_ms', 0)} "
        f"advisory_writes_off={not getattr(session, 'execution_agent_writes_enabled', False)}\n"
    )
    body = raw.strip() + "\n" if raw.strip() else ""
    live = getattr(session, "execution_raw_proposal", "") or ""
    if live and getattr(session, "live_generation_used", False):
        snippet = live[:3500] + ("…" if len(live) > 3500 else "")
        body += f"\n[NIM draft proposal snippet]\n{snippet}\n"
    if not body.strip():
        return ""
    return meta + body


def clear_execution_agent_fields(session: Any) -> None:
    session.execution_agent_used = False
    session.execution_mode = ""
    session.selected_execution_model = ""
    session.selected_targets = []
    session.execution_context_summary = {}
    session.proposed_edits = []
    session.execution_confidence = 0.0
    session.execution_reasoning_lines = []
    session.execution_agent_provenance = {}
    session.execution_agent_prompt = ""
    session.execution_agent_writes_enabled = False
    session.planner_model = ""
    session.execution_model_resolved = ""
    session.repair_model_resolved = ""
    session.general_fallback_model = ""
    session.execution_fallback_model = ""
    # No borrar provider_backend: lo fija el CLI (gateway OpenAI) y debe persistir si el agente no corre.
    session.live_generation_used = False
    session.provider_ok = True
    session.provider_error_message = ""
    session.provider_latency_ms = 0.0
    session.provider_model = ""
    session.proposal_parsed = False
    session.execution_raw_proposal = ""
    session.provider_response_snapshot = {}
    session.benchmark_results = []


def planner_model_selection() -> ModelSelection:
    mid = os.environ.get(ENV_PLANNER_MODEL, DEFAULT_PLANNER_MODEL).strip() or DEFAULT_PLANNER_MODEL
    return ModelSelection(
        model_id=mid,
        role="planner",
        provider="nvidia_nim" if nim_configured() else "ghost_default",
        reason="GHOST_PLANNER_MODEL",
    )
