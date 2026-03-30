"""Resolución de URL del gateway CLI y normalización OpenAI base URL."""
from __future__ import annotations

import os
from unittest import mock

import httpx

from apps.cli.runtime.gateway_endpoint import (
    DEFAULT_GHOST_GATEWAY_URL,
    _strip_openai_v1_suffix,
    format_preflight_failure_console,
    probe_gateway,
    resolve_cli_gateway_url,
)


def test_resolve_default_when_no_env() -> None:
    keys = ("GHOST_SERVER_URL", "OPENAI_API_URL", "OPENAI_BASE_URL")
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        assert resolve_cli_gateway_url() == DEFAULT_GHOST_GATEWAY_URL
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_resolve_ghost_server_url_wins() -> None:
    with mock.patch.dict(
        os.environ,
        {
            "GHOST_SERVER_URL": "http://10.0.0.5:9000",
            "OPENAI_BASE_URL": "http://ignored:1111/v1",
        },
        clear=False,
    ):
        assert resolve_cli_gateway_url() == "http://10.0.0.5:9000"


def test_strip_v1_suffix_from_openai_base() -> None:
    with mock.patch.dict(os.environ, {"OPENAI_BASE_URL": "https://api.example.com/v1"}, clear=False):
        assert resolve_cli_gateway_url() == "https://api.example.com"


def test_strip_openai_v1_suffix_helper() -> None:
    assert _strip_openai_v1_suffix("http://x/v1/") == "http://x"
    assert _strip_openai_v1_suffix("http://x/v1") == "http://x"


def test_probe_gateway_connection_error() -> None:
    with mock.patch("apps.cli.runtime.gateway_endpoint.http_get") as g:
        g.side_effect = httpx.ConnectError("refused")
        r = probe_gateway("http://127.0.0.1:8000", timeout_sec=0.1)
        assert r.ok is False
        assert r.base_url == "http://127.0.0.1:8000"
        assert "refused" in (r.error or "").lower() or r.error
        assert r.failure_class in {"gateway_not_running", "gateway_unreachable"}
        assert r.remedy_command == "ghost start"


def test_format_preflight_failure_console_surfaces_next_command() -> None:
    with mock.patch("apps.cli.runtime.gateway_endpoint.http_get") as g:
        g.side_effect = httpx.ConnectError("refused")
        result = probe_gateway("http://127.0.0.1:8000", timeout_sec=0.1)
    rendered = format_preflight_failure_console(result)
    assert "Gateway local no disponible" in rendered
    assert "Next:" in rendered
    assert "ghost start" in rendered


def test_probe_gateway_first_health_ok() -> None:
    class Resp:
        status_code = 200

    with mock.patch("apps.cli.runtime.gateway_endpoint.http_get") as g:
        g.return_value = Resp()
        r = probe_gateway("http://h:1", timeout_sec=0.2)
        assert r.ok is True
        assert r.checked_url == "http://h:1/health"
        g.assert_called_once()
