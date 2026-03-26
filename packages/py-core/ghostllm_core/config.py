"""
GhostLLM Core Configuration
============================
Model registry, gateway config, and runtime resolution helpers.

Architecture:
  configs/models.yaml   → ModelRegistry (name → upstream_id, tool_calling, etc.)
  configs/default.yaml  → GhostConfig (server, upstream, monitoring)

CLI resolution chain:
  GHOST_*_MODEL env vars  → alias name (e.g. "kimi")
         ↓
  resolve_upstream_model_id()  → upstream_id (e.g. "moonshotai/kimi-k2.5")
         ↓
  Server gateway /v1/chat/completions → NIM provider
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Model registry schema
# ---------------------------------------------------------------------------

class ModelMapping(BaseModel):
    """One row in configs/models.yaml — must stay aligned with the real file."""

    model_config = ConfigDict(extra="ignore")

    name: str
    upstream_id: str
    description: Optional[str] = None
    enabled: bool = True
    expensive: bool = False
    tool_calling: bool = True   # Most modern NIM models support tool-calling


class ModelRegistry(BaseModel):
    """Top-level models.yaml: `models` plus optional profile_tiers / profiles (ignored here)."""

    model_config = ConfigDict(extra="ignore")

    models: List[ModelMapping]


# ---------------------------------------------------------------------------
# Server / infra config schema
# ---------------------------------------------------------------------------

class UpstreamConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    nvidia_api_key: str
    base_url: str = "https://integrate.api.nvidia.com/v1"


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    api_key: Optional[str] = None


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    allowlist: List[ModelMapping]


class MonitoringConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prometheus_port: int = 9090
    log_level: str = "INFO"
    log_format: str = "json"


class GhostConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    server: ServerConfig
    upstream: UpstreamConfig
    models: ModelsConfig
    monitoring: MonitoringConfig


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_config(path: str) -> GhostConfig:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return GhostConfig(**data)


def load_registry(path: str) -> ModelRegistry:
    if not os.path.exists(path):
        return ModelRegistry(models=[])
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        return ModelRegistry(models=[])
    if not isinstance(data, dict):
        raise ValueError(f"Model registry YAML must be a mapping, got {type(data).__name__}")
    return ModelRegistry(**data)


def save_config(path: str, config: GhostConfig) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(config.model_dump(), f, default_flow_style=False)


# ---------------------------------------------------------------------------
# Runtime resolution helpers
# ---------------------------------------------------------------------------

def bootstrap_enabled_models(
    path: str,
) -> Tuple[Dict[str, ModelMapping], Dict[str, str], Optional[str]]:
    """
    Load models.yaml and build runtime maps. Never raises — returns error string for logging / HTTP hints.

    Returns:
        enabled_by_name  : registry ``name`` -> ModelMapping (enabled only)
        name_to_upstream : alias -> upstream_id (for bridge / provider)
        error            : None if OK, else a short diagnostic
    """
    if not os.path.exists(path):
        return {}, {}, f"FileNotFoundError: model registry YAML not found at {path}"
    try:
        registry = load_registry(path)
    except Exception as e:
        return {}, {}, f"{type(e).__name__}: {e}"
    enabled: Dict[str, ModelMapping] = {m.name: m for m in registry.models if m.enabled}
    mapping: Dict[str, str] = {name: m.upstream_id for name, m in enabled.items()}
    return enabled, mapping, None


def resolve_upstream_model_id(requested: str, name_to_upstream: Dict[str, str]) -> str:
    """
    Turn a registry name, full ``upstream_id``, or short basename into the
    canonical ``upstream_id`` for the provider.

    Priority:
      1. Exact registry name match  (e.g. ``kimi`` → ``moonshotai/kimi-k2.5``)
      2. Full upstream_id passthrough  (e.g. ``moonshotai/kimi-k2.5``)
      3. Basename match when unambiguous  (e.g. ``kimi-k2.5`` when unique)
      4. Return unchanged (caller decides how to handle unknown model)
    """
    r = (requested or "").strip()
    if not r or not name_to_upstream:
        return r

    values = list(name_to_upstream.values())

    # 1. Exact name match
    if r in name_to_upstream:
        return name_to_upstream[r]

    # 2. Already a full upstream_id
    if r in values:
        return r

    # 3. Basename match (e.g. "kimi-k2.5" → "moonshotai/kimi-k2.5"), unique only
    if "/" not in r:
        matches = sorted({u for u in values if u.split("/")[-1] == r})
        if len(matches) == 1:
            return matches[0]

    # 4. Unknown — return as-is so the server can produce a 403 with a clear name
    return r


def resolve_gateway_model_id(model_hint: str, registry_path: str) -> str:
    """Load registry from path and normalize ``model_hint`` for API/bridge calls."""
    _, mapping, err = bootstrap_enabled_models(registry_path)
    if err or not mapping:
        return (model_hint or "").strip()
    return resolve_upstream_model_id(model_hint, mapping)


def tool_calling_supported(name: str, enabled_by_name: Dict[str, ModelMapping]) -> bool:
    """Return True if the given registry name supports tool-calling."""
    m = enabled_by_name.get(name)
    return bool(m and m.tool_calling)
