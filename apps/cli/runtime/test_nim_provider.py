"""NIM provider unit tests — mocked HTTP, no live NIM required."""
from __future__ import annotations

import os
import unittest
from unittest import mock

import httpx

from apps.cli.runtime.nim_provider import (
    ProviderRequest,
    ProviderResponse,
    build_chat_completions_payload,
    nim_chat_complete,
)
from apps.cli.runtime.nim_smoke_test import run_nim_smoke_test


class TestNimProvider(unittest.TestCase):
    def test_payload_shape(self) -> None:
        req = ProviderRequest(model="m1", system="s", user="u", temperature=0.1, max_tokens=100)
        p = build_chat_completions_payload(req)
        self.assertEqual(p["model"], "m1")
        self.assertEqual(len(p["messages"]), 2)
        self.assertEqual(p["messages"][0]["role"], "system")
        self.assertEqual(p["messages"][1]["content"], "u")

    def test_env_missing_returns_graceful(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"GHOST_NIM_BASE_URL": "", "GHOST_NIM_API_KEY": ""},
            clear=False,
        ):
            r = run_nim_smoke_test("any", "hi")
        self.assertFalse(r.ok)
        self.assertIn("missing", r.error_message)

    def test_empty_choices(self) -> None:
        class Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"choices": [], "usage": {}}

        sess = mock.MagicMock()
        sess.post.return_value = Resp()
        r = nim_chat_complete(
            "https://x.com",
            "k",
            ProviderRequest(model="m", user="u"),
            session=sess,
        )
        self.assertFalse(r.ok)
        self.assertIn("empty", r.error_message.lower())

    def test_http_401(self) -> None:
        class Resp:
            status_code = 401
            text = "no"

            def json(self):
                return {}

        sess = mock.MagicMock()
        sess.post.return_value = Resp()
        r = nim_chat_complete(
            "https://x.com",
            "k",
            ProviderRequest(model="m", user="u"),
            session=sess,
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.error_detail.http_status if r.error_detail else None, 401)

    def test_http_429(self) -> None:
        class Resp:
            status_code = 429
            text = "slow"

            def json(self):
                return {}

        sess = mock.MagicMock()
        sess.post.return_value = Resp()
        r = nim_chat_complete("https://x.com", "k", ProviderRequest(model="m", user="u"), session=sess)
        self.assertFalse(r.ok)
        self.assertIn("rate", r.error_message.lower())

    def test_success_parses_content(self) -> None:
        class Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {
                    "model": "m",
                    "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                }

        sess = mock.MagicMock()
        sess.post.return_value = Resp()
        r = nim_chat_complete("https://x.com", "k", ProviderRequest(model="m", user="u"), session=sess)
        self.assertTrue(r.ok)
        self.assertEqual(r.raw_text, "hello")
        self.assertGreater(r.timing.latency_ms, 0)

    def test_empty_model_output(self) -> None:
        class Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {
                    "choices": [{"message": {"content": "   "}, "finish_reason": "stop"}],
                    "usage": {},
                }

        sess = mock.MagicMock()
        sess.post.return_value = Resp()
        r = nim_chat_complete("https://x.com", "k", ProviderRequest(model="m", user="u"), session=sess)
        self.assertFalse(r.ok)
        self.assertIn("empty", r.error_message.lower())

    def test_timeout_path(self) -> None:
        sess = mock.MagicMock()
        sess.post.side_effect = httpx.TimeoutException("timeout")
        r = nim_chat_complete("https://x.com", "k", ProviderRequest(model="m", user="u"), session=sess)
        self.assertFalse(r.ok)
        self.assertIn("timeout", r.error_message.lower())

    def test_smoke_test_structured_response_with_mock(self) -> None:
        class Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {},
                }

        sess = mock.MagicMock()
        sess.post.return_value = Resp()
        env = {
            "GHOST_NIM_BASE_URL": "https://nim.test",
            "GHOST_NIM_API_KEY": "secret",
            "GHOST_USE_NVIDIA_NIM": "1",
        }
        with mock.patch.dict("os.environ", env, clear=False):
            r = nim_chat_complete(
                "https://nim.test",
                "secret",
                ProviderRequest(model="vendor/test-coder", user="ping"),
                session=sess,
            )
        self.assertIsInstance(r, ProviderResponse)
        self.assertTrue(r.ok)


if __name__ == "__main__":
    unittest.main()
