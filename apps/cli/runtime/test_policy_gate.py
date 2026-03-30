import unittest
from pathlib import Path
from rich.console import Console

from apps.cli.runtime.policy_gate import PolicyGate


class TestPolicyGate(unittest.TestCase):
    def setUp(self):
        self.console = Console(quiet=True)
        self.gate = PolicyGate(self.console, auto_approve=True)

    def test_plan_mode_restrictions(self):
        self.assertFalse(self.gate.check_permission("Write File", "test.py", "Plan"))
        self.assertFalse(self.gate.check_permission("Execute Shell", "npm run build", "Plan"))

    def test_delete_hardening(self):
        metadata = {"task_type": "ask"}
        self.assertFalse(self.gate.check_permission("Delete File", "unused.py", "Execute", metadata))

        metadata = {"task_type": "fix"}
        self.assertTrue(self.gate.check_permission("Delete File", "unused.py", "Execute", metadata))

        metadata = {"task_type": "ask", "safety_override": True}
        self.assertTrue(self.gate.check_permission("Delete File", "unused.py", "Execute", metadata))

    def test_npm_install_protection(self):
        metadata = {"task_type": "code"}
        self.assertTrue(self.gate.check_permission("Execute Shell", "npm install lodash", "Execute", metadata))

        metadata = {"task_type": "debug", "missing_dependency": True}
        self.assertTrue(self.gate.check_permission("Execute Shell", "npm install lodash", "Execute", metadata))

        metadata = {"task_type": "fix"}
        self.assertTrue(self.gate.check_permission("Execute Shell", "cd notes-app && npm install", "Execute", metadata))

        metadata = {"task_type": "scaffold"}
        self.assertTrue(self.gate.check_permission("Execute Shell", "pnpm install", "Execute", metadata))

        metadata = {"task_type": "direct_edit"}
        self.assertTrue(self.gate.check_permission("Execute Shell", "npm install better-sqlite3", "Execute", metadata))

        metadata = {"task_type": "fix"}
        self.assertFalse(self.gate.check_permission("Execute Shell", "npm install -g create-next-app", "Execute", metadata))

    def test_windows_compatibility_regex(self):
        metadata = {"task_type": "code"}
        self.assertFalse(self.gate.check_permission("Execute Shell", "grep 'error' log.txt", "Execute", metadata))
        self.assertFalse(self.gate.check_permission("Execute Shell", "sed -i 's/a/b/g' file.txt", "Execute", metadata))
        self.assertTrue(self.gate.check_permission("Execute Shell", "git grep 'error'", "Execute", metadata))
        self.assertTrue(self.gate.check_permission("Execute Shell", "ls ./src/grepper/main.py", "Execute", metadata))

    def test_forbidden_destructive_patterns(self):
        metadata = {"task_type": "code"}
        self.assertFalse(self.gate.check_permission("Execute Shell", "rm -rf /", "Execute", metadata))
        self.assertFalse(self.gate.check_permission("Execute Shell", "del /s /q *", "Execute", metadata))

    def test_check_permission_accepts_assistant_shell_kwargs(self):
        gate = PolicyGate(self.console, auto_approve=False)
        captured = {}

        def fake_manual(action, detail, original_command=None):
            captured["action"] = action
            captured["detail"] = detail
            captured["original_command"] = original_command
            return True

        gate._manual_confirm = fake_manual
        allowed = gate.check_permission(
            "Execute Shell",
            "Get-Content foo.py",
            "Execute",
            {"task_type": "code"},
            original_command="cat foo.py",
            batch_preapproved=False,
        )
        self.assertTrue(allowed)
        self.assertEqual(captured["action"], "Execute Shell")
        self.assertEqual(captured["detail"], "Get-Content foo.py")
        self.assertEqual(captured["original_command"], "cat foo.py")

    def test_batch_preapproved_skips_manual_confirmation(self):
        gate = PolicyGate(self.console, auto_approve=False)

        def fail_manual(*args, **kwargs):
            raise AssertionError("manual confirmation should not run for batch-preapproved shell")

        gate._manual_confirm = fail_manual
        allowed = gate.check_permission(
            "Execute Shell",
            "Get-Content foo.py",
            "Execute",
            {"task_type": "code"},
            original_command="cat foo.py",
            batch_preapproved=True,
        )
        self.assertTrue(allowed)

    def test_confirm_action_alias(self):
        gate = PolicyGate(self.console, auto_approve=False)
        gate._manual_confirm = lambda *args, **kwargs: True
        self.assertTrue(gate.confirm_action("Delete File", "Evidence: cleanup"))

    def test_project_policy_denies_network_shell(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".ghost").mkdir()
            (root / ".ghost" / "policy.yaml").write_text(
                "sandbox:\n"
                "  allow: [read, write, shell_exec]\n"
                "  deny: [network]\n",
                encoding="utf-8",
            )
            gate = PolicyGate(self.console, auto_approve=True, cwd=str(root))
            allowed = gate.check_permission(
                "Execute Shell",
                "curl https://example.com",
                "Execute",
                {"task_type": "code"},
            )
            self.assertFalse(allowed)

    def test_project_policy_forces_manual_approval(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".ghost").mkdir()
            (root / ".ghost" / "policy.yaml").write_text(
                "sandbox:\n"
                "  allow: [read, write, shell_exec]\n"
                "  require_approval: [shell_exec]\n",
                encoding="utf-8",
            )
            gate = PolicyGate(self.console, auto_approve=True, cwd=str(root))
            seen = {}

            def fake_manual(action, detail, original_command=None):
                seen["action"] = action
                seen["detail"] = detail
                return True

            gate._manual_confirm = fake_manual
            allowed = gate.check_permission(
                "Execute Shell",
                "python -m pytest",
                "Execute",
                {"task_type": "fix"},
            )
            self.assertTrue(allowed)
            self.assertEqual(seen["action"], "Execute Shell")


if __name__ == "__main__":
    unittest.main()
