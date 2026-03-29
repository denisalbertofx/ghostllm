from __future__ import annotations

from typing import Any, Mapping

import httpx


def _build_timeout(timeout: float | tuple[float, float]) -> httpx.Timeout:
    if isinstance(timeout, tuple):
        connect_t, read_t = timeout
        return httpx.Timeout(connect=float(connect_t), read=float(read_t), write=float(read_t), pool=float(connect_t))
    return httpx.Timeout(float(timeout))


def get(url: str, *, timeout: float | tuple[float, float], headers: Mapping[str, str] | None = None) -> httpx.Response:
    with httpx.Client(timeout=_build_timeout(timeout), follow_redirects=True) as client:
        return client.get(url, headers=dict(headers or {}))


def post(
    url: str,
    *,
    timeout: float | tuple[float, float],
    headers: Mapping[str, str] | None = None,
    json_payload: Any = None,
) -> httpx.Response:
    with httpx.Client(timeout=_build_timeout(timeout), follow_redirects=True) as client:
        return client.post(url, headers=dict(headers or {}), json=json_payload)
