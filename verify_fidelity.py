import os
import sys
from unittest.mock import MagicMock

# Path setup
sys.path.append(os.getcwd())

# Mock Typer/Rich
sys.modules['typer'] = MagicMock()
sys.modules['rich'] = MagicMock()
sys.modules['rich.console'] = MagicMock()
sys.modules['rich.panel'] = MagicMock()
sys.modules['rich.status'] = MagicMock()
sys.modules['rich.table'] = MagicMock()
sys.modules['rich.syntax'] = MagicMock()
sys.modules['rich.markup'] = MagicMock()
sys.modules['rich.box'] = MagicMock()

# Import the assistant
from apps.cli.assistant import CodexAssistant

def test_multiline_fidelity():
    assistant = CodexAssistant()
    assistant.debug = True
    assistant._chat_loop = MagicMock()
    
    print("\n--- TEST: Multiline wrapping ---")
    multiline_prompt = "line 1\nline 2"
    
    assistant._process_input(multiline_prompt)
    
    # Check if history contains the wrapped block
    latest_msg = assistant.history[-1]
    if "<pasted_block>" in latest_msg["content"]:
        print("PASS: Multiline block wrapped in tags.")
        print(f"Content: {latest_msg['content']}")
    else:
        print("FAIL: Multiline block NOT wrapped!")
        sys.exit(1)

if __name__ == "__main__":
    test_multiline_fidelity()
    print("\n[SUCCESS] Multiline fidelity verified.")
