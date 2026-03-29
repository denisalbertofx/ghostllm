"""
CLI gateway resolution and connectivity preflight.

The local Ghost gateway defaults to 127.0.0.1:8000 and exposes `/health`,
`/v1/models`, and `/v1/chat/completions`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

import httpx

from apps.cli.runtime.http_client import get as http_get

DEFAULT_GHOST_GATEWAY_URL = "http://127.0.0.1:8000"
LEGACY_OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"


def _strip_openai_v1_suffix(url: str) -> str:
    u = url.rstrip("/")
    if u.endswith("/v1"):
        return u[:-3].rstrip("/")
    return u


def resolve_cli_gateway_url() -> str:
    raw = (
        os.environ.get("GHOST_SERVER_URL")
        or os.environ.get("OPENAI_API_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or ""
    ).strip()
    if not raw:
        return DEFAULT_GHOST_GATEWAY_URL
    return _strip_openai_v1_suffix(raw) or DEFAULT_GHOST_GATEWAY_URL


def preflight_connect_timeout_sec() -> float:
    try:
        return max(0.5, min(15.0, float(os.getenv("GHOST_GATEWAY_PREFLIGHT_TIMEOUT", "2.5"))))
    except ValueError:
        return 2.5


def default_provider_backend_label() -> str:
    return "openai_compatible"


@dataclass(frozen=True)
class GatewayPreflightResult:
    base_url: str
    ok: bool
    checked_url: str
    status_code: Optional[int]
    error: str
    hint: str
    failure_class: str = ""
    reason_summary: str = ""
    remedy_command: str = ""
    autostart_attempted: bool = False
    autostart_succeeded: bool = False


def classify_gateway_preflight_failure(
    error: str,
    *,
    status_code: Optional[int] = None,
) -> tuple[str, str, str]:
    lower = str(error or "").strip().lower()
    if status_code and status_code >= 500:
        return (
            "gateway_unhealthy",
            "El gateway local respondió con un error interno.",
            "ghost stop && ghost start",
        )
    if "401" in lower or "403" in lower or "auth" in lower or "unauthorized" in lower or "forbidden" in lower:
        return (
            "auth_config_invalid",
            "La configuración o las credenciales del gateway/proveedor no son válidas.",
            "ghost init",
        )
    if "timeout" in lower:
        return (
            "gateway_timeout",
            "El gateway local no respondió a tiempo.",
            "ghost start",
        )
    if any(
        token in lower
        for token in ("refused", "actively refused", "connection refused", "connecterror", "unreachable")
    ):
        return (
            "gateway_not_running",
            "El gateway local no está corriendo o no acepta conexiones.",
            "ghost start",
        )
    return (
        "gateway_unreachable",
        "Ghost no pudo comunicarse con el gateway local.",
        "ghost start",
    )


def probe_gateway(
    base_url: str,
    *,
    api_key: Optional[str] = None,
    timeout_sec: Optional[float] = None,
) -> GatewayPreflightResult:
    t = timeout_sec if timeout_sec is not None else preflight_connect_timeout_sec()
    base = base_url.rstrip("/")
    headers_models = {}
    if api_key:
        headers_models["Authorization"] = f"Bearer {api_key}"

    candidates: Tuple[Tuple[str, dict], ...] = (
        (f"{base}/health", {}),
        (f"{base}/healthz", {}),
        (f"{base}/v1/models", headers_models),
    )

    last_err = ""
    last_status: Optional[int] = None
    for url, hdrs in candidates:
        try:
            response = http_get(url, timeout=t, headers=hdrs)
            last_status = response.status_code
            if response.status_code < 500:
                return GatewayPreflightResult(
                    base_url=base,
                    ok=True,
                    checked_url=url,
                    status_code=response.status_code,
                    error="",
                    hint="",
                )
            last_err = f"HTTP {response.status_code} from {url}"
        except httpx.ConnectError as exc:
            last_err = str(exc)[:500]
        except httpx.TimeoutException:
            last_err = f"timeout ({t}s) on {url}"
        except httpx.HTTPError as exc:
            last_err = str(exc)[:500]
        except OSError as exc:
            last_err = str(exc)[:500]

    failure_class, reason_summary, remedy_command = classify_gateway_preflight_failure(
        last_err,
        status_code=last_status,
    )
    hint_lines = [
        f"URL efectiva: {base}",
        "Variables: GHOST_SERVER_URL (prioridad), OPENAI_BASE_URL / OPENAI_API_URL.",
        f"Por defecto se usa el gateway Ghost en {DEFAULT_GHOST_GATEWAY_URL} (no {LEGACY_OLLAMA_DEFAULT_URL}).",
    ]
    return GatewayPreflightResult(
        base_url=base,
        ok=False,
        checked_url=candidates[-1][0],
        status_code=last_status,
        error=last_err or "unreachable",
        hint="\n".join(hint_lines),
        failure_class=failure_class,
        reason_summary=reason_summary,
        remedy_command=remedy_command,
    )


def format_preflight_failure_console(result: GatewayPreflightResult) -> str:
    reason = result.reason_summary or "Ghost no pudo llegar al gateway local."
    next_cmd = result.remedy_command or "ghost start"
    return (
        "[bold red]✘ Gateway local no disponible[/bold red]\n"
        f"[dim]{reason}[/dim]\n"
        f"[bold]Next:[/bold] [cyan]{next_cmd}[/cyan]\n"
        f"[dim]{result.hint}[/dim]\n"
    )
