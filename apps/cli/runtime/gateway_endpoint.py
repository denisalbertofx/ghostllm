"""
Resolución del gateway LLM para el CLI y preflight de conectividad.

El servidor GhostLLM (FastAPI) escucha por defecto en 127.0.0.1:8000 y expone
``/health``, ``/v1/models`` y ``/v1/chat/completions``.

Historial: ``apps/cli/main.py`` usaba por defecto ``http://127.0.0.1:11434`` (Ollama),
lo que provocaba conexión rechazada cuando solo el gateway Ghost estaba activo.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

import requests

# Gateway GhostLLM local (configs/default.yaml → server.port 8000)
DEFAULT_GHOST_GATEWAY_URL = "http://127.0.0.1:8000"

# Valor antiguo hardcodeado en main.py (Ollama). Solo se usa para mensajes de ayuda.
LEGACY_OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"


def _strip_openai_v1_suffix(url: str) -> str:
    u = url.rstrip("/")
    if u.endswith("/v1"):
        return u[:-3].rstrip("/")
    return u


def resolve_cli_gateway_url() -> str:
    """
    URL base del gateway OpenAI-compatible (sin ``/v1`` final).

    Precedencia:
    1. ``GHOST_SERVER_URL``
    2. ``OPENAI_API_URL`` / ``OPENAI_BASE_URL`` (se normaliza quitando ``/v1``)
    3. ``DEFAULT_GHOST_GATEWAY_URL``
    """
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
    """Backend lógico del transporte principal del CLI hacia el gateway."""
    return "openai_compatible"


@dataclass(frozen=True)
class GatewayPreflightResult:
    base_url: str
    ok: bool
    checked_url: str
    status_code: Optional[int]
    error: str
    hint: str


def probe_gateway(
    base_url: str,
    *,
    api_key: Optional[str] = None,
    timeout_sec: Optional[float] = None,
) -> GatewayPreflightResult:
    """
    Comprueba conectividad con timeout corto.

    Orden: ``/health`` → ``/healthz`` → ``/v1/models`` (algunos proxies solo exponen uno).
    """
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
    for url, hdrs in candidates:
        try:
            r = requests.get(url, timeout=t, headers=hdrs)
            if r.status_code < 500:
                return GatewayPreflightResult(
                    base_url=base,
                    ok=True,
                    checked_url=url,
                    status_code=r.status_code,
                    error="",
                    hint="",
                )
            last_err = f"HTTP {r.status_code} from {url}"
        except requests.exceptions.ConnectionError as e:
            last_err = str(e)[:500]
        except requests.exceptions.Timeout:
            last_err = f"timeout ({t}s) on {url}"
        except OSError as e:
            last_err = str(e)[:500]

    hint_lines = [
        f"URL efectiva: {base}",
        "Variables: GHOST_SERVER_URL (prioridad), OPENAI_BASE_URL / OPENAI_API_URL.",
        f"Por defecto se usa el gateway Ghost en {DEFAULT_GHOST_GATEWAY_URL} (no {LEGACY_OLLAMA_DEFAULT_URL}).",
        "Arranca el servidor: `python apps/server/main.py` o `ghost start` desde la raíz del repo.",
    ]
    return GatewayPreflightResult(
        base_url=base,
        ok=False,
        checked_url=candidates[-1][0],
        status_code=None,
        error=last_err or "unreachable",
        hint="\n".join(hint_lines),
    )


def format_preflight_failure_console(result: GatewayPreflightResult) -> str:
    return (
        f"[bold red]✘ Gateway LLM inalcanzable[/bold red]\n"
        f"[dim]{result.error}[/dim]\n\n"
        f"{result.hint}\n"
    )
