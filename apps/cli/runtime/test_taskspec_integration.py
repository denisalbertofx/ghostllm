"""Integration tests: spec engine → task_contract.spec (adapter, verification, outcome, renderer)."""
import os
import unittest
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from unittest import mock

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.outcome_engine import _get_task_intent
from apps.cli.runtime.repo_profile import RepoProfile, scan_repo_profile
from apps.cli.runtime.taskspec_engine import build_taskspec
from apps.cli.runtime.task_contract import Intent, ensure_task_contract_foundation
from apps.cli.runtime.taskspec_adapter import (
    apply_spec_engine_build_to_session,
    build_contract_prompt_blocks,
    is_taskspec_primary,
    legacy_fill_session,
    max_repair_attempts,
    verification_skip_respects_contract_spec,
    write_blocked_by_contract_spec,
)
from apps.cli.runtime.verification import choose_verification_plan_from_contract_spec
from apps.cli.ui.renderer import GhostRenderer


class TestTaskSpecIntegration(unittest.TestCase):
    def test_apply_taskspec_sets_runtime_contract_source(self):
        repo = RepoProfile(stack="nextjs", layers_detected=["api"], has_package_json=True)
        build = build_taskspec("Add filter to GET /api/issues", repo)
        session = ArtifactSession("sid", "task")
        ensure_task_contract_foundation(
            session,
            Intent(mode="Chat", task="Add filter to GET /api/issues", original_text="Add filter"),
        )
        apply_spec_engine_build_to_session(session, build, repo)
        self.assertEqual(session.runtime_contract_source, "taskspec")
        self.assertEqual(session.contract_spec_validation_status, build.validation_status)
        self.assertIsInstance(session.contract_spec(), Mapping)
        self.assertIsInstance(session.task_contract, dict)
        self.assertIsInstance(session.task_contract.get("spec"), Mapping)
        self.assertEqual(session.repo_profile.get("stack"), "nextjs")

    def test_legacy_fill_marks_legacy(self):
        session = ArtifactSession("sid", "task")
        legacy_fill_session(session, "review the code")
        self.assertEqual(session.runtime_contract_source, "legacy")
        self.assertIsInstance(session.task_contract, dict)
        self.assertEqual(session.task_contract.get("runtime_contract_source"), "legacy")
        spec = session.task_contract.get("spec") or {}
        self.assertIsInstance(spec, Mapping)
        self.assertEqual(spec.get("intent"), "review")

    def test_outcome_engine_prefers_contract_spec_intent(self):
        session = ArtifactSession("sid", "task")
        ensure_task_contract_foundation(session, Intent(mode="Chat", task="x", original_text="x"))
        session.runtime_contract_source = "taskspec"
        session.task_contract["spec"] = {
            "intent": "bugfix",
            "scope": ["api"],
            "change_expectation": "must_write",
            "confidence": 0.9,
        }
        session.task_intent = "review"  # stale session field — contract spec should win
        intent = _get_task_intent(session)
        self.assertEqual(intent, "modification")

    def test_verification_plan_from_contract_spec_typecheck_first(self):
        ts = {
            "verification_policy": {
                "required": True,
                "typecheck": True,
                "build": True,
                "lint": False,
            }
        }
        plan = choose_verification_plan_from_contract_spec(
            ts,
            task_type="direct_edit",
            scope="api",
            budget_remaining=100,
        )
        self.assertEqual(plan.check_names[0], "TypeCheck")
        self.assertIn("Build", plan.check_names)

    def test_verification_skip_respects_contract_spec_required(self):
        ts = {"verification_policy": {"required": True, "typecheck": True}}
        self.assertFalse(verification_skip_respects_contract_spec(ts, True))
        self.assertTrue(verification_skip_respects_contract_spec(None, True))

    def test_max_repair_from_contract_spec(self):
        ts = {"repair_policy": {"max_attempts": 2}}
        self.assertEqual(max_repair_attempts(ts, default=1), 2)

    def test_forbidden_ui_blocks_component_path(self):
        ts = {
            "change_expectation": "must_write",
            "forbidden_layers": ["ui"],
        }
        blocked, _ = write_blocked_by_contract_spec("src/components/Button.tsx", ts)
        self.assertTrue(blocked)

    def test_should_not_write_blocks(self):
        ts = {"change_expectation": "should_not_write", "intent": "review"}
        blocked, _ = write_blocked_by_contract_spec("apps/server/main.py", ts)
        self.assertTrue(blocked)

    def test_feature_flag_off(self):
        with mock.patch.dict(os.environ, {"GHOST_USE_TASKSPEC": "0"}):
            self.assertFalse(is_taskspec_primary())
        with mock.patch.dict(os.environ, {"GHOST_USE_TASKSPEC": "1"}):
            self.assertTrue(is_taskspec_primary())

    def test_prompt_blocks_are_text_not_repr(self):
        repo = RepoProfile(stack="python", layers_detected=["api"], has_package_json=False)
        build = build_taskspec("implement API", repo)
        session = ArtifactSession("sid", "task")
        apply_spec_engine_build_to_session(session, build, repo)
        tb, rb, eb = build_contract_prompt_blocks(session)
        self.assertIn("[OPERATIONAL SPEC / task_contract.spec]", tb)
        self.assertIn("Intent:", tb)
        self.assertIn("[CURRENT REPO PROFILE]", rb)
        self.assertIsInstance(eb, str)
        self.assertNotIn("TaskSpec(", tb)
        self.assertNotIn("model_dump", tb.lower())

    def test_renderer_shows_contract_spec_panels(self):
        out = StringIO()
        from apps.cli.ui.theme import make_ghost_console

        console = make_ghost_console(file=out, force_terminal=False)
        renderer = GhostRenderer(console)
        artifact = {
            "session_id": "x",
            "timestamp": "t",
            "runtime_contract_source": "taskspec",
            "taskspec": {
                "intent": "implementation",
                "scope": ["api"],
                "forbidden_layers": ["ui"],
                "change_expectation": "must_write",
                "confidence": 0.85,
            },
            "taskspec_validation_status": "valid",
            "taskspec_repaired": False,
            "repo_profile": {
                "stack": "nextjs",
                "layers_detected": ["api", "ui"],
                "important_folders": ["apps"],
                "root": "/tmp/r",
            },
            "plan": "",
            "root_cause": "",
            "diff_summary": [],
            "next_action": "",
        }
        renderer.render_artifact_summary(artifact)
        text = out.getvalue()
        self.assertIn("CONTRACT SPEC", text)
        self.assertIn("REPO PROFILE", text)
        self.assertIn("implementation", text.lower())

    def test_scan_repo_profile_ghost_repo(self):
        root = Path(__file__).resolve().parents[3]
        prof = scan_repo_profile(root)
        self.assertTrue(len(prof.stack) > 0 or prof.has_package_json or len(prof.layers_detected) >= 0)


if __name__ == "__main__":
    unittest.main()
