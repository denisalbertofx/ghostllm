import sys
import os
import json
from unittest.mock import MagicMock, patch

# Add project root and packages to sys.path
root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(root)
sys.path.append(os.path.join(root, "packages", "py-core"))

from apps.cli.assistant import CodexAssistant

def test_hardened_shell():
    # Initialize 
    assistant = CodexAssistant(server_url="http://localhost:11434", api_key="fake", model="llama3")
    assistant.console = MagicMock()
    assistant.renderer = MagicMock()
    
    # Test A: Exact command failure (Terminal stop)
    text_a = "/do ejecuta exactamente: head -10 pyproject.toml"
    print(f"--- Test A: Exact Command Failure ---")
    
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="File not found")
        
        # MOCK LLM to return the tool call
        with patch.object(assistant, "_stream_completion") as mock_stream:
            # First call returns tool call, second call returns None to break loop (if it didn't return early)
            mock_stream.side_effect = [
                {"content": "", "tool_calls": [{"name": "run_shell", "arguments": {"command": "head -10 pyproject.toml"}}]},
                None
            ]
            
            with patch.object(assistant.policy_gate, "check_permission", return_value=True):
                # Execute
                assistant._process_input(text_a)
                
                # Check Intent
                intent = assistant.current_intent
                print(f"Intent classified as exact command: {intent.is_exact_command}")
                
                # Validation A
                # Count calls to subprocess.run that involve Get-Content
                target_calls = [str(c[0][0]) for c in mock_run.call_args_list if "Get-Content" in str(c[0][0])]
                target_count = len(target_calls)
                print(f"Target Run Count (Normalized): {target_count}")
                if target_count > 0:
                    print(f"Actual Command Executed: {target_calls[0]}")
                
                calls_to_console = [str(c[0][0]) if c[0] else "" for c in assistant.console.print.call_args_list]
                found_terminal_fail = any("TERMINAL FAIL" in c for c in calls_to_console)
                print(f"Terminal Fail Emitted: {found_terminal_fail}")
                
                success_a = intent.is_exact_command and target_count == 1 and found_terminal_fail
                print(f"TEST A RESULT: {'PASSED' if success_a else 'FAILED'}")

    # Test B: Exact command success
    text_b = "/do ejecuta exactamente: head -10 package.json"
    print(f"\n--- Test B: Exact Command Success ---")
    
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="content", stderr="")
        
        with patch.object(assistant, "_stream_completion") as mock_stream:
            mock_stream.side_effect = [
                {"content": "", "tool_calls": [{"name": "run_shell", "arguments": {"command": "head -10 package.json"}}]},
                None
            ]
            
            with patch.object(assistant.policy_gate, "check_permission", return_value=True):
                assistant._process_input(text_b)
                
                target_calls = [str(c[0][0]) for c in mock_run.call_args_list if "Get-Content" in str(c[0][0])]
                target_count = len(target_calls)
                print(f"Target Run Count: {target_count}")
                
                success_b = target_count == 1
                print(f"TEST B RESULT: {'PASSED' if success_b else 'FAILED'}")

    if not (success_a and success_b):
        sys.exit(1)

if __name__ == "__main__":
    test_hardened_shell()
