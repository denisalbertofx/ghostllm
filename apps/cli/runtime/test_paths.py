import os
import unittest
from tempfile import TemporaryDirectory

from apps.cli.runtime.paths import WorkingDirectoryGuard


class TestWorkingDirectoryGuard(unittest.TestCase):
    def test_detect_context_repo_shell_for_fresh_git_repo(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            context, markers = WorkingDirectoryGuard.detect_context(tmp)
            self.assertEqual(context, "repo_shell")
            self.assertIn(".git", markers)

    def test_validate_scaffold_allows_fresh_git_repo_in_place(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            action, msg, options = WorkingDirectoryGuard.validate_scaffold(tmp, None)
            self.assertEqual(action, "ALLOW")
            self.assertIn("Repositorio recién inicializado", msg)
            self.assertEqual(options, [])

    def test_validate_scaffold_allows_repo_shell_with_ghost_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            os.mkdir(os.path.join(tmp, ".ghost"))
            with open(os.path.join(tmp, "ghost_memory.db"), "w", encoding="utf-8") as fh:
                fh.write("")
            action, msg, options = WorkingDirectoryGuard.validate_scaffold(tmp, None)
            self.assertEqual(action, "ALLOW")
            self.assertIn("Repositorio recién inicializado", msg)
            self.assertEqual(options, [])

    def test_detect_context_project_when_strong_marker_exists(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            with open(os.path.join(tmp, "pyproject.toml"), "w", encoding="utf-8") as fh:
                fh.write("[project]\nname = 'demo'\n")
            context, markers = WorkingDirectoryGuard.detect_context(tmp)
            self.assertEqual(context, "project")
            self.assertIn("pyproject.toml", markers)

    def test_validate_scaffold_blocks_real_project_root(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
                fh.write("{}")
            action, msg, options = WorkingDirectoryGuard.validate_scaffold(tmp, None)
            self.assertEqual(action, "HARD_BLOCK")
            self.assertIn("proyecto activo", msg)
            self.assertTrue(options)


if __name__ == "__main__":
    unittest.main()
