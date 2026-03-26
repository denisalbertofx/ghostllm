"""Integration: RepoProfile v2 drives exploration ranking, verification scope, and UI read blocks."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.exploration_planner import build_exploration_plan, path_under_ui_roots
from apps.cli.runtime.repo_profile import RepoProfile
from apps.cli.runtime.repo_profile_v2 import (
    ApiRouteEntry,
    EntrypointsBlock,
    RepoProfileV2,
    StackBlock,
    VerificationCommandsBlock,
)
from apps.cli.runtime.taskspec_engine import build_taskspec
from apps.cli.runtime.taskspec_adapter import apply_spec_engine_build_to_session
from apps.cli.runtime.verification import VerificationManager, choose_verification_plan_from_contract_spec


def _sample_v2(root: str = "/tmp/repo") -> RepoProfileV2:
    return RepoProfileV2(
        root=root,
        generated_at=datetime.now(timezone.utc),
        stack=StackBlock(language=["typescript"], framework=["nextjs"], runtime="node"),
        layers_detected=["api", "data", "ui"],
        entrypoints=EntrypointsBlock(
            api_roots=["app/api"],
            db_schema_roots=["lib/db"],
            ui_roots=["src/app", "components"],
        ),
        api_routes=[
            ApiRouteEntry(
                file="app/api/issues/route.ts",
                method="GET",
                path="/api/issues",
                kind="route",
            )
        ],
        db_schema_files=["lib/db/schema.ts"],
        validation_files=["lib/validations/issues.ts"],
        key_files=["package.json", "src/app/page.tsx"],
        verification_commands=VerificationCommandsBlock(
            typecheck="pnpm exec tsc --noEmit",
            build="pnpm run build",
            lint="pnpm run lint",
            source=["package.json", "AGENTS.md"],
        ),
    )


class TestRepoProfileRuntimeIntegration(unittest.TestCase):
    def test_api_scope_prioritizes_routes_schema_validation(self):
        v2 = _sample_v2()
        ts = {
            "intent": "modification",
            "scope": ["api"],
            "forbidden_layers": [],
            "change_expectation": "must_write",
            "confidence": 0.9,
        }
        plan = build_exploration_plan(ts, v2.model_dump(mode="json"), "Add filter to GET /api/issues")
        files = plan.ranked_files
        self.assertIn("app/api/issues/route.ts", files)
        self.assertIn("lib/db/schema.ts", files)
        self.assertIn("lib/validations/issues.ts", files)
        idx_route = files.index("app/api/issues/route.ts")
        idx_schema = files.index("lib/db/schema.ts")
        self.assertLess(idx_route, idx_schema)
        self.assertTrue(any("App Router route" in e for e in plan.evidence_lines))
        self.assertTrue(any("validation layer" in e for e in plan.evidence_lines))

    def test_forbidden_ui_filters_ui_paths_and_sets_flag(self):
        v2 = _sample_v2()
        ts = {
            "intent": "modification",
            "scope": ["api"],
            "forbidden_layers": ["ui"],
            "change_expectation": "must_write",
            "confidence": 0.9,
        }
        plan = build_exploration_plan(ts, v2.model_dump(mode="json"), "")
        self.assertTrue(plan.ui_exploration_blocked)
        self.assertNotIn("src/app/page.tsx", plan.ranked_files)
        self.assertTrue(any("blocked" in e.lower() or "forbids" in e.lower() for e in plan.evidence_lines))

    def test_missing_v2_records_fallback_evidence(self):
        ts = {"intent": "implementation", "scope": ["api"], "forbidden_layers": []}
        plan = build_exploration_plan(ts, None, "")
        self.assertTrue(
            any("missing" in e.lower() or "incomplete" in e.lower() for e in plan.evidence_lines)
        )

    def test_verification_commands_from_profile_not_generic(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "package.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
            vm = VerificationManager(
                tmp,
                {
                    "typecheck": "pnpm exec tsc --noEmit",
                    "build": "pnpm run build",
                    "lint": "pnpm run lint",
                },
            )
            checks = vm.get_applicable_checks()
            by_name = {c["name"]: c["command"] for c in checks}
            self.assertEqual(by_name.get("TypeCheck"), "pnpm exec tsc --noEmit")
            self.assertEqual(by_name.get("Build"), "pnpm run build")
            self.assertEqual(by_name.get("Lint"), "pnpm run lint")

    def test_verification_plan_uses_repo_v2_scope_when_unknown(self):
        ts = {
            "verification_policy": {
                "required": True,
                "typecheck": True,
                "build": True,
                "lint": False,
            }
        }
        repo_v2 = {
            "layers_detected": ["api"],
            "api_routes": [{"file": "app/api/x/route.ts"}],
            "stack": {"language": ["typescript"]},
        }
        plan = choose_verification_plan_from_contract_spec(
            ts,
            task_type="direct_edit",
            scope="unknown",
            budget_remaining=100,
            repo_v2=repo_v2,
        )
        self.assertEqual(plan.check_names[0], "TypeCheck")

    def test_apply_taskspec_session_stores_exploration_and_influence(self):
        v2 = _sample_v2()
        repo = RepoProfile(
            root=Path("/tmp/repo"),
            stack="nextjs",
            layers_detected=["api"],
            has_package_json=True,
            v2=v2,
        )
        build = build_taskspec("Tune GET /api/issues query params", repo)
        session = ArtifactSession("sid", "task")
        apply_spec_engine_build_to_session(session, build, repo, user_prompt="GET /api/issues")
        self.assertTrue(session.exploration_plan.get("ranked_files"))
        self.assertTrue(session.repo_profile_influence)
        self.assertTrue(
            any(
                "RepoProfile" in str(ev.get("details", ""))
                for ev in session.events
                if ev.get("event") == "RepoProfileInfluence"
            )
        )

    def test_path_under_ui_roots(self):
        self.assertTrue(path_under_ui_roots("src/app/page.tsx", ["src/app"]))
        self.assertFalse(path_under_ui_roots(".", ["src/app"]))
        self.assertFalse(path_under_ui_roots("app/api/route.ts", ["src/app"]))


if __name__ == "__main__":
    unittest.main()
