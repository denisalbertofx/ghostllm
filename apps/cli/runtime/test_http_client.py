import asyncio

import httpx

from apps.cli.runtime.http_client import get, post_async, stream_lines_async


class _FakeAsyncResponse:
    def __init__(self, status_code: int, *, lines: list[str] | None = None, body: bytes = b"") -> None:
        self.status_code = status_code
        self._lines = list(lines or [])
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return self._body


class _FakeAsyncClient:
    post_responses: list[httpx.Response] = []
    stream_responses: list[_FakeAsyncResponse] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        return self.post_responses.pop(0)

    def stream(self, method, url, headers=None, json=None):
        return self.stream_responses.pop(0)


class _FakeSyncClient:
    responses: list[httpx.Response] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def request(self, method, url, headers=None, json=None):
        return self.responses.pop(0)


def test_post_async_retries_on_retryable_status(monkeypatch) -> None:
    _FakeAsyncClient.post_responses = [
        httpx.Response(503, request=httpx.Request("POST", "http://test")),
        httpx.Response(200, request=httpx.Request("POST", "http://test")),
    ]
    monkeypatch.setattr("apps.cli.runtime.http_client.httpx.AsyncClient", _FakeAsyncClient)

    async def _run():
        response = await post_async("http://test", timeout=5, json_payload={"ok": True})
        assert response.status_code == 200

    asyncio.run(_run())


def test_get_retries_on_retryable_status(monkeypatch) -> None:
    _FakeSyncClient.responses = [
        httpx.Response(503, request=httpx.Request("GET", "http://test")),
        httpx.Response(200, request=httpx.Request("GET", "http://test")),
    ]
    monkeypatch.setattr("apps.cli.runtime.http_client.httpx.Client", _FakeSyncClient)
    monkeypatch.setattr("apps.cli.runtime.http_client.time.sleep", lambda _: None)

    response = get("http://test", timeout=5)

    assert response.status_code == 200


def test_stream_lines_async_emits_incremental_lines(monkeypatch) -> None:
    _FakeAsyncClient.stream_responses = [
        _FakeAsyncResponse(200, lines=["data: one", "data: two", "data: [DONE]"]),
    ]
    monkeypatch.setattr("apps.cli.runtime.http_client.httpx.AsyncClient", _FakeAsyncClient)
    seen: list[str] = []

    async def _run():
        await stream_lines_async(
            "http://test",
            timeout=5,
            json_payload={"stream": True},
            on_line=seen.append,
        )

    asyncio.run(_run())
    assert seen == ["data: one", "data: two", "data: [DONE]"]
