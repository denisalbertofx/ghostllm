import os
import json
import unittest
from unittest.mock import MagicMock
import tempfile
import shutil
from apps.cli.assistant import CodexAssistant

class TestGhostTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp()
        cls.assistant = CodexAssistant("http://mock", "key", "model")
        cls.assistant.renderer = MagicMock()
        cls.assistant.console = MagicMock()
        cls.assistant.auto_approve = True
        cls.assistant.policy_gate.set_auto_approve(True)
        cls.assistant.cwd = cls.test_dir

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.test_dir)

    def test_01_ls(self):
        with open(os.path.join(self.test_dir, "test_file.txt"), "w") as f: f.write("hello")
        res = self.assistant._execute_tool({"name": "ls", "arguments": {"path": "."}})
        self.assertIn("test_file.txt", res["files"])
        print("✓ Tool 'ls' passed.")

    def test_02_read_file(self):
        path = os.path.join(self.test_dir, "read_me.py")
        with open(path, "w") as f: f.write("print('hello')")
        
        res = self.assistant._execute_tool({"name": "read_file", "arguments": {"path": "read_me.py"}})
        self.assertIn("print('hello')", res["content"])
        
        res = self.assistant._execute_tool({"name": "read_file", "arguments": {"path": "non_existent.py"}})
        self.assertIn("error", res)
        print("✓ Tool 'read_file' passed.")

    def test_03_write_file(self):
        res = self.assistant._execute_tool({"name": "write_file", "arguments": {"path": "new.txt", "content": "data"}})
        self.assertEqual(res["status"], "success")
        with open(os.path.join(self.test_dir, "new.txt"), "r") as f: content = f.read()
        self.assertEqual(content, "data")
        print("✓ Tool 'write_file' passed.")

    def test_04_edit_file(self):
        path = os.path.join(self.test_dir, "edit.txt")
        with open(path, "w") as f: f.write("version = 1.0")
        
        res = self.assistant._execute_tool({
            "name": "edit_file", 
            "arguments": {"path": "edit.txt", "old_str": "1.0", "new_str": "2.0"}
        })
        self.assertEqual(res["status"], "success")
        with open(path, "r") as f: content = f.read()
        self.assertIn("version = 2.0", content)
        print("✓ Tool 'edit_file' passed.")

    def test_05_run_shell(self):
        res = self.assistant._execute_tool({"name": "run_shell", "arguments": {"command": "echo hello"}})
        self.assertIn("hello", res["stdout"].strip())
        self.assertEqual(res["exit_code"], 0)
        print("✓ Tool 'run_shell' passed.")

    def test_06_git_status(self):
        res = self.assistant._execute_tool({"name": "git_status", "arguments": {}})
        self.assertIn("error", res)
        print("✓ Tool 'git_status' passed.")

    def test_07_summarize_repo(self):
        res = self.assistant._execute_tool({"name": "summarize_repo", "arguments": {}})
        self.assertIn("summary", res)
        print("✓ Tool 'summarize_repo' passed.")

if __name__ == "__main__":
    unittest.main()
