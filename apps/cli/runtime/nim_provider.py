"""
NVIDIA NIM OpenAI-compatible chat/completions client.
Structured ProviderResponse for Execution Agent, smoke tests, and benchmarks.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

ENV_NIM_BASE_URL = "GHOST_NIM_BASE_URL"
ENV_NIM_API_KEY = "GHOST_NIM_API_KEY"
ENV_NIM_TIMEOUT = "GHOST_NIM_TIMEOUT_SECONDS"
DEFAULT_NIM_TIMEOUT = 120.0


class ProviderUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ProviderTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latency_ms: float = 0.0


class ProviderError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = ""
    message: str = ""
    http_status: Optional[int] = None


class ProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    system: str = ""
    user: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096


class ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = ""
    raw_text: str = ""
    finish_reason: str = ""
    usage: ProviderUsage = Field(default_factory=ProviderUsage)
    timing: ProviderTiming = Field(default_factory=ProviderTiming)
    ok: bool = False
    error_message: str = ""
    error_detail: Optional[ProviderError] = None


def _nim_timeout_seconds() -> float:
    try:
        return max(5.0, float(os.environ.get(ENV_NIM_TIMEOUT, str(DEFAULT_NIM_TIMEOUT))))
    except (TypeError, ValueError):
        return DEFAULT_NIM_TIMEOUT


def build_chat_completions_payload(req: ProviderRequest) -> Dict[str, Any]:
    messages: List[Dict[str, str]] = []
    if req.system.strip():
        messages.append({"role": "system", "content": req.system})
    messages.append({"role": "user", "content": req.user})
    return {
        "model": req.model,
        "messages": messages,
        "temperature": req.temperature,
        "max_tokens": req.max_tokens,
    }


def _parse_usage(data: Dict[str, Any]) -> ProviderUsage:
    u = data.get("usage") or {}
    if not isinstance(u, dict):
        return ProviderUsage()
    return ProviderUsage(
        prompt_tokens=int(u.get("prompt_tokens") or 0),
        completion_tokens=int(u.get("completion_tokens") or 0),
        total_tokens=int(u.get("total_tokens") or 0),
    )


def nim_chat_complete(
    base_url: str,
    api_key: str,
    req: ProviderRequest,
    *,
    timeout_seconds: Optional[float] = None,
    session: Optional[httpx.Client] = None,
) -> ProviderResponse:
    """
    POST {base}/v1/chat/completions. Returns ProviderResponse (ok=False on any failure).
    """
    t0 = time.perf_counter()
    timeout = timeout_seconds if timeout_seconds is not None else _nim_timeout_seconds()
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = build_chat_completions_payload(req)

    def _fail(msg: str, *, code: str = "error", status: Optional[int] = None) -> ProviderResponse:
        ms = (time.perf_counter() - t0) * 1000
        return ProviderResponse(
            model=req.model,
            ok=False,
            error_message=msg,
            error_detail=ProviderError(code=code, message=msg, http_status=status),
            timing=ProviderTiming(latency_ms=round(ms, 2)),
        )

    sess = session or httpx.Client(follow_redirects=True)
    try:
        r = sess.post(url, headers=headers, json=payload, timeout=httpx.Timeout(connect=10.0, read=timeout, write=timeout, pool=10.0))
    except httpx.TimeoutException:
        return _fail("request_timeout", code="timeout")
    except httpx.ConnectError as e:
        return _fail(f"connection_error: {e}", code="network")
    except httpx.HTTPError as e:
        return _fail(f"request_error: {e}", code="network")
    finally:
        if session is None:
            sess.close()

    ms = (time.perf_counter() - t0) * 1000
    timing = ProviderTiming(latency_ms=round(ms, 2))

    if r.status_code == 401:
        return ProviderResponse(
            model=req.model,
            ok=False,
            error_message="auth_failed",
            error_detail=ProviderError(code="auth", message="401 unauthorized", http_status=401),
            timing=timing,
        )
    if r.status_code == 429:
        return ProviderResponse(
            model=req.model,
            ok=False,
            error_message="rate_limited",
            error_detail=ProviderError(code="rate_limit", message=r.text[:500], http_status=429),
            timing=timing,
        )
    if r.status_code >= 400:
        body = r.text[:800]
        return ProviderResponse(
            model=req.model,
            ok=False,
            error_message=f"http_{r.status_code}: {body}",
            error_detail=ProviderError(code="http", message=body, http_status=r.status_code),
            timing=timing,
        )

    try:
        data = r.json()
    except json.JSONDecodeError:
        return ProviderResponse(
            model=req.model,
            ok=False,
            error_message="invalid_json_response",
            timing=timing,
        )

    choices = data.get("choices") or []
    if not choices:
        return ProviderResponse(
            model=req.model,
            ok=False,
            error_message="empty_choices",
            usage=_parse_usage(data),
            timing=timing,
        )

    ch0 = choices[0] if isinstance(choices[0], dict) else {}
    msg = ch0.get("message") or {}
    content = ""
    if isinstance(msg, dict):
        content = str(msg.get("content") or "")
    finish = str(ch0.get("finish_reason") or "")

    if not content.strip():
        return ProviderResponse(
            model=str(data.get("model") or req.model),
            ok=False,
            error_message="empty_model_output",
            finish_reason=finish,
            usage=_parse_usage(data),
            timing=timing,
        )

    return ProviderResponse(
        model=str(data.get("model") or req.model),
        raw_text=content,
        finish_reason=finish,
        usage=_parse_usage(data),
        timing=timing,
        ok=True,
    )
