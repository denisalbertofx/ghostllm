import sys
import os
from unittest.mock import MagicMock, patch

# Add project root and packages to sys.path
root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(root)
sys.path.append(os.path.join(root, "packages", "py-core"))

from apps.cli.runtime.shell_normalizer import ShellNormalizer

def test_normalization():
    test_cases = [
        ("head -10 file.txt", "Get-Content file.txt | Select-Object -First 10", True),
        ("head -n 5 README.md", "Get-Content README.md | Select-Object -First 5", True),
        ("tail -20 logs.log", "Get-Content logs.log | Select-Object -Last 20", True),
        ("tail -n 3 temp.txt", "Get-Content temp.txt | Select-Object -Last 3", True),
        ("grep \"error\" app.log", "Select-String -Path app.log -Pattern \"error\"", True),
        ("grep pattern source.py", "Select-String -Path source.py -Pattern \"pattern\"", True),
        ("mkdir -p a/b/c", "New-Item -ItemType Directory -Force -Path a/b/c", True),
        ("ls", "ls", False),  # Already valid/handled
        ("npm install", "npm install", False),
        ("sed 's/a/b/g' file.txt", "sed 's/a/b/g' file.txt", False) # Blocked but not changed by normalizer logic directly
    ]

    print(f"--- Shell Normalization Unit Tests ---")
    all_passed = True
    for original, expected, should_change in test_cases:
        norm, changed, expl = ShellNormalizer.normalize(original)
        status = "PASSED" if norm == expected and changed == should_change else "FAILED"
        print(f"[{status}] '{original}' -> '{norm}' (Changed: {changed})")
        if status == "FAILED":
            print(f"  Expected: '{expected}' (Changed: {should_change})")
            all_passed = False
            
    # Test Block Logic
    print(f"\n--- Block Logic Tests ---")
    sed_norm, sed_changed, sed_expl = ShellNormalizer.normalize("sed 's/a/b/g' file.txt")
    if sed_expl and "PowerShell" in sed_expl:
        print(f"[PASSED] 'sed' blocked with explanation: {sed_expl}")
    else:
        print(f"[FAILED] 'sed' was not blocked correctly")
        all_passed = False

    if not all_passed:
        sys.exit(1)

if __name__ == "__main__":
    test_normalization()
