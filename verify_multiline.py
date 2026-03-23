import os
import sys
from unittest.mock import patch

# Add necessary paths for the monorepo structure
sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), "packages", "py-core"))

# Mock environment before imports
os.environ["NATIVE_TOOLS_FORCE_NON_STREAM"] = "true"

from apps.cli.assistant import CodexAssistant

# Mock dependencies
assistant = CodexAssistant("http://localhost:11434", "key", "model")

test_input = [
    "<<<",
    "quiero que leas esto como un solo bloque:",
    "",
    "línea 1",
    "línea 2",
    "línea 3",
    "",
    "y me respondas exactamente cuántas líneas pegué y repitas la última línea.",
    "EOF"
]

print("--- SIMULANDO PEGADO MULTILINEA COMPLEJO ---")
with patch('builtins.input', side_effect=test_input):
    result = assistant._read_user_input()
    print(f"\nCAPTURED RESULT:")
    print(repr(result))

# Analysis of newlines
newline_count = result.count("\n")
lines_count = len(result.splitlines())

print(f"\nDIAGNOSTICS:")
print(f"  \\n count: {newline_count}")
print(f"  Total lines (splitlines): {lines_count}")

# Verification of structure preservation
expected_lines = 8 # (1 info, 1 empty, 3 data, 1 empty, 1 question) - wait, let's count:
# 1. quiero...
# 2. (empty)
# 3. línea 1
# 4. línea 2
# 5. línea 3
# 6. (empty)
# 7. y me respondas...
# That's 7 lines.

if lines_count == 7:
    print("\n✓ SUCCESS: Multiline structure preserved exactly (7 lines).")
else:
    print(f"\n✘ FAILURE: Expected 7 lines, got {lines_count}.")

if "\n\n" in result:
    print("✓ SUCCESS: Empty lines preserved.")
else:
    print("✘ FAILURE: Empty lines NOT preserved.")
