import unittest
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
        self.assertFalse(self.gate.check_permission("Execute Shell", "npm install lodash", "Execute", metadata))

        metadata = {"task_type": "debug", "missing_dependency": True}
        self.assertTrue(self.gate.check_permission("Execute Shell", "npm install lodash", "Execute", metadata))

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


if __name__ == "__main__":
    unittest.main()
