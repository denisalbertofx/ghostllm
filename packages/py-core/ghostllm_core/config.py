"""
GhostLLM Core Configuration
============================
Model registry, gateway config, and runtime resolution helpers.

Architecture:
  configs/models.yaml   → ModelRegistry (name → upstream_id, tool_calling, etc.)
  configs/default.yaml  → GhostConfig (server, upstream, monitoring)

CLI resolution chain:
  GHOST_*_MODEL env vars  → alias name (e.g. "planner" or "coder")
         ↓
  resolve_upstream_model_id()  → upstream_id (e.g. "qwen/qwen3-coder-480b-a35b-instruct")
         ↓
  Server gateway /v1/chat/completions → NIM provider
"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict
from yaml.nodes import MappingNode, Node, SequenceNode


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


class ConfigValidationError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Config path and env overrides
# ---------------------------------------------------------------------------

def resolve_config_path(
    default_path: str,
    *,
    env_var: str = "GHOST_CONFIG_PATH",
) -> str:
    """
    Resolve the config file path with a stable precedence:
      1. explicit environment override
      2. sibling `default.local.yaml`
      3. repository `default.yaml`
    """
    env_path = os.getenv(env_var, "").strip()
    if env_path:
        return env_path

    base = Path(default_path)
    local_override = base.with_name("default.local.yaml")
    if local_override.exists():
        return str(local_override)
    return str(base)


def _apply_env_overrides(data: Dict) -> Dict:
    data = dict(data or {})
    server = dict(data.get("server") or {})
    upstream = dict(data.get("upstream") or {})
    monitoring = dict(data.get("monitoring") or {})

    env_server_host = os.getenv("GHOST_SERVER_HOST")
    if env_server_host is not None and env_server_host.strip():
        server["host"] = env_server_host.strip()
    else:
        server["host"] = server.get("host", "127.0.0.1")
    if os.getenv("GHOST_SERVER_PORT", "").strip():
        server["port"] = int(os.environ["GHOST_SERVER_PORT"])
    if os.getenv("GHOST_SERVER_API_KEY", "").strip():
        server["api_key"] = os.environ["GHOST_SERVER_API_KEY"]

    env_upstream_base = os.getenv("GHOST_UPSTREAM_BASE_URL")
    if env_upstream_base is not None and env_upstream_base.strip():
        upstream["base_url"] = env_upstream_base.strip()
    else:
        upstream["base_url"] = upstream.get("base_url", "https://integrate.api.nvidia.com/v1")
    upstream_key = (
        os.getenv("GHOST_NVIDIA_API_KEY", "").strip()
        or os.getenv("NVIDIA_API_KEY", "").strip()
        or upstream.get("nvidia_api_key", "")
    )
    upstream["nvidia_api_key"] = upstream_key

    env_log_level = os.getenv("GHOST_LOG_LEVEL")
    if env_log_level is not None and env_log_level.strip():
        monitoring["log_level"] = env_log_level.strip()
    else:
        monitoring["log_level"] = monitoring.get("log_level", "INFO")
    env_log_format = os.getenv("GHOST_LOG_FORMAT")
    if env_log_format is not None and env_log_format.strip():
        monitoring["log_format"] = env_log_format.strip()
    else:
        monitoring["log_format"] = monitoring.get("log_format", "json")
    if os.getenv("GHOST_PROMETHEUS_PORT", "").strip():
        monitoring["prometheus_port"] = int(os.environ["GHOST_PROMETHEUS_PORT"])

    data["server"] = server
    data["upstream"] = upstream
    data["monitoring"] = monitoring
    return data


def _schema_path() -> Path:
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / "ghost.schema.json"
        if candidate.exists():
            return candidate
    return here.parents[3] / "ghost.schema.json"


def _load_schema_document() -> Dict[str, Any]:
    with open(_schema_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def _schema_for_kind(kind: str) -> Dict[str, Any]:
    schema = _load_schema_document()
    defs = schema.get("$defs") or {}
    selected = defs.get(kind)
    if not isinstance(selected, dict):
        raise ConfigValidationError(f"Schema definition '{kind}' not found in {_schema_path()}")
    return {
        "$schema": schema.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
        "$ref": f"#/$defs/{kind}",
        "$defs": defs,
    }


def _build_yaml_line_map(raw: str) -> Dict[Tuple[Any, ...], int]:
    root = yaml.compose(raw)
    if root is None:
        return {(): 1}

    line_map: Dict[Tuple[Any, ...], int] = {(): 1}

    def walk(node: Node, path: Tuple[Any, ...]) -> None:
        line_map[path] = int(node.start_mark.line) + 1
        if isinstance(node, MappingNode):
            for key_node, value_node in node.value:
                key = str(getattr(key_node, "value", ""))
                key_path = path + (key,)
                line_map[key_path] = int(key_node.start_mark.line) + 1
                walk(value_node, key_path)
        elif isinstance(node, SequenceNode):
            for idx, child in enumerate(node.value):
                item_path = path + (idx,)
                line_map[item_path] = int(child.start_mark.line) + 1
                walk(child, item_path)

    walk(root, ())
    return line_map


def _best_line_for_path(line_map: Dict[Tuple[Any, ...], int], path: Tuple[Any, ...]) -> int:
    probe = tuple(path)
    while probe:
        if probe in line_map:
            return line_map[probe]
        probe = probe[:-1]
    return line_map.get((), 1)


def _schema_node_for_path(schema: Dict[str, Any], path: Tuple[Any, ...]) -> Dict[str, Any]:
    current: Dict[str, Any] = dict(schema or {})
    for part in path:
        if isinstance(part, int):
            items = current.get("items")
            if not isinstance(items, dict):
                break
            current = items
            continue
        props = current.get("properties")
        if isinstance(props, dict) and isinstance(props.get(part), dict):
            current = props[part]
            continue
        break
    return current


def _additional_property_name(message: str) -> str:
    match = re.search(r"'([^']+)'", message or "")
    return match.group(1) if match else ""


def _format_validation_error(
    *,
    path: str,
    kind: str,
    schema: Dict[str, Any],
    line_map: Dict[Tuple[Any, ...], int],
    data: Dict[str, Any],
    error: Any,
) -> str:
    error_path = tuple(error.absolute_path)
    line = _best_line_for_path(line_map, error_path)
    path_str = ".".join(str(part) for part in error_path) if error_path else "<root>"
    message = str(error.message or "invalid configuration")
    suggestion = ""
    schema_node = _schema_node_for_path(schema, error_path[:-1] if error.validator == "additionalProperties" else error_path)

    if error.validator == "additionalProperties":
        bad_key = _additional_property_name(message)
        allowed = sorted((schema_node.get("properties") or {}).keys())
        if bad_key and allowed:
            close = difflib.get_close_matches(bad_key, allowed, n=1)
            if close:
                suggestion = f" Did you mean '{close[0]}'?"
            else:
                suggestion = f" Allowed keys here: {', '.join(allowed)}."
    elif error.validator == "required":
        missing = _additional_property_name(message)
        if missing:
            parent = data
            for part in error_path:
                if isinstance(parent, dict):
                    parent = parent.get(part)
                elif isinstance(parent, list) and isinstance(part, int) and 0 <= part < len(parent):
                    parent = parent[part]
                else:
                    parent = None
                    break
            close = []
            if isinstance(parent, dict):
                close = difflib.get_close_matches(missing, list(parent.keys()), n=1)
            if close:
                suggestion = f" Did you mean '{missing}'? Found similar key '{close[0]}'."
            else:
                suggestion = f" Add the required key '{missing}' under '{path_str}'."
    elif error.validator == "type":
        expected = error.validator_value
        suggestion = f" Expected type: {expected}."

    return f"Invalid {kind} config at {path}:{line} [{path_str}]: {message}.{suggestion}".rstrip()


def _load_validated_yaml_mapping(path: str, *, kind: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ConfigValidationError(f"Invalid {kind} config at {path}: top-level document must be a mapping.")
    schema = _schema_for_kind(kind)
    validator = Draft202012Validator(schema)
    line_map = _build_yaml_line_map(raw)
    errors = sorted(
        validator.iter_errors(data),
        key=lambda err: (tuple(str(part) for part in err.absolute_path), err.message),
    )
    if errors:
        raise ConfigValidationError(
            _format_validation_error(
                path=path,
                kind=kind,
                schema=schema,
                line_map=line_map,
                data=data,
                error=errors[0],
            )
        )
    return data


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_config(path: str) -> GhostConfig:
    data = _load_validated_yaml_mapping(path, kind="ghostConfig")
    return GhostConfig(**_apply_env_overrides(data))


def load_registry(path: str) -> ModelRegistry:
    if not os.path.exists(path):
        return ModelRegistry(models=[])
    data = _load_validated_yaml_mapping(path, kind="modelRegistry")
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
      1. Exact registry name match  (e.g. ``coder`` → ``qwen/qwen3-coder-480b-a35b-instruct``)
      2. Full upstream_id passthrough  (e.g. ``qwen/qwen3-coder-480b-a35b-instruct``)
      3. Basename match when unambiguous  (e.g. ``qwen3-coder-480b-a35b-instruct`` when unique)
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

    # 3. Basename match (e.g. "qwen3-coder-480b-a35b-instruct" → full upstream_id), unique only
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
