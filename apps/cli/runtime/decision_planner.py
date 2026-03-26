"""
Decision Planner v1 — deterministic core (Sprint 1).
Advisory-only: produces structured plans; runtime execution unchanged unless consumers opt in.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field

from apps.cli.runtime.exploration_planner import path_under_ui_roots
from apps.cli.runtime.task_contract import (
    contract_has_operational_spec,
    get_task_contract_decision_plan,
    get_task_contract_spec,
    sync_contract_mirrors_to_session,
)

PLANNER_VERSION = "1.0.0"
ENV_USE_DECISION_PLANNER = "GHOST_USE_DECISION_PLANNER"


def is_decision_planner_enabled() -> bool:
    v = os.environ.get(ENV_USE_DECISION_PLANNER, "0").strip().lower()
    return v in ("1", "true", "yes", "on")


# --- Pydantic models ---


class RankedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    kind: Literal["file", "folder"]
    reason: str
    score: float = Field(ge=0.0, le=1.0)
    source: Literal["taskspec", "repo_profile", "heuristic", "model"]


class ExplorationAvoid(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forbidden_layers: List[str] = Field(default_factory=list)
    forbidden_roots: List[str] = Field(default_factory=list)
    forbidden_paths: List[str] = Field(default_factory=list)


class ExplorationLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_reads: int = 20
    max_bytes_per_file: int = 262_144
    max_shell_calls: int = 999999
    reserve_for_repair: int = 0


class ExplorationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "1.0.0"
    goal: str = ""
    ranked_candidates: List[RankedCandidate] = Field(default_factory=list)
    avoid: ExplorationAvoid = Field(default_factory=ExplorationAvoid)
    limits: ExplorationLimits = Field(default_factory=ExplorationLimits)


class ExecutionGuardrails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forbid_layers: List[str] = Field(default_factory=list)
    touch_only: List[str] = Field(default_factory=list)
    max_diff_lines: int = 200


class ExecutionRisk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["low", "medium", "high"] = "low"
    reasons: List[str] = Field(default_factory=list)
    score: int = 0


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "1.0.0"
    execution_strategy: Literal["no_op", "direct_edit", "staged_edit", "refactor_first"] = "direct_edit"
    expected_files_changed: List[str] = Field(default_factory=list)
    guardrails: ExecutionGuardrails = Field(default_factory=ExecutionGuardrails)
    blast_radius: Literal["file", "module", "system"] = "file"
    risk: ExecutionRisk = Field(default_factory=ExecutionRisk)
    estimated_complexity: Literal["low", "medium", "high"] = "low"


CheckName = Literal["typecheck", "lint", "build", "tests"]
CostTier = Literal["cheap", "standard", "expensive"]
CmdSource = Literal["agents_md", "package_json", "defaults", "repo_profile"]


class VerificationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: CheckName
    command: str
    source: CmdSource
    cost: CostTier
    gate: bool
    reason: str


class VerificationPolicyBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rerun_failed_first: bool = True
    no_custom_pipes: bool = True
    must_be_truthful: bool = True


class VerificationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "1.0.0"
    required: bool = True
    strategy: Literal["cheap_first", "targeted", "full"] = "cheap_first"
    steps: List[VerificationStep] = Field(default_factory=list)
    policy: VerificationPolicyBlock = Field(default_factory=VerificationPolicyBlock)


class LastFailedCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: str = ""
    command: str = ""
    error_fingerprint: str = ""


class RepairNextAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["patch", "adjust_plan", "request_info"]
    reason: str


class RepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "1.0.0"
    max_attempts: int = 1
    attempts_used: int = 0
    repair_on: List[str] = Field(default_factory=lambda: ["typecheck", "build", "tests"])
    stop_on: List[str] = Field(
        default_factory=lambda: ["budget_exhausted", "policy_block", "permission_denied"]
    )
    rerun_failed_first: bool = True
    last_failed_check: Optional[LastFailedCheck] = None
    next_actions: List[RepairNextAction] = Field(default_factory=list)


class PlannerProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planner_version: str = PLANNER_VERSION
    advisory_mode: bool = True
    rules_applied: List[str] = Field(default_factory=list)
    generated_at: str = ""
    notes: str = ""


class PlannerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    taskspec: Dict[str, Any] = Field(default_factory=dict)
    repo_profile_v2: Dict[str, Any] = Field(default_factory=dict)
    session_state: Dict[str, Any] = Field(default_factory=dict)


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exploration_plan: ExplorationPlan
    execution_plan: ExecutionPlan
    verification_plan: VerificationPlan
    repair_plan: RepairPlan
    provenance: PlannerProvenance


# --- Helpers ---


def _norm_path(p: str) -> str:
    """Normalize path: forward slashes, collapse //, trim ./."""
    s = p.replace("\\", "/").strip().strip("./")
    while "//" in s:
        s = s.replace("//", "/")
    return s


def _is_doc_file(path: str) -> bool:
    """True if path is a doc file (excluded from expected_files for implementation tasks)."""
    pl = path.replace("\\", "/").lower()
    if pl in ("agents.md", "readme.md", "contributing.md", "changelog.md"):
        return True
    if pl.startswith("docs/"):
        return True
    return False


def _dedupe_norm_paths(paths: List[str], *, limit: int = 6) -> List[str]:
    """Normalize, drop empty, dedupe case-insensitively, preserve order."""
    out: List[str] = []
    seen: Set[str] = set()
    for raw in paths:
        n = _norm_path(raw)
        if not n:
            continue
        key = n.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
        if len(out) >= limit:
            break
    return out


def _ts_scope_set(ts: Dict[str, Any]) -> Set[str]:
    s = ts.get("scope") or []
    return set(s) if isinstance(s, list) else set()


def _budget_policy(ts: Dict[str, Any]) -> Dict[str, Any]:
    bp = ts.get("budget_policy") or {}
    return bp if isinstance(bp, dict) else {}


def _verification_policy(ts: Dict[str, Any]) -> Dict[str, Any]:
    vp = ts.get("verification_policy") or {}
    return vp if isinstance(vp, dict) else {}


def _repair_policy(ts: Dict[str, Any]) -> Dict[str, Any]:
    rp = ts.get("repair_policy") or {}
    return rp if isinstance(rp, dict) else {}


def _vcmds(repo: Dict[str, Any]) -> Dict[str, Any]:
    vc = repo.get("verification_commands") or {}
    return vc if isinstance(vc, dict) else {}


def _infer_cmd_source(vc: Dict[str, Any]) -> CmdSource:
    src = vc.get("source")
    if isinstance(src, list):
        joined = " ".join(str(x).lower() for x in src)
        if "agents" in joined:
            return "agents_md"
        if "package" in joined:
            return "package_json"
    return "repo_profile"


def _coerce_cmd_source(x: Any, default: CmdSource) -> CmdSource:
    if x in ("agents_md", "package_json", "defaults", "repo_profile"):
        return x  # type: ignore[return-value]
    return default


def normalize_verification_steps(
    raw_steps: List[Dict[str, Any]],
    *,
    default_source: CmdSource = "repo_profile",
) -> List[VerificationStep]:
    """Drop unsafe commands (pipes/chaining); assign defaults."""
    out: List[VerificationStep] = []
    order = {"typecheck": 0, "lint": 1, "build": 2, "tests": 3}
    for s in raw_steps:
        cmd = str(s.get("command") or "").strip()
        if not cmd or "|" in cmd or ";" in cmd:
            continue
        chk = s.get("check")
        if chk not in ("typecheck", "lint", "build", "tests"):
            continue
        cost = s.get("cost") or "standard"
        if cost not in ("cheap", "standard", "expensive"):
            cost = "standard"
        out.append(
            VerificationStep(
                check=chk,
                command=cmd,
                source=_coerce_cmd_source(s.get("source"), default_source),
                cost=cost,  # type: ignore[arg-type]
                gate=bool(s.get("gate", False)),
                reason=str(s.get("reason") or ""),
            )
        )
    out.sort(key=lambda x: (order.get(x.check, 9), x.check))
    return out


def make_session_state_for_planner(
    *,
    budget_remaining: int,
    last_verification: Optional[Dict[str, Any]] = None,
    repair_attempt_count: int = 0,
    changed_files: Optional[List[str]] = None,
    tool_usage_counters: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    return {
        "budget_remaining": int(budget_remaining),
        "tool_usage_counters": dict(tool_usage_counters or {"read_file": 0, "ls": 0, "run_shell": 0}),
        "last_verification": dict(last_verification or {}),
        "repair_attempt_count": int(repair_attempt_count),
        "changed_files": list(changed_files or []),
    }


def build_exploration_plan(
    taskspec: Dict[str, Any],
    repo_profile_v2: Dict[str, Any],
    session_state: Dict[str, Any],
) -> ExplorationPlan:
    ts = taskspec or {}
    repo = repo_profile_v2 if isinstance(repo_profile_v2, dict) else {}
    bp = _budget_policy(ts)
    forbidden = list(ts.get("forbidden_layers") or [])
    scope = _ts_scope_set(ts)
    intent = str(ts.get("intent") or "").lower()

    avoid = ExplorationAvoid(
        forbidden_layers=list(forbidden),
        forbidden_roots=[],
        forbidden_paths=[],
    )
    budget_rem = int(session_state.get("budget_remaining") or 100)
    max_reads = int(bp.get("max_reads") or 20)
    max_shell = int(bp.get("max_shell_calls") or 999999)
    reserve = int(bp.get("reserve_for_repair") or 0)
    if budget_rem < 30:
        max_reads = min(max_reads, 8)
    limits = ExplorationLimits(
        max_reads=max_reads,
        max_bytes_per_file=int(bp.get("max_bytes_per_file") or 262_144),
        max_shell_calls=max_shell,
        reserve_for_repair=reserve,
    )

    ui_roots: List[str] = []
    if repo.get("entrypoints"):
        ep = repo["entrypoints"] or {}
        if isinstance(ep, dict):
            ui_roots = [str(x) for x in (ep.get("ui_roots") or []) if x]
            if "ui" in forbidden:
                avoid.forbidden_roots = list(dict.fromkeys(avoid.forbidden_roots + ui_roots))

    best: Dict[str, RankedCandidate] = {}

    def add_candidate(c: RankedCandidate) -> None:
        p = _norm_path(c.path)
        if not p:
            return
        if "ui" in forbidden and c.kind == "file" and path_under_ui_roots(p, ui_roots):
            return
        if "ui" in forbidden and c.kind == "folder" and path_under_ui_roots(p + "/x", ui_roots):
            return
        key = f"{c.kind}:{p.lower()}"
        if key not in best or c.score > best[key].score:
            best[key] = RankedCandidate(
                path=p.replace("\\", "/"),
                kind=c.kind,
                reason=c.reason,
                score=c.score,
                source=c.source,
            )

    for f in ts.get("target_files") or []:
        if isinstance(f, str) and f.strip():
            add_candidate(
                RankedCandidate(
                    path=f,
                    kind="file",
                    reason="TaskSpec target_files",
                    score=1.0,
                    source="taskspec",
                )
            )

    api_data = bool(scope & {"api", "data"}) or intent in (
        "implementation",
        "modification",
        "bugfix",
        "refactor",
        "verification",
    )

    if api_data or scope & {"api", "data"}:
        ep_block = repo.get("entrypoints") or {}
        if isinstance(ep_block, dict):
            for d in (ep_block.get("api_roots") or [])[:6]:
                if isinstance(d, str) and d.strip():
                    add_candidate(
                        RankedCandidate(
                            path=d.strip().rstrip("/"),
                            kind="folder",
                            reason="RepoProfile api_roots (list before deep read)",
                            score=0.55,
                            source="repo_profile",
                        )
                    )
        for r in (repo.get("api_routes") or [])[:25]:
            if not isinstance(r, dict):
                continue
            f = r.get("file")
            if not f:
                continue
            pth = r.get("path") or ""
            meth = r.get("method") or "?"
            add_candidate(
                RankedCandidate(
                    path=str(f),
                    kind="file",
                    reason=f"RepoProfile api route ({meth} {pth})",
                    score=0.92,
                    source="repo_profile",
                )
            )

        for vf in repo.get("validation_files") or []:
            if isinstance(vf, str) and vf.strip():
                add_candidate(
                    RankedCandidate(
                        path=vf,
                        kind="file",
                        reason="RepoProfile validation layer",
                        score=0.88,
                        source="repo_profile",
                    )
                )

        for sf in repo.get("db_schema_files") or []:
            if isinstance(sf, str) and sf.strip():
                add_candidate(
                    RankedCandidate(
                        path=sf,
                        kind="file",
                        reason="RepoProfile DB schema file",
                        score=0.86,
                        source="repo_profile",
                    )
                )

    for kf in (repo.get("key_files") or [])[:12]:
        if isinstance(kf, str) and kf.strip():
            add_candidate(
                RankedCandidate(
                    path=kf,
                    kind="file",
                    reason="RepoProfile key_files",
                    score=0.72,
                    source="repo_profile",
                )
            )

    explicit_targets = [str(x).strip() for x in (ts.get("target_files") or []) if isinstance(x, str) and str(x).strip()]
    skip_heuristic_fallback = len(explicit_targets) > 0 and len(explicit_targets) <= 2
    if (
        not skip_heuristic_fallback
        and len([k for k in best if k.startswith("file:")]) < 3
        and not repo.get("key_files")
    ):
        for fb in ("tsconfig.json", "package.json", "pyproject.toml"):
            add_candidate(
                RankedCandidate(
                    path=fb,
                    kind="file",
                    reason="Fallback generic project marker (no rich RepoProfile key_files)",
                    score=0.35,
                    source="heuristic",
                )
            )

    merged = list(best.values())
    merged.sort(key=lambda x: (-x.score, 0 if x.kind == "file" else 1, x.path.lower()))
    merged = merged[: limits.max_reads]

    goal = ts.get("intent") or "explore_and_implement"
    return ExplorationPlan(
        version="1.0.0",
        goal=str(goal),
        ranked_candidates=merged,
        avoid=avoid,
        limits=limits,
    )


def compute_risk_score(
    taskspec: Dict[str, Any],
    repo_profile_v2: Dict[str, Any],
    exploration_plan: ExplorationPlan,
    execution_plan: ExecutionPlan,
) -> Dict[str, Any]:
    ts = taskspec or {}
    paths = list(execution_plan.expected_files_changed) or [
        c.path for c in exploration_plan.ranked_candidates if c.kind == "file"
    ][:8]
    score = 0
    reasons: List[str] = []

    auth_re = re.compile(
        r"(auth|billing|middleware|security|password|jwt|stripe|payment)",
        re.I,
    )
    infra_re = re.compile(
        r"(docker|kubernetes|terraform|\.github/workflows|nginx|infra)",
        re.I,
    )
    config_markers = (
        "package.json",
        "pyproject.toml",
        "next.config",
        "vite.config",
        "webpack",
        "turbo.json",
    )
    schema_markers = ("schema", "drizzle", "prisma", "migrations", "/db/")

    for p in paths:
        pl = p.lower().replace("\\", "/")
        if auth_re.search(pl):
            score += 3
            reasons.append(f"auth/billing/security path: {p}")
        elif infra_re.search(pl):
            score += 3
            reasons.append(f"infra path: {p}")
        elif any(m in pl for m in config_markers):
            score += 2
            reasons.append(f"config/build manifest: {p}")
        elif any(m in pl for m in schema_markers):
            score += 2
            reasons.append(f"DB schema path: {p}")
        elif "/api/" in pl or "route.ts" in pl or "routes/" in pl:
            score += 1
            reasons.append(f"API route file: {p}")

    if len(paths) > 2:
        score += 1
        reasons.append("multiple expected files (+1)")

    if score <= 2:
        level: Literal["low", "medium", "high"] = "low"
    elif score <= 5:
        level = "medium"
    else:
        level = "high"

    return {"score": score, "level": level, "reasons": reasons}


def build_execution_plan(
    taskspec: Dict[str, Any],
    repo_profile_v2: Dict[str, Any],
    exploration_plan: ExplorationPlan,
    session_state: Dict[str, Any],
) -> ExecutionPlan:
    ts = taskspec or {}
    bp = _budget_policy(ts)
    scope_list = list(ts.get("scope") or []) if isinstance(ts.get("scope"), list) else []
    forbidden = list(ts.get("forbidden_layers") or [])
    change_exp = str(ts.get("change_expectation") or "may_write")
    intent = str(ts.get("intent") or "").lower()

    max_diff = 200
    try:
        max_diff = int(bp.get("max_diff_lines") or 200)
    except (TypeError, ValueError):
        pass

    touch_only = [s for s in scope_list if s in ("api", "data", "ui", "infra", "config", "tests")]
    if not touch_only and scope_list:
        touch_only = scope_list[:]

    all_files = [c.path for c in exploration_plan.ranked_candidates if c.kind == "file"]
    if intent in ("implementation", "modification", "bugfix", "refactor"):
        all_files = [f for f in all_files if not _is_doc_file(_norm_path(f) or f)]
    files = _dedupe_norm_paths(all_files, limit=6)
    draft = ExecutionPlan(
        execution_strategy="direct_edit",
        expected_files_changed=files,
        guardrails=ExecutionGuardrails(
            forbid_layers=forbidden,
            touch_only=touch_only or ["api", "data", "ui", "infra"],
            max_diff_lines=max_diff,
        ),
    )

    if change_exp == "should_not_write":
        return ExecutionPlan(
            execution_strategy="no_op",
            expected_files_changed=[],
            guardrails=draft.guardrails,
            blast_radius="file",
            risk=ExecutionRisk(level="low", reasons=["change_expectation is should_not_write"], score=0),
            estimated_complexity="low",
        )

    risk_data = compute_risk_score(ts, repo_profile_v2, exploration_plan, draft)
    rs = int(risk_data["score"])
    lvl = risk_data["level"]
    nfiles = len(files)

    if intent == "refactor":
        blast_rf: Literal["file", "module", "system"] = "module"
        if lvl == "high" or any("package.json" in f for f in files):
            blast_rf = "system"
        return ExecutionPlan(
            execution_strategy="refactor_first",
            expected_files_changed=files,
            guardrails=draft.guardrails,
            blast_radius=blast_rf,
            risk=ExecutionRisk(
                level=lvl,
                reasons=list(risk_data["reasons"]) + ["intent is refactor"],
                score=rs,
            ),
            estimated_complexity="medium" if nfiles <= 3 else "high",
        )

    strategy_es: Literal["no_op", "direct_edit", "staged_edit", "refactor_first"] = "direct_edit"
    if lvl == "high" or nfiles > 2 or rs >= 4:
        strategy_es = "staged_edit"
    else:
        strategy_es = "direct_edit"

    blast: Literal["file", "module", "system"] = "file"
    if lvl == "high" or any("package.json" in f for f in files):
        blast = "system"
    elif lvl == "medium" or nfiles > 1:
        blast = "module"

    complexity: Literal["low", "medium", "high"] = "low"
    if lvl == "high" or nfiles > 3:
        complexity = "high"
    elif lvl == "medium" or nfiles > 1:
        complexity = "medium"

    return ExecutionPlan(
        execution_strategy=strategy_es,
        expected_files_changed=files,
        guardrails=draft.guardrails,
        blast_radius=blast,
        risk=ExecutionRisk(
            level=lvl,
            reasons=risk_data["reasons"],
            score=rs,
        ),
        estimated_complexity=complexity,
    )


def build_verification_plan(
    taskspec: Dict[str, Any],
    repo_profile_v2: Dict[str, Any],
    execution_plan: ExecutionPlan,
    session_state: Dict[str, Any],
) -> VerificationPlan:
    ts = taskspec or {}
    repo = repo_profile_v2 if isinstance(repo_profile_v2, dict) else {}
    vp = _verification_policy(ts)
    vc = _vcmds(repo)
    src = _infer_cmd_source(vc)

    required = bool(vp.get("required", True))
    if not required:
        return VerificationPlan(
            required=False,
            strategy="targeted",
            steps=[],
            policy=VerificationPolicyBlock(),
        )

    budget_rem = int(session_state.get("budget_remaining") or 100)
    scope = _ts_scope_set(ts)
    stack = repo.get("stack") or {}
    langs = stack.get("language") if isinstance(stack, dict) else []
    lang_list = [str(x).lower() for x in (langs or [])] if isinstance(langs, list) else []
    touches_ts = any("typescript" in x or "ts" == x for x in lang_list)
    want_ts_first = bool(scope & {"api", "data"}) or touches_ts

    exp_files = execution_plan.expected_files_changed
    touches_pkg = any("package.json" in f.replace("\\", "/").lower() for f in exp_files)
    touches_next = any("next.config" in f.lower() for f in exp_files)
    blast = execution_plan.blast_radius
    risk_lvl = execution_plan.risk.level

    typecheck_cmd = (vc.get("typecheck") or "").strip()
    build_cmd = (vc.get("build") or "").strip()
    lint_cmd = (vc.get("lint") or "").strip()
    tests_cmd = (vc.get("tests") or "").strip()

    if not typecheck_cmd and want_ts_first:
        typecheck_cmd = "npx tsc --noEmit"
        src_tc: CmdSource = "defaults"
    else:
        src_tc = src if typecheck_cmd else "defaults"

    steps_raw: List[Dict[str, Any]] = []

    def add_step(
        check: CheckName,
        cmd: str,
        *,
        cost: CostTier,
        gate: bool,
        reason: str,
        cmd_source: CmdSource,
    ) -> None:
        if not cmd:
            return
        steps_raw.append(
            {
                "check": check,
                "command": cmd,
                "source": cmd_source,
                "cost": cost,
                "gate": gate,
                "reason": reason,
            }
        )

    if want_ts_first and typecheck_cmd:
        add_step(
            "typecheck",
            typecheck_cmd,
            cost="standard",
            gate=True,
            reason="TS/API/data scope: typecheck first (cheap-first)",
            cmd_source=src_tc,
        )

    want_lint = bool(vp.get("lint")) or bool(scope & {"fullstack", "ui"}) or risk_lvl == "high"
    if want_lint and lint_cmd:
        add_step(
            "lint",
            lint_cmd,
            cost="cheap",
            gate=False,
            reason="Lint: policy or scope",
            cmd_source=src,
        )

    want_tests = bool(vp.get("tests"))
    if want_tests and tests_cmd:
        add_step(
            "tests",
            tests_cmd,
            cost="expensive",
            gate=True,
            reason="TaskSpec verification_policy.tests",
            cmd_source=src,
        )

    want_build = bool(vp.get("build"))
    if not want_build:
        want_build = touches_pkg or touches_next or blast == "system" or risk_lvl == "high"
    if want_build and build_cmd:
        add_step(
            "build",
            build_cmd,
            cost="expensive",
            gate=True,
            reason="Build: package/config affected, blast_radius/system, or policy/risk",
            cmd_source=src if vc.get("build") else "defaults",
        )
    elif want_build and not build_cmd:
        add_step(
            "build",
            "npm run build",
            cost="expensive",
            gate=True,
            reason="Build required by policy/risk; default command",
            cmd_source="defaults",
        )

    if not want_ts_first and not steps_raw:
        if build_cmd:
            add_step("build", build_cmd, cost="expensive", gate=True, reason="Primary check", cmd_source=src)
        elif lint_cmd:
            add_step("lint", lint_cmd, cost="cheap", gate=False, reason="Primary check", cmd_source=src)

    steps = normalize_verification_steps(steps_raw, default_source=src)

    if budget_rem < 25 and len(steps) > 1:
        tc = next((s for s in steps if s.check == "typecheck"), None)
        steps = [tc] if tc else steps[:1]

    strategy: Literal["cheap_first", "targeted", "full"] = "cheap_first"
    if len(steps) <= 1:
        strategy = "targeted"

    return VerificationPlan(
        required=True,
        strategy=strategy,
        steps=steps,
        policy=VerificationPolicyBlock(),
    )


def _map_verification_name_to_check(name: str) -> str:
    n = (name or "").lower()
    if "type" in n:
        return "typecheck"
    if "lint" in n:
        return "lint"
    if "build" in n:
        return "build"
    if "test" in n:
        return "tests"
    return str(name or "unknown").lower()


def build_repair_plan(taskspec: Dict[str, Any], session_state: Dict[str, Any]) -> RepairPlan:
    ts = taskspec or {}
    rp = _repair_policy(ts)
    try:
        max_attempts = max(0, int(rp.get("max_attempts", 1)))
    except (TypeError, ValueError):
        max_attempts = 1

    last_v = session_state.get("last_verification") or {}
    checks = last_v.get("checks") or []
    last_failed: Optional[LastFailedCheck] = None
    for c in checks:
        if not isinstance(c, dict):
            continue
        if c.get("status") in ("failed", "error"):
            last_failed = LastFailedCheck(
                check=_map_verification_name_to_check(str(c.get("name") or "")),
                command=str(c.get("command") or c.get("command_executed") or ""),
                error_fingerprint=(str(c.get("output_summary") or c.get("stderr") or "")[:120]),
            )
            break

    next_actions: List[RepairNextAction] = []
    if last_failed and last_failed.check:
        next_actions.append(
            RepairNextAction(
                action="patch",
                reason=f"Address last failed check ({last_failed.check}) then re-verify.",
            )
        )

    attempts_used = int(session_state.get("repair_attempt_count") or 0)

    return RepairPlan(
        max_attempts=max_attempts or 1,
        attempts_used=attempts_used,
        rerun_failed_first=True,
        last_failed_check=last_failed,
        next_actions=next_actions,
    )


def build_plans(inp: PlannerInput) -> PlannerOutput:
    ts = dict(inp.taskspec or {})
    repo = dict(inp.repo_profile_v2 or {})
    sess = dict(inp.session_state or {})

    rules: List[str] = []

    exploration = build_exploration_plan(ts, repo, sess)
    rules.append("exploration_rank_v1")

    execution = build_execution_plan(ts, repo, exploration, sess)
    rules.append("execution_strategy_v1")

    verification = build_verification_plan(ts, repo, execution, sess)
    rules.append("verification_cheap_first_v1")

    repair = build_repair_plan(ts, sess)
    rules.append("repair_policy_v1")

    provenance = PlannerProvenance(
        planner_version=PLANNER_VERSION,
        advisory_mode=True,
        rules_applied=rules,
        generated_at=datetime.now(timezone.utc).isoformat(),
        notes="Sprint 1 deterministic planner; advisory only.",
    )

    return PlannerOutput(
        exploration_plan=exploration,
        execution_plan=execution,
        verification_plan=verification,
        repair_plan=repair,
        provenance=provenance,
    )


def planner_output_to_prompt_block(output: PlannerOutput) -> str:
    """Human-readable advisory blocks (no Pydantic repr)."""
    lines: List[str] = []
    ex = output.exploration_plan
    ep = output.execution_plan
    vp = output.verification_plan
    rp = output.repair_plan
    prov = output.provenance

    lines.append("[DECISION PLANNER — ADVISORY]")
    lines.append(f"Planner version: {prov.planner_version} | Mode: advisory (non-binding)")
    lines.append(
        f"Execution strategy: {ep.execution_strategy} | Risk: {ep.risk.level} (score={ep.risk.score}) | "
        f"Blast radius: {ep.blast_radius} | Complexity: {ep.estimated_complexity}"
    )
    if ep.expected_files_changed:
        lines.append("Expected files changed (hint):")
        for p in ep.expected_files_changed[:12]:
            lines.append(f"  - {p}")
    else:
        lines.append("Expected files changed: (none — e.g. no_op or TBD)")
    lines.append("")

    lines.append("[EXPLORATION PLAN]")
    top = [c for c in ex.ranked_candidates if c.kind == "file"][:15]
    if not top:
        lines.append("(no ranked file candidates)")
    for c in top:
        lines.append(f"- {c.path} :: {c.reason} :: score={c.score:.2f} :: {c.source}")
    if ex.avoid.forbidden_layers:
        lines.append(f"Avoid layers: {', '.join(ex.avoid.forbidden_layers)}")
    lines.append(f"Limits: max_reads={ex.limits.max_reads} max_shell_calls={ex.limits.max_shell_calls}")
    lines.append("")

    lines.append("[VERIFICATION STRATEGY]")
    lines.append(f"Required: {vp.required} | Strategy: {vp.strategy}")
    if not vp.steps:
        lines.append("(no steps — verification not required or no commands)")
    for s in vp.steps:
        lines.append(
            f"- {s.check} :: {s.command} :: gate={s.gate} :: cost={s.cost} :: source={s.source} :: {s.reason}"
        )
    lines.append("")

    lines.append("[REPAIR STRATEGY]")
    lines.append(f"Max attempts: {rp.max_attempts} | Attempts used: {rp.attempts_used} | Rerun failed first: {rp.rerun_failed_first}")
    lines.append("Stop conditions:")
    for s in rp.stop_on:
        lines.append(f"  - {s}")
    if rp.last_failed_check:
        lf = rp.last_failed_check
        lines.append(f"Last failed check: {lf.check} | cmd={lf.command[:80]}")
    if rp.next_actions:
        lines.append("Suggested next actions:")
        for a in rp.next_actions:
            lines.append(f"  - {a.action}: {a.reason}")
    lines.append("")

    return "\n".join(lines) + "\n"


def build_decision_planner_prompt_block(session: Any) -> str:
    """Advisory prompt text from TaskContract.decision_plan (empty if unused)."""
    if getattr(session, "micro_task_kind", None):
        return ""
    if not getattr(session, "planner_used", False):
        return ""
    raw = get_task_contract_decision_plan(session)
    if not isinstance(raw, dict) or not raw:
        return ""
    try:
        out = PlannerOutput.model_validate(raw)
    except Exception:
        return ""
    return planner_output_to_prompt_block(out)


def _set_decision_plan_on_contract(tc: Any, dumped: Dict[str, Any]) -> None:
    ra = tc.get("runtime_annotations")
    if not isinstance(ra, dict):
        ra = {"decision_plan": {}}
        tc["runtime_annotations"] = ra
    ra["decision_plan"] = dumped


def _clear_decision_plan_on_contract(tc: Any) -> None:
    ra = tc.get("runtime_annotations")
    if isinstance(ra, dict):
        ra["decision_plan"] = {}


def apply_decision_planner_to_session(session: Any, *, budget_remaining: int) -> None:
    """
    Populate task_contract runtime_annotations['decision_plan'] from spec + profile_v2.
    No-op if disabled or no spec.
    """
    if not is_decision_planner_enabled():
        session.planner_used = False
        tc = getattr(session, "task_contract", None)
        if isinstance(tc, dict):
            _clear_decision_plan_on_contract(tc)
        sync_contract_mirrors_to_session(session)
        return

    if not contract_has_operational_spec(session):
        session.planner_used = False
        session.planner_version = PLANNER_VERSION
        session.planner_advisory_mode = True
        session.planner_risk_level = ""
        session.planner_estimated_complexity = ""
        session.planner_provenance = {"skipped": "no_contract_spec"}
        tc = getattr(session, "task_contract", None)
        if isinstance(tc, dict):
            _clear_decision_plan_on_contract(tc)
        sync_contract_mirrors_to_session(session)
        return

    ts = get_task_contract_spec(session)
    if ts is None or not isinstance(ts, Mapping):
        session.planner_used = False
        tc = getattr(session, "task_contract", None)
        if isinstance(tc, dict):
            _clear_decision_plan_on_contract(tc)
        sync_contract_mirrors_to_session(session)
        return

    rpd = getattr(session, "repo_profile", None) or {}
    v2 = rpd.get("profile_v2") if isinstance(rpd, dict) else None
    repo_v2 = v2 if isinstance(v2, dict) else {}

    sess_state = make_session_state_for_planner(
        budget_remaining=budget_remaining,
        last_verification=getattr(session, "verification", None) or {},
        repair_attempt_count=int(getattr(session, "repair_attempt_count", 0) or 0),
        changed_files=[
            d.get("file")
            for d in (getattr(session, "diff_summary", None) or [])
            if isinstance(d, dict) and d.get("file")
        ],
    )

    inp = PlannerInput(taskspec=dict(ts), repo_profile_v2=repo_v2, session_state=sess_state)
    out = build_plans(inp)
    dumped = out.model_dump(mode="json")
    tc = getattr(session, "task_contract", None)
    if not isinstance(tc, dict):
        session.planner_used = False
        sync_contract_mirrors_to_session(session)
        return
    _set_decision_plan_on_contract(tc, dumped)
    tc["risk_level"] = out.execution_plan.risk.level
    tc["blast_radius"] = str(out.execution_plan.blast_radius)
    session.planner_used = True
    session.planner_version = PLANNER_VERSION
    session.planner_advisory_mode = True
    session.planner_risk_level = out.execution_plan.risk.level
    session.planner_estimated_complexity = out.execution_plan.estimated_complexity
    session.planner_provenance = out.provenance.model_dump(mode="json")
    sync_contract_mirrors_to_session(session)
