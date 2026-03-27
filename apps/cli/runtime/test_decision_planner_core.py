"""Decision Planner v1 — deterministic core and advisory integration tests."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.task_contract import Intent, ensure_task_contract_foundation
from apps.cli.runtime.decision_planner import (
    PlannerInput,
    PlannerOutput,
    _is_doc_file,
    _norm_path,
    apply_decision_planner_to_session,
    build_decision_planner_prompt_block,
    build_execution_plan,
    build_exploration_plan,
    build_plans,
    build_repair_plan,
    build_verification_plan,
    compute_risk_score,
    is_decision_planner_enabled,
    make_session_state_for_planner,
    normalize_verification_steps,
)


def _repo_v2_api() -> dict:
    return {
        "entrypoints": {
            "api_roots": ["app/api"],
            "ui_roots": ["src/app", "components"],
        },
        "api_routes": [
            {"file": "app/api/issues/route.ts", "method": "GET", "path": "/api/issues"},
        ],
        "validation_files": ["lib/validations/issues.ts"],
        "db_schema_files": ["lib/db/schema.ts"],
        "key_files": ["package.json", "tsconfig.json"],
        "stack": {"language": ["typescript"]},
        "verification_commands": {
            "typecheck": "pnpm exec tsc --noEmit",
            "build": "pnpm run build",
            "lint": "pnpm run lint",
            "source": ["package.json"],
        },
    }


class TestDecisionPlannerCore(unittest.TestCase):
    def test_api_task_ranks_route_validation_schema_order(self):
        ts = {
            "intent": "modification",
            "scope": ["api"],
            "target_files": [],
            "forbidden_layers": [],
        }
        sess = make_session_state_for_planner(budget_remaining=100)
        ex = build_exploration_plan(ts, _repo_v2_api(), sess)
        paths = [c.path for c in ex.ranked_candidates if c.kind == "file"]
        self.assertIn("app/api/issues/route.ts", paths)
        self.assertIn("lib/validations/issues.ts", paths)
        self.assertIn("lib/db/schema.ts", paths)
        ri = paths.index("app/api/issues/route.ts")
        vi = paths.index("lib/validations/issues.ts")
        si = paths.index("lib/db/schema.ts")
        self.assertLess(ri, vi)
        self.assertLess(vi, si)

    def test_forbidden_ui_excludes_ui_roots(self):
        ts = {
            "intent": "modification",
            "scope": ["api"],
            "forbidden_layers": ["ui"],
            "target_files": ["src/app/page.tsx"],
        }
        ex = build_exploration_plan(ts, _repo_v2_api(), make_session_state_for_planner(budget_remaining=100))
        paths = [c.path for c in ex.ranked_candidates if c.kind == "file"]
        self.assertNotIn("src/app/page.tsx", paths)

    def test_cheap_first_typecheck_before_build(self):
        ts = {
            "intent": "modification",
            "scope": ["api"],
            "verification_policy": {"required": True, "typecheck": True, "build": True, "lint": False},
        }
        repo = _repo_v2_api()
        sess = make_session_state_for_planner(budget_remaining=100)
        ex = build_exploration_plan(ts, repo, sess)
        ep = build_execution_plan(ts, repo, ex, sess)
        vp = build_verification_plan(ts, repo, ep, sess)
        checks = [s.check for s in vp.steps]
        if "typecheck" in checks and "build" in checks:
            self.assertLess(checks.index("typecheck"), checks.index("build"))

    def test_build_only_when_policy_risk_or_config(self):
        ts = {
            "intent": "modification",
            "scope": ["api"],
            "verification_policy": {"required": True, "typecheck": True, "build": False, "lint": False},
            "target_files": ["app/api/x.ts"],
        }
        repo = {
            "api_routes": [],
            "validation_files": [],
            "db_schema_files": [],
            "key_files": [],
            "stack": {"language": ["typescript"]},
            "entrypoints": {},
            "verification_commands": {"typecheck": "tsc", "build": "npm run build"},
        }
        sess = make_session_state_for_planner(budget_remaining=100)
        ex = build_exploration_plan(ts, repo, sess)
        ep = build_execution_plan(ts, repo, ex, sess)
        vp = build_verification_plan(ts, repo, ep, sess)
        self.assertFalse(any(s.check == "build" for s in vp.steps))

    def test_repair_plan_failed_check_and_rerun_first(self):
        ts = {"repair_policy": {"max_attempts": 2}}
        sess = make_session_state_for_planner(
            budget_remaining=50,
            last_verification={
                "checks": [
                    {
                        "name": "TypeCheck",
                        "status": "failed",
                        "command": "npx tsc --noEmit",
                        "output_summary": "error TS2322",
                    }
                ]
            },
            repair_attempt_count=1,
        )
        rp = build_repair_plan(ts, sess)
        self.assertTrue(rp.rerun_failed_first)
        self.assertEqual(rp.max_attempts, 2)
        self.assertIsNotNone(rp.last_failed_check)
        assert rp.last_failed_check is not None
        self.assertEqual(rp.last_failed_check.check, "typecheck")
        self.assertTrue(any("typecheck" in a.reason.lower() for a in rp.next_actions))

    def test_budget_low_reduces_exploration_breadth(self):
        ts = {"intent": "modification", "scope": ["api"], "budget_policy": {"max_reads": 20}}
        repo = _repo_v2_api()
        ex_hi = build_exploration_plan(ts, repo, make_session_state_for_planner(budget_remaining=100))
        ex_lo = build_exploration_plan(ts, repo, make_session_state_for_planner(budget_remaining=15))
        self.assertLessEqual(len(ex_lo.ranked_candidates), len(ex_hi.ranked_candidates))
        self.assertLessEqual(ex_lo.limits.max_reads, ex_hi.limits.max_reads)

    def test_budget_low_single_verification_step(self):
        ts = {
            "intent": "modification",
            "scope": ["api"],
            "verification_policy": {"required": True, "typecheck": True, "build": True, "lint": True},
        }
        repo = _repo_v2_api()
        ex = build_exploration_plan(ts, repo, make_session_state_for_planner(budget_remaining=100))
        ep = build_execution_plan(ts, repo, ex, make_session_state_for_planner(budget_remaining=10))
        vp = build_verification_plan(ts, repo, ep, make_session_state_for_planner(budget_remaining=10))
        if vp.steps:
            self.assertLessEqual(len(vp.steps), 1)

    def test_structured_verification_commands_preserve_cwd(self):
        ts = {
            "intent": "modification",
            "scope": ["api"],
            "verification_policy": {
                "required": True,
                "typecheck": True,
                "build": True,
                "lint": True,
                "tests": True,
            },
        }
        repo = _repo_v2_api()
        repo["verification_commands"] = {
            "typecheck": {"command": "pnpm exec tsc --noEmit", "cwd": "apps/web", "source": ["package_json"]},
            "build": {"command": "pnpm run build", "cwd": "apps/web", "source": ["package_json"]},
            "lint": {"command": "pnpm run lint", "cwd": "apps/web", "source": ["package_json"]},
            "tests": {"command": "pnpm test", "cwd": "apps/web", "source": ["package_json"]},
            "source": ["package_json"],
        }
        sess = make_session_state_for_planner(budget_remaining=100)
        ex = build_exploration_plan(ts, repo, sess)
        ep = build_execution_plan(ts, repo, ex, sess)
        vp = build_verification_plan(ts, repo, ep, sess)
        by_check = {s.check: s for s in vp.steps}
        self.assertEqual(by_check["typecheck"].cwd, "apps/web")
        self.assertEqual(by_check["build"].cwd, "apps/web")
        self.assertEqual(by_check["lint"].cwd, "apps/web")
        self.assertEqual(by_check["tests"].cwd, "apps/web")
        self.assertEqual(by_check["typecheck"].source, "package_json")

    def test_python_target_does_not_schedule_node_typecheck_in_polyglot_repo(self):
        ts = {
            "intent": "bugfix",
            "scope": [],
            "change_expectation": "must_write",
            "target_files": ["apps/cli/main.py"],
            "verification_policy": {"required": True, "typecheck": False, "build": False, "lint": False, "tests": True},
        }
        repo = {
            "entrypoints": {"api_roots": ["apps/server"], "ui_roots": ["apps/web/src/app"]},
            "api_routes": [],
            "validation_files": [],
            "db_schema_files": [],
            "key_files": ["pyproject.toml", "apps/web/package.json"],
            "stack": {"language": ["typescript", "python"]},
            "verification_commands": {
                "typecheck": {"command": "npm run typecheck", "cwd": "apps/web", "source": ["package_json"]},
                "build": {"command": "npm run build", "cwd": "apps/web", "source": ["package_json"]},
                "lint": {"command": "npm run lint", "cwd": "apps/web", "source": ["package_json"]},
                "tests": {"command": "uv run python -m pytest", "cwd": ".", "source": ["defaults"]},
                "source": ["package_json", "defaults"],
            },
        }
        sess = make_session_state_for_planner(budget_remaining=100)
        ex = build_exploration_plan(ts, repo, sess)
        ep = build_execution_plan(ts, repo, ex, sess)
        vp = build_verification_plan(ts, repo, ep, sess)
        checks = [step.check for step in vp.steps]
        self.assertFalse(any(step.check == "typecheck" for step in vp.steps))
        self.assertIn("tests", checks)

    def test_refactor_first_when_intent_refactor(self):
        ts = {
            "intent": "refactor",
            "scope": ["api"],
            "change_expectation": "must_write",
            "target_files": ["lib/util.ts"],
        }
        repo = {
            "api_routes": [],
            "validation_files": [],
            "db_schema_files": [],
            "key_files": [],
            "stack": {"language": ["typescript"]},
            "entrypoints": {},
            "verification_commands": {},
        }
        sess = make_session_state_for_planner(budget_remaining=100)
        ex = build_exploration_plan(ts, repo, sess)
        ep = build_execution_plan(ts, repo, ex, sess)
        self.assertEqual(ep.execution_strategy, "refactor_first")

    def test_no_op_when_should_not_write(self):
        ts = {"intent": "review", "scope": ["api"], "change_expectation": "should_not_write"}
        repo = _repo_v2_api()
        ex = build_exploration_plan(ts, repo, make_session_state_for_planner(budget_remaining=100))
        ep = build_execution_plan(ts, repo, ex, make_session_state_for_planner(budget_remaining=100))
        self.assertEqual(ep.execution_strategy, "no_op")
        self.assertEqual(ep.expected_files_changed, [])

    def test_direct_edit_simple_low_risk(self):
        ts = {
            "intent": "modification",
            "scope": ["api"],
            "change_expectation": "must_write",
            "target_files": ["app/small.ts"],
        }
        repo = {
            "api_routes": [],
            "validation_files": [],
            "db_schema_files": [],
            "key_files": [],
            "stack": {"language": ["typescript"]},
            "verification_commands": {},
        }
        sess = make_session_state_for_planner(budget_remaining=100)
        ex = build_exploration_plan(ts, repo, sess)
        ep = build_execution_plan(ts, repo, ex, sess)
        self.assertEqual(ep.execution_strategy, "direct_edit")
        self.assertEqual(ep.risk.level, "low")

    def test_focused_target_files_lock_expected_files_changed(self):
        ts = {
            "intent": "bugfix",
            "scope": [],
            "change_expectation": "must_write",
            "target_files": ["apps/cli/main.py"],
            "verification_policy": {"required": True, "tests": True},
        }
        repo = _repo_v2_api()
        sess = make_session_state_for_planner(budget_remaining=100)
        ex = build_exploration_plan(ts, repo, sess)
        ep = build_execution_plan(ts, repo, ex, sess)
        self.assertEqual(ep.expected_files_changed, ["apps/cli/main.py"])
        self.assertEqual(ep.execution_strategy, "direct_edit")
        self.assertEqual(ep.blast_radius, "file")

    def test_staged_edit_medium_or_multi_file(self):
        ts = {
            "intent": "modification",
            "scope": ["api", "data"],
            "change_expectation": "must_write",
            "target_files": ["a.ts", "b.ts", "c.ts"],
        }
        repo = _repo_v2_api()
        sess = make_session_state_for_planner(budget_remaining=100)
        ex = build_exploration_plan(ts, repo, sess)
        ep = build_execution_plan(ts, repo, ex, sess)
        self.assertEqual(ep.execution_strategy, "staged_edit")

    def test_planner_output_schema_valid(self):
        inp = PlannerInput(
            taskspec={"intent": "modification", "scope": ["api"], "verification_policy": {"required": True}},
            repo_profile_v2=_repo_v2_api(),
            session_state=make_session_state_for_planner(budget_remaining=100),
        )
        out = build_plans(inp)
        raw = out.model_dump(mode="json")
        restored = PlannerOutput.model_validate(raw)
        self.assertEqual(restored.provenance.planner_version, out.provenance.planner_version)

    def test_advisory_flag_off_no_planner_used(self):
        session = ArtifactSession("s1", "task")
        ensure_task_contract_foundation(session, Intent(mode="Chat", task="x", original_text="x"))
        session.runtime_contract_source = "taskspec"
        session.task_contract["spec"] = {"intent": "modification", "scope": ["api"]}
        session.repo_profile = {"profile_v2": _repo_v2_api()}
        with mock.patch.dict(os.environ, {"GHOST_USE_DECISION_PLANNER": "0"}):
            apply_decision_planner_to_session(session, budget_remaining=100)
        self.assertFalse(session.planner_used)
        self.assertEqual(session.contract_decision_plan(), {})

    def test_advisory_flag_on_artifact_fields(self):
        session = ArtifactSession("s1", "task")
        ensure_task_contract_foundation(session, Intent(mode="Chat", task="x", original_text="x"))
        session.runtime_contract_source = "taskspec"
        session.task_contract["spec"] = {
            "intent": "modification",
            "scope": ["api"],
            "verification_policy": {"required": True, "typecheck": True},
        }
        session.repo_profile = {"profile_v2": _repo_v2_api()}
        with mock.patch.dict(os.environ, {"GHOST_USE_DECISION_PLANNER": "1"}):
            apply_decision_planner_to_session(session, budget_remaining=100)
        self.assertTrue(session.planner_used)
        d = session.to_dict()
        self.assertIn("decision_plan", d)
        self.assertTrue(d["decision_plan"])
        self.assertEqual(d["planner_version"], "1.0.0")
        block = build_decision_planner_prompt_block(session)
        self.assertIn("[DECISION PLANNER", block)
        self.assertNotIn("PlannerOutput(", block)

    def test_advisory_flag_on_accepts_structured_verification_commands(self):
        session = ArtifactSession("s1", "task")
        ensure_task_contract_foundation(session, Intent(mode="Chat", task="x", original_text="x"))
        session.runtime_contract_source = "taskspec"
        session.task_contract["spec"] = {
            "intent": "modification",
            "scope": ["api"],
            "verification_policy": {"required": True, "typecheck": True, "build": True},
        }
        repo = _repo_v2_api()
        repo["verification_commands"] = {
            "typecheck": {"command": "pnpm exec tsc --noEmit", "cwd": "apps/web", "source": ["package_json"]},
            "build": {"command": "pnpm run build", "cwd": "apps/web", "source": ["package_json"]},
            "lint": {"command": "pnpm run lint", "cwd": "apps/web", "source": ["package_json"]},
            "source": ["package_json"],
        }
        session.repo_profile = {"profile_v2": repo}
        with mock.patch.dict(os.environ, {"GHOST_USE_DECISION_PLANNER": "1"}):
            apply_decision_planner_to_session(session, budget_remaining=100)
        self.assertTrue(session.planner_used)
        decision_plan = session.contract_decision_plan()
        steps = decision_plan["verification_plan"]["steps"]
        self.assertTrue(any(step["cwd"] == "apps/web" for step in steps))

    def test_normalize_verification_steps_rejects_pipes(self):
        steps = normalize_verification_steps(
            [
                {"check": "typecheck", "command": "tsc | head", "gate": True, "reason": "x"},
                {"check": "build", "command": "npm run build", "gate": True, "reason": "y"},
            ]
        )
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].check, "build")

    def test_norm_path_collapses_double_slash(self):
        self.assertEqual(_norm_path("app/api/issues//route.ts"), "app/api/issues/route.ts")
        self.assertEqual(_norm_path("a//b//c"), "a/b/c")

    def test_doc_file_excluded_from_expected_files(self):
        ts = {
            "intent": "implementation",
            "scope": ["api"],
            "target_files": [],
            "verification_policy": {"required": True},
        }
        repo = _repo_v2_api()
        sess = make_session_state_for_planner(budget_remaining=100)
        ex = build_exploration_plan(ts, repo, sess)
        ep = build_execution_plan(ts, repo, ex, sess)
        self.assertNotIn("AGENTS.md", ep.expected_files_changed)
        self.assertTrue(_is_doc_file("AGENTS.md"))

    def test_compute_risk_score_high_for_auth_path(self):
        ts = {"intent": "modification", "scope": ["api"]}
        repo = {}
        from apps.cli.runtime.decision_planner import ExplorationPlan, ExecutionPlan, ExplorationAvoid, ExplorationLimits

        ex = ExplorationPlan(
            ranked_candidates=[],
            avoid=ExplorationAvoid(),
            limits=ExplorationLimits(),
        )
        ep = ExecutionPlan(expected_files_changed=["apps/server/auth/security.py"])
        r = compute_risk_score(ts, repo, ex, ep)
        self.assertIn(r["level"], ("medium", "high"))


if __name__ == "__main__":
    unittest.main()
