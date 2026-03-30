import json
import httpx

base_url = "http://127.0.0.1:11434/v1"
api_key = "ghost-dev-2026" # Default Admin API Key

headers = {
    "Authorization": f"Bearer {api_key}",
    "x-api-key": api_key,
    "Content-Type": "application/json"
}

def test_ghost_api():
    print("Testing GhostLLM (OpenAI API) compatibility on /v1/chat/completions...")
    payload = {
        "model": "fast",
        "messages": [{"role": "user", "content": "Write a 1-line python hello world."}],
        "max_tokens": 50
    }
    resp = httpx.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=30.0)
    print(f"Status: {resp.status_code}")
    try:
        print(f"Response: {resp.json()}\n")
    except json.JSONDecodeError:
        print(f"Raw Response: {resp.text}\n")

if __name__ == "__main__":
    test_ghost_api()
