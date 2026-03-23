import sys
import os
import json
import time

# Add project root and packages to path
root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, root)
sys.path.insert(0, os.path.join(root, "packages", "py-core"))

from apps.cli.assistant import CodexAssistant

def run_e2e_debug():
    print(">>> Initializing CodexAssistant (E2E Debug Mode)")
    # Using 'fast' model (Llama-3.1-8b) - the trace interceptor is triggered by 'TEST_E2E'
    assistant = CodexAssistant("http://127.0.0.1:11434", "ghost-dev-2026", "fast", mode="Chat", auto_approve=True)
    
    task = "TEST_E2E"
    print(f">>> Task: {task}")
    
    try:
        # This will trigger _stream_completion and the tool execution loop
        assistant._process_input(task)
    except Exception as e:
        print(f"\n[ERROR] E2E Execution failed: {str(e)}")

if __name__ == "__main__":
    run_e2e_debug()
