from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root not in sys.path:
    sys.path.insert(0, root)
py_core = os.path.join(root, "packages", "py-core")
if py_core not in sys.path:
    sys.path.insert(0, py_core)


class _StubProvider:
    async def chat_completions(self, payload):
        return {
            "id": "chatcmpl-smoke",
            "object": "chat.completion",
            "model": payload.get("model", "moonshotai/kimi-k2.5"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ghost smoke ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }


class TestClaudeBridgeSmoke(unittest.TestCase):
    def test_v1_messages_bridge_returns_anthropic_message_shape(self) -> None:
        import apps.server.main as server_main

        fake_user = server_main.User(
            id=1,
            username="ghost",
            hashed_password="x",
            api_key="ghost-dev-2026",
            role="admin",
            quota_limit=999999,
            quota_used=0,
            is_active=True,
        )

        server_main.app.dependency_overrides[server_main.get_current_user] = lambda: fake_user
        original_provider = server_main.nvidia_provider
        try:
            server_main.nvidia_provider = _StubProvider()
            with patch("apps.server.main.log_usage") as log_usage_mock, TestClient(server_main.app) as client:
                response = client.post(
                    "/v1/messages",
                    headers={
                        "x-api-key": "ghost-dev-2026",
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "kimi",
                        "messages": [{"role": "user", "content": "say hello"}],
                        "max_tokens": 16,
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body.get("type"), "message")
            self.assertEqual(body.get("role"), "assistant")
            self.assertEqual(body.get("model"), "kimi")
            self.assertTrue(body.get("content"))
            self.assertEqual(body["content"][0]["type"], "text")
            self.assertIn("ghost smoke ok", body["content"][0]["text"])
            log_usage_mock.assert_called_once()
        finally:
            server_main.nvidia_provider = original_provider
            server_main.app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
