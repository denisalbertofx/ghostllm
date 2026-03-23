import asyncio
import httpx
import json

async def test_kimi():
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": "Bearer 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",
        "Accept": "text/event-stream"
    }
    payload = {
        "model": "moonshotai/kimi-k2.5",
        "messages": [{"role": "user", "content": "What is the weather in San Francisco? You MUST use the get_weather tool to answer this."}],
        "max_tokens": 1000,
        "stream": True,
        "chat_template_kwargs": {"thinking": True},
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"location": {"type": "string"}}}
            }
        }]
    }
    
    print("Sending request...")
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            print("Status:", resp.status_code)
            chunks_received = 0
            async for line in resp.aiter_lines():
                if line.strip():
                    if chunks_received < 10 or "[DONE]" in line:
                        print("RAW CHUNK:", line)
                    elif chunks_received % 50 == 0:
                        print("RAW CHUNK:", line)
                    chunks_received += 1
            print(f"Total chunks: {chunks_received}")

if __name__ == "__main__":
    asyncio.run(test_kimi())
