"""
Operational UX profiles (ghost fast, ghost safe, …): map user intent → env + model routing.

PRD: users choose speed vs safety; Ghost sets models, flags, and parallelism hints.
Does not replace explicit env vars the user already exported (only fills missing keys
unless force=True — profile commands use force for profile-managed keys).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

PROFILE_IDS = frozenset({"simple", "dev", "fast", "turbo", "safe"})

# Embedded defaults if configs/models.yaml has no `profiles` / `profile_tiers`.
_DEFAULT_TIERS: Dict[str, str] = {
    "medium": "coder",
    "fast": "fast",
    "strong": "coder",
}

_DEFAULT_PROFILES: Dict[str, Dict[str, Any]] = {
    "simple": {
        "operational_mode": "chat",
        "session": "medium",
        "planner": "medium",
        "execution": "medium",
        "repair": "medium",
        "auto_approve": False,
        "batch_executor": False,
        "approval_compression": True,
        "use_taskspec": True,
        "use_planner": True,
        "use_retrieval": True,
        "retrieval_influences_runtime": False,
        "use_execution_agent": True,
        "execution_agent_live": False,
        "parallel_pipeline": "partial",
        "verification_level": "basic",
        "retrieval_aggression": "normal",
    },
    "dev": {
        "operational_mode": "execute",
        "session": "medium",
        "planner": "medium",
        "execution": "strong",
        "repair": "strong",
        "auto_approve": False,
        "batch_executor": True,
        "approval_compression": True,
        "use_taskspec": True,
        "use_planner": True,
        "use_retrieval": True,
        "retrieval_influences_runtime": True,
        "use_execution_agent": True,
        "execution_agent_live": True,
        "parallel_pipeline": "partial",
        "verification_level": "normal",
        "retrieval_aggression": "normal",
    },
    "fast": {
        "operational_mode": "auto",
        "session": "fast",
        "planner": "fast",
        "execution": "fast",
        "repair": "medium",
        "auto_approve": False,
        "batch_executor": True,
        "approval_compression": True,
        "use_taskspec": True,
        "use_planner": True,
        "use_retrieval": True,
        "retrieval_influences_runtime": True,
        "use_execution_agent": True,
        "execution_agent_live": True,
        "parallel_pipeline": "on",
        "verification_level": "smart",
        "retrieval_aggression": "aggressive",
    },
    "turbo": {
        "operational_mode": "auto",
        "session": "fast",
        "planner": "fast",
        "execution": "fast",
        "repair": "strong",
        "auto_approve": False,
        "batch_executor": True,
        "approval_compression": True,
        "use_taskspec": True,
        "use_planner": True,
        "use_retrieval": True,
        "retrieval_influences_runtime": True,
        "use_execution_agent": True,
        "execution_agent_live": True,
        "parallel_pipeline": "aggressive",
        "verification_level": "smart",
        "retrieval_aggression": "aggressive",
    },
    "safe": {
        "operational_mode": "auto",
        "session": "strong",
        "planner": "strong",
        "execution": "strong",
        "repair": "strong",
        "auto_approve": False,
        "batch_executor": False,
        "approval_compression": False,
        "use_taskspec": True,
        "use_planner": True,
        "use_retrieval": True,
        "retrieval_influences_runtime": True,
        "use_execution_agent": True,
        "execution_agent_live": True,
        "parallel_pipeline": "off",
        "verification_level": "strict",
        "retrieval_aggression": "normal",
    },
}

def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _load_yaml_path(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def resolve_models_yaml_path(cwd: str, ghost_repo_root: Path) -> Path:
    """Prefer project configs/models.yaml, else Ghost repo copy."""
    c = Path(cwd).resolve() / "configs" / "models.yaml"
    if c.is_file():
        return c
    return ghost_repo_root / "configs" / "models.yaml"


def load_models_doc(cwd: str, ghost_repo_root: Path) -> Dict[str, Any]:
    return _load_yaml_path(resolve_models_yaml_path(cwd, ghost_repo_root))


def _name_to_upstream(models: List[Any], name: str) -> str:
    for m in models:
        if not isinstance(m, dict):
            continue
        if str(m.get("name") or "") != name:
            continue
        if m.get("enabled") is False:
            continue
        uid = m.get("upstream_id")
        if uid:
            return str(uid)
    return name


def _tier_to_registry_name(tiers: Mapping[str, str], tier: str) -> str:
    t = (tier or "medium").strip().lower()
    return str(tiers.get(t) or tiers.get("medium") or "coder")


@dataclass
class ResolvedOperationalProfile:
    profile_id: str
    session_model_alias: str
    planner_upstream_id: str
    execution_upstream_id: str
    repair_upstream_id: str
    execution_fallback_upstream_id: str
    general_fallback_upstream_id: str
    auto_approve: bool
    parallel_pipeline: str
    verification_level: str
    retrieval_aggression: str
    operational_mode: str
    raw_profile: Dict[str, Any] = field(default_factory=dict)


def resolve_operational_profile(
    profile_id: str,
    cwd: str,
    ghost_repo_root: Path,
) -> ResolvedOperationalProfile:
    pid = profile_id.strip().lower()
    if pid not in PROFILE_IDS:
        raise ValueError(f"Unknown operational profile: {profile_id!r} (expected one of {sorted(PROFILE_IDS)})")

    doc = load_models_doc(cwd, ghost_repo_root)
    models = doc.get("models") if isinstance(doc.get("models"), list) else []
    tiers = {**_DEFAULT_TIERS}
    if isinstance(doc.get("profile_tiers"), dict):
        for k, v in doc["profile_tiers"].items():
            if k and v:
                tiers[str(k).strip().lower()] = str(v).strip()

    profiles: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in _DEFAULT_PROFILES.items()}
    if isinstance(doc.get("profiles"), dict):
        for k, v in doc["profiles"].items():
            kid = str(k).strip().lower()
            if kid not in PROFILE_IDS or not isinstance(v, dict):
                continue
            base = dict(_DEFAULT_PROFILES.get(kid, {}))
            base.update(v)
            profiles[kid] = base

    spec = profiles.get(pid) or _DEFAULT_PROFILES[pid]

    def up(role_key: str) -> str:
        tier = str(spec.get(role_key) or "medium")
        reg_name = _tier_to_registry_name(tiers, tier)
        return _name_to_upstream(models, reg_name)

    # Fallback tiers: optional keys or heuristic
    fb_exec_tier = spec.get("execution_fallback_tier") or spec.get("session") or "fast"
    fb_gen_tier = spec.get("general_fallback_tier") or "fast"
    fb_exec_name = _tier_to_registry_name(tiers, str(fb_exec_tier))
    fb_gen_name = _tier_to_registry_name(tiers, str(fb_gen_tier))

    return ResolvedOperationalProfile(
        profile_id=pid,
        session_model_alias=_tier_to_registry_name(tiers, str(spec.get("session") or "medium")),
        planner_upstream_id=up("planner"),
        execution_upstream_id=up("execution"),
        repair_upstream_id=up("repair"),
        execution_fallback_upstream_id=_name_to_upstream(models, fb_exec_name),
        general_fallback_upstream_id=_name_to_upstream(models, fb_gen_name),
        auto_approve=_truthy(spec.get("auto_approve", False)),
        parallel_pipeline=str(spec.get("parallel_pipeline") or "off"),
        verification_level=str(spec.get("verification_level") or "normal"),
        retrieval_aggression=str(spec.get("retrieval_aggression") or "normal"),
        operational_mode=str(spec.get("operational_mode") or "chat"),
        raw_profile=dict(spec),
    )


def apply_operational_profile_to_environment(
    profile_id: str,
    cwd: str,
    ghost_repo_root: Path,
) -> ResolvedOperationalProfile:
    """
    Apply profile to os.environ.

    Identity (active profile + session model alias + auto-approve hint) is always pinned
    to match the invoked command. Other keys are set only when missing so
    shell / autoloaded .ghost.env can override feature flags and role models.
    """
    r = resolve_operational_profile(profile_id, cwd, ghost_repo_root)

    def set_soft(key: str, val: str) -> None:
        if os.environ.get(key, "").strip():
            return
        os.environ[key] = val

    def set_hard(key: str, val: str) -> None:
        os.environ[key] = val

    set_hard("GHOST_ACTIVE_PROFILE", r.profile_id)
    set_hard("GHOST_RESOLVED_SESSION_MODEL", r.session_model_alias)
    set_hard("GHOST_OPERATIONAL_MODE", r.operational_mode)
    set_soft("GHOST_VERIFICATION_LEVEL", r.verification_level)
    set_soft("GHOST_PARALLEL_PIPELINE", r.parallel_pipeline)
    set_soft("GHOST_RETRIEVAL_AGGRESSION", r.retrieval_aggression)

    spec = r.raw_profile
    set_soft("GHOST_USE_TASKSPEC", "1" if _truthy(spec.get("use_taskspec", True)) else "0")
    set_soft("GHOST_USE_DECISION_PLANNER", "1" if _truthy(spec.get("use_planner", True)) else "0")
    set_soft("GHOST_USE_RETRIEVAL", "1" if _truthy(spec.get("use_retrieval", True)) else "0")
    set_soft(
        "GHOST_RETRIEVAL_INFLUENCES_RUNTIME",
        "1" if _truthy(spec.get("retrieval_influences_runtime", False)) else "0",
    )
    set_soft("GHOST_USE_EXECUTION_AGENT", "1" if _truthy(spec.get("use_execution_agent", True)) else "0")
    set_soft(
        "GHOST_EXECUTION_AGENT_LIVE",
        "1" if _truthy(spec.get("execution_agent_live", False)) else "0",
    )
    set_soft("GHOST_USE_BATCH_EXECUTOR", "1" if _truthy(spec.get("batch_executor", False)) else "0")
    set_soft(
        "GHOST_USE_APPROVAL_COMPRESSION",
        "1" if _truthy(spec.get("approval_compression", True)) else "0",
    )

    set_soft("GHOST_PLANNER_MODEL", r.planner_upstream_id)
    set_soft("GHOST_EXECUTION_MODEL", r.execution_upstream_id)
    set_soft("GHOST_REPAIR_MODEL", r.repair_upstream_id)
    set_soft("GHOST_EXECUTION_FALLBACK_MODEL", r.execution_fallback_upstream_id)
    set_soft("GHOST_GENERAL_FALLBACK_MODEL", r.general_fallback_upstream_id)

    stamp_auto_approve_hint(r.auto_approve)
    return r


def get_operational_mode() -> str:
    """chat | execute | auto — empty env defaults to chat (no coercion)."""
    return os.environ.get("GHOST_OPERATIONAL_MODE", "").strip().lower() or "chat"


def maybe_coerce_intent_for_operational_mode(intent: Any) -> None:
    """
    For execute/auto profiles, promote generic Chat turns into Execute so the agent
    uses tools and reaches verification without requiring /do.
    """
    mode = get_operational_mode()
    if mode not in ("execute", "auto"):
        return
    if getattr(intent, "is_slash_command", False):
        return
    if getattr(intent, "mode", "") != "Chat":
        return
    task = (getattr(intent, "task", None) or "").strip()
    if not task:
        return
    if task.endswith("?") and len(task) < 120 and not getattr(intent, "requires_tools", False):
        return
    if mode == "auto":
        intent.mode = "Execute"
        intent.requires_tools = True
        return
    # execute (dev): coerce when heuristics already flag tooling or obvious implementation verbs
    tt = getattr(intent, "task_type", "ask")
    tl = task.lower()
    work_kw = (
        "implement",
        "implementa",
        "implementar",
        "add ",
        "añade",
        "crea ",
        "create ",
        "build ",
        "fix ",
        "arregla",
        "corrige",
        "write ",
        "escribe",
        "endpoint",
        " api",
        "refactor",
        "migrate",
        "update ",
        "actualiza",
    )
    if getattr(intent, "requires_tools", False) or tt != "ask":
        intent.mode = "Execute"
        intent.requires_tools = True
        return
    if any(k in tl for k in work_kw):
        intent.mode = "Execute"
        intent.requires_tools = True


def build_profile_summary_for_display() -> Dict[str, str]:
    """Short labels for simplified startup / doctor (reads current os.environ)."""
    pid = os.environ.get("GHOST_ACTIVE_PROFILE", "").strip().upper()
    if not pid:
        return {}
    par = os.environ.get("GHOST_PARALLEL_PIPELINE", "off").strip().lower()
    par_label = {
        "off": "OFF",
        "partial": "PARTIAL",
        "on": "ON",
        "aggressive": "AGGRESSIVE",
    }.get(par, par.upper() or "OFF")
    ver = os.environ.get("GHOST_VERIFICATION_LEVEL", "normal").strip().upper()
    retr = os.environ.get("GHOST_RETRIEVAL_AGGRESSION", "normal").strip().upper()
    session = os.environ.get("GHOST_RESOLVED_SESSION_MODEL", "—")
    auto_on = os.environ.get("_GHOST_PROFILE_AUTO_APPROVE", "") == "1"
    op_mode = os.environ.get("GHOST_OPERATIONAL_MODE", "").strip().upper() or "CHAT"
    return {
        "profile": pid,
        "operational_mode": op_mode,
        "session_model": session,
        "parallel": par_label,
        "verification": ver,
        "retrieval": retr,
        "auto_approve": "ON" if auto_on else "OFF",
        "batch_tools": "ON" if os.environ.get("GHOST_USE_BATCH_EXECUTOR") == "1" else "OFF",
        "compression": "ON" if os.environ.get("GHOST_USE_APPROVAL_COMPRESSION") == "1" else "OFF",
        "trace": "ON" if os.environ.get("GHOST_CLI_TRACE_SUMMARY", "0") == "1" else "OFF",
    }


def stamp_auto_approve_hint(enabled: bool) -> None:
    """Internal flag so startup summary can show auto_approve without Typer."""
    os.environ["_GHOST_PROFILE_AUTO_APPROVE"] = "1" if enabled else "0"
