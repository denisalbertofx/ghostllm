"""
NVIDIA NIM connectivity smoke tests (no file writes, no assistant integration).
Run manually with valid GHOST_NIM_* env when you have network access.
"""
from __future__ import annotations

import os

from apps.cli.runtime.execution_agent import general_fallback_model_id, resolve_model_role_map
from apps.cli.runtime.nim_provider import ProviderRequest, ProviderResponse, nim_chat_complete

SMOKE_USER_PROMPT = "Reply with exactly: NIM_OK"


def run_nim_smoke_test(model_name: str, prompt: str) -> ProviderResponse:
    base = os.environ.get("GHOST_NIM_BASE_URL", "").strip()
    key = os.environ.get("GHOST_NIM_API_KEY", "").strip()
    if not base or not key:
        return ProviderResponse(model=model_name, ok=False, error_message="nim_base_url_or_api_key_missing")
    if os.environ.get("GHOST_USE_NVIDIA_NIM", "0").strip().lower() not in ("1", "true", "yes", "on"):
        return ProviderResponse(model=model_name, ok=False, error_message="GHOST_USE_NVIDIA_NIM_not_enabled")
    req = ProviderRequest(
        model=model_name,
        system="You are a connectivity probe. Answer very briefly.",
        user=prompt,
        max_tokens=64,
    )
    return nim_chat_complete(base, key, req)


def run_execution_model_smoke_test() -> ProviderResponse:
    m = resolve_model_role_map()["execution"]
    return run_nim_smoke_test(m, SMOKE_USER_PROMPT)


def run_planner_model_smoke_test() -> ProviderResponse:
    m = resolve_model_role_map()["planner"]
    return run_nim_smoke_test(m, SMOKE_USER_PROMPT)


def run_repair_model_smoke_test() -> ProviderResponse:
    m = resolve_model_role_map()["repair"]
    return run_nim_smoke_test(m, SMOKE_USER_PROMPT)


def run_general_fallback_smoke_test() -> ProviderResponse:
    """Smoke del fallback general (por defecto deepseek-v3.2)."""
    return run_nim_smoke_test(general_fallback_model_id(), SMOKE_USER_PROMPT)
