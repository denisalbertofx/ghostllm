import sys
import os
from unittest.mock import MagicMock, patch

# Add project root and packages to sys.path
root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(root)
sys.path.append(os.path.join(root, "packages", "py-core"))

from apps.cli.assistant import CodexAssistant

def test_regression():
    assistant = CodexAssistant(server_url="http://localhost:11434", api_key="fake", model="llama3")
    assistant.console = MagicMock()
    assistant.renderer = MagicMock()
    
    # Exact input that caused the leak
    text = "inicializa un nuevo proyecto Next.js desde cero en el directorio actual"
    
    print(f"--- Regression Test: Preflight Leak Check ---")
    print(f"Input: {text}")
    
    # Simulate being in a project
    with patch("apps.cli.runtime.paths.WorkingDirectoryGuard.detect_context", return_value=("project", [".git", "package.json"])):
        # Patch critical points that denote "entering the loop"
        with patch.object(assistant.task_manager, "create_task") as mock_create:
            with patch.object(assistant.artifact_manager, "start_session") as mock_session:
                with patch.object(assistant, "_chat_loop") as mock_loop:
                    with patch.object(assistant.budget_manager, "consume") as mock_budget:
                        
                        # Execute the preflight entry point
                        assistant._process_input(text)
                        
                        # Check Console Output
                        calls = [str(c) for c in assistant.console.print.call_args_list]
                        found_block = any("CWD GUARD CONFLICT" in c for c in calls)
                        found_options = any("Opciones válidas" in c for c in calls)
                        
                        # VALIDATION: No state-changing methods should be called
                        task_created = mock_create.called
                        session_started = mock_session.called
                        loop_entered = mock_loop.called
                        budget_consumed = mock_budget.called
                        
                        print(f"Terminal block emitted: {found_block}")
                        print(f"Options suggested: {found_options}")
                        print(f"Task creation avoided: {not task_created}")
                        print(f"Session startup avoided: {not session_started}")
                        print(f"Chat loop avoided: {not loop_entered}")
                        print(f"Budget consumption avoided: {not budget_consumed}")
                        
                        success = found_block and not task_created and not loop_entered
                        print(f"\nREGRESSION TEST RESULT: {'PASSED' if success else 'FAILED'}")
                        
                        if not success:
                            sys.exit(1)

if __name__ == "__main__":
    test_regression()
