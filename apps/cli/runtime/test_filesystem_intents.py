from __future__ import annotations

from apps.cli.runtime.filesystem_intents import likely_mutating_shell_command, revert_filesystem_intent


def test_revert_write_file_removes_new_file(tmp_path) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("new", encoding="utf-8")

    ok, detail = revert_filesystem_intent(
        str(tmp_path),
        {
            "op_type": "write_file",
            "relpath": "demo.txt",
            "payload": {
                "path": "demo.txt",
                "had_existing_file": False,
                "old_content": "",
            },
        },
    )

    assert ok is True
    assert "Removed newly created file" in detail
    assert not target.exists()


def test_revert_edit_file_restores_previous_content(tmp_path) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("edited", encoding="utf-8")

    ok, _detail = revert_filesystem_intent(
        str(tmp_path),
        {
            "op_type": "edit_file",
            "relpath": "demo.txt",
            "payload": {
                "path": "demo.txt",
                "had_existing_file": True,
                "old_content": "before",
            },
        },
    )

    assert ok is True
    assert target.read_text(encoding="utf-8") == "before"


def test_mutating_shell_command_heuristic_detects_installs() -> None:
    assert likely_mutating_shell_command("uv sync")
    assert likely_mutating_shell_command("npm install")
    assert not likely_mutating_shell_command("pytest tests/test_app.py -q")
