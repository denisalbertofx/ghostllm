import subprocess
import os
import json
import re
from typing import Dict, Any, List, Optional

class VerificationManager:
    """
    Handles post-edit verification (build, lint, tests).
    GEP-5: Verification Contract implementation.
    """
    def __init__(self, cwd: str):
        self.cwd = cwd

    def verify_change(self, history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Attempts to verify the current state of the project.
        history: recent conversation history to avoid redundant checks.
        """
        checks = self.get_applicable_checks()
        if not checks:
            return {"status": "skipped", "reason": "No supported stack detected for auto-verification."}

        # Deduplication logic: don't run commands the LLM just ran
        if history:
            recent_cmds = []
            for msg in history[-10:]:
                if msg.get("role") == "tool" and msg.get("name") == "run_shell":
                    try:
                        res = json.loads(msg.get("content", "{}"))
                        # Extract command from previous user message or context? 
                        # Easier to just look for common patterns in tool output
                        pass
                    except: pass
            
            # Simple heuristic: if we see "npm run build" succeeded in the last 3 tool results, skip it.
            # (Keeping it simple for now to avoid over-engineering)

        results = []
        overall_success = True
        
        for check in checks:
            name = check["name"]
            cmd = check["command"]
            try:
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=self.cwd, timeout=300)
                success = res.returncode == 0
                if not success: overall_success = False
                
                results.append({
                    "name": name,
                    "command": cmd,
                    "status": "success" if success else "failed",
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                    "exit_code": res.returncode
                })
            except Exception as e:
                overall_success = False
                results.append({
                    "name": name,
                    "command": cmd,
                    "status": "error",
                    "error": str(e)
                })

        return {
            "status": "success" if overall_success else "failed",
            "checks": results
        }

    def get_applicable_checks(self) -> List[Dict[str, str]]:
        """
        Intelligently detects which verificaton commands should be run.
        """
        checks = []
        
        # 1. Node.js Detection
        pkg_path = os.path.join(self.cwd, "package.json")
        if os.path.exists(pkg_path):
            try:
                with open(pkg_path, "r") as f:
                    pkg = json.load(f)
                    scripts = pkg.get("scripts", {})
                    # Prioritize key lifecycle scripts
                    if "build" in scripts:
                        checks.append({"name": "Build", "command": "npm run build"})
                    elif os.path.exists(os.path.join(self.cwd, "tsconfig.json")):
                        # Fallback for TS repos without a build script
                        checks.append({"name": "TypeCheck", "command": "npx tsc --noEmit"})
                        
                    if "lint" in scripts:
                        checks.append({"name": "Lint", "command": "npm run lint"})
                    if "test" in scripts:
                        checks.append({"name": "Test", "command": "npm test"})
            except: pass

        # 2. Python Detection
        is_python = any(os.path.exists(os.path.join(self.cwd, f)) for f in ["pyproject.toml", "requirements.txt", "setup.py", "Pipfile"])
        if is_python:
            has_uv = subprocess.run("uv --version", shell=True, capture_output=True).returncode == 0
            if os.path.exists(os.path.join(self.cwd, "tests")) or os.path.exists(os.path.join(self.cwd, "test")):
                if has_uv:
                    checks.append({"name": "Python Tests (uv)", "command": "uv run pytest"})
                else:
                    has_pytest = subprocess.run("pytest --version", shell=True, capture_output=True).returncode == 0
                    if has_pytest:
                        checks.append({"name": "Python Tests (pytest)", "command": "pytest"})
                    else:
                        checks.append({"name": "Python Tests (unittest)", "command": "python -m unittest discover"})

        return checks
