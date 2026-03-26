"""
CLI runtime environment: optional local env autoload, feature flags, startup summary data.

Precedence: variables already set in the process environment win; file only fills missing keys.
Never auto-load *.example or ghost_nim_flow.env.example.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Import trace helpers only for wrong-repo heuristics (no circular import with assistant)
from apps.cli.runtime.ghost_profile import build_profile_summary_for_display
from apps.cli.runtime.session_trace import paths_outside_repo_from_task, wrong_repo_note_from_task

_PROJECT_MARKERS = (
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "Gemfile",
)


def _env_truthy(name: str) -> bool:
    v = os.environ.get(name)
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _safe_flag_value(name: str) -> str:
    v = os.environ.get(name)
    if v is None:
        return "0"
    return "1" if str(v).strip().lower() in ("1", "true", "yes", "on") else str(v).strip()[:32]


def collect_active_feature_flags() -> Dict[str, str]:
    keys = (
        "GHOST_USE_TASKSPEC",
        "GHOST_USE_DECISION_PLANNER",
        "GHOST_USE_RETRIEVAL",
        "GHOST_RETRIEVAL_INFLUENCES_RUNTIME",
        "GHOST_USE_EXECUTION_AGENT",
        "GHOST_EXECUTION_AGENT_LIVE",
        "GHOST_PARALLEL_PIPELINE",
        "GHOST_USE_BATCH_EXECUTOR",
        "GHOST_USE_APPROVAL_COMPRESSION",
        "GHOST_USE_NVIDIA_NIM",
        "GHOST_AUTOLOAD_ENV",
        "GHOST_CLI_TRACE_SUMMARY",
    )
    return {k: _safe_flag_value(k) for k in keys}


def parse_env_lines(content: str) -> List[Tuple[str, str]]:
    """Parse KEY=VALUE lines; # comments; no export prefix required."""
    out: List[Tuple[str, str]] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        val = rest.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        out.append((key, val))
    return out


def _is_forbidden_autoload_path(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(".example"):
        return True
    if "nim_flow" in name:
        return True
    return False


def apply_env_file(path: Path, *, only_missing: bool = True) -> Tuple[int, List[str]]:
    """Set os.environ from file. Returns (count_set, keys_set)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0, []
    applied: List[str] = []
    for key, val in parse_env_lines(text):
        if only_missing and key in os.environ:
            continue
        os.environ[key] = val
        applied.append(key)
    return len(applied), applied


def resolve_autoload_env_file(cwd: str) -> Optional[Path]:
    """First loadable path, or None. Does not read ghost_nim_flow or *.example."""
    root = Path(cwd).resolve()
    candidates = [
        root / ".ghost.env",
        root / "configs" / "ghost.local.env",
    ]
    for p in candidates:
        try:
            if p.is_file() and not _is_forbidden_autoload_path(p):
                return p
        except OSError:
            continue
    return None


def autoload_local_env(cwd: str) -> Tuple[bool, Optional[str], int, List[str]]:
    """
    If GHOST_AUTOLOAD_ENV is set, load first existing .ghost.env or configs/ghost.local.env.
    Returns (did_attempt_autoload, loaded_relative_path_or_none, n_vars_applied, warnings).
    """
    warnings: List[str] = []
    if not _env_truthy("GHOST_AUTOLOAD_ENV"):
        return False, None, 0, warnings
    path = resolve_autoload_env_file(cwd)
    if not path:
        return True, None, 0, warnings
    n, keys = apply_env_file(path, only_missing=True)
    try:
        rel = path.relative_to(Path(cwd).resolve())
        rel_s = str(rel).replace("\\", "/")
    except ValueError:
        rel_s = path.name
    if n == 0 and keys == []:
        warnings.append(
            f"Autoload: file '{rel_s}' found but no new variables applied (all keys already in environment)."
        )
    return True, rel_s, n, warnings


def weak_project_root_warnings(cwd: str) -> List[str]:
    """Deterministic hint when CWD may not be a project root."""
    root = Path(cwd).resolve()
    if any((root / m).is_file() for m in _PROJECT_MARKERS):
        return []
    return [
        "Current directory has no common project marker (package.json, pyproject.toml, go.mod, …). "
        "Ghost uses the current working directory as the repo root — cd into your project first."
    ]


def wrong_repo_warnings(cwd: str, task_text: str = "") -> List[str]:
    out: List[str] = []
    note = wrong_repo_note_from_task(task_text, cwd)
    if note:
        out.append(note + " Run Ghost from the target repository root if you intend to edit those files.")
    outside = paths_outside_repo_from_task(task_text, cwd)
    if outside:
        out.append(
            "Task references paths that do not exist under the current repo root: "
            + ", ".join(outside[:5])
            + (". Run Ghost from the correct repository root." if len(outside) <= 5 else " …")
        )
    return out


@dataclass
class RuntimePrepResult:
    cwd: str
    repo_root_effective: str
    config_source: str
    autoload_env_used: bool
    env_file_loaded: Optional[str]
    vars_applied_from_file: int
    startup_warnings: List[str] = field(default_factory=list)
    active_feature_flags: Dict[str, str] = field(default_factory=dict)
    runtime_contract_hint: str = ""
    # Set when main() applied an operational profile (ghost fast, ghost dev, …)
    operational_profile: str = ""
    profile_summary: Dict[str, str] = field(default_factory=dict)


def build_runtime_contract_hint() -> str:
    if _env_truthy("GHOST_USE_TASKSPEC"):
        return "taskspec (when session starts)"
    return "legacy intent"


def prepare_runtime(cwd: Optional[str] = None, initial_task: str = "") -> RuntimePrepResult:
    """
    Apply optional autoload, collect flags and warnings. Call once before CodexAssistant
    from the target project directory (typically os.getcwd()).
    """
    cwd = os.path.abspath(cwd or os.getcwd())
    warnings: List[str] = list(weak_project_root_warnings(cwd))
    warnings.extend(wrong_repo_warnings(cwd, initial_task or ""))

    attempted, rel_loaded, n_applied, w_autoload = autoload_local_env(cwd)
    warnings.extend(w_autoload)

    flags = collect_active_feature_flags()

    if not attempted:
        config_src = "environment variables only (GHOST_AUTOLOAD_ENV off)"
    elif rel_loaded:
        config_src = f"{rel_loaded} + environment (shell wins on conflicts)"
    else:
        config_src = "environment variables only (no loadable .ghost.env / configs/ghost.local.env)"

    op_prof = os.environ.get("GHOST_ACTIVE_PROFILE", "").strip().lower()
    prof_sum = build_profile_summary_for_display() if op_prof else {}

    return RuntimePrepResult(
        cwd=cwd,
        repo_root_effective=cwd,
        config_source=config_src,
        autoload_env_used=bool(attempted and rel_loaded),
        env_file_loaded=rel_loaded,
        vars_applied_from_file=n_applied,
        startup_warnings=warnings,
        active_feature_flags=flags,
        runtime_contract_hint=build_runtime_contract_hint(),
        operational_profile=op_prof,
        profile_summary=prof_sum,
    )


def feature_on(flags: Dict[str, str], key: str) -> bool:
    return flags.get(key, "0") == "1"


def enrich_prep_after_operational_profile(prep: RuntimePrepResult) -> None:
    """Call after apply_operational_profile_to_environment + optional autoload order fixes."""
    op = os.environ.get("GHOST_ACTIVE_PROFILE", "").strip().lower()
    prep.operational_profile = op
    prep.profile_summary = build_profile_summary_for_display() if op else {}
    prep.active_feature_flags = collect_active_feature_flags()
    try:
        from apps.cli.runtime.terminal_capability import ghost_ui_uses_ascii

        if ghost_ui_uses_ascii():
            prep.startup_warnings.append(
                "Console: ASCII-only UI (limited encoding or GHOST_ASCII_UI=1). "
                "For full borders/emoji use Windows Terminal, ghost.bat, or PYTHONUTF8=1."
            )
    except Exception:
        pass
