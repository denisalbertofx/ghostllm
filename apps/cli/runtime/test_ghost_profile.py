"""Tests for operational UX profiles (ghost fast, safe, …)."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from apps.cli.runtime.ghost_profile import (
    PROFILE_IDS,
    apply_operational_profile_to_environment,
    maybe_coerce_intent_for_operational_mode,
    resolve_operational_profile,
    resolve_models_yaml_path,
)
from apps.cli.runtime.task_contract import Intent
from apps.cli.runtime.repo_retrieval import build_retrieval_query_from_contract_spec


class TestGhostProfile(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in list(os.environ.keys()) if k.startswith("GHOST_") or k.startswith("_GHOST_")}

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k in (
            "GHOST_ACTIVE_PROFILE",
            "GHOST_RESOLVED_SESSION_MODEL",
            "GHOST_OPERATIONAL_MODE",
            "_GHOST_PROFILE_AUTO_APPROVE",
            "GHOST_VERIFICATION_LEVEL",
            "GHOST_PARALLEL_PIPELINE",
            "GHOST_RETRIEVAL_AGGRESSION",
        ):
            if k not in self._saved:
                os.environ.pop(k, None)

    def test_profile_ids_complete(self) -> None:
        self.assertEqual(PROFILE_IDS, {"simple", "dev", "fast", "turbo", "safe"})

    def test_resolve_simple_parallel_partial(self) -> None:
        root = Path(__file__).resolve().parents[3]
        r = resolve_operational_profile("simple", str(root), root)
        self.assertEqual(r.parallel_pipeline, "partial")

    def test_resolve_fast_defaults(self) -> None:
        root = Path(__file__).resolve().parents[3]
        r = resolve_operational_profile("fast", str(root), root)
        self.assertEqual(r.profile_id, "fast")
        self.assertEqual(r.session_model_alias, "fast")
        self.assertEqual(r.operational_mode, "auto")
        self.assertTrue(r.auto_approve)
        self.assertEqual(r.parallel_pipeline, "on")

    def test_apply_sets_active_profile(self) -> None:
        root = Path(__file__).resolve().parents[3]
        r = resolve_operational_profile("simple", str(root), root)
        apply_operational_profile_to_environment("simple", str(root), root)
        self.assertEqual(os.environ.get("GHOST_ACTIVE_PROFILE"), "simple")
        self.assertEqual(os.environ.get("GHOST_RESOLVED_SESSION_MODEL"), r.session_model_alias)
        self.assertEqual(os.environ.get("GHOST_OPERATIONAL_MODE"), "chat")

    def test_coerce_auto_to_execute(self) -> None:
        os.environ["GHOST_OPERATIONAL_MODE"] = "auto"
        try:
            it = Intent(mode="Chat", task="Add OAuth2 to the API", original_text="x")
            maybe_coerce_intent_for_operational_mode(it)
            self.assertEqual(it.mode, "Execute")
        finally:
            os.environ.pop("GHOST_OPERATIONAL_MODE", None)

    def test_coerce_short_question_stays_chat(self) -> None:
        os.environ["GHOST_OPERATIONAL_MODE"] = "auto"
        try:
            it = Intent(mode="Chat", task="What is asyncio?", original_text="x")
            maybe_coerce_intent_for_operational_mode(it)
            self.assertEqual(it.mode, "Chat")
        finally:
            os.environ.pop("GHOST_OPERATIONAL_MODE", None)

    def test_retrieval_aggressive_max_hits(self) -> None:
        os.environ["GHOST_RETRIEVAL_AGGRESSION"] = "aggressive"
        try:
            q = build_retrieval_query_from_contract_spec(
                {"intent": "implement x", "scope": [], "target_files": []},
                {},
            )
            self.assertEqual(q.max_hits, 24)
        finally:
            os.environ.pop("GHOST_RETRIEVAL_AGGRESSION", None)

    def test_models_yaml_path_prefers_cwd(self) -> None:
        root = Path(__file__).resolve().parents[3]
        p = resolve_models_yaml_path(str(root), root)
        self.assertTrue(p.name == "models.yaml")


if __name__ == "__main__":
    unittest.main()
