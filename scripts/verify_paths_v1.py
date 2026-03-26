import unittest
import os
import sys
from unittest.mock import patch, MagicMock

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from apps.cli.runtime.paths import PathComposer, WorkingDirectoryGuard

class TestPathComposer(unittest.TestCase):
    def test_compose_normalization(self):
        # Basic join
        self.assertEqual(PathComposer.compose("C:\\ghost", "app"), "C:/ghost/app" if os.name != 'nt' else "C:\\ghost\\app")
        
        # Double slashes
        self.assertEqual(PathComposer.compose("C:\\ghost\\", "\\app"), "C:/ghost/app" if os.name != 'nt' else "C:\\ghost\\app")
        self.assertEqual(PathComposer.compose("C:/ghost//", "//app"), "C:/ghost/app" if os.name != 'nt' else "C:\\ghost\\app")
        
        # Relative parts
        self.assertEqual(PathComposer.compose("C:/ghost/foo", "../bar"), "C:/ghost/bar" if os.name != 'nt' else "C:\\ghost\\bar")
        self.assertEqual(PathComposer.compose("C:/ghost", "./app"), "C:/ghost/app" if os.name != 'nt' else "C:\\ghost\\app")

    def test_is_absolute(self):
        if os.name == 'nt':
            self.assertTrue(PathComposer.is_absolute("C:\\foo"))
            self.assertTrue(PathComposer.is_absolute("D:/bar"))
            self.assertFalse(PathComposer.is_absolute("foo/bar"))
        else:
            self.assertTrue(PathComposer.is_absolute("/foo/bar"))
            self.assertFalse(PathComposer.is_absolute("foo/bar"))

class TestWorkingDirectoryGuard(unittest.TestCase):
    @patch("os.path.exists")
    def test_detect_context_empty(self, mock_exists):
        mock_exists.return_value = False
        context, markers = WorkingDirectoryGuard.detect_context("/tmp/empty")
        self.assertEqual(context, "empty")

    @patch("os.path.exists")
    def test_detect_context_project(self, mock_exists):
        # Mock .git existing in the same dir
        def side_effect(path):
            return ".git" in path or "package.json" in path
        mock_exists.side_effect = side_effect
        context, markers = WorkingDirectoryGuard.detect_context("/repo")
        self.assertEqual(context, "project")

    @patch("os.path.exists")
    def test_validate_scaffold_conflict(self, mock_exists):
        # Mock already in a project
        mock_exists.return_value = True
        is_safe, msg = WorkingDirectoryGuard.validate_scaffold("/repo", None)
        self.assertFalse(is_safe)
        self.assertIn("proyecto activo", msg)

    @patch("os.path.exists")
    def test_validate_scaffold_safe(self, mock_exists):
        # Mock NOT in a project
        mock_exists.return_value = False
        is_safe, msg = WorkingDirectoryGuard.validate_scaffold("/empty", "new-app")
        self.assertTrue(is_safe)

if __name__ == "__main__":
    unittest.main()
