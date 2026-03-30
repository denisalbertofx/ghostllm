import os
import json
import pytest
from unittest.mock import ANY, MagicMock
from apps.cli.ui.ui_contract import MSG_ACTIVE_WORKSET_PLAIN_PREFIX
from apps.cli.assistant import CodexAssistant
from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.session_phase import SessionPhase
from apps.cli.runtime.task_contract import Intent, ensure_task_contract_foundation, seal_task_contract_after_intake

@pytest.fixture
def assistant():
    # Setup a mock assistant in a temporary directory
    mock_as = CodexAssistant("http://mock", "key", "model")
    mock_as.renderer = MagicMock()
    mock_as.console = MagicMock()
    # Mock auto_approve for tests
    mock_as.auto_approve = True
    mock_as.policy_gate.set_auto_approve(True)
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
    assert res["status"] == "success"
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
    res = assistant._execute_tool({"name": "git_status", "arguments": {}})
    assert "error" in res

def test_tool_read_file_slice(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    file_path = tmp_path / "slice.py"
    file_path.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")

    res = assistant._execute_tool(
        {"name": "read_file", "arguments": {"path": "slice.py", "start_line": 2, "max_lines": 2}}
    )

    assert res["content"].replace("\r\n", "\n") == "line2\nline3\n"
    assert res["sliced"] is True
    assert res["start_line"] == 2
    assert res["end_line"] == 3
    assistant.renderer.append_tool_trace.assert_called_with(
        "read_file",
        "slice.py:2-3",
        True,
        auto_approved=True,
        duration_ms=ANY,
    )

def test_tool_edit_file_recovers_newline_mismatch(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    file_path = tmp_path / "crlf.txt"
    with open(file_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("alpha\r\nbeta\r\n")

    res = assistant._execute_tool(
        {
            "name": "edit_file",
            "arguments": {
                "path": "crlf.txt",
                "old_str": "alpha\nbeta\n",
                "new_str": "alpha\nbeta2\n",
            },
        }
    )

    assert res["status"] == "success"
    assert res["match_mode"] == "normalized_newlines"
    with open(file_path, "r", encoding="utf-8", newline="") as handle:
        assert handle.read() == "alpha\r\nbeta2\r\n"

def test_tool_summarize_repo(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    # Should call get_project_summary which we just fixed
    res = assistant._execute_tool({"name": "summarize_repo", "arguments": {}})
    assert "summary" in res
    assert "Project Tree" in res["summary"]


def test_mark_fast_path_intake_state_uses_passed_session_spec(assistant):
    session = ArtifactSession("s1", "task")
    ensure_task_contract_foundation(session, Intent(mode="Chat", task="x", original_text="x"))
    session.runtime_contract_source = "taskspec"
    session.task_contract["spec"] = {
        "intent": "bugfix",
        "change_expectation": "must_write",
        "target_files": ["apps/cli/main.py"],
    }
    seal_task_contract_after_intake(session)
    assistant._mark_fast_path_intake_state(session)
    assert session.fast_path_eligible is True


def test_contract_spec_blocks_nonverification_shell_when_edit_recovery_pending(assistant):
    session = ArtifactSession("s1", "task")
    ensure_task_contract_foundation(session, Intent(mode="Chat", task="x", original_text="x"))
    session.runtime_contract_source = "taskspec"
    session.task_contract["spec"] = {
        "intent": "bugfix",
        "change_expectation": "must_write",
        "target_files": ["apps/cli/main.py"],
        "budget_policy": {"max_tool_calls": 12, "reserved_write_tool_calls": 2},
    }
    seal_task_contract_after_intake(session)
    assistant.artifact_manager.current_session = session
    assistant._pending_edit_recovery = {
        "next_tool": "read_file",
        "arguments": {"path": "apps/cli/main.py", "start_line": 278, "max_lines": 12},
    }

    ok, reason = assistant._contract_spec_allow_tool(
        "run_shell",
        {"command": "python -c \"content = open('apps/cli/main.py', 'rb').read(); print(repr(content))\""},
    )

    assert ok is False
    assert "read_file instead of run_shell" in reason
    assert "apps/cli/main.py" in reason


def test_detect_already_implemented_fast_path_for_focused_cli_bugfix(assistant):
    session = ArtifactSession("s1", "task")
    ensure_task_contract_foundation(
        session,
        Intent(mode="Chat", task="doctor ready message", original_text="doctor ready message"),
    )
    session.runtime_contract_source = "taskspec"
    session.task_contract["spec"] = {
        "intent": "bugfix",
        "change_expectation": "must_write",
        "target_files": ["apps/cli/main.py"],
    }
    session.repo_profile = {
        "profile_v2": {
            "verification_commands": {
                "typecheck": {"command": "npx tsc --noEmit", "cwd": "apps/web"},
                "build": {"command": "npm run build", "cwd": "apps/web"},
                "lint": {"command": "npm run lint", "cwd": "apps/web"},
            }
        }
    }
    seal_task_contract_after_intake(session)
    assistant.artifact_manager.current_session = session
    assistant.history = [
        {"role": "tool", "name": "ls", "content": json.dumps({"files": ["test_provider_initialization.py"]})},
        {
            "role": "tool",
            "name": "read_file",
            "content": json.dumps({"content": "def check_provider_ready(base_url=None):\n    ...\n"}),
        },
        {
            "role": "tool",
            "name": "read_file",
            "content": json.dumps(
                {"content": "from apps.cli.main import check_provider_ready\n\ndef test_ok():\n    assert callable(check_provider_ready)\n"}
            ),
        },
    ]

    signal = assistant._detect_already_implemented_fast_path()

    assert signal is not None
    assert "apps/cli/main.py" in signal["target_files"]
    assert any("test_provider_initialization.py" in cmd for cmd in signal["targeted_tests"])


def test_explore_tools_suppressed_when_already_implemented_answer_pending(assistant):
    assistant._already_implemented_answer_pending = {
        "target_files": ["apps/cli/main.py"],
        "targeted_tests": ["uv run python -m pytest tests/test_provider_initialization.py"],
    }

    defs = assistant._explore_tools_for_turn(1)

    assert defs == []


def test_contract_spec_blocks_verification_shell_when_already_implemented(assistant):
    session = ArtifactSession("s1", "task")
    ensure_task_contract_foundation(
        session,
        Intent(mode="Chat", task="doctor ready message", original_text="doctor ready message"),
    )
    session.runtime_contract_source = "taskspec"
    session.task_contract["spec"] = {
        "intent": "bugfix",
        "change_expectation": "must_write",
        "target_files": ["apps/cli/main.py"],
    }
    session.repo_profile = {
        "profile_v2": {
            "verification_commands": {
                "typecheck": {"command": "npx tsc --noEmit", "cwd": "apps/web"},
                "build": {"command": "npm run build", "cwd": "apps/web"},
                "lint": {"command": "npm run lint", "cwd": "apps/web"},
            }
        }
    }
    seal_task_contract_after_intake(session)
    assistant.artifact_manager.current_session = session
    assistant.history = [
        {"role": "tool", "name": "read_file", "content": json.dumps({"content": "def doctor():\n    pass\n"})},
        {
            "role": "tool",
            "name": "read_file",
            "content": json.dumps(
                {"content": "from apps.cli.main import check_provider_ready\n\ndef test_ok():\n    assert callable(check_provider_ready)\n"}
            ),
        },
    ]

    ok, reason = assistant._contract_spec_allow_tool(
        "run_shell",
        {"command": "python -m pytest tests/test_provider_initialization.py -xvs 2>&1"},
    )

    assert ok is False
    assert "Already implemented fast-path" in reason


def test_active_workset_tracks_reads_edits_and_related_tests(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    session = ArtifactSession("s1", "task")
    assistant.artifact_manager.current_session = session
    src = tmp_path / "pkg" / "mod.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("VALUE = 1\n", encoding="utf-8")
    test_file = tmp_path / "tests" / "test_mod.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("from pkg.mod import VALUE\n", encoding="utf-8")

    assistant._execute_tool({"name": "read_file", "arguments": {"path": "pkg/mod.py"}})
    assistant._execute_tool({"name": "read_file", "arguments": {"path": "tests/test_mod.py"}})
    assistant._execute_tool(
        {
            "name": "edit_file",
            "arguments": {"path": "pkg/mod.py", "old_str": "VALUE = 1", "new_str": "VALUE = 2"},
        }
    )

    ws = session.active_workset
    assert "pkg/mod.py" in ws["read_files"]
    assert "pkg/mod.py" in ws["edited_files"]
    assert "tests/test_mod.py" in ws["related_tests"]
    assert "pkg/mod.py" in ws["candidate_files"]


def test_phase_transition_appends_phase_checkpoint(assistant):
    session = ArtifactSession("s1", "task")
    session.plan = "Implement multi-file fix"
    session.note_workset_candidate("apps/cli/main.py")
    assistant.artifact_manager.current_session = session

    assistant._apply_session_phase(SessionPhase.EXPLORE, detail="initial focused exploration")
    assistant._apply_session_phase(SessionPhase.ACT, detail="apply batch 1")

    assert len(session.phase_checkpoints) >= 2
    assert session.phase_checkpoints[-1]["phase"] == SessionPhase.ACT.value
    assert "apps/cli/main.py" in session.phase_checkpoints[-1]["focus_files"]


def test_contract_spec_blocks_write_file_for_structural_repair_on_existing_file(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    target = tmp_path / "apps" / "server" / "database.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def broken():\n pass\n", encoding="utf-8")
    task_text = "repara únicamente el error de sintaxis/import en apps/server/database.py. No cambies diseño."
    session = ArtifactSession("s1", "task")
    ensure_task_contract_foundation(
        session,
        Intent(
            mode="Chat",
            task=task_text,
            original_text=task_text,
        ),
    )
    session.runtime_contract_source = "taskspec"
    session.task_contract["spec"] = {
        "intent": "bugfix",
        "change_expectation": "must_write",
        "target_files": ["apps/server/database.py"],
    }
    seal_task_contract_after_intake(session)
    assistant.current_intent = Intent(mode="Chat", task=task_text, original_text=task_text)
    assistant.artifact_manager.current_session = session

    ok, reason = assistant._contract_spec_allow_tool(
        "write_file",
        {"path": "apps/server/database.py", "content": "replacement"},
    )

    assert ok is False
    assert "edit_file instead of write_file" in reason


def test_contract_spec_blocks_write_file_for_small_bugfix_even_without_syntax_words(assistant, tmp_path):
    assistant.cwd = str(tmp_path)
    target = tmp_path / "apps" / "server" / "database.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    task_text = "haz el cambio minimo en apps/server/database.py para arreglar el bug sin tocar nada mas"
    session = ArtifactSession("s1", "task")
    ensure_task_contract_foundation(
        session,
        Intent(
            mode="Chat",
            task=task_text,
            original_text=task_text,
        ),
    )
    session.runtime_contract_source = "taskspec"
    session.task_contract["spec"] = {
        "intent": "bugfix",
        "change_expectation": "must_write",
        "target_files": ["apps/server/database.py"],
    }
    seal_task_contract_after_intake(session)
    assistant.current_intent = Intent(mode="Chat", task=task_text, original_text=task_text)
    assistant.artifact_manager.current_session = session

    ok, reason = assistant._contract_spec_allow_tool(
        "write_file",
        {"path": "apps/server/database.py", "content": "replacement"},
    )

    assert ok is False
    assert "edit_file instead of write_file" in reason


def test_execution_context_nudge_mentions_active_workset_and_incremental_verify(assistant):
    session = ArtifactSession("s1", "task")
    session.note_workset_edit("apps/server/main.py", "edit")
    session.note_related_test("tests/test_runtime_health.py")
    session.record_incremental_verification(
        {
            "status": "failed",
            "verification_scope": "python_targeted",
            "checks": [{"name": "Tests", "status": "failed"}],
            "steps_executed_count": 1,
        },
        diff_summary=[{"file": "apps/server/main.py", "status": "success"}],
        context_label="iter_3",
    )
    assistant.artifact_manager.current_session = session
    assistant.session_phase = SessionPhase.REPAIR

    ctx = assistant._compute_execution_context()

    assert MSG_ACTIVE_WORKSET_PLAIN_PREFIX in ctx["nudge"]
    assert "Incremental verify:" in ctx["nudge"]


def test_repair_focus_files_prefers_edited_files_and_related_tests(assistant):
    session = ArtifactSession("s1", "task")
    session.note_workset_edit("apps/server/main.py", "edit")
    session.note_related_test("tests/test_runtime_health.py")

    focus = assistant._repair_focus_files(session, [{"name": "Tests", "status": "failed"}])

    assert focus[0] == "apps/server/main.py"
    assert "tests/test_runtime_health.py" in focus
