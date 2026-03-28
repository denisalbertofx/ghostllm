import httpx
import json
import logging
import asyncio
from typing import Dict, Any, AsyncGenerator, Optional

logger = logging.getLogger("ghostllm.providers.nvidia")

class NVIDIAError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)

class NvidiaProvider:
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        # Keep a generous timeout for large reasoning/coding models and slower first-token latency.
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0))

    def _get_headers(self, stream: bool = False) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if stream:
            headers["Accept"] = "text/event-stream"
        else:
            headers["Accept"] = "application/json"
        return headers

    def _prepare_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Injects model-specific parameters like 'thinking' if not already set."""
        model = payload.get("model", "")
        if "qwen3-coder-480b-a35b-instruct" in model.lower():
            # Keep a sane default token budget for the primary coder model when callers omit one.
            if "max_tokens" not in payload:
                payload["max_tokens"] = 8192
        return payload

    def _map_error(self, status_code: int, error_text: str) -> NVIDIAError:
        try:
            data = json.loads(error_text)
            msg = data.get("detail", data.get("error", error_text))
        except:
            msg = error_text
        return NVIDIAError(f"[{status_code}] {msg}", status_code)

    async def chat_completions(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handles non-streaming chat completions with telemetry and retries."""
        import time
        url = f"{self.base_url}/chat/completions"
        model = payload.get("model", "unknown")
        max_tokens = payload.get("max_tokens", "default")
        
        for attempt in range(3):
            start_time = time.time()
            try:
                logger.info(f"UPSTREAM REQ: model={model}, max_tokens={max_tokens}, attempt={attempt+1}")
                resp = await self.client.post(url, json=payload, headers=self._get_headers(stream=False))
                latency = time.time() - start_time
                
                if resp.status_code == 429:
                    logger.warning(f"UPSTREAM 429: latency={latency:.3f}s. Retrying...")
                    await asyncio.sleep((attempt + 1) * 0.5)
                    continue

                if resp.status_code != 200:
                    logger.error(f"UPSTREAM ERROR: status={resp.status_code}, latency={latency:.3f}s")
                    raise self._map_error(resp.status_code, resp.text)
                
                logger.info(f"UPSTREAM OK: status=200, latency={latency:.3f}s")
                return resp.json()
            except httpx.RequestError as e:
                logger.error(f"UPSTREAM NET ERROR: {str(e)}, attempt={attempt+1}")
                if attempt == 2:
                    raise NVIDIAError(f"Network error: {str(e)}")
                await asyncio.sleep(0.5)
        
        raise NVIDIAError("Max retries exceeded (429)", 429)

    async def stream_chat_completions(self, payload: Dict[str, Any]) -> AsyncGenerator[str, None]:
        """Handles streaming chat completions with telemetry and exponential backoff retries."""
        import time
        url = f"{self.base_url}/chat/completions"
        model = payload.get("model", "unknown")
        max_tokens = payload.get("max_tokens", "default")
        payload["stream"] = True
        
        start_time = time.time()
        first_token_time = None
        
        for attempt in range(5): # Increased to 5 attempts for streaming
            try:
                # Debug logging to file
                log_path = r"c:\Users\denis\OneDrive\Escritorio\GhostLLM\nvidia_debug.log"
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n[{time.ctime()}] UPSTREAM STREAM REQ: model={model}, max_tokens={max_tokens}, attempt={attempt+1}\n")
                
                async with self.client.stream("POST", url, json=payload, headers=self._get_headers(stream=True)) as response:
                    if response.status_code == 429:
                        error_text = (await response.aread()).decode()
                        logger.warning(f"UPSTREAM STREAM 429: attempt={attempt+1}. Retrying...")
                        # Exponential backoff: 1s, 2s, 4s, 8s
                        await asyncio.sleep(2**attempt)
                        continue
                        
                    if response.status_code != 200:
                        error_text = (await response.aread()).decode()
                        logger.error(f"UPSTREAM STREAM ERROR: status={response.status_code}")
                        raise self._map_error(response.status_code, error_text)

                    async for line in response.aiter_lines():
                        if line.strip():
                            if first_token_time is None:
                                first_token_time = time.time()
                                logger.info(f"UPSTREAM STREAM START: TTFT={(first_token_time - start_time):.3f}s")
                            yield f"{line}\n\n"
                    return 
            except (httpx.RequestError, httpx.TimeoutException) as e:
                logger.error(f"UPSTREAM STREAM NET ERROR: {str(e)}, attempt={attempt+1}")
                if attempt == 4:
                    # Final attempt failed
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                    yield "data: [DONE]\n\n"
                    raise NVIDIAError(f"Stream Network Error: {str(e)}")
                await asyncio.sleep(1.0)
        
        # If we reach here, 429 retries were exhausted
        err_msg = "Max streaming retries exceeded (429)"
        yield f"data: {json.dumps({'error': err_msg})}\n\n"
        yield "data: [DONE]\n\n"
        raise NVIDIAError(err_msg, 429)

    async def close(self):
        await self.client.aclose()
