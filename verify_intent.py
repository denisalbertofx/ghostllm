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

def test_intent_routing():
    assistant = CodexAssistant()
    assistant.debug = True
    assistant._chat_loop = MagicMock()
    assistant._inspect_file_locally = MagicMock()
    
    print("\n--- TEST 1: Multiline without file ---")
    multiline_prompt = """quiero que leas esto como un solo bloque:
línea 1
línea 2
línea 3
y me respondas exactamente cuántas líneas pegué y repitas la última línea."""
    
    assistant._process_input(multiline_prompt)
    if not assistant.stop_loop:
        print("PASS: Bypass NOT triggered for generic multiline block.")
    else:
        print("FAIL: Hijacking detected for generic prompt!")
        sys.exit(1)

    print("\n--- TEST 2: Explicit file path + Intent ---")
    # Mock file existence
    original_exists = os.path.exists
    os.path.exists = lambda p: "pyproject.toml" in p or original_exists(p)
    
    assistant._process_input("dime el total de líneas de pyproject.toml")
    if assistant.stop_loop:
        print("PASS: Bypass triggered correctly for pyproject.toml.")
    else:
        print("FAIL: Bypass NOT triggered for explicit file request!")
        sys.exit(1)

if __name__ == "__main__":
    test_intent_routing()
    print("\n[SUCCESS] Intent routing logic verified.")
