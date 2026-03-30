from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import yaml


_ROUTING_KEYS = {"explore", "act", "verify", "fallback"}


@dataclass(frozen=True)
class ProjectRuntimeConfig:
    path: str
    exists: bool
    valid: bool
    routing: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def project_runtime_config_path(cwd: str) -> Path:
    return Path(cwd) / "ghost.yaml"


def _yaml_error_detail(exc: Exception) -> str:
    detail = str(exc)
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        detail = f"line {int(mark.line) + 1}, column {int(mark.column) + 1}: {detail}"
    return detail


def load_and_validate_project_runtime_config(cwd: str) -> ProjectRuntimeConfig:
    path = project_runtime_config_path(cwd)
    if not path.exists():
        return ProjectRuntimeConfig(path=str(path), exists=False, valid=True)

    errors: List[str] = []
    warnings: List[str] = []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return ProjectRuntimeConfig(
            path=str(path),
            exists=True,
            valid=False,
            errors=[f"ghost.yaml parse error: {_yaml_error_detail(exc)}"],
        )

    if not isinstance(raw, dict):
        return ProjectRuntimeConfig(
            path=str(path),
            exists=True,
        valid=False,
        errors=["ghost.yaml: top-level document must be a mapping."],
    )

    routing = raw.get("routing") or {}
    if routing and not isinstance(routing, dict):
        return ProjectRuntimeConfig(
            path=str(path),
            exists=True,
            valid=False,
            errors=["ghost.yaml:routing must be a mapping."],
        )

    if not routing:
        errors.append(
            "ghost.yaml:routing must define explore, act, verify, and fallback as non-empty strings."
        )

    for key in raw.keys():
        if key != "routing":
            warnings.append(f"Unknown top-level key: {key}")

    normalized: Dict[str, str] = {}
    for key, value in routing.items():
        if key not in _ROUTING_KEYS:
            warnings.append(f"ghost.yaml:routing.{key} is unknown")
            continue
        if not isinstance(value, str) or not value.strip():
            errors.append(f"ghost.yaml:routing.{key} must be a non-empty string.")
            continue
        normalized[key] = value.strip()

    missing_keys = sorted(_ROUTING_KEYS - set(normalized.keys()))
    for key in missing_keys:
        errors.append(f"ghost.yaml:routing.{key} is required and must be a non-empty string.")

    return ProjectRuntimeConfig(
        path=str(path),
        exists=True,
        valid=not errors,
        routing=normalized,
        errors=errors,
        warnings=warnings,
    )


def apply_project_routing_to_role_map(
    role_map: Dict[str, str],
    config: ProjectRuntimeConfig,
) -> Dict[str, str]:
    resolved = dict(role_map)
    routing = dict(config.routing or {}) if config.exists and config.valid else {}
    explore = routing.get("explore") or resolved.get("planner") or resolved.get("execution") or ""
    act = routing.get("act") or resolved.get("execution") or explore
    verify = routing.get("verify") or routing.get("act") or resolved.get("repair") or act
    fallback = routing.get("fallback") or resolved.get("general_fallback") or act

    resolved["explore"] = explore
    resolved["act"] = act
    resolved["verify"] = verify
    resolved["fallback"] = fallback
    resolved["planner"] = explore or resolved.get("planner", "")
    resolved["execution"] = act or resolved.get("execution", "")
    resolved["repair"] = act or resolved.get("repair", "")
    resolved["general_fallback"] = fallback or resolved.get("general_fallback", "")
    resolved["execution_fallback"] = fallback or resolved.get("execution_fallback", "")
    return resolved
