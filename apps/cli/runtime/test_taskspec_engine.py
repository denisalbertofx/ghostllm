import unittest
from pathlib import Path

from apps.cli.runtime.intent_classifier import (
    INTENT_ANALYSIS,
    INTENT_BUGFIX,
    INTENT_IMPLEMENTATION,
    INTENT_REFACTOR,
    INTENT_REVIEW,
)
from apps.cli.runtime.repo_profile import RepoProfile, scan_repo_profile
from apps.cli.runtime.taskspec_engine import (
    TaskSpec,
    build_taskspec,
    extract_target_files_from_prompt,
    infer_cli_maintenance_targets,
    repair_taskspec,
    validate_taskspec,
)


class TestTaskSpecEngine(unittest.TestCase):
    def _empty_repo(self) -> RepoProfile:
        return RepoProfile(
            stack="unknown",
            layers_detected=[],
            has_package_json=False,
            important_folders=[],
        )

    def _node_repo(self) -> RepoProfile:
        return RepoProfile(
            stack="nextjs",
            layers_detected=["api", "ui", "data"],
            has_package_json=True,
            important_folders=["apps", "src"],
        )

    def test_implementation_api_task(self):
        repo = self._node_repo()
        r = build_taskspec(
            "Implement GET /api/issues with status filter. Do not touch UI.",
            repo,
        )
        self.assertIn(r.validation_status, ("valid", "repaired"))
        ts = r.taskspec
        self.assertEqual(ts.intent, INTENT_IMPLEMENTATION)
        self.assertEqual(ts.change_expectation, "must_write")
        self.assertIn("api", ts.scope)
        self.assertTrue(ts.verification_policy.get("typecheck"))
        self.assertEqual(ts.repair_policy.get("max_attempts"), 1)
        self.assertEqual(ts.budget_policy.get("max_shell_calls"), 4)
        self.assertEqual(ts.budget_policy.get("max_tool_calls"), 24)

    def test_review_task(self):
        repo = self._node_repo()
        r = build_taskspec("Review the authentication flow and report findings only.", repo)
        ts = r.taskspec
        self.assertEqual(ts.intent, INTENT_REVIEW)
        self.assertEqual(ts.change_expectation, "should_not_write")
        self.assertFalse(ts.verification_policy.get("required"))
        self.assertFalse(any(ts.verification_policy.get(k) for k in ("typecheck", "build", "lint", "tests")))
        self.assertEqual(ts.budget_policy.get("max_shell_calls"), 0)

    def test_plan_biggest_bug_is_analysis_read_only(self):
        repo = self._node_repo()
        r = build_taskspec("/plan dime el bug más grande que encuentres en este proyecto", repo)
        ts = r.taskspec
        self.assertEqual(ts.intent, INTENT_ANALYSIS)
        self.assertEqual(ts.change_expectation, "should_not_write")
        self.assertFalse(ts.verification_policy.get("required"))
        self.assertFalse(any(ts.verification_policy.get(k) for k in ("typecheck", "build", "lint", "tests")))
        self.assertEqual(ts.budget_policy.get("max_shell_calls"), 0)

    def test_inspection_prompt_explicit_pytest_raises_shell_budget(self):
        repo = self._node_repo()
        r = build_taskspec(
            "/plan dime el bug más grande y ejecuta pytest al final",
            repo,
        )
        ts = r.taskspec
        self.assertEqual(ts.intent, INTENT_ANALYSIS)
        self.assertGreaterEqual(int(ts.budget_policy.get("max_shell_calls") or 0), 3)

    def test_llm_merge_cannot_force_bugfix_on_inspection_prompt(self):
        repo = self._node_repo()

        def malicious(_u: str, _r: str, _d: dict) -> dict:
            return {"intent": INTENT_BUGFIX, "change_expectation": "must_write"}

        r = build_taskspec("/plan encuentra el bug más grave del repo", repo, llm_merge_fn=malicious)
        ts = r.taskspec
        self.assertEqual(ts.intent, INTENT_ANALYSIS)
        self.assertEqual(ts.change_expectation, "should_not_write")

    def test_repo_overview_prompt_is_analysis_read_only(self):
        repo = RepoProfile(
            stack="polyglot",
            layers_detected=["api", "ui", "docs", "tests"],
            has_package_json=True,
            important_folders=["apps", "docs", "packages"],
        )
        r = build_taskspec(
            "/plan listame todo los archivos de esta app y dime el stack tecnologico cual es",
            repo,
        )
        ts = r.taskspec
        self.assertEqual(ts.intent, "analysis")
        self.assertEqual(ts.change_expectation, "should_not_write")
        self.assertEqual(ts.scope, [])
        self.assertFalse(ts.verification_policy.get("required"))
        self.assertEqual(ts.budget_policy.get("max_shell_calls"), 0)
        self.assertEqual(ts.budget_policy.get("max_tool_calls"), 4)

    def test_strategy_plan_prompt_is_broad_read_only(self):
        repo = RepoProfile(
            stack="polyglot",
            layers_detected=["api", "ui", "docs", "tests"],
            has_package_json=True,
            important_folders=["apps", "docs", "packages"],
        )
        r = build_taskspec(
            "/plan creame un plan para mejorar la arquitectura de esta app",
            repo,
        )
        ts = r.taskspec
        self.assertEqual(ts.intent, "analysis")
        self.assertEqual(ts.change_expectation, "should_not_write")
        self.assertEqual(ts.scope, [])
        self.assertFalse(ts.verification_policy.get("required"))
        self.assertEqual(ts.budget_policy.get("max_shell_calls"), 0)
        self.assertEqual(ts.budget_policy.get("max_tool_calls"), 16)

    def test_broad_plan_audit_prompt_gets_planning_budget(self):
        repo = RepoProfile(
            stack="polyglot",
            layers_detected=["api", "ui", "docs", "tests"],
            has_package_json=True,
            important_folders=["apps", "docs", "packages"],
        )
        r = build_taskspec(
            "/plan encuentra los bugs mas importantes",
            repo,
        )
        ts = r.taskspec
        self.assertEqual(ts.intent, "analysis")
        self.assertEqual(ts.change_expectation, "should_not_write")
        self.assertEqual(ts.budget_policy.get("max_shell_calls"), 0)
        self.assertEqual(ts.budget_policy.get("max_tool_calls"), 16)

    def test_cli_command_bugfix_infers_main_target(self):
        repo = RepoProfile(
            stack="polyglot",
            layers_detected=["api", "config", "data", "docs", "tests", "ui"],
            has_package_json=True,
            important_folders=["apps", "tests"],
        )
        r = build_taskspec(
            "/do corrige el comando doctor para que reporte claramente cuando el gateway responde /health pero no /ready. "
            "Mantén el cambio mínimo, añade test si existe cobertura cercana, y verifica al final.",
            repo,
        )
        ts = r.taskspec
        self.assertEqual(ts.intent, INTENT_BUGFIX)
        self.assertEqual(ts.change_expectation, "must_write")
        self.assertEqual(ts.scope, [])
        self.assertIn("apps/cli/main.py", ts.target_files or [])
        self.assertFalse(ts.verification_policy.get("build"))

    def test_infer_cli_maintenance_targets_doctor(self):
        targets = infer_cli_maintenance_targets(
            "corrige el comando doctor para que muestre mejor el estado"
        )
        self.assertEqual(targets, ["apps/cli/main.py"])

    def test_infer_cli_maintenance_targets_does_not_trigger_on_greenfield_cli(self):
        targets = infer_cli_maintenance_targets(
            "crea desde cero una CLI de tareas en Python con SQLite"
        )
        self.assertEqual(targets, [])

    def test_greenfield_scaffold_prompt_does_not_infer_ghost_cli_target(self):
        repo = self._empty_repo()
        r = build_taskspec(
            "/do crea desde cero una CLI de tareas en Python con SQLite. README y tests incluidos.",
            repo,
        )
        ts = r.taskspec
        self.assertEqual(ts.change_expectation, "must_write")
        self.assertFalse(ts.target_files)
        self.assertTrue(
            any("Greenfield scaffold detected" in line for line in ts.reasoning_lines)
        )

    def test_bugfix_task(self):
        repo = self._node_repo()
        r = build_taskspec("Fix broken PUT /api/users when body is empty", repo)
        ts = r.taskspec
        self.assertEqual(ts.intent, INTENT_BUGFIX)
        self.assertTrue(ts.verification_policy.get("required"))
        self.assertTrue(
            any(
                ts.verification_policy.get(k)
                for k in ("typecheck", "build", "lint", "tests")
            )
        )

    def test_refactor_task(self):
        repo = self._node_repo()
        r = build_taskspec("Refactor the helper module for clarity; keep behavior.", repo)
        ts = r.taskspec
        self.assertEqual(ts.intent, INTENT_REFACTOR)
        self.assertEqual(ts.change_expectation, "may_write")

    def test_inconsistent_spec_auto_repair(self):
        repo = self._node_repo()

        def bad_merge(_u: str, _r: str, d: dict) -> dict:
            # Force api scope but strip typecheck — validator must reject and repair
            return {
                "intent": INTENT_IMPLEMENTATION,
                "scope": ["api"],
                "verification_policy": {"typecheck": False, "tests": False},
            }

        r = build_taskspec("add API feature", repo, llm_merge_fn=bad_merge)
        self.assertTrue(r.repaired)
        self.assertEqual(r.validation_status, "repaired")
        self.assertTrue(
            r.taskspec.verification_policy.get("typecheck")
            or r.taskspec.verification_policy.get("tests")
        )
        self.assertEqual(len(r.validation_errors), 0)

    def test_verification_policy_assignment(self):
        repo = RepoProfile(
            layers_detected=["api"],
            has_package_json=True,
        )
        r = build_taskspec("Update API route handlers", repo)
        ts = r.taskspec
        self.assertTrue(
            ts.verification_policy.get("typecheck") or ts.verification_policy.get("tests")
        )

    def test_ui_scope_lint(self):
        repo = RepoProfile(layers_detected=["ui"], has_package_json=True)
        r = build_taskspec("Fix styles in components/Button.tsx", repo)
        ts = r.taskspec
        if "ui" in ts.scope:
            self.assertTrue(ts.verification_policy.get("lint"))

    def test_budget_policy_assignment(self):
        r = build_taskspec("implement x", self._node_repo())
        ts = r.taskspec
        self.assertEqual(ts.budget_policy.get("max_shell_calls"), 4)
        self.assertEqual(ts.budget_policy.get("max_tool_calls"), 24)
        self.assertEqual(ts.budget_policy.get("reserved_write_tool_calls"), 4)
        self.assertEqual(ts.budget_policy.get("soft_read_only_tool_calls"), 12)

    def test_extract_target_files(self):
        p = "Edit src/app/route.ts and configs/foo.yaml"
        self.assertIn("src/app/route.ts", extract_target_files_from_prompt(p))

    def test_validate_api_without_typecheck_invalid_then_repair(self):
        ts = TaskSpec(
            intent=INTENT_IMPLEMENTATION,
            scope=["api"],
            forbidden_layers=[],
            change_expectation="must_write",
            acceptance_criteria=["x"],
            verification_policy={"required": True, "typecheck": False, "build": False, "lint": False, "tests": False},
            repair_policy={"max_attempts": 1},
            budget_policy={"max_shell_calls": 2, "max_tool_calls": 12},
            confidence=0.9,
            reasoning_lines=[],
        )
        v = validate_taskspec(ts)
        self.assertFalse(v.is_valid)
        fixed = repair_taskspec(ts, v.errors)
        v2 = validate_taskspec(fixed)
        self.assertTrue(v2.is_valid)
        self.assertTrue(
            fixed.verification_policy.get("typecheck") or fixed.verification_policy.get("tests")
        )

    def test_scan_repo_profile_smoke(self):
        root = Path(__file__).resolve().parents[3]
        prof = scan_repo_profile(root)
        self.assertIsNotNone(prof.stack)
        self.assertIsInstance(prof.layers_detected, list)


if __name__ == "__main__":
    unittest.main()
