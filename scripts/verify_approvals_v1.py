import os
import sys
import json
from unittest.mock import MagicMock, patch

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from apps.cli.runtime.policy_gate import PolicyGate, ApprovalLevel
from apps.cli.runtime.session_phase import SessionPhase
from apps.cli.ui.renderer import GhostRenderer
from apps.cli.assistant import CodexAssistant

def test_approval_categorization():
    print("\n--- Testing Approval Categorization ---")
    gate = PolicyGate(MagicMock())
    
    # AUTO
    assert gate.get_approval_level("read_file", "test.py") == ApprovalLevel.AUTO
    assert gate.get_approval_level("ls", ".") == ApprovalLevel.AUTO
    
    # BUNDLE
    assert gate.get_approval_level("write_file", "app.py") == ApprovalLevel.BUNDLE
    assert gate.get_approval_level("edit_file", "config.json") == ApprovalLevel.BUNDLE
    
    # MANUAL
    assert gate.get_approval_level("run_shell", "npm install") == ApprovalLevel.MANUAL
    assert gate.get_approval_level("run_shell", "rm -rf /") == ApprovalLevel.MANUAL
    
    print("✓ Categorization logic is correct.")

def test_bundled_approval_flow():
    print("\n--- Testing Bundled Approval Flow ---")
    # Mocking Console and Renderer
    console = MagicMock()
    renderer = MagicMock(spec=GhostRenderer)
    renderer.render_bundled_approval_request.return_value = True # User approves bundle
    
    # assistant initialization
    # Adding mock server_url to avoid TypeError
    assistant = CodexAssistant(api_key="mock", project_name="test", profile="coder", model="mock", server_url="http://localhost:8000")
    assistant.renderer = renderer
    # PolicyGate is initialized in CodexAssistant.__init__, but we swap it to ensure clean state
    assistant.policy_gate = PolicyGate(console)
    assistant.auto_approve = False
    
    # Simulated tool calls (2 writes)
    tool_calls = [
        {"id": "tc1", "function": {"name": "write_file", "arguments": json.dumps({"path": "file1.py", "content": "print('hi')"})}},
        {"id": "tc2", "function": {"name": "write_file", "arguments": json.dumps({"path": "file2.py", "content": "print('hello')"})}}
    ]
    
    # Mock _execute_tool to avoid actual file system operations
    assistant._execute_tool = MagicMock(return_value={"status": "success"})
    assistant.session_phase = SessionPhase.ACT
    assistant.stagnation_detector.check_stagnation = MagicMock(return_value=(False, ""))
    assistant.budget_manager.consume = MagicMock()
    assistant.budget_manager.record_taskspec_tool_invocation = MagicMock()
    assistant.memory = MagicMock()
    assistant.history = []

    msg = {
        "content": "I will create two files.",
        "tool_calls": tool_calls,
    }
    status = MagicMock()
    task_m = MagicMock()
    with patch.object(assistant, "task_manager", MagicMock()):
        assistant._dispatch_model_tool_round(msg, status, task_m)
             
    # Verify bundled authorization was requested ONCE
    assert renderer.render_bundled_approval_request.called
    assert renderer.render_bundled_approval_request.call_count == 1
    
    # Verify tools were executed with is_authorized=True
    assert assistant._execute_tool.call_count == 2
    args1 = assistant._execute_tool.call_args_list[0]
    args2 = assistant._execute_tool.call_args_list[1]
    assert args1[1]["is_authorized"] == True
    assert args2[1]["is_authorized"] == True

    print("✓ Bundled approval flow verified (1 prompt for 2 files).")

if __name__ == "__main__":
    try:
        test_approval_categorization()
        test_bundled_approval_flow()
        print("\n[SUCCESS] Approval Model v1 verification complete.")
    except Exception as e:
        print(f"\n[FAILURE] Verification failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
