import os
import unittest
from tempfile import TemporaryDirectory

from apps.cli.runtime.paths import PathComposer, WorkingDirectoryGuard


class TestWorkingDirectoryGuard(unittest.TestCase):
    def test_normalize_path_segments_strips_trailing_markup_suffix(self) -> None:
        self.assertEqual(
            PathComposer.normalize_path_segments("notes-app/types.ts\n</definition>"),
            "notes-app/types.ts",
        )

    def test_normalize_path_segments_coerces_malformed_scheme_path(self) -> None:
        self.assertEqual(
            PathComposer.normalize_path_segments("notes://app/pages/api/notes.ts"),
            "notes-app/pages/api/notes.ts",
        )

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
            os.mkdir(os.path.join(tmp, "src"))
            context, markers = WorkingDirectoryGuard.detect_context(tmp)
            self.assertEqual(context, "project")
            self.assertIn("pyproject.toml", markers)

    def test_validate_scaffold_blocks_real_project_root(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
                fh.write("{}")
            os.mkdir(os.path.join(tmp, "src"))
            action, msg, options = WorkingDirectoryGuard.validate_scaffold(tmp, None)
            self.assertEqual(action, "HARD_BLOCK")
            self.assertIn("proyecto activo", msg)
            self.assertTrue(options)

    def test_detect_context_bootstrap_partial_for_repo_with_readme_and_single_source_file(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as fh:
                fh.write("# demo\n")
            with open(os.path.join(tmp, "task_manager.py"), "w", encoding="utf-8") as fh:
                fh.write("print('hi')\n")
            context, markers = WorkingDirectoryGuard.detect_context(tmp)
            self.assertEqual(context, "bootstrap_partial")
            self.assertIn(".git", markers)
            self.assertIn("README.md", markers)

    def test_validate_scaffold_allows_partial_greenfield_repo(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as fh:
                fh.write("# demo\n")
            with open(os.path.join(tmp, "task_manager.py"), "w", encoding="utf-8") as fh:
                fh.write("print('hi')\n")
            action, msg, options = WorkingDirectoryGuard.validate_scaffold(tmp, None)
            self.assertEqual(action, "ALLOW")
            self.assertIn("Scaffold greenfield parcial", msg)
            self.assertEqual(options, [])

    def test_detect_context_bootstrap_partial_for_seeded_python_project(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            with open(os.path.join(tmp, "pyproject.toml"), "w", encoding="utf-8") as fh:
                fh.write("[project]\nname='demo'\n")
            with open(os.path.join(tmp, "main.py"), "w", encoding="utf-8") as fh:
                fh.write("def app():\n    return None\n")
            context, markers = WorkingDirectoryGuard.detect_context(tmp)
            self.assertEqual(context, "bootstrap_partial")
            self.assertIn("pyproject.toml", markers)

    def test_validate_scaffold_allows_seeded_python_project(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            with open(os.path.join(tmp, "pyproject.toml"), "w", encoding="utf-8") as fh:
                fh.write("[project]\nname='demo'\n")
            with open(os.path.join(tmp, "main.py"), "w", encoding="utf-8") as fh:
                fh.write("def app():\n    return None\n")
            action, msg, options = WorkingDirectoryGuard.validate_scaffold(tmp, None)
            self.assertEqual(action, "ALLOW")
            self.assertIn("Scaffold greenfield parcial", msg)
            self.assertEqual(options, [])

    def test_detect_context_bootstrap_partial_for_single_nested_web_scaffold(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            os.mkdir(os.path.join(tmp, ".ghost"))
            nested = os.path.join(tmp, "notes-app")
            os.mkdir(nested)
            with open(os.path.join(nested, "package.json"), "w", encoding="utf-8") as fh:
                fh.write('{"name":"notes-app"}')
            context, markers = WorkingDirectoryGuard.detect_context(tmp)
            self.assertEqual(context, "bootstrap_partial")
            self.assertIn(".git", markers)

    def test_validate_scaffold_allows_single_nested_web_scaffold_continuation(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            os.mkdir(os.path.join(tmp, ".ghost"))
            nested = os.path.join(tmp, "notes-app")
            os.mkdir(nested)
            with open(os.path.join(nested, "package.json"), "w", encoding="utf-8") as fh:
                fh.write('{"name":"notes-app"}')
            action, msg, options = WorkingDirectoryGuard.validate_scaffold(tmp, None)
            self.assertEqual(action, "ALLOW")
            self.assertIn("Scaffold greenfield parcial", msg)
            self.assertEqual(options, [])


if __name__ == "__main__":
    unittest.main()
