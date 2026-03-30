from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml


_SANDBOX_KEYS = {"allow", "deny", "require_approval"}
_SANDBOX_CAPS = {"read", "write", "delete", "network", "shell_exec"}
_ARTIFACT_FORMATS = {"structured_json", "markdown", "text"}
_ARTIFACT_REQUIRED = {"review_packet", "diff_summary", "artifact_json", "artifact_markdown", "plan", "handoff"}


@dataclass(frozen=True)
class ProjectPolicyValidation:
    path: str
    exists: bool
    valid: bool
    data: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def project_policy_path(cwd: str) -> Path:
    return Path(cwd) / ".ghost" / "policy.yaml"


def load_and_validate_project_policy(cwd: str) -> ProjectPolicyValidation:
    path = project_policy_path(cwd)
    if not path.exists():
        return ProjectPolicyValidation(path=str(path), exists=False, valid=True)

    errors: List[str] = []
    warnings: List[str] = []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return ProjectPolicyValidation(
            path=str(path),
            exists=True,
            valid=False,
            errors=[f"Could not parse YAML: {exc}"],
        )

    if not isinstance(raw, dict):
        return ProjectPolicyValidation(
            path=str(path),
            exists=True,
            valid=False,
            errors=["Top-level policy document must be a mapping."],
        )

    sandbox = raw.get("sandbox") or {}
    artifacts = raw.get("artifacts") or {}
    if sandbox and not isinstance(sandbox, dict):
        errors.append("sandbox must be a mapping.")
        sandbox = {}
    if artifacts and not isinstance(artifacts, dict):
        errors.append("artifacts must be a mapping.")
        artifacts = {}

    for key in sandbox.keys():
        if key not in _SANDBOX_KEYS:
            warnings.append(f"Unknown sandbox key: {key}")

    normalized_sandbox: Dict[str, List[str]] = {}
    for key in ("allow", "deny", "require_approval"):
        value = sandbox.get(key) or []
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append(f"sandbox.{key} must be a list of strings.")
            continue
        normalized = sorted({str(item).strip() for item in value if str(item).strip()})
        unknown = [item for item in normalized if item not in _SANDBOX_CAPS]
        if unknown:
            errors.append(f"sandbox.{key} contains unsupported capabilities: {', '.join(unknown)}")
        normalized_sandbox[key] = normalized

    overlap = sorted(set(normalized_sandbox.get("allow", [])) & set(normalized_sandbox.get("deny", [])))
    if overlap:
        errors.append(f"sandbox allow/deny conflict: {', '.join(overlap)}")

    required = artifacts.get("required") or []
    if required and (not isinstance(required, list) or any(not isinstance(item, str) for item in required)):
        errors.append("artifacts.required must be a list of strings.")
    elif required:
        unknown_required = sorted({str(item).strip() for item in required if str(item).strip()} - _ARTIFACT_REQUIRED)
        if unknown_required:
            errors.append(
                f"artifacts.required contains unsupported outputs: {', '.join(unknown_required)}"
            )
    artifact_format = str(artifacts.get("format") or "").strip()
    if artifact_format and artifact_format not in _ARTIFACT_FORMATS:
        errors.append(
            f"artifacts.format must be one of: {', '.join(sorted(_ARTIFACT_FORMATS))}"
        )

    return ProjectPolicyValidation(
        path=str(path),
        exists=True,
        valid=not errors,
        data={"sandbox": normalized_sandbox, "artifacts": artifacts},
        errors=errors,
        warnings=warnings,
    )


def required_repo_capabilities(cwd: str) -> List[str]:
    root = Path(cwd)
    caps = {"read", "write"}
    if (root / "pyproject.toml").exists() or (root / "package.json").exists() or (root / "tests").exists():
        caps.add("shell_exec")
    if (root / "package.json").exists():
        caps.add("network")
    return sorted(caps)


def sandbox_conflicts_for_repo(policy: ProjectPolicyValidation, cwd: str) -> List[str]:
    if not policy.exists or not policy.valid:
        return []
    sandbox = dict(policy.data.get("sandbox") or {})
    deny = set(sandbox.get("deny") or [])
    require_approval = set(sandbox.get("require_approval") or [])
    required = required_repo_capabilities(cwd)
    conflicts: List[str] = []
    for capability in required:
        if capability in deny:
            conflicts.append(f"policy denies required capability '{capability}'")
    if "shell_exec" in require_approval and "shell_exec" in required:
        conflicts.append("shell_exec requires approval; autonomous verify/build may stall")
    return conflicts
