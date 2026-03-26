import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Check statuses used by VerificationCoordinator.attach_coordinator_view
CHECK_BLOCKED = "blocked"
CHECK_ERROR = "error"
CHECK_FAILED = "failed"


@dataclass(frozen=True)
class ChosenVerificationPlan:
    """Ordered check names to run (before intersection with repo-applicable checks)."""

    check_names: Tuple[str, ...]


def choose_verification_plan_from_contract_spec(
    spec: Dict[str, Any],
    *,
    task_type: str = "",
    scope: str = "",
    files_changed: Optional[List[Dict[str, Any]]] = None,
    budget_remaining: Optional[int] = None,
    failed_check_names: Optional[List[str]] = None,
    repo_v2: Optional[Dict[str, Any]] = None,
) -> ChosenVerificationPlan:
    _ = task_type, scope, files_changed, budget_remaining, failed_check_names, repo_v2
    vp = spec.get("verification_policy")
    if not isinstance(vp, dict):
        return ChosenVerificationPlan(check_names=())
    names: List[str] = []
    if bool(vp.get("typecheck")):
        names.append("TypeCheck")
    if bool(vp.get("build")):
        names.append("Build")
    if bool(vp.get("lint")):
        names.append("Lint")
    if bool(vp.get("test")):
        names.append("Test")
    return ChosenVerificationPlan(check_names=tuple(names))


def choose_verification_plan(
    *,
    task_type: str = "",
    scope: str = "",
    files_changed: Optional[List[Dict[str, Any]]] = None,
    budget_remaining: Optional[int] = None,
    failed_check_names: Optional[List[str]] = None,
    repo_v2: Optional[Dict[str, Any]] = None,
) -> ChosenVerificationPlan:
    """Default cheap-first preference when no task_contract verification_policy."""
    _ = task_type, scope, files_changed, budget_remaining, failed_check_names, repo_v2
    return ChosenVerificationPlan(check_names=("TypeCheck", "Build", "Lint", "Test"))


def filter_plan_to_available_checks(
    plan: ChosenVerificationPlan,
    all_checks: Sequence[Dict[str, str]],
) -> ChosenVerificationPlan:
    available = {c.get("name") for c in all_checks if c.get("name")}
    ordered = tuple(n for n in plan.check_names if n in available)
    return ChosenVerificationPlan(check_names=ordered)


def adjust_verification_plan_for_ux_level(
    plan: ChosenVerificationPlan,
    all_checks: Sequence[Dict[str, str]],
    *,
    failed_check_names: Optional[List[str]] = None,
) -> ChosenVerificationPlan:
    _ = all_checks, failed_check_names
    return plan


def _checks_by_name(all_checks: Sequence[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    return {c["name"]: dict(c) for c in all_checks if c.get("name")}


def _normalize_repo_vc_key(key: str) -> str:
    """Map RepoProfile / taskspec keys (typecheck) to check labels (TypeCheck)."""
    k = str(key).strip()
    return {
        "typecheck": "TypeCheck",
        "build": "Build",
        "lint": "Lint",
        "test": "Test",
    }.get(k.lower(), k)


class VerificationManager:
    """
    Handles post-edit verification (build, lint, tests).
    GEP-5: Verification Contract implementation.
    """

    def __init__(
        self,
        cwd: str,
        repo_verification_commands: Optional[Dict[str, Any]] = None,
    ):
        self.cwd = cwd
        self._repo_vc = repo_verification_commands or {}

    def set_repo_verification_commands(self, commands: Dict[str, Any]) -> None:
        """Update the repo verification commands map (called after profile scan at INTAKE)."""
        if commands:
            self._repo_vc.update(commands)

    def get_applicable_checks(
        self,
        task_type: Optional[str] = None,
        scope: Optional[str] = None,
        files_changed: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, str]]:
        _ = task_type, scope, files_changed
        checks: List[Dict[str, str]] = []

        pkg_path = os.path.join(self.cwd, "package.json")
        if os.path.exists(pkg_path):
            try:
                with open(pkg_path, "r", encoding="utf-8") as f:
                    pkg = json.load(f)
                scripts = pkg.get("scripts", {}) or {}
                if os.path.exists(os.path.join(self.cwd, "tsconfig.json")):
                    checks.append({"name": "TypeCheck", "command": "npx tsc --noEmit"})
                if "build" in scripts:
                    checks.append({"name": "Build", "command": "npm run build"})
                elif not checks:
                    # Original fallback: TS project without a build script
                    if os.path.exists(os.path.join(self.cwd, "tsconfig.json")):
                        checks.append({"name": "TypeCheck", "command": "npx tsc --noEmit"})
                if "lint" in scripts:
                    checks.append({"name": "Lint", "command": "npm run lint"})
                if "test" in scripts:
                    checks.append({"name": "Test", "command": "npm test"})
            except Exception:
                pass

        is_python = any(
            os.path.exists(os.path.join(self.cwd, f))
            for f in ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile")
        )
        if is_python:
            has_uv = subprocess.run("uv --version", shell=True, capture_output=True).returncode == 0
            if os.path.exists(os.path.join(self.cwd, "tests")) or os.path.exists(
                os.path.join(self.cwd, "test")
            ):
                if has_uv:
                    checks.append({"name": "Python Tests (uv)", "command": "uv run pytest"})
                else:
                    has_pytest = (
                        subprocess.run("pytest --version", shell=True, capture_output=True).returncode == 0
                    )
                    if has_pytest:
                        checks.append({"name": "Python Tests (pytest)", "command": "pytest"})
                    else:
                        checks.append(
                            {"name": "Python Tests (unittest)", "command": "python -m unittest discover"}
                        )

        # Optional repo profile overrides (typecheck/build/... or TypeCheck/...)
        if isinstance(self._repo_vc, dict):
            by_name = _checks_by_name(checks)
            for key, val in self._repo_vc.items():
                if not isinstance(key, str):
                    continue
                cmd = val if isinstance(val, str) else (val.get("command") if isinstance(val, dict) else None)
                if not cmd:
                    continue
                cname = _normalize_repo_vc_key(key)
                by_name[cname] = {"name": cname, "command": str(cmd)}
            checks = list(by_name.values())

        return checks

    def verify_change(
        self,
        history: Optional[List[Dict[str, Any]]] = None,
        *,
        task_type: str = "",
        scope: str = "",
        blocked_commands: Optional[List[Any]] = None,
        skip_execution: bool = False,
        skip_reason: Optional[str] = None,
        files_changed: Optional[List[Dict[str, Any]]] = None,
        budget_remaining: Optional[int] = None,
        failed_check_names: Optional[List[str]] = None,
        contract_spec: Optional[Dict[str, Any]] = None,
        repo_v2: Optional[Dict[str, Any]] = None,
        verify_batch_approval_fn: Optional[Callable[[List[str]], bool]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        _ = history, blocked_commands, budget_remaining, extra
        if verify_batch_approval_fn:
            pass  # reserved for batch shell approval UX

        if skip_execution:
            return {
                "status": "incomplete",
                "checks": [],
                "steps_executed_count": 0,
                "skip_reason": skip_reason or "skip_execution",
            }

        all_checks = self.get_applicable_checks(
            task_type=task_type or None,
            scope=scope or None,
            files_changed=files_changed,
        )
        if not all_checks:
            return {
                "status": "skipped",
                "reason": "No supported stack detected for auto-verification.",
                "checks": [],
                "steps_executed_count": 0,
            }

        if contract_spec is not None:
            plan = choose_verification_plan_from_contract_spec(
                contract_spec,
                task_type=task_type,
                scope=scope,
                files_changed=files_changed,
                budget_remaining=budget_remaining,
                failed_check_names=failed_check_names,
                repo_v2=repo_v2,
            )
        else:
            plan = choose_verification_plan(
                task_type=task_type,
                scope=scope,
                files_changed=files_changed,
                budget_remaining=budget_remaining,
                failed_check_names=failed_check_names,
                repo_v2=repo_v2,
            )
        plan = filter_plan_to_available_checks(plan, all_checks)
        plan = adjust_verification_plan_for_ux_level(
            plan, all_checks, failed_check_names=failed_check_names
        )

        by_name = _checks_by_name(all_checks)
        if plan.check_names:
            ordered = [by_name[n] for n in plan.check_names if n in by_name]
        else:
            ordered = list(all_checks)

        results: List[Dict[str, Any]] = []
        overall_success = True
        steps_executed = 0

        for check in ordered:
            name = check["name"]
            cmd = check["command"]
            try:
                res = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, cwd=self.cwd, timeout=300
                )
                success = res.returncode == 0
                if not success:
                    overall_success = False
                steps_executed += 1
                results.append(
                    {
                        "name": name,
                        "command": cmd,
                        "status": "passed" if success else CHECK_FAILED,
                        "provenance": "executed",
                        "stdout": res.stdout,
                        "stderr": res.stderr,
                        "exit_code": res.returncode,
                    }
                )
            except Exception as e:
                overall_success = False
                steps_executed += 1
                results.append(
                    {
                        "name": name,
                        "command": cmd,
                        "status": CHECK_ERROR,
                        "provenance": "executed",
                        "error": str(e),
                    }
                )

        return {
            "status": "success" if overall_success else "failed",
            "checks": results,
            "steps_executed_count": steps_executed,
        }
