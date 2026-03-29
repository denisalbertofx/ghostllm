import json
import httpx

base_url = "http://127.0.0.1:11434"
api_key = "fg-SP32l6ibdFIXEQyk1Nov1KqQYhKHxM0jM1T2amKu--s" # Hardcoded default admin key from setup

headers = {
    "x-api-key": api_key,
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "max-tokens-3-5-sonnet-2024-07-15",
    "Content-Type": "application/json"
}

def test_count_tokens():
    print("Testing /v1/messages/count_tokens...")
    payload = {
        "model": "coder",
        "messages": [{"role": "user", "content": "Hello, how are you?"}]
    }
    resp = httpx.post(f"{base_url}/v1/messages/count_tokens", headers=headers, json=payload, timeout=30.0)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.json()}\n")

def test_messages():
    print("Testing /v1/messages...")
    payload = {
        "model": "coder",
        "messages": [{"role": "user", "content": "Say 'hello world' and nothing else."}],
        "max_tokens": 100
    }
    resp = httpx.post(f"{base_url}/v1/messages", headers=headers, json=payload, timeout=30.0)
    print(f"Status: {resp.status_code}")
    print("Headers:", resp.headers)
    print(f"Response: {resp.json()}\n")

if __name__ == "__main__":
    test_count_tokens()
    test_messages()
