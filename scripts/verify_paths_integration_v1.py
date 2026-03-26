import sys
import os
from unittest.mock import MagicMock, patch

# Add project root and packages to sys.path
root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(root)
sys.path.append(os.path.join(root, "packages", "py-core"))

from apps.cli.assistant import CodexAssistant

def test_integration():
    assistant = CodexAssistant(server_url="http://localhost:11434", api_key="fake", model="llama3")
    assistant.console = MagicMock()
    assistant.renderer = MagicMock()
    
    # Test B: Preflight HARD_BLOCK enforcement
    print("--- Test B: Preflight HARD_BLOCK enforcement ---")
    text = "inicializa un nuevo proyecto Next.js desde cero en el directorio actual"
    
    # Simulate being in a project
    with patch("apps.cli.runtime.paths.WorkingDirectoryGuard.detect_context", return_value=("project", [".git", "package.json"])):
        # Patch everything that could cause side effects or serialization errors
        with patch.object(assistant.task_manager, "create_task") as mock_create:
            with patch.object(assistant.artifact_manager, "start_session") as mock_session:
                with patch.object(assistant.artifact_manager, "persist"):
                    
                    # _process_input should return early
                    assistant._process_input(text)
                    
                    # Check if console printed conflict warning and options
                    calls = [str(c) for c in assistant.console.print.call_args_list]
                    found_conflict = any("CWD GUARD CONFLICT" in c for c in calls)
                    found_options = any("Opciones válidas" in c for c in calls)
                    
                    # KEY CHECKS
                    task_created = mock_create.called
                    session_started = mock_session.called
                    
                    print(f"Preflight Block detected: {found_conflict}")
                    print(f"Options displayed: {found_options}")
                    print(f"Task creation bypassed: {not task_created}")
                    print(f"Session startup bypassed: {not session_started}")
                    print(f"Session marked as failed: {assistant._session_failed}")

if __name__ == "__main__":
    test_integration()
