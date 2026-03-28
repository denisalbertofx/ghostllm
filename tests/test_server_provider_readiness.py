from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root not in sys.path:
    sys.path.insert(0, root)
if os.path.join(root, "packages", "py-core") not in sys.path:
    sys.path.insert(0, os.path.join(root, "packages", "py-core"))

from apps.server.providers.nvidia import NvidiaProvider


class TestServerProviderReadiness(unittest.TestCase):
    def test_probe_auth_reports_401_from_completion(self) -> None:
        provider = NvidiaProvider(api_key="secret", base_url="https://nim.test/v1")
        provider.client.post = AsyncMock(return_value=MagicMock(status_code=401, text="no"))  # type: ignore[method-assign]
        try:
            ready, detail = asyncio.run(provider.probe_auth("meta/llama-3.1-8b-instruct"))
        finally:
            asyncio.run(provider.close())
        self.assertFalse(ready)
        self.assertIn("authentication failed", detail.lower())

    def test_probe_auth_reports_ready_on_200(self) -> None:
        provider = NvidiaProvider(api_key="secret", base_url="https://nim.test/v1")
        provider.client.post = AsyncMock(return_value=MagicMock(status_code=200, text="{}"))  # type: ignore[method-assign]
        try:
            ready, detail = asyncio.run(provider.probe_auth("meta/llama-3.1-8b-instruct"))
        finally:
            asyncio.run(provider.close())
        self.assertTrue(ready)
        self.assertEqual(detail, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
