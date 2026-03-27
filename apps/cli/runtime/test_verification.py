import json
import os
import shutil
import tempfile
import unittest

from apps.cli.runtime.verification import (
    CHECK_DENIED,
    PROVENANCE_DENIED_BY_USER,
    VerificationManager,
    _shrink_verification_stream,
    parse_pytest_focus_targets,
)


class TestPytestFocusTargets(unittest.TestCase):
    def test_parse_failed_node_ids(self) -> None:
        out = """
============================= test session starts =============================
FAILED tests/unit/test_x.py::test_a - AssertionError: x
FAILED tests/unit/test_x.py::test_b - AssertionError: y
"""
        targets = parse_pytest_focus_targets(stderr=out)
        self.assertIn("tests/unit/test_x.py::test_a", targets)
        self.assertIn("tests/unit/test_x.py::test_b", targets)

    def test_parse_error_collecting(self) -> None:
        err = "\nERROR collecting tests/broken.py\n"
        targets = parse_pytest_focus_targets(stderr=err)
        self.assertTrue(any("tests/broken.py" in t for t in targets))


class TestVerificationManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.manager = VerificationManager(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_detect_root_node_with_build(self):
        pkg = {"scripts": {"build": "echo building"}}
        with open(os.path.join(self.test_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump(pkg, f)

        checks = self.manager.get_applicable_checks(
            files_changed=[{"file": "src/index.ts"}],
        )
        names = [c["name"] for c in checks]
        self.assertIn("Build", names)
        self.assertTrue(any(c["command"] == "npm run build" for c in checks))
        self.assertTrue(all(c.get("cwd") == "." for c in checks))

    def test_detect_node_ts_fallback(self):
        with open(os.path.join(self.test_dir, "package.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        with open(os.path.join(self.test_dir, "tsconfig.json"), "w", encoding="utf-8") as f:
            f.write("{}")

        checks = self.manager.get_applicable_checks(files_changed=[{"file": "src/page.tsx"}])
        self.assertEqual(checks[0]["name"], "TypeCheck")
        self.assertEqual(checks[0]["command"], "npx tsc --noEmit")
        self.assertEqual(checks[0]["cwd"], ".")

    def test_detect_python_tests_for_cli_target(self):
        with open(os.path.join(self.test_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write("[project]\nname='x'\n")
        os.makedirs(os.path.join(self.test_dir, "tests"))
        with open(os.path.join(self.test_dir, "tests", "test_x.py"), "w", encoding="utf-8") as f:
            f.write("def test_ok():\n    assert True\n")

        checks = self.manager.get_applicable_checks(files_changed=[{"file": "apps/cli/main.py"}])
        self.assertTrue(any(c["kind"] == "tests" for c in checks))
        self.assertTrue(any("Python Tests" in c["name"] for c in checks))

    def test_detect_python_tests_targeted_by_module_import(self):
        with open(os.path.join(self.test_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write("[project]\nname='x'\n")
        os.makedirs(os.path.join(self.test_dir, "tests"))
        with open(
            os.path.join(self.test_dir, "tests", "test_provider_initialization.py"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(
                "from apps.cli.main import check_provider_ready\n\n"
                "def test_ok():\n"
                "    assert callable(check_provider_ready)\n"
            )

        checks = self.manager.get_applicable_checks(files_changed=[{"file": "apps/cli/main.py"}])
        test_check = next(c for c in checks if c["kind"] == "tests")
        self.assertIn("targeted", test_check["name"])
        self.assertIn("tests/test_provider_initialization.py", test_check["command"])
        self.assertTrue(
            test_check["command"].startswith("uv run python -m pytest")
            or test_check["command"].startswith("python -m pytest")
        )

    def test_detect_python_tests_uses_explicit_contract_command(self):
        with open(os.path.join(self.test_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write("[project]\nname='x'\n")
        os.makedirs(os.path.join(self.test_dir, "tests"))
        with open(os.path.join(self.test_dir, "tests", "test_server_model_registry_boot.py"), "w", encoding="utf-8") as f:
            f.write("def test_ok():\n    assert True\n")

        checks = self.manager.get_applicable_checks(
            files_changed=[{"file": "apps/server/database.py"}],
            contract_spec={
                "acceptance_criteria": [
                    "verifica sólo con: python -m pytest tests/test_claude_bridge_smoke.py tests/test_server_model_registry_boot.py -qSi esos tests pasan, detente."
                ]
            },
        )
        test_check = next(c for c in checks if c["kind"] == "tests")
        self.assertEqual(test_check["name"], "Python Tests (explicit, targeted)")
        self.assertIn("tests/test_claude_bridge_smoke.py", test_check["command"])
        self.assertIn("tests/test_server_model_registry_boot.py", test_check["command"])
        self.assertIn("-q", test_check["command"])

    def test_verify_change_reruns_only_failed_check_first(self):
        self.manager.set_repo_verification_commands(
            {
                "typecheck": {"command": 'python -c "print(\'typecheck\')"', "cwd": "."},
                "build": {"command": 'python -c "print(\'build\')"', "cwd": "."},
                "lint": {"command": 'python -c "print(\'lint\')"', "cwd": "."},
            }
        )

        res = self.manager.verify_change(
            files_changed=[{"file": "apps/web/src/app/page.tsx"}],
            failed_check_names=["Lint"],
        )

        self.assertEqual(res["status"], "success")
        self.assertEqual(len(res["checks"]), 1)
        self.assertEqual(res["checks"][0]["name"], "Lint")
        self.assertEqual(res["checks"][0]["status"], "passed")

    def test_detect_python_tests_falls_back_without_nearby_imports(self):
        with open(os.path.join(self.test_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write("[project]\nname='x'\n")
        os.makedirs(os.path.join(self.test_dir, "tests"))
        with open(os.path.join(self.test_dir, "tests", "test_x.py"), "w", encoding="utf-8") as f:
            f.write("def test_ok():\n    assert True\n")

        checks = self.manager.get_applicable_checks(files_changed=[{"file": "apps/cli/main.py"}])
        test_check = next(c for c in checks if c["kind"] == "tests")
        self.assertIn(test_check["command"], ("uv run python -m pytest", "python -m pytest"))

    def test_monorepo_python_target_skips_node_checks(self):
        with open(os.path.join(self.test_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write("[project]\nname='x'\n")
        os.makedirs(os.path.join(self.test_dir, "tests"))
        with open(os.path.join(self.test_dir, "tests", "test_x.py"), "w", encoding="utf-8") as f:
            f.write("def test_ok():\n    assert True\n")

        self.manager.set_repo_verification_commands(
            {
                "typecheck": {"command": "npm run typecheck", "cwd": "apps/web"},
                "build": {"command": "npm run build", "cwd": "apps/web"},
                "lint": {"command": "npm run lint", "cwd": "apps/web"},
            }
        )

        checks = self.manager.get_applicable_checks(files_changed=[{"file": "apps/cli/main.py"}])
        names = [c["name"] for c in checks]
        self.assertTrue(any("Python Tests" in name for name in names))
        self.assertNotIn("Build", names)
        self.assertNotIn("Lint", names)
        self.assertNotIn("TypeCheck", names)

    def test_node_target_uses_repo_profile_cwd(self):
        self.manager.set_repo_verification_commands(
            {
                "typecheck": {"command": "npx tsc --noEmit", "cwd": "apps/web"},
                "build": {"command": "npm run build", "cwd": "apps/web"},
                "lint": {"command": "npm run lint", "cwd": "apps/web"},
            }
        )

        checks = self.manager.get_applicable_checks(files_changed=[{"file": "apps/web/src/app/page.tsx"}])
        by_name = {c["name"]: c for c in checks}
        self.assertEqual(by_name["TypeCheck"]["cwd"], "apps/web")
        self.assertEqual(by_name["Build"]["cwd"], "apps/web")
        self.assertEqual(by_name["Lint"]["cwd"], "apps/web")

    def test_verify_change_output_structure(self):
        self.manager.set_repo_verification_commands(
            {
                "build": {"command": 'python -c "print(\'success\')"', "cwd": "."},
            }
        )

        res = self.manager.verify_change(
            files_changed=[{"file": "apps/web/src/app/page.tsx"}],
            contract_spec={
                "verification_policy": {
                    "required": True,
                    "typecheck": False,
                    "build": True,
                    "lint": False,
                    "tests": False,
                }
            },
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(res["checks"]), 1)
        self.assertEqual(res["checks"][0]["name"], "Build")
        self.assertEqual(res["checks"][0]["status"], "passed")
        self.assertEqual(res["checks"][0]["cwd"], ".")
        self.assertEqual(res["verification_scope"], "node")

    def test_verify_batch_denial_returns_denied_checks(self):
        self.manager.set_repo_verification_commands(
            {
                "build": {"command": "npm run build", "cwd": "apps/web"},
                "lint": {"command": "npm run lint", "cwd": "apps/web"},
            }
        )

        res = self.manager.verify_change(
            files_changed=[{"file": "apps/web/src/app/page.tsx"}],
            contract_spec={
                "verification_policy": {
                    "required": True,
                    "typecheck": False,
                    "build": True,
                    "lint": True,
                    "tests": False,
                }
            },
            verify_batch_approval_fn=lambda _cmds: False,
        )

        self.assertEqual(res["status"], "blocked")
        denied = [c for c in res["checks"] if c["status"] == CHECK_DENIED]
        self.assertEqual(len(denied), 2)
        self.assertTrue(all(c["provenance"] == PROVENANCE_DENIED_BY_USER for c in denied))


class TestVerificationStreamCap(unittest.TestCase):
    def test_shrink_truncates_huge_stream(self) -> None:
        huge = "L" * 80_000
        out = _shrink_verification_stream(huge, 8000)
        self.assertLess(len(out), 9000)
        self.assertIn("ghost_verify_stream_truncated", out)


if __name__ == "__main__":
    unittest.main()
