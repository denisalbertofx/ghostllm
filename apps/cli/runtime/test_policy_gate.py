import unittest
from rich.console import Console
from policy_gate import PolicyGate

class TestPolicyGate(unittest.TestCase):
    def setUp(self):
        self.console = Console(quiet=True)
        self.gate = PolicyGate(self.console, auto_approve=True)

    def test_plan_mode_restrictions(self):
        # Destructive actions are forbidden in Plan mode regardless of auto-approve
        self.assertFalse(self.gate.check_permission("Write File", "test.py", "Plan"))
        self.assertFalse(self.gate.check_permission("Execute Shell", "npm run build", "Plan"))

    def test_delete_hardening(self):
        # Deletion requires structured intent: fix or debug
        # 1. Deny without proper task_type
        metadata = {"task_type": "ask"}
        self.assertFalse(self.gate.check_permission("Delete File", "unused.py", "Execute", metadata))
        
        # 2. Allow with fix/debug intent (auto-approve is True)
        metadata = {"task_type": "fix"}
        self.assertTrue(self.gate.check_permission("Delete File", "unused.py", "Execute", metadata))
        
        # 3. Allow with safety override
        metadata = {"task_type": "ask", "safety_override": True}
        self.assertTrue(self.gate.check_permission("Delete File", "unused.py", "Execute", metadata))

    def test_npm_install_protection(self):
        # 1. Deny without missing_dependency and research/debug intent
        metadata = {"task_type": "code"}
        self.assertFalse(self.gate.check_permission("Execute Shell", "npm install lodash", "Execute", metadata))
        
        # 2. Allow with proper intent and evidence
        metadata = {"task_type": "debug", "missing_dependency": True}
        self.assertTrue(self.gate.check_permission("Execute Shell", "npm install lodash", "Execute", metadata))

    def test_windows_compatibility_regex(self):
        # 1. Standalone Unix-isms are blocked
        metadata = {"task_type": "code"}
        self.assertFalse(self.gate.check_permission("Execute Shell", "grep 'error' log.txt", "Execute", metadata))
        self.assertFalse(self.gate.check_permission("Execute Shell", "sed -i 's/a/b/g' file.txt", "Execute", metadata))
        
        # 2. Substrings in larger valid commands ARE ALLOWED (False positive check)
        # 'git grep' is a valid git command, shouldn't be blocked by standalone 'grep' filter
        self.assertTrue(self.gate.check_permission("Execute Shell", "git grep 'error'", "Execute", metadata))
        
        # 3. Path with 'grep' in it should be allowed
        self.assertTrue(self.gate.check_permission("Execute Shell", "ls ./src/grepper/main.py", "Execute", metadata))

    def test_forbidden_destructive_patterns(self):
        # Destructive patterns are blocked
        metadata = {"task_type": "code"}
        self.assertFalse(self.gate.check_permission("Execute Shell", "rm -rf /", "Execute", metadata))
        self.assertFalse(self.gate.check_permission("Execute Shell", "del /s /q *", "Execute", metadata))

if __name__ == "__main__":
    unittest.main()
