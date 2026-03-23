import os
import json
import pytest
from unittest.mock import MagicMock
from apps.cli.assistant import CodexAssistant

@pytest.fixture
def assistant():
    # Setup a mock assistant in a temporary directory
    mock_as = CodexAssistant("http://mock", "key", "model")
    mock_as.renderer = MagicMock()
    mock_as.console = MagicMock()
    # Mock auto_approve for tests
    mock_as.auto_approve = True
    return mock_as

def test_tool_ls(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    # Create a dummy file
    (tmp_path / "test_file.txt").write_text("hello")
    res = assistant._execute_tool({"name": "ls", "arguments": {"path": "."}})
    assert "test_file.txt" in res["files"]

def test_tool_read_file(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    file_path = tmp_path / "read_me.py"
    file_path.write_text("print('hello')")
    
    # Success path
    res = assistant._execute_tool({"name": "read_file", "arguments": {"path": "read_me.py"}})
    assert "print('hello')" in res["content"]
    
    # Failure path (missing)
    res = assistant._execute_tool({"name": "read_file", "arguments": {"path": "non_existent.py"}})
    assert "error" in res

def test_tool_write_file(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    res = assistant._execute_tool({"name": "write_file", "arguments": {"path": "new.txt", "content": "data"}})
    assert res["status"] == "success"
    assert (tmp_path / "new.txt").read_text() == "data"

def test_tool_edit_file(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    file_path = tmp_path / "edit.txt"
    file_path.write_text("version = 1.0")
    
    # Success path
    res = assistant._execute_tool({
        "name": "edit_file", 
        "arguments": {"path": "edit.txt", "old_str": "1.0", "new_str": "2.0"}
    })
    assert res["status"] == "patched"
    assert "version = 2.0" in file_path.read_text()
    
    # Failure path (not found)
    res = assistant._execute_tool({
        "name": "edit_file", 
        "arguments": {"path": "edit.txt", "old_str": "wrong", "new_str": "2.0"}
    })
    assert "error" in res

def test_tool_run_shell(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    res = assistant._execute_tool({"name": "run_shell", "arguments": {"command": "echo hello"}})
    assert "hello" in res["stdout"].strip()
    assert res["exit_code"] == 0

def test_tool_git_status(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    # Even without git, it should return a safe response (stdout or clean)
    res = assistant._execute_tool({"name": "git_status", "arguments": {}})
    assert "stdout" in res

def test_tool_summarize_repo(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    # Should call get_project_summary which we just fixed
    res = assistant._execute_tool({"name": "summarize_repo", "arguments": {}})
    assert "summary" in res
    assert "Project Tree" in res["summary"]
