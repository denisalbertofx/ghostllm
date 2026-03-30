from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any, Awaitable, Callable, Mapping, Optional

import httpx


_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


def build_timeout(timeout: float | tuple[float, float]) -> httpx.Timeout:
    if isinstance(timeout, tuple):
        connect_t, read_t = timeout
        return httpx.Timeout(
            connect=float(connect_t),
            read=float(read_t),
            write=float(read_t),
            pool=float(connect_t),
        )
    return httpx.Timeout(float(timeout))


def _retry_limits() -> tuple[int, float, float, float]:
    max_retries = max(1, int(os.getenv("GHOST_HTTP_MAX_RETRIES", "3")))
    base = float(os.getenv("GHOST_HTTP_RETRY_BASE_SEC", "0.55"))
    max_wait = float(os.getenv("GHOST_HTTP_RETRY_MAX_WAIT_SEC", "32"))
    jitter = float(os.getenv("GHOST_HTTP_RETRY_JITTER_SEC", "0.35"))
    return max_retries, base, max_wait, jitter


def _retry_delay(attempt: int, *, base: float, max_wait: float, jitter: float) -> float:
    return min(max_wait, base * (2**attempt) + random.uniform(0, jitter * (attempt + 1)))


def _is_retryable_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ),
    )


def sync_client(*, timeout: float | tuple[float, float], follow_redirects: bool = True) -> httpx.Client:
    return httpx.Client(timeout=build_timeout(timeout), follow_redirects=follow_redirects)


def async_client(*, timeout: float | tuple[float, float], follow_redirects: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=build_timeout(timeout), follow_redirects=follow_redirects)


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES


def _request_sync_with_retries(
    method: str,
    url: str,
    *,
    timeout: float | tuple[float, float],
    headers: Mapping[str, str] | None = None,
    json_payload: Any = None,
) -> httpx.Response:
    max_retries, base, max_wait, jitter = _retry_limits()
    last_exc: Optional[Exception] = None
    with sync_client(timeout=timeout) as client:
        for attempt in range(max_retries):
            try:
                response = client.request(method, url, headers=dict(headers or {}), json=json_payload)
                if _is_retryable_status(response.status_code) and attempt < max_retries - 1:
                    time.sleep(_retry_delay(attempt, base=base, max_wait=max_wait, jitter=jitter))
                    continue
                return response
            except Exception as exc:  # noqa: BLE001
                if not _is_retryable_error(exc):
                    raise
                last_exc = exc
                if attempt >= max_retries - 1:
                    raise
                time.sleep(_retry_delay(attempt, base=base, max_wait=max_wait, jitter=jitter))
    if last_exc:
        raise last_exc
    raise RuntimeError("GHOST: sync HTTP retry exhausted")


def get(url: str, *, timeout: float | tuple[float, float], headers: Mapping[str, str] | None = None) -> httpx.Response:
    return _request_sync_with_retries("GET", url, timeout=timeout, headers=headers)


def post(
    url: str,
    *,
    timeout: float | tuple[float, float],
    headers: Mapping[str, str] | None = None,
    json_payload: Any = None,
) -> httpx.Response:
    return _request_sync_with_retries(
        "POST",
        url,
        timeout=timeout,
        headers=headers,
        json_payload=json_payload,
    )


async def post_async(
    url: str,
    *,
    timeout: float | tuple[float, float],
    headers: Mapping[str, str] | None = None,
    json_payload: Any = None,
) -> httpx.Response:
    max_retries, base, max_wait, jitter = _retry_limits()
    last_exc: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=build_timeout(timeout), follow_redirects=True) as client:
        for attempt in range(max_retries):
            try:
                response = await client.post(url, headers=dict(headers or {}), json=json_payload)
                if response.status_code in _RETRYABLE_STATUS_CODES and attempt < max_retries - 1:
                    await asyncio.sleep(_retry_delay(attempt, base=base, max_wait=max_wait, jitter=jitter))
                    continue
                return response
            except Exception as exc:  # noqa: BLE001
                if not _is_retryable_error(exc):
                    raise
                last_exc = exc
                if attempt >= max_retries - 1:
                    raise
                await asyncio.sleep(_retry_delay(attempt, base=base, max_wait=max_wait, jitter=jitter))
    if last_exc:
        raise last_exc
    raise RuntimeError("GHOST: async HTTP retry exhausted")


async def stream_lines_async(
    url: str,
    *,
    timeout: float | tuple[float, float],
    headers: Mapping[str, str] | None = None,
    json_payload: Any = None,
    on_line: Callable[[str], None],
    on_error_response: Optional[Callable[[httpx.Response], Awaitable[None]]] = None,
) -> None:
    max_retries, base, max_wait, jitter = _retry_limits()
    async with httpx.AsyncClient(timeout=build_timeout(timeout), follow_redirects=True) as client:
        for attempt in range(max_retries):
            try:
                async with client.stream(
                    "POST",
                    url,
                    headers=dict(headers or {}),
                    json=json_payload,
                ) as response:
                    if response.status_code in _RETRYABLE_STATUS_CODES and attempt < max_retries - 1:
                        await response.aread()
                        await asyncio.sleep(_retry_delay(attempt, base=base, max_wait=max_wait, jitter=jitter))
                        continue
                    if response.status_code >= 400:
                        if on_error_response is not None:
                            await on_error_response(response)
                            return
                        response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line:
                            on_line(line)
                    return
            except Exception as exc:  # noqa: BLE001
                if not _is_retryable_error(exc):
                    raise
                if attempt >= max_retries - 1:
                    raise
                await asyncio.sleep(_retry_delay(attempt, base=base, max_wait=max_wait, jitter=jitter))
