"""Model registry YAML parsing (ghostllm_core) — aligned with configs/models.yaml."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import yaml

root = Path(__file__).resolve().parents[1]


class TestModelRegistryConfig(unittest.TestCase):
    def test_real_models_yaml_loads_without_validation_error(self) -> None:
        from ghostllm_core.config import load_registry

        path = root / "configs" / "models.yaml"
        self.assertTrue(path.is_file(), f"missing {path}")
        reg = load_registry(str(path))
        names = {m.name for m in reg.models}
        self.assertIn("planner", names)
        self.assertIn("coder", names)
        self.assertIn("smart", names)
        planner = next(m for m in reg.models if m.name == "planner")
        self.assertTrue(planner.tool_calling)
        self.assertTrue(planner.expensive)
        fast = next(m for m in reg.models if m.name == "fast")
        self.assertTrue(fast.tool_calling)
        self.assertFalse(fast.expensive)

    def test_bootstrap_missing_file_returns_error(self) -> None:
        from ghostllm_core.config import bootstrap_enabled_models

        enabled, mapping, err = bootstrap_enabled_models("/nonexistent/path/ghost_models.yaml")
        self.assertIsNotNone(err)
        self.assertIn("FileNotFoundError", err)
        self.assertEqual(enabled, {})
        self.assertEqual(mapping, {})

    def test_bootstrap_enabled_models_succeeds_on_repo_file(self) -> None:
        from ghostllm_core.config import bootstrap_enabled_models

        path = str(root / "configs" / "models.yaml")
        enabled, mapping, err = bootstrap_enabled_models(path)
        self.assertIsNone(err, err)
        self.assertIn("coder", enabled)
        self.assertEqual(mapping.get("coder"), "qwen/qwen3-coder-480b-a35b-instruct")
        self.assertIn("openai/gpt-oss-120b", mapping.values())

    def test_bootstrap_never_raises_on_bad_yaml(self) -> None:
        from ghostllm_core.config import bootstrap_enabled_models

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write("models:\n  - not_a_mapping\n")
            bad_path = f.name
        try:
            enabled, mapping, err = bootstrap_enabled_models(bad_path)
            self.assertIsNotNone(err)
            self.assertEqual(enabled, {})
            self.assertEqual(mapping, {})
        finally:
            os.unlink(bad_path)

    def test_unknown_per_model_fields_are_ignored(self) -> None:
        from ghostllm_core.config import load_registry

        doc = {
            "models": [
                {
                    "name": "x",
                    "upstream_id": "vendor/x",
                    "enabled": True,
                    "tool_calling": True,
                    "expensive": False,
                    "future_field_xyz": 123,
                }
            ]
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            yaml.safe_dump(doc, f)
            p = f.name
        try:
            reg = load_registry(p)
            self.assertEqual(len(reg.models), 1)
            self.assertEqual(reg.models[0].name, "x")
        finally:
            os.unlink(p)

    def test_resolve_upstream_model_id_key_full_and_basename(self) -> None:
        from ghostllm_core.config import resolve_upstream_model_id

        m = {"coder": "qwen/qwen3-coder-480b-a35b-instruct", "smart": "openai/gpt-oss-120b"}
        self.assertEqual(
            resolve_upstream_model_id("coder", m),
            "qwen/qwen3-coder-480b-a35b-instruct",
        )
        self.assertEqual(
            resolve_upstream_model_id("qwen/qwen3-coder-480b-a35b-instruct", m),
            "qwen/qwen3-coder-480b-a35b-instruct",
        )
        self.assertEqual(
            resolve_upstream_model_id("qwen3-coder-480b-a35b-instruct", m),
            "qwen/qwen3-coder-480b-a35b-instruct",
        )

    def test_resolve_upstream_model_id_ambiguous_basename_unchanged(self) -> None:
        from ghostllm_core.config import resolve_upstream_model_id

        m = {"a": "vendor1/same", "b": "vendor2/same"}
        self.assertEqual(resolve_upstream_model_id("same", m), "same")

    def test_resolve_gateway_model_id_uses_registry_file(self) -> None:
        from ghostllm_core.config import resolve_gateway_model_id

        path = str(root / "configs" / "models.yaml")
        self.assertEqual(
            resolve_gateway_model_id("qwen3-coder-480b-a35b-instruct", path),
            "qwen/qwen3-coder-480b-a35b-instruct",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
