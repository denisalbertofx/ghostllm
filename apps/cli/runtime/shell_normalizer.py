import re
from typing import Tuple, Optional

class ShellNormalizer:
    """
    Normalizes Unix-style shell commands to PowerShell equivalents on Windows.
    GEP-3: Windows Compatibility layer.
    """

    @staticmethod
    def normalize(command: str) -> Tuple[str, bool, Optional[str]]:
        """
        Takes a Unix-style command and returns (normalized_command, was_changed, explanation).
        """
        original = command.strip()
        cmd = original
        changed = False
        explanation = None

        # 1. head [-n] <count> <file> -> Get-Content <file> | Select-Object -First <count>
        head_match = re.search(r"^head\s+(?:-n\s+)?-?(\d+)\s+(.+)$", cmd)
        if head_match:
            count = head_match.group(1)
            path = head_match.group(2)
            cmd = f"Get-Content {path} | Select-Object -First {count}"
            changed = True

        # 2. tail [-n] <count> <file> -> Get-Content <file> | Select-Object -Last <count>
        tail_match = re.search(r"^tail\s+(?:-n\s+)?-?(\d+)\s+(.+)$", cmd)
        if tail_match:
            count = tail_match.group(1)
            path = tail_match.group(2)
            cmd = f"Get-Content {path} | Select-Object -Last {count}"
            changed = True

        # 3. grep "pattern" <file> -> Select-String -Path <file> -Pattern "pattern"
        # Support for: grep "pattern" file, grep pattern file
        grep_match = re.search(r"^grep\s+(?:-E\s+)?(?:[\"'](.+?)[\"']|([^\s]+))\s+(.+)$", cmd)
        if grep_match:
            pattern = grep_match.group(1) or grep_match.group(2)
            path = grep_match.group(3)
            cmd = f"Select-String -Path {path} -Pattern \"{pattern}\""
            changed = True

        # 4. mkdir -p <path> -> New-Item -ItemType Directory -Force -Path <path>
        mkdir_match = re.search(r"^mkdir\s+-p\s+(.+)$", cmd)
        if mkdir_match:
            path = mkdir_match.group(1)
            cmd = f"New-Item -ItemType Directory -Force -Path {path}"
            changed = True

        # 5. Unsupported but known Unix-isms block list (for detailed explanation)
        unsupported = {
            "sed": "PowerShell has no direct single-line equivalent for 'sed'. Use (Get-Content file).Replace() or a text processing script.",
            "awk": "PowerShell has no direct equivalent for 'awk'. Consider using 'Import-Csv' or object manipulation.",
            "touch": "Use 'New-Item -ItemType File -Path <file>' to simulate 'touch' on Windows.",
            "find": "Use 'Get-ChildItem -Recurse -Filter <pattern>' as equivalent of 'find'.",
            "rm -rf": "Use 'Remove-Item -Recurse -Force -Path <path>' for recursive deletion."
        }

        for unix_cmd, msg in unsupported.items():
            if cmd.startswith(unix_cmd):
                return original, False, msg

        return cmd, changed, None
