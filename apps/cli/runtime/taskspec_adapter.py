"""
Spec engine (``taskspec_engine``) → ``ArtifactSession`` + ``task_contract`` (operational path).

Module name and engine types remain ``taskspec_*`` for historical imports; runtime storage is
contract-native (``task_contract["spec"]``, ``contract_spec_*`` session fields).

Legacy text-only fill lives in ``apps.cli.runtime.adapters.legacy_intake``.
"""
from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple

from apps.cli.runtime.exploration_planner import (
    ExplorationPlan,
    build_exploration_plan,
    exploration_plan_to_prompt_block,
)
from apps.cli.runtime.execution_agent import clear_execution_agent_fields
from apps.cli.runtime.repo_retrieval import (
    clear_retrieval_session_fields,
    merged_candidate_order_to_prompt_block,
)
from apps.cli.runtime.repo_profile import RepoProfile
from apps.cli.runtime.repo_profile_v2 import PROFILE_VERSION
from apps.cli.runtime.task_contract import (
    RUNTIME_CONTRACT_SOURCE_SPEC_ENGINE_LEGACY,
    get_task_contract_spec,
    update_task_contract_after_spec_apply,
)
from apps.cli.runtime.adapters.legacy_intake import (
    is_taskspec_engine_enabled as is_taskspec_primary,
    legacy_fill_session,
)
from apps.cli.runtime.taskspec_engine import TaskSpec, TaskSpecBuildResult


def taskspec_to_dict(ts: TaskSpec) -> Dict[str, Any]:
    """JSON-serializable snapshot for session / artifacts."""
    return ts.model_dump(mode="json")


def primary_scope_string(scope_list: List[str]) -> str:
    """
    Single scope string for APIs that still expect one value (e.g. choose_verification_plan).
    Prefers api > data > ui > first entry > unknown.
    """
    if not scope_list:
        return "unknown"
    s = set(scope_list)
    for pref in ("api", "data", "ui", "infra"):
        if pref in s:
            return pref
    if "fullstack" in s:
        return "fullstack"
    return scope_list[0]


def apply_spec_engine_build_to_session(
    session: Any,
    build: TaskSpecBuildResult,
    repo_profile: RepoProfile,
    user_prompt: str = "",
) -> None:
    """Persist spec-engine output onto session + ``task_contract`` (operational spec)."""
    ts = build.taskspec
    ts_dict = taskspec_to_dict(ts)
    session.contract_spec_validation_status = build.validation_status
    session.contract_spec_repaired = build.repaired
    session.contract_spec_validation_errors = list(build.validation_errors)
    session.repo_profile = repo_profile_to_dict(repo_profile)
    session.runtime_contract_source = RUNTIME_CONTRACT_SOURCE_SPEC_ENGINE_LEGACY
    session.task_intent = ts.intent
    session.task_scope = primary_scope_string(ts.scope)
    session.change_expectation = ts.change_expectation
    session.intent_confidence = ts.confidence
    session.intent_reasoning_lines = list(ts.reasoning_lines)

    session.exploration_plan = {}
    session.repo_profile_influence = []
    v2 = getattr(repo_profile, "v2", None)
    if v2 is not None:
        v2d = v2.model_dump(mode="json")
        exp = build_exploration_plan(ts_dict, v2d, user_prompt or "")
        session.exploration_plan = {
            "ranked_files": exp.ranked_files,
            "ranked_dirs": exp.ranked_dirs,
            "ui_exploration_blocked": exp.ui_exploration_blocked,
            "evidence_lines": exp.evidence_lines,
        }
        session.repo_profile_influence = list(exp.evidence_lines)
        for line in exp.evidence_lines[:25]:
            session.events.append(
                {
                    "event": "RepoProfileInfluence",
                    "timestamp": time.time(),
                    "details": line,
                }
            )
    update_task_contract_after_spec_apply(session, ts_dict)


# Deprecated public name (kept for callers/tests).
apply_taskspec_build_to_session = apply_spec_engine_build_to_session


def repo_profile_to_dict(rp: RepoProfile) -> Dict[str, Any]:
    root = str(rp.root) if rp.root else ""
    base: Dict[str, Any] = {
        "root": root,
        "stack": rp.stack,
        "layers_detected": list(rp.layers_detected),
        "has_package_json": rp.has_package_json,
        "important_folders": list(rp.important_folders),
    }
    if getattr(rp, "v2", None) is not None:
        v2 = rp.v2
        full = v2.model_dump(mode="json")
        base["version"] = full.get("version", PROFILE_VERSION)
        base["confidence"] = full.get("confidence", 0.0)
        base["stack_detail"] = full.get("stack")
        base["entrypoints"] = full.get("entrypoints")
        base["key_files"] = full.get("key_files", [])
        base["api_routes"] = full.get("api_routes", [])
        base["db_schema_files"] = full.get("db_schema_files", [])
        base["validation_files"] = full.get("validation_files", [])
        base["verification_commands"] = full.get("verification_commands")
        base["evidence_lines"] = full.get("evidence_lines", [])
        base["cache"] = full.get("cache")
        base["profile_v2"] = full
    return base


def build_contract_prompt_blocks(session: Any) -> Tuple[str, str, str]:
    """
    Deterministic markdown-style blocks for system prompt injection.
    Returns (contract_spec_block, repo_profile_block, exploration_block).
    """
    ts = get_task_contract_spec(session)
    if not isinstance(ts, Mapping) or not ts:
        return "", "", ""

    forbidden = ts.get("forbidden_layers") or []
    ac = ts.get("acceptance_criteria") or []
    vp = ts.get("verification_policy") or {}
    rp_ = ts.get("repair_policy") or {}
    bp = ts.get("budget_policy") or {}

    def _lines(prefix: str, d: Dict[str, Any]) -> str:
        parts = []
        for k, v in sorted(d.items()):
            parts.append(f"- {k}: {v}")
        return "\n".join(parts) if parts else "- (defaults)"

    ac_lines = "\n".join(f"- {c}" for c in ac[:20]) if ac else "- (see user task)"

    ts_block = f"""[OPERATIONAL SPEC / task_contract.spec]
Intent: {ts.get("intent", "")}
Scope: {", ".join(ts.get("scope") or []) or "[]"}
Forbidden layers: {", ".join(forbidden) if forbidden else "(none)"}
Change expectation: {ts.get("change_expectation", "")}
Target files: {", ".join(ts.get("target_files") or []) or "(none specified)"}
Confidence: {float(ts.get("confidence") or 0):.0%}
Acceptance criteria:
{ac_lines}
Verification policy:
{_lines("", vp)}
Repair policy:
{_lines("", rp_)}
Budget policy:
{_lines("", bp)}
Validation: {getattr(session, "contract_spec_validation_status", "")} | repaired={getattr(session, "contract_spec_repaired", False)}
"""

    rpd = getattr(session, "repo_profile", None) or {}
    if not isinstance(rpd, dict):
        rpd = {}
    layers = ", ".join(rpd.get("layers_detected") or []) or "(none)"
    folders = ", ".join(rpd.get("important_folders") or []) or "(none)"
    signals = []
    if rpd.get("has_package_json"):
        signals.append("package.json")
    if rpd.get("stack"):
        signals.append(f"stack={rpd.get('stack')}")
    sig = ", ".join(signals) or "(minimal)"
    sd = rpd.get("stack_detail") or {}
    fw = ", ".join(sd.get("framework") or []) if isinstance(sd, dict) else ""
    lang = ", ".join(sd.get("language") or []) if isinstance(sd, dict) else ""
    vc = rpd.get("verification_commands") or {}
    vc_line = ""
    if isinstance(vc, dict):
        vc_line = (
            f"typecheck={vc.get('typecheck')!r} build={vc.get('build')!r} "
            f"lint={vc.get('lint')!r} tests={vc.get('tests')!r}"
        )
    conf = rpd.get("confidence")
    conf_s = f"{float(conf):.0%}" if isinstance(conf, (int, float)) else ""

    repo_block = f"""[CURRENT REPO PROFILE]
Stack: {rpd.get("stack", "unknown")} | Framework: {fw or "—"} | Languages: {lang or "—"}
Detected layers: {layers}
Important folders: {folders}
Root: {rpd.get("root", "")}
Key signals: {sig}
Confidence: {conf_s or "—"}
Verification commands: {vc_line or "—"}
"""
    exp_block = ""
    ep = getattr(session, "exploration_plan", None) or {}
    mode = str(getattr(session, "retrieval_mode", "") or "")
    merged = getattr(session, "merged_candidate_order", None) or []
    if (
        getattr(session, "retrieval_enabled", False)
        and mode == "influencing"
        and merged
    ):
        exp_block = merged_candidate_order_to_prompt_block(
            merged,
            ranked_dirs=list(ep.get("ranked_dirs") or []),
            ui_exploration_blocked=bool(ep.get("ui_exploration_blocked")),
            evidence_lines=list(ep.get("evidence_lines") or []),
        )
    elif ep:
        p = ExplorationPlan(
            ranked_files=list(ep.get("ranked_files") or []),
            ranked_dirs=list(ep.get("ranked_dirs") or []),
            evidence_lines=list(ep.get("evidence_lines") or []),
            ui_exploration_blocked=bool(ep.get("ui_exploration_blocked")),
        )
        exp_block = exploration_plan_to_prompt_block(p)
    return ts_block.strip() + "\n", repo_block.strip() + "\n", exp_block


def path_matches_forbidden_layer(norm_path: str, layer: str) -> bool:
    """
    Heuristic: map path segments to layers for write blocking.
    norm_path uses forward slashes, relative to repo.
    """
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
        return "/api/" in p or "/routes/" in p or "apps/server" in p or "/server/" in p
    if layer == "data":
        return "schema" in p or "drizzle" in p or "prisma" in p or "/db/" in p or "migrations" in p
    return False


def write_blocked_by_contract_spec(norm_path: str, contract_spec: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Returns (blocked, reason) using ``task_contract['spec']``-shaped dict."""
    if not contract_spec:
        return False, ""
    forbidden = contract_spec.get("forbidden_layers") or []
    for layer in forbidden:
        if path_matches_forbidden_layer(norm_path, layer):
            return True, f"Contract spec forbids writes to {layer} layer (path matches {layer})"
    if contract_spec.get("change_expectation") == "should_not_write":
        return True, "change_expectation is should_not_write; no file writes allowed"
    return False, ""


def contract_spec_dict_from_session(session: Any) -> Optional[Dict[str, Any]]:
    """Operational spec body from ``task_contract['spec']`` (read-only helper)."""
    return get_task_contract_spec(session)


def verification_skip_respects_contract_spec(
    contract_spec: Optional[Dict[str, Any]],
    skip_manual_only: bool,
) -> bool:
    """
    When the contract requires verification, do not skip checks just because the model
    did not invoke run_shell (manual-only shortcut).
    """
    if not contract_spec:
        return skip_manual_only
    vp = contract_spec.get("verification_policy") or {}
    if not vp.get("required", False):
        return skip_manual_only
    if any(vp.get(k) for k in ("typecheck", "build", "lint", "tests")):
        return False
    return skip_manual_only


def max_repair_attempts(contract_spec: Optional[Dict[str, Any]], default: int = 1) -> int:
    if not contract_spec:
        return default
    rp = contract_spec.get("repair_policy") or {}
    try:
        return max(0, int(rp.get("max_attempts", default)))
    except (TypeError, ValueError):
        return default


def effective_max_repair_attempts(
    contract_spec: Optional[Dict[str, Any]],
    *,
    failed_checks_blob: str = "",
    extra_causal_attempt: bool = False,
    default: int = 1,
) -> int:
    """
    When contract allows only one repair but failure is structural (parse/import/indent),
    allow one extra causal attempt so a bad first patch can be corrected without widening scope.

    When ``extra_causal_attempt`` (e.g. repair applied edits and causal digest shifted but verify
    still failing), allow one more controlled attempt without opening scope.
    """
    base = max_repair_attempts(contract_spec, default=default)
    if base < 1:
        return base
    try:
        cap = int(os.environ.get("GHOST_REPAIR_ATTEMPT_CAP", "4"))
    except (TypeError, ValueError):
        cap = 4
    cap = max(2, min(8, cap))
    effective = base
    if base < 2:
        blob = (failed_checks_blob or "").lower()
        markers = (
            "indentationerror",
            "syntaxerror",
            "importerror",
            "modulenotfounderror",
            "attributeerror",
            "unexpected indent",
        )
        if any(m in blob for m in markers):
            effective = 2
    if extra_causal_attempt:
        effective += 1
    # Never below contract base; cap adaptive bumps without undercutting explicit max_attempts
    return max(base, min(effective, max(cap, base)))


# --- Deprecated public aliases (same behavior; prefer contract_spec names). ---
write_blocked_by_taskspec = write_blocked_by_contract_spec
task_spec_dict_from_session = contract_spec_dict_from_session
verification_skip_respects_taskspec = verification_skip_respects_contract_spec
