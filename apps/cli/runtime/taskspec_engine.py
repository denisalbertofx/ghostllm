"""
TaskSpec Engine — operational contract from user prompt + repo profile.
No execution tools; produces TaskSpec for Planner / Verification only.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from apps.cli.runtime.intent_classifier import (
    INTENT_ANALYSIS,
    INTENT_BUGFIX,
    INTENT_IMPLEMENTATION,
    INTENT_MODIFICATION,
    INTENT_REFACTOR,
    INTENT_REVIEW,
    INTENT_VERIFICATION,
    SCOPE_FULLSTACK,
    SCOPE_UNKNOWN,
    classify_task_intent,
    prompt_locks_taskspec_as_inspection_readonly,
    user_explicitly_requests_verify_shell,
)
from apps.cli.runtime.planning_task import (
    detect_broad_readonly_plan_request,
    detect_strategy_plan_request,
)
from apps.cli.runtime.repo_profile import RepoProfile
from apps.cli.runtime.repo_overview_task import detect_repo_overview_question

logger = logging.getLogger(__name__)

# Optional LLM merge: (user_prompt, repo_json, draft_dict) -> partial dict to merge into draft
LLMMergeFn = Callable[[str, str, Dict[str, Any]], Dict[str, Any]]


class TaskSpec(BaseModel):
    """Contract consumed by Planner and Verification (not raw prompt)."""

    intent: str
    scope: List[str] = Field(default_factory=list)
    forbidden_layers: List[str] = Field(default_factory=list)
    change_expectation: str
    target_files: Optional[List[str]] = None
    acceptance_criteria: List[str] = Field(default_factory=list)
    verification_policy: Dict[str, Any] = Field(default_factory=dict)
    repair_policy: Dict[str, Any] = Field(default_factory=dict)
    budget_policy: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    reasoning_lines: List[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v)))


@dataclass
class ValidationResult:
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    suspicious: bool = False
    suspicious_reasons: List[str] = field(default_factory=list)


@dataclass
class TaskSpecBuildResult:
    """Full engine output including validation metadata."""

    taskspec: TaskSpec
    validation_status: Literal["valid", "invalid", "repaired"]
    repaired: bool
    validation_errors: List[str]


_FILE_PATH_RE = re.compile(
    r"(?:^|\s|`)([\w./\\-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|json|md|yaml|yml|toml))\b",
    re.IGNORECASE,
)
_CLI_COMMAND_TARGETS = {
    "doctor": ["apps/cli/main.py"],
    "status": ["apps/cli/main.py"],
    "start": ["apps/cli/main.py"],
    "stop": ["apps/cli/main.py"],
    "restart": ["apps/cli/main.py"],
    "serve": ["apps/cli/main.py"],
    "claude": ["apps/cli/main.py"],
    "dev": ["apps/cli/main.py"],
    "plan": ["apps/cli/main.py"],
    "do": ["apps/cli/main.py"],
    "edit": ["apps/cli/main.py"],
    "fix": ["apps/cli/main.py"],
    "models list": ["apps/cli/main.py"],
}


def extract_target_files_from_prompt(prompt: str) -> List[str]:
    """Heuristic file path extraction from natural language."""
    if not prompt:
        return []
    seen: set[str] = set()
    out: List[str] = []
    for m in _FILE_PATH_RE.finditer(prompt):
        p = m.group(1).replace("\\", "/").strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def infer_cli_maintenance_targets(prompt: str) -> List[str]:
    """
    Infer CLI entrypoint targets for maintenance tasks around commands like
    `ghost doctor` so the runtime can stop exploring and move into ACT sooner.
    """
    t = (prompt or "").strip().lower()
    if not t:
        return []
    command_hint = any(
        marker in t
        for marker in (
            "comando",
            "command",
            "ghost ",
            "`ghost",
            " cli",
        )
    )
    if not command_hint:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for command_name, targets in _CLI_COMMAND_TARGETS.items():
        if not re.search(rf"(?<![\w]){re.escape(command_name)}(?![\w])", t):
            continue
        for target in targets:
            if target not in seen:
                seen.add(target)
                out.append(target)
    return out


def _scope_string_to_list(scope: str, repo: RepoProfile, intent: Optional[str] = None) -> List[str]:
    if scope == SCOPE_FULLSTACK:
        return ["ui", "api", "data"]
    if scope == SCOPE_UNKNOWN:
        if intent in (INTENT_REVIEW, INTENT_ANALYSIS):
            return []
        return list(repo.layers_detected) if repo.layers_detected else []
    return [scope]


def _enforce_intent_change_expectation(intent: str, change: str) -> str:
    """Deterministic rules: intent implies change_expectation."""
    if intent in (INTENT_REVIEW, INTENT_ANALYSIS):
        return "should_not_write"
    if intent in (INTENT_IMPLEMENTATION, INTENT_BUGFIX, INTENT_MODIFICATION):
        return "must_write"
    if intent == INTENT_REFACTOR:
        return "may_write"
    if intent == INTENT_VERIFICATION:
        return "may_write"
    return change


def _default_acceptance_criteria(user_prompt: str) -> List[str]:
    t = (user_prompt or "").strip()
    if not t:
        return ["Satisfy user request with evidence."]
    lines = [ln.strip() for ln in t.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if len(lines) <= 3:
        return [t[:500]]
    return lines[:8]


def _repo_verification_hints(repo: RepoProfile) -> Dict[str, bool]:
    """What verification commands exist (from RepoProfile v2 or legacy signals)."""
    hints = {"typecheck": False, "build": False, "lint": False, "tests": False}
    v2 = getattr(repo, "v2", None)
    if v2 is not None:
        vc = v2.verification_commands
        hints["typecheck"] = bool(vc.typecheck)
        hints["build"] = bool(vc.build)
        hints["lint"] = bool(vc.lint)
        hints["tests"] = bool(vc.tests) or ("pytest" in (v2.stack.test_runner or []))
        return hints
    if repo.has_package_json:
        hints["typecheck"] = True
        hints["build"] = True
        hints["lint"] = True
        hints["tests"] = True
    return hints


def _targets_node_layer(target_files: Optional[List[str]]) -> bool:
    for raw in target_files or []:
        path = str(raw or "").replace("\\", "/").strip().lower()
        if not path:
            continue
        if path.startswith(("apps/web/", "web/")):
            return True
        if path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")) and not path.startswith(
            ("apps/cli/", "apps/server/", "packages/py-core/")
        ):
            return True
    return False


def _targets_python_layer(target_files: Optional[List[str]]) -> bool:
    for raw in target_files or []:
        path = str(raw or "").replace("\\", "/").strip().lower()
        if not path:
            continue
        if path.startswith(("apps/cli/", "apps/server/", "packages/py-core/")):
            return True
        if path.endswith(".py") or path in ("pyproject.toml", "requirements.txt", "setup.py"):
            return True
        if path.startswith("configs/"):
            return True
    return False


def _build_verification_policy(
    scope: List[str],
    repo: RepoProfile,
    intent: str,
    *,
    target_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if intent in (INTENT_REVIEW, INTENT_ANALYSIS):
        return {
            "required": False,
            "typecheck": False,
            "build": False,
            "lint": False,
            "tests": False,
        }
    vp: Dict[str, Any] = {
        "required": True,
        "typecheck": False,
        "build": False,
        "lint": False,
        "tests": False,
    }
    scope_set = set(scope)
    vhints = _repo_verification_hints(repo)
    node_targeted = _targets_node_layer(target_files)
    python_targeted = _targets_python_layer(target_files)

    if node_targeted:
        vp["typecheck"] = bool(vhints.get("typecheck", False))
        vp["build"] = bool(vhints.get("build", False))
        vp["lint"] = bool(vhints.get("lint", False))
        vp["tests"] = bool(vhints.get("tests", False))
    if python_targeted:
        vp["tests"] = bool(vhints.get("tests", False))

    if not (node_targeted or python_targeted):
        if "api" in scope_set or "data" in scope_set:
            vp["typecheck"] = bool(vhints.get("typecheck", False))
            vp["tests"] = bool(vhints.get("tests", False))
        if "ui" in scope_set:
            vp["typecheck"] = bool(vhints.get("typecheck", False))
            vp["build"] = bool(vhints.get("build", False))
            vp["lint"] = bool(vhints.get("lint", False))
            vp["tests"] = bool(vhints.get("tests", False))
        if repo.has_package_json and intent in (
            INTENT_IMPLEMENTATION,
            INTENT_MODIFICATION,
            INTENT_BUGFIX,
            INTENT_REFACTOR,
        ) and not scope_set:
            vp["build"] = bool(vhints.get("build", False))
    if intent == INTENT_BUGFIX:
        if not any(vp[k] for k in ("typecheck", "build", "lint", "tests")):
            vp["tests"] = bool(vhints.get("tests", False))
            if not vp["tests"]:
                vp["typecheck"] = bool(vhints.get("typecheck", False))
            if not any(vp[k] for k in ("typecheck", "build", "lint", "tests")):
                vp["build"] = bool(vhints.get("build", False))
    return vp


def _default_repair_policy() -> Dict[str, Any]:
    return {"max_attempts": 1, "rerun_failed_first": True}


def _default_budget_policy(*, intent: Optional[str] = None, change_expectation: Optional[str] = None) -> Dict[str, Any]:
    if change_expectation == "should_not_write" or intent in (INTENT_REVIEW, INTENT_ANALYSIS):
        return {"max_shell_calls": 0, "max_tool_calls": 12, "soft_read_only_tool_calls": 8}
    return {
        "max_shell_calls": 4,
        "max_tool_calls": 24,
        "reserved_write_tool_calls": 4,
        "soft_read_only_tool_calls": 12,
    }


def validate_taskspec(ts: TaskSpec) -> ValidationResult:
    """
    Structural + policy validation. Does not execute tools.
    """
    errors: List[str] = []
    suspicious = False
    suspicious_reasons: List[str] = []

    if ts.intent == INTENT_IMPLEMENTATION and ts.change_expectation == "should_not_write":
        errors.append("implementation intent cannot have change_expectation=should_not_write")

    if ts.intent == INTENT_BUGFIX:
        vp = ts.verification_policy or {}
        if not vp.get("required", False):
            errors.append("bugfix requires verification_policy.required=true")
        if not any(vp.get(k) for k in ("typecheck", "build", "lint", "tests")):
            errors.append("bugfix requires at least one verification check enabled")

    if ts.change_expectation == "must_write":
        has_scope = bool(ts.scope)
        has_targets = bool(ts.target_files)
        if not has_scope and not has_targets:
            suspicious = True
            suspicious_reasons.append("must_write without scope or target_files")

    if ts.intent not in (INTENT_ANALYSIS, INTENT_REVIEW):
        if "api" in (ts.scope or []) and not any(
            (ts.verification_policy or {}).get(k) for k in ("typecheck", "tests")
        ):
            errors.append("api scope requires verification_policy.typecheck=true or tests=true")

    is_valid = len(errors) == 0
    return ValidationResult(
        is_valid=is_valid,
        errors=errors,
        suspicious=suspicious,
        suspicious_reasons=suspicious_reasons,
    )


def repair_taskspec(ts: TaskSpec, errors: List[str]) -> TaskSpec:
    """Deterministic auto-repair from validation error messages."""
    return _repair_taskspec_deterministic(ts, errors)


def _repair_taskspec_deterministic(ts: TaskSpec, errors: List[str]) -> TaskSpec:
    """Apply deterministic fixes for known validation failures."""
    data = ts.model_dump()
    vp = dict(data.get("verification_policy") or {})
    reasoning = list(data.get("reasoning_lines") or [])

    for err in errors:
        if "implementation intent cannot have" in err:
            data["change_expectation"] = "must_write"
            reasoning.append("auto-repair: implementation -> change_expectation=must_write")
        if "bugfix requires verification" in err:
            vp["required"] = True
            vp.setdefault("typecheck", True)
            reasoning.append("auto-repair: bugfix -> verification enabled")
        if "api scope requires verification_policy.typecheck" in err:
            if not vp.get("typecheck") and not vp.get("tests"):
                vp["tests"] = True
                reasoning.append("auto-repair: api scope -> tests=true")

    ce = data.get("change_expectation")
    intent = data.get("intent")
    if intent:
        fixed = _enforce_intent_change_expectation(intent, ce)
        if fixed != ce:
            data["change_expectation"] = fixed
            reasoning.append(f"auto-repair: intent {intent} -> change_expectation={fixed}")

    scope = list(data.get("scope") or [])
    if data.get("change_expectation") == "must_write" and not scope and not data.get("target_files"):
        reasoning.append("auto-repair: must_write missing scope — cannot infer without repo rerun")

    data["verification_policy"] = vp
    data["reasoning_lines"] = reasoning
    return TaskSpec.model_validate(data)


def _merge_llm_draft(
    base: Dict[str, Any],
    llm_partial: Dict[str, Any],
) -> Dict[str, Any]:
    """Shallow merge LLM keys into base (only known fields)."""
    allowed = {
        "intent",
        "scope",
        "forbidden_layers",
        "change_expectation",
        "target_files",
        "acceptance_criteria",
        "verification_policy",
        "repair_policy",
        "budget_policy",
        "confidence",
        "reasoning_lines",
    }
    out = dict(base)
    for k, v in llm_partial.items():
        if k not in allowed:
            continue
        if k in ("verification_policy", "repair_policy", "budget_policy") and isinstance(v, dict):
            out[k] = {**(out.get(k) or {}), **v}
        elif k == "reasoning_lines" and isinstance(v, list):
            out[k] = list(out.get(k) or []) + [str(x) for x in v]
        else:
            out[k] = v
    return out


_UI_FORBIDDEN_PHRASES = (
    "do not touch ui",
    "no toques la ui",
    "no toques ui",
    "don't touch ui",
    "no tocar ui",
    "sin ui",
    "only api",
    "solo api",
    "only data",
    "solo datos",
    "trabaja solo en la capa de datos",
    "solo capa de api",
)


def _infer_forbidden_layers(prompt: str) -> List[str]:
    p = (prompt or "").lower()
    if any(x in p for x in _UI_FORBIDDEN_PHRASES):
        return ["ui"]
    return []


def _draft_from_classifier(
    user_prompt: str,
    repo: RepoProfile,
) -> Dict[str, Any]:
    pre = classify_task_intent(user_prompt)
    repo_overview = detect_repo_overview_question(user_prompt)
    strategy_plan = detect_strategy_plan_request(user_prompt)
    broad_plan = detect_broad_readonly_plan_request(user_prompt)
    scope_list = _scope_string_to_list(pre.scope, repo, pre.intent)
    ce = _enforce_intent_change_expectation(pre.intent, pre.change_expectation)
    targets = extract_target_files_from_prompt(user_prompt)
    cli_targets = infer_cli_maintenance_targets(user_prompt)
    if cli_targets:
        seen = set(targets)
        for target in cli_targets:
            if target not in seen:
                targets.append(target)
                seen.add(target)
        if pre.scope == SCOPE_UNKNOWN:
            scope_list = []
    forbidden = _infer_forbidden_layers(user_prompt)
    draft = {
        "intent": pre.intent,
        "scope": scope_list,
        "forbidden_layers": forbidden,
        "change_expectation": ce,
        "target_files": targets if targets else None,
        "acceptance_criteria": _default_acceptance_criteria(user_prompt),
        "confidence": pre.confidence,
        "reasoning_lines": list(pre.reasoning_lines),
    }
    if cli_targets:
        draft.setdefault("reasoning_lines", []).append(
            "CLI maintenance task detected -> inferred target_files for command entrypoint"
        )
    if repo_overview:
        draft["budget_policy"] = {"max_shell_calls": 0, "max_tool_calls": 4}
        draft.setdefault("reasoning_lines", []).append(
            "Repo overview question detected -> bounded read-only budget"
        )
    elif strategy_plan or broad_plan:
        draft["budget_policy"] = {"max_shell_calls": 0, "max_tool_calls": 16}
        draft.setdefault("reasoning_lines", []).append(
            "Broader read-only planning request detected -> wider planning tool budget"
        )
    if pre.intent in (INTENT_REVIEW, INTENT_ANALYSIS) and user_explicitly_requests_verify_shell(user_prompt):
        bp = dict(draft.get("budget_policy") or {})
        bp["max_shell_calls"] = max(int(bp.get("max_shell_calls") or 0), 3)
        draft["budget_policy"] = bp
        draft.setdefault("reasoning_lines", []).append(
            "User explicitly requested verification commands -> shell budget raised"
        )
    return draft


def _apply_policy_defaults(data: Dict[str, Any], repo: RepoProfile) -> TaskSpec:
    intent = str(data["intent"])
    scope = list(data.get("scope") or [])
    vp = _build_verification_policy(scope, repo, intent, target_files=data.get("target_files"))
    user_vp = data.get("verification_policy") or {}
    if isinstance(user_vp, dict):
        vp = {**vp, **user_vp}
    rp = {**_default_repair_policy(), **(data.get("repair_policy") or {})}
    bp = {
        **_default_budget_policy(intent=intent, change_expectation=str(data.get("change_expectation") or "")),
        **(data.get("budget_policy") or {}),
    }
    data["verification_policy"] = vp
    data["repair_policy"] = rp
    data["budget_policy"] = bp
    if not data.get("acceptance_criteria"):
        data["acceptance_criteria"] = _default_acceptance_criteria(data.get("_user_prompt", ""))
    data.pop("_user_prompt", None)
    return TaskSpec.model_validate(data)


def build_taskspec(
    user_prompt: str,
    repo_profile: RepoProfile,
    *,
    llm_merge_fn: Optional[LLMMergeFn] = None,
) -> TaskSpecBuildResult:
    """
    Hybrid pipeline:
    1. Deterministic pre-classifier (rules via intent_classifier)
    2. Repo context from RepoProfile (layers, package.json)
    3. Optional LLM structured merge (JSON partial)
    4. Deterministic policy defaults + validator
    5. Auto-repair + re-validate if invalid
    """
    draft = _draft_from_classifier(user_prompt, repo_profile)

    if llm_merge_fn is not None:
        try:
            if getattr(repo_profile, "v2", None) is not None:
                repo_summary = json.dumps(
                    repo_profile.v2.model_dump(mode="json"),
                    sort_keys=True,
                )
            else:
                repo_summary = json.dumps(
                    {
                        "stack": repo_profile.stack,
                        "layers_detected": repo_profile.layers_detected,
                        "has_package_json": repo_profile.has_package_json,
                        "important_folders": repo_profile.important_folders,
                    },
                    sort_keys=True,
                )
            partial = llm_merge_fn(user_prompt, repo_summary, dict(draft))
            if isinstance(partial, dict):
                draft = _merge_llm_draft(draft, partial)
                intent = draft.get("intent")
                if intent:
                    draft["change_expectation"] = _enforce_intent_change_expectation(
                        str(intent), str(draft.get("change_expectation") or "may_write")
                    )
                draft.setdefault("reasoning_lines", []).append("LLM merge applied to TaskSpec draft")
        except Exception as e:
            logger.warning("TaskSpec LLM merge failed: %s", e)
            draft.setdefault("reasoning_lines", []).append(f"LLM merge skipped: {type(e).__name__}")

    if prompt_locks_taskspec_as_inspection_readonly(user_prompt):
        locked_pre = classify_task_intent(user_prompt)
        draft["intent"] = INTENT_ANALYSIS
        draft["change_expectation"] = _enforce_intent_change_expectation(
            INTENT_ANALYSIS, str(draft.get("change_expectation") or "")
        )
        draft["scope"] = _scope_string_to_list(locked_pre.scope, repo_profile, INTENT_ANALYSIS)
        draft.setdefault("reasoning_lines", []).append(
            "Inspection-style user prompt locks intent=analysis (post-merge safeguard)"
        )
        if user_explicitly_requests_verify_shell(user_prompt):
            bp = dict(draft.get("budget_policy") or {})
            bp["max_shell_calls"] = max(int(bp.get("max_shell_calls") or 0), 3)
            draft["budget_policy"] = bp

    draft["_user_prompt"] = user_prompt
    taskspec = _apply_policy_defaults(draft, repo_profile)

    vr = validate_taskspec(taskspec)
    validation_errors = list(vr.errors)
    repaired = False

    if not vr.is_valid:
        taskspec = repair_taskspec(taskspec, validation_errors)
        repaired = True
        vr2 = validate_taskspec(taskspec)
        validation_errors = list(vr2.errors)
        status: Literal["valid", "invalid", "repaired"] = (
            "valid" if vr2.is_valid else "invalid"
        )
        if repaired and vr2.is_valid:
            status = "repaired"
    else:
        status = "valid"
        if vr.suspicious:
            validation_errors.extend([f"suspicious: {s}" for s in vr.suspicious_reasons])

    return TaskSpecBuildResult(
        taskspec=taskspec,
        validation_status=status,
        repaired=repaired,
        validation_errors=validation_errors,
    )


def taskspec_llm_via_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
) -> LLMMergeFn:
    """
    Factory: call OpenAI-compatible /v1/chat/completions with JSON response.
    Only used when explicitly configured (no import-time side effects).
    """
    import requests

    def _merge(user_prompt: str, repo_summary: str, draft: Dict[str, Any]) -> Dict[str, Any]:
        system = (
            "You refine a TaskSpec draft. Reply with ONLY a JSON object containing any of these keys "
            "you want to override: intent, scope, change_expectation, target_files, acceptance_criteria, "
            "verification_policy, repair_policy, budget_policy, confidence, reasoning_lines. "
            "Do not include execution commands."
        )
        user = json.dumps(
            {"user_prompt": user_prompt, "repo": json.loads(repo_summary), "draft": draft},
            ensure_ascii=False,
        )
        url = f"{base_url.rstrip('/')}/v1/chat/completions"
        body: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        r = requests.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120,
        )
        if r.status_code >= 400:
            body.pop("response_format", None)
            r = requests.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=120,
            )
        r.raise_for_status()
        raw = r.json()
        content = raw["choices"][0]["message"]["content"]
        return json.loads(content)

    return _merge


def default_llm_merge_from_env() -> Optional[LLMMergeFn]:
    """If GHOST_TASKSPEC_LLM=1 and GHOST_SERVER_URL + key, return merge fn; else None."""
    if os.environ.get("GHOST_TASKSPEC_LLM", "").strip() != "1":
        return None
    base = os.environ.get("GHOST_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
    key = os.environ.get("GHOST_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    model = os.environ.get("GHOST_TASKSPEC_MODEL", "fast")
    if not key:
        return None
    return taskspec_llm_via_openai_compatible(base, key, model)
