import os
import unittest
from tempfile import TemporaryDirectory

from apps.cli.assistant import (
    _filter_workspace_listing_entries,
    _is_internal_workspace_metadata_path,
    _read_utf8_text_for_tool,
    _should_start_greenfield_in_act,
)
from apps.cli.runtime.task_contract import route_intake_intent


class TestAssistantGreenfieldHelpers(unittest.TestCase):
    def test_filter_workspace_listing_entries_hides_internal_metadata_at_root(self) -> None:
        items = [".ghost", ".git", "ghost_memory.db", "README.md", "src", ".gitignore"]
        filtered = _filter_workspace_listing_entries(".", items)
        self.assertEqual(filtered, ["README.md", "src", ".gitignore"])

    def test_internal_workspace_metadata_path_detection(self) -> None:
        self.assertTrue(_is_internal_workspace_metadata_path(".ghost/plans/foo.md"))
        self.assertTrue(_is_internal_workspace_metadata_path("ghost_memory.db"))
        self.assertFalse(_is_internal_workspace_metadata_path(".gitignore"))
        self.assertFalse(_is_internal_workspace_metadata_path("src/main.py"))

    def test_should_start_greenfield_in_act_for_repo_shell_bootstrap(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            intent = route_intake_intent("/do crea desde cero una CLI de tareas en Python con SQLite")
            self.assertTrue(_should_start_greenfield_in_act(tmp, intent))

    def test_read_utf8_text_for_tool_returns_error_for_binary_file(self) -> None:
        with TemporaryDirectory() as tmp:
            fp = os.path.join(tmp, "ghost_memory.db")
            with open(fp, "wb") as fh:
                fh.write(b"\x00\x8a\xff\x10sqlite")
            content, err = _read_utf8_text_for_tool(fp)
            self.assertIsNone(content)
            self.assertIn("not UTF-8 text", err or "")


if __name__ == "__main__":
    unittest.main()
