"""Server boot: model registry globals; /v1/models and /health inspectable."""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest.mock import patch

root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root not in sys.path:
    sys.path.insert(0, root)
if os.path.join(root, "packages", "py-core") not in sys.path:
    sys.path.insert(0, os.path.join(root, "packages", "py-core"))

try:
    import jose  # noqa: F401 — apps.server.auth.security
except ImportError:
    _SERVER_AUTH_AVAILABLE = False
else:
    _SERVER_AUTH_AVAILABLE = True


def setUpModule() -> None:
    if not _SERVER_AUTH_AVAILABLE:
        raise unittest.SkipTest("python-jose not installed; skip FastAPI server integration tests")


class TestServerModelRegistryBoot(unittest.TestCase):
    def test_v1_models_ok_and_no_nameerror_with_real_registry(self) -> None:
        from fastapi.testclient import TestClient

        import apps.server.main as server_main

        client = TestClient(server_main.app)
        r = client.get("/v1/models")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body.get("object"), "list")
        ids = {row.get("id") for row in body.get("data", [])}
        if server_main.MODEL_REGISTRY_ERROR is None:
            self.assertIn("coder", ids)
            self.assertFalse("ghost_diagnostics" in body)
        else:
            self.assertIn("ghost_diagnostics", body)

    def test_health_reports_model_registry_block(self) -> None:
        from fastapi.testclient import TestClient

        import apps.server.main as server_main

        client = TestClient(server_main.app)
        r = client.get("/health")
        self.assertEqual(r.status_code, 200, r.text)
        h = r.json()
        self.assertIn("model_registry", h)
        mr = h["model_registry"]
        self.assertIn("healthy", mr)
        self.assertIn("enabled_count", mr)
        self.assertIn("error", mr)
        self.assertIn("registry_path", mr)

    def test_is_model_allowed_accepts_unique_short_upstream_basename(self) -> None:
        import apps.server.main as server_main

        if server_main.MODEL_REGISTRY_ERROR:
            self.skipTest("registry not loaded in this environment")
        self.assertTrue(server_main.is_model_allowed("qwen3-coder-480b-a35b-instruct"))

    def test_list_models_when_bootstrap_fails_no_crash(self) -> None:
        """Import server with failed bootstrap — must not raise NameError on /v1/models."""
        mod_name = "apps.server.main"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        try:
            with patch(
                "ghostllm_core.config.bootstrap_enabled_models",
                return_value=({}, {}, "ValidationError: forced test"),
            ):
                sm = importlib.import_module(mod_name)
            self.assertEqual(sm.ENABLED_MODELS, {})
            self.assertIsNotNone(sm.MODEL_REGISTRY_ERROR)
            from fastapi.testclient import TestClient

            r = TestClient(sm.app).get("/v1/models")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body.get("object"), "list")
            diag = body.get("ghost_diagnostics") or {}
            self.assertFalse(diag.get("model_registry_healthy", True))
            self.assertIn("registry_load_error", diag)
        finally:
            if mod_name in sys.modules:
                del sys.modules[mod_name]
            importlib.import_module(mod_name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
