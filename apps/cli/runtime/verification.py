import json
import locale
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from collections.abc import Mapping

logger = logging.getLogger(__name__)


def _verification_stream_cap() -> int:
    try:
        v = int(os.environ.get("GHOST_VERIFY_STREAM_CAP", "320000"))
    except (TypeError, ValueError):
        v = 320_000
    return max(50_000, min(900_000, v))


def _shrink_verification_stream(text: Any, cap: int) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= cap:
        return s
    reserve = 80
    each = max(1, (cap - reserve) // 2)
    lost = len(s) - 2 * each
    return s[:each] + f"\n...[ghost_verify_stream_truncated n={lost}]...\n" + s[-each:]


def _shrink_verification_result_rows(rows: List[Dict[str, Any]]) -> None:
    cap = _verification_stream_cap()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if "stdout" in row:
            row["stdout"] = _shrink_verification_stream(row.get("stdout"), cap)
        if "stderr" in row:
            row["stderr"] = _shrink_verification_stream(row.get("stderr"), cap)


def _read_json_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _decode_subprocess_output(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if not isinstance(data, (bytes, bytearray)):
        return str(data)
    blob = bytes(data)
    if not blob:
        return ""
    tried: List[str] = []
    for encoding in ("utf-8", locale.getpreferredencoding(False), "cp1252"):
        enc = str(encoding or "").strip()
        if not enc:
            continue
        key = enc.lower()
        if key in tried:
            continue
        tried.append(key)
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
        except LookupError:
            continue
    return blob.decode("utf-8", errors="replace")


def _parse_semver_major(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _resolve_next_major_version(abs_dir: str, pkg: Dict[str, Any]) -> Optional[int]:
    installed_pkg = os.path.join(abs_dir, "node_modules", "next", "package.json")
    installed = _read_json_file(installed_pkg)
    major = _parse_semver_major(installed.get("version"))
    if major is not None:
        return major
    for section in ("dependencies", "devDependencies"):
        deps = pkg.get(section) or {}
        if not isinstance(deps, dict):
            continue
        major = _parse_semver_major(deps.get("next"))
        if major is not None:
            return major
    return None


def _should_skip_node_lint_check(pkg: Dict[str, Any], abs_dir: str) -> bool:
    scripts = pkg.get("scripts") or {}
    if not isinstance(scripts, dict):
        return False
    lint_script = str(scripts.get("lint") or "").strip().lower()
    if not lint_script.startswith("next lint"):
        return False
    next_major = _resolve_next_major_version(abs_dir, pkg)
    if next_major is not None:
        return next_major >= 16
    for section in ("dependencies", "devDependencies"):
        deps = pkg.get(section) or {}
        if not isinstance(deps, dict):
            continue
        raw = str(deps.get("next") or "").strip().lower()
        if raw in {"latest", "canary", "next"}:
            return True
    return False


# Check statuses used by VerificationCoordinator.attach_coordinator_view
CHECK_BLOCKED = "blocked"
CHECK_DENIED = "denied"
CHECK_ERROR = "error"
CHECK_FAILED = "failed"

PROVENANCE_EXECUTED = "executed"
PROVENANCE_DENIED_BY_USER = "denied_by_user"
PROVENANCE_PLANNED_UNAVAILABLE = "planned_unavailable"

_CHECK_KIND_TYPECHECK = "typecheck"
_CHECK_KIND_BUILD = "build"
_CHECK_KIND_LINT = "lint"
_CHECK_KIND_TESTS = "tests"

_PYTHON_TARGET_PREFIXES = (
    "apps/cli/",
    "apps/server/",
    "packages/py-core/",
)
_NODE_TARGET_PREFIXES = (
    "apps/web/",
    "web/",
)
_PYTHON_TEST_SEARCH_ROOTS = ("tests", "apps", "packages")
_IGNORED_TEST_DISCOVERY_DIRS = {
    ".git",
    ".ghost",
    ".mypy_cache",
    ".next",
    ".pnpm-store",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "out",
    "venv",
}
_GENERIC_PYTHON_BASENAMES = {
    "__init__",
    "base",
    "config",
    "constants",
    "helpers",
    "main",
    "models",
    "types",
    "utils",
}

_EXPLICIT_PYTEST_START_RE = re.compile(r"(?i)\b((?:uv run )?python -m pytest)\b")

# Pytest output → focused re-run targets (paths or path::nodeid)
_PYTEST_FAILED_LINE_RE = re.compile(r"(?m)^FAILED\s+(\S+)")
_PYTEST_ERROR_COLLECT_RE = re.compile(r"(?mi)^ERROR\s+collecting\s+(\S+)")
_PYTEST_ERRORS_NODE_RE = re.compile(r"(?m)^ERROR\s+(\S+::\S+)")


def _extract_explicit_pytest_command_from_text(text: str) -> Optional[str]:
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _EXPLICIT_PYTEST_START_RE.search(raw)
    if not match:
        return None
    base = match.group(1)
    tail = raw[match.end() :].strip()
    if not tail:
        return base
    collected: List[str] = []
    for piece in re.split(r"\s+", tail):
        token = piece.strip().strip(",;:()[]{}")
        if not token:
            continue
        lower = token.lower()
        if lower.startswith("-qsi"):
            collected.append("-q")
            break
        if lower in ("-q", "-x", "-s", "-v", "-vv", "-xvs", "-xv", "-qs"):
            collected.append(token)
            continue
        if token.endswith(".py") or ".py::" in token:
            collected.append(token)
            continue
        if lower.startswith("tests/") or lower.startswith("apps/") or lower.startswith("packages/"):
            collected.append(token)
            continue
        break
    return f"{base} {' '.join(collected)}".strip()


def parse_pytest_focus_targets(stdout: str = "", stderr: str = "", *, limit: int = 12) -> List[str]:
    """
    Extract pytest node ids or file paths from combined output for failed-first re-runs.
    """
    blob = f"{stdout or ''}\n{stderr or ''}"
    seen: List[str] = []
    for rx in (_PYTEST_FAILED_LINE_RE, _PYTEST_ERRORS_NODE_RE, _PYTEST_ERROR_COLLECT_RE):
        for m in rx.finditer(blob):
            t = str(m.group(1)).strip()
            if not t or t.startswith("="):
                continue
            if t not in seen:
                seen.append(t)
            if len(seen) >= limit:
                return seen
    return seen


def _explicit_pytest_command_from_contract_spec(spec: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(spec, dict):
        return None
    for item in spec.get("acceptance_criteria") or []:
        cmd = _extract_explicit_pytest_command_from_text(str(item or ""))
        if cmd:
            return cmd
    return None


@dataclass(frozen=True)
class ChosenVerificationPlan:
    """Ordered canonical check labels to run (before intersection with repo-applicable checks)."""

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
    vp = spec.get("verification_policy") if isinstance(spec, Mapping) else None
    if not isinstance(vp, Mapping):
        return ChosenVerificationPlan(check_names=())
    names: List[str] = []
    if bool(vp.get("typecheck")):
        names.append("TypeCheck")
    if bool(vp.get("build")):
        names.append("Build")
    if bool(vp.get("lint")):
        names.append("Lint")
    if bool(vp.get("tests")) or bool(vp.get("test")):
        names.append("Tests")
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
    return ChosenVerificationPlan(check_names=("TypeCheck", "Build", "Lint", "Tests"))


def _canonical_check_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in ("typecheck", "type check"):
        return _CHECK_KIND_TYPECHECK
    if text == "build":
        return _CHECK_KIND_BUILD
    if text == "lint":
        return _CHECK_KIND_LINT
    if text in ("test", "tests") or "test" in text:
        return _CHECK_KIND_TESTS
    return text


def filter_plan_to_available_checks(
    plan: ChosenVerificationPlan,
    all_checks: Sequence[Dict[str, Any]],
) -> ChosenVerificationPlan:
    available = {
        _canonical_check_key(c.get("kind") or c.get("name"))
        for c in all_checks
        if c.get("name")
    }
    ordered = tuple(n for n in plan.check_names if _canonical_check_key(n) in available)
    return ChosenVerificationPlan(check_names=ordered)


def _missing_required_check_names(
    plan: ChosenVerificationPlan,
    all_checks: Sequence[Dict[str, Any]],
) -> List[str]:
    available = {
        _canonical_check_key(c.get("kind") or c.get("name"))
        for c in all_checks
        if c.get("name")
    }
    missing: List[str] = []
    for name in plan.check_names:
        key = _canonical_check_key(name)
        if not key or key in available:
            continue
        display = _normalize_repo_vc_key(str(name))
        if display not in missing:
            missing.append(display)
    return missing


def adjust_verification_plan_for_ux_level(
    plan: ChosenVerificationPlan,
    all_checks: Sequence[Dict[str, Any]],
    *,
    failed_check_names: Optional[List[str]] = None,
) -> ChosenVerificationPlan:
    if failed_check_names:
        available = {
            _canonical_check_key(c.get("kind") or c.get("name"))
            for c in all_checks
            if c.get("name")
        }
        failed_first = tuple(
            dict.fromkeys(
                key
                for key in (_canonical_check_key(name) for name in failed_check_names)
                if key and key in available
            )
        )
        if failed_first:
            return ChosenVerificationPlan(check_names=failed_first)
    return plan


def _checks_by_name(all_checks: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for check in all_checks:
        name = str(check.get("name") or "")
        if not name:
            continue
        out[name] = dict(check)
        out[_canonical_check_key(check.get("kind") or name)] = dict(check)
    return out


def _normalize_repo_vc_key(key: str) -> str:
    """Map RepoProfile / taskspec keys (typecheck/tests) to check labels."""
    k = str(key).strip().lower()
    return {
        "typecheck": "TypeCheck",
        "build": "Build",
        "lint": "Lint",
        "test": "Tests",
        "tests": "Tests",
    }.get(k, key)


def _normalize_rel_path(path: str) -> str:
    rel = str(path or "").replace("\\", "/").strip().strip("./")
    while "//" in rel:
        rel = rel.replace("//", "/")
    return rel


def _files_changed_paths(files_changed: Optional[List[Dict[str, Any]]]) -> List[str]:
    out: List[str] = []
    for row in files_changed or []:
        if not isinstance(row, dict):
            continue
        path = _normalize_rel_path(str(row.get("file") or row.get("path") or ""))
        if path:
            out.append(path)
    return out


def _infer_verification_targets(
    *,
    files_changed: Optional[List[Dict[str, Any]]],
    scope: Optional[str],
    cwd: str,
) -> List[str]:
    rel_paths = _files_changed_paths(files_changed)
    families: List[str] = []

    def add_family(name: str) -> None:
        if name not in families:
            families.append(name)

    for path in rel_paths:
        low = path.lower()
        if any(low.startswith(prefix) for prefix in _NODE_TARGET_PREFIXES):
            add_family("node")
            continue
        if any(low.startswith(prefix) for prefix in _PYTHON_TARGET_PREFIXES):
            add_family("python")
            continue
        if low.endswith(".py") or low in ("pyproject.toml", "requirements.txt", "setup.py"):
            add_family("python")
            continue
        if low.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
            add_family("node")
            continue

    scope_low = str(scope or "").strip().lower()
    if not families:
        if scope_low == "ui":
            add_family("node")
        elif scope_low in ("api", "data", "infra", "config"):
            add_family("python")

    if not families:
        if os.path.exists(os.path.join(cwd, "pyproject.toml")) or os.path.exists(
            os.path.join(cwd, "requirements.txt")
        ):
            add_family("python")
        if os.path.exists(os.path.join(cwd, "package.json")):
            add_family("node")
        elif os.path.exists(os.path.join(cwd, "apps", "web", "package.json")):
            add_family("node")

    return families


def _candidate_node_project_dirs(
    cwd: str,
    files_changed: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    candidates: List[str] = []

    def add_candidate(rel_dir: str) -> None:
        rel = _normalize_rel_path(rel_dir)
        rel = "." if rel in ("", ".") else rel
        if rel not in candidates:
            candidates.append(rel)

    if os.path.exists(os.path.join(cwd, "package.json")):
        add_candidate(".")
    if os.path.exists(os.path.join(cwd, "apps", "web", "package.json")):
        add_candidate("apps/web")

    for path in _files_changed_paths(files_changed):
        current = os.path.dirname(path)
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            if os.path.exists(os.path.join(cwd, current, "package.json")):
                add_candidate(current)
                break
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

    if not candidates:
        nested_dirs: List[str] = []
        try:
            for entry in os.scandir(cwd):
                if not entry.is_dir():
                    continue
                name = str(entry.name or "").strip()
                if not name or name.startswith("."):
                    continue
                if os.path.exists(os.path.join(entry.path, "package.json")):
                    nested_dirs.append(name.replace("\\", "/"))
        except OSError:
            nested_dirs = []
        if len(nested_dirs) == 1:
            add_candidate(nested_dirs[0])

    return candidates


def _scope_label_from_families(families: Sequence[str]) -> str:
    uniq = list(dict.fromkeys(str(x) for x in families if str(x).strip()))
    if not uniq:
        return "unknown"
    if len(uniq) == 1:
        return uniq[0]
    return "combined"


def _command_spec_from_value(value: Any) -> Optional[Dict[str, Any]]:
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json")
        except Exception:
            value = None
    if isinstance(value, str) and value.strip():
        return {"command": value.strip(), "cwd": ".", "source": ["legacy_string"]}
    if isinstance(value, dict):
        cmd = value.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return None
        src = value.get("source")
        if isinstance(src, str):
            src = [src]
        elif not isinstance(src, list):
            src = []
        return {
            "command": cmd.strip(),
            "cwd": str(value.get("cwd") or ".").replace("\\", "/") or ".",
            "source": [str(x) for x in src if str(x).strip()],
        }
    return None


def _absolute_check_cwd(repo_root: str, check_cwd: str) -> str:
    if os.path.isabs(check_cwd):
        return check_cwd
    if check_cwd in ("", "."):
        return repo_root
    return os.path.join(repo_root, check_cwd)


def _has_command(name: str) -> bool:
    return subprocess.run(f"{name} --version", shell=True, capture_output=True).returncode == 0


def _python_module_probes_for_path(path: str) -> List[str]:
    rel = _normalize_rel_path(path)
    if not rel.lower().endswith(".py"):
        return []
    without_ext = rel[:-3]
    probes: List[str] = []

    def add_probe(text: str) -> None:
        probe = str(text or "").strip().replace("\\", "/")
        if not probe or probe in probes:
            return
        probes.append(probe)

    add_probe(without_ext.replace("/", "."))
    if without_ext.endswith("/__init__"):
        add_probe(without_ext[: -len("/__init__")].replace("/", "."))
    py_core_prefix = "packages/py-core/"
    if without_ext.startswith(py_core_prefix):
        add_probe(without_ext[len(py_core_prefix) :].replace("/", "."))
    return probes


def _iter_python_test_files_under(cwd: str, root_name: str) -> List[str]:
    rel_paths: List[str] = []
    root_path = os.path.join(cwd, root_name)
    if not os.path.isdir(root_path):
        return rel_paths
    for current_root, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _IGNORED_TEST_DISCOVERY_DIRS and not d.startswith(".")
        ]
        for filename in filenames:
            if not filename.startswith("test_") or not filename.endswith(".py"):
                continue
            abs_path = os.path.join(current_root, filename)
            rel_paths.append(_normalize_rel_path(os.path.relpath(abs_path, cwd)))
    rel_paths.sort()
    return rel_paths


def _shell_quote_arg(value: str) -> str:
    return '"' + str(value or "").replace('"', '\\"') + '"'


def _module_reference_markers(probe: str) -> List[str]:
    text = str(probe or "").strip()
    if not text:
        return []
    return [
        f"from {text} import ",
        f"import {text}",
        f'patch("{text}',
        f"patch('{text}",
        f'"{text}.',
        f"'{text}.",
    ]


def _find_targeted_python_tests(
    cwd: str,
    files_changed: Optional[List[Dict[str, Any]]],
    *,
    max_matches: int = 6,
) -> List[str]:
    rel_paths = [
        path
        for path in _files_changed_paths(files_changed)
        if str(path).lower().endswith(".py")
    ]
    if not rel_paths or len(rel_paths) > 2:
        return []

    content_cache: Dict[str, str] = {}
    for root_name in _PYTHON_TEST_SEARCH_ROOTS:
        available_tests = _iter_python_test_files_under(cwd, root_name)
        if not available_tests:
            continue
        scores: Dict[str, int] = {}
        for changed_path in rel_paths:
            normalized_changed = _normalize_rel_path(changed_path)
            basename = os.path.basename(normalized_changed).lower()
            stem = os.path.splitext(basename)[0]
            module_probes = _python_module_probes_for_path(normalized_changed)

            if basename.startswith("test_") and normalized_changed in available_tests:
                scores[normalized_changed] = max(scores.get(normalized_changed, 0), 100)

            for test_rel_path in available_tests:
                score = 0
                test_basename = os.path.basename(test_rel_path).lower()
                if stem and stem not in _GENERIC_PYTHON_BASENAMES and test_basename == f"test_{stem}.py":
                    score += 12
                if module_probes:
                    text = content_cache.get(test_rel_path)
                    if text is None:
                        abs_test_path = os.path.join(cwd, test_rel_path)
                        try:
                            with open(abs_test_path, "r", encoding="utf-8", errors="ignore") as handle:
                                text = handle.read(262144)
                        except OSError:
                            text = ""
                        content_cache[test_rel_path] = text
                    if any(
                        marker in text
                        for probe in module_probes
                        for marker in _module_reference_markers(probe)
                    ):
                        score += 30
                if score > 0:
                    scores[test_rel_path] = max(scores.get(test_rel_path, 0), score)

        if scores:
            ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            return [path for path, _score in ordered[:max_matches]]
    return []


def _build_python_test_check(
    cwd: str,
    *,
    files_changed: Optional[List[Dict[str, Any]]] = None,
    contract_spec: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    has_test_tree = os.path.exists(os.path.join(cwd, "tests")) or os.path.exists(os.path.join(cwd, "test"))
    if not has_test_tree:
        return None
    env = {"PYTHONPATH": cwd}
    explicit_cmd = _explicit_pytest_command_from_contract_spec(contract_spec)
    if explicit_cmd:
        return {
            "name": "Python Tests (explicit, targeted)",
            "kind": _CHECK_KIND_TESTS,
            "command": explicit_cmd,
            "pytest_base_command": explicit_cmd,
            "cwd": ".",
            "source": ["python_default", "contract_spec_explicit"],
            "env": env,
        }
    targeted_tests = _find_targeted_python_tests(cwd, files_changed)
    pytest_suffix = ""
    label_suffix = ""
    source = ["python_default"]
    if targeted_tests:
        pytest_suffix = " " + " ".join(_shell_quote_arg(path) for path in targeted_tests)
        label_suffix = ", targeted"
        source = ["python_default", "python_targeted"]
    if _has_command("uv"):
        base = "uv run python -m pytest"
        return {
            "name": f"Python Tests (uv{label_suffix})",
            "kind": _CHECK_KIND_TESTS,
            "command": f"{base}{pytest_suffix}",
            "pytest_base_command": base,
            "cwd": ".",
            "source": source,
            "env": env,
        }
    if _has_command("pytest"):
        base = "python -m pytest"
        return {
            "name": f"Python Tests (pytest{label_suffix})",
            "kind": _CHECK_KIND_TESTS,
            "command": f"{base}{pytest_suffix}",
            "pytest_base_command": base,
            "cwd": ".",
            "source": source,
            "env": env,
        }
    return {
        "name": "Python Tests (unittest)",
        "kind": _CHECK_KIND_TESTS,
        "command": "python -m unittest discover",
        "cwd": ".",
        "source": ["python_default"],
        "env": env,
    }


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

    def _get_node_checks_from_package_dir(self, check_cwd: str = ".") -> List[Dict[str, Any]]:
        checks: List[Dict[str, Any]] = []
        abs_dir = _absolute_check_cwd(self.cwd, check_cwd)
        pkg_path = os.path.join(abs_dir, "package.json")
        if not os.path.exists(pkg_path):
            return checks
        try:
            pkg = _read_json_file(pkg_path)
            scripts = pkg.get("scripts", {}) or {}
            if os.path.exists(os.path.join(abs_dir, "tsconfig.json")):
                checks.append(
                    {
                        "name": "TypeCheck",
                        "kind": _CHECK_KIND_TYPECHECK,
                        "command": "npx tsc --noEmit",
                        "cwd": check_cwd,
                        "source": ["root_package_json"],
                    }
                )
            if "build" in scripts:
                checks.append(
                    {
                        "name": "Build",
                        "kind": _CHECK_KIND_BUILD,
                        "command": "npm run build",
                        "cwd": check_cwd,
                        "source": ["root_package_json"],
                    }
                )
            elif not checks and os.path.exists(os.path.join(abs_dir, "tsconfig.json")):
                checks.append(
                    {
                        "name": "TypeCheck",
                        "kind": _CHECK_KIND_TYPECHECK,
                        "command": "npx tsc --noEmit",
                        "cwd": check_cwd,
                        "source": ["root_package_json"],
                    }
                )
            if "lint" in scripts and not _should_skip_node_lint_check(pkg, abs_dir):
                checks.append(
                    {
                        "name": "Lint",
                        "kind": _CHECK_KIND_LINT,
                        "command": "npm run lint",
                        "cwd": check_cwd,
                        "source": ["root_package_json"],
                    }
                )
            if "test" in scripts:
                checks.append(
                    {
                        "name": "Tests",
                        "kind": _CHECK_KIND_TESTS,
                        "command": "npm run test",
                        "cwd": check_cwd,
                        "source": ["root_package_json"],
                    }
                )
        except Exception:
            return []
        return checks

    def _get_node_checks_from_repo_profile(self) -> List[Dict[str, Any]]:
        checks: List[Dict[str, Any]] = []
        for key, value in (self._repo_vc or {}).items():
            if not isinstance(key, str) or key == "source":
                continue
            spec = _command_spec_from_value(value)
            if not spec:
                continue
            kind = _canonical_check_key(key)
            if kind not in (
                _CHECK_KIND_TYPECHECK,
                _CHECK_KIND_BUILD,
                _CHECK_KIND_LINT,
                _CHECK_KIND_TESTS,
            ):
                continue
            checks.append(
                {
                    "name": _normalize_repo_vc_key(key),
                    "kind": kind,
                    "command": str(spec["command"]),
                    "cwd": str(spec.get("cwd") or "."),
                    "source": list(spec.get("source") or []),
                }
            )
        return checks

    def get_applicable_checks(
        self,
        task_type: Optional[str] = None,
        scope: Optional[str] = None,
        files_changed: Optional[List[Dict[str, Any]]] = None,
        contract_spec: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        _ = task_type
        checks: List[Dict[str, Any]] = []
        families = _infer_verification_targets(files_changed=files_changed, scope=scope, cwd=self.cwd)

        if "python" in families:
            py_check = _build_python_test_check(
                self.cwd,
                files_changed=files_changed,
                contract_spec=contract_spec,
            )
            if py_check:
                checks.append(py_check)

        if "node" in families:
            node_checks = self._get_node_checks_from_repo_profile()
            if not node_checks:
                for candidate_cwd in _candidate_node_project_dirs(self.cwd, files_changed):
                    node_checks = self._get_node_checks_from_package_dir(candidate_cwd)
                    if node_checks:
                        break
            checks.extend(node_checks)

        if not checks:
            py_check = _build_python_test_check(
                self.cwd,
                files_changed=files_changed,
                contract_spec=contract_spec,
            )
            if py_check:
                checks.append(py_check)
            fallback_node_checks = self._get_node_checks_from_repo_profile()
            if not fallback_node_checks:
                for candidate_cwd in _candidate_node_project_dirs(self.cwd, files_changed):
                    fallback_node_checks = self._get_node_checks_from_package_dir(candidate_cwd)
                    if fallback_node_checks:
                        break
            checks.extend(fallback_node_checks)

        by_kind: Dict[str, Dict[str, Any]] = {}
        ordered: List[Dict[str, Any]] = []
        for check in checks:
            kind = _canonical_check_key(check.get("kind") or check.get("name"))
            if not kind or kind in by_kind:
                continue
            item = dict(check)
            item["kind"] = kind
            item["cwd"] = str(item.get("cwd") or ".").replace("\\", "/") or "."
            ordered.append(item)
            by_kind[kind] = item
        return ordered

    def resolve_ordered_checks(
        self,
        *,
        task_type: str = "",
        scope: str = "",
        files_changed: Optional[List[Dict[str, Any]]] = None,
        budget_remaining: Optional[int] = None,
        failed_check_names: Optional[List[str]] = None,
        contract_spec: Optional[Dict[str, Any]] = None,
        repo_v2: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """
        Same ordered check list as verify_change will execute (single source of truth for UI labels).
        """
        families = _infer_verification_targets(files_changed=files_changed, scope=scope, cwd=self.cwd)
        verification_scope = _scope_label_from_families(families)

        all_checks = self.get_applicable_checks(
            task_type=task_type or None,
            scope=scope or None,
            files_changed=files_changed,
            contract_spec=contract_spec,
        )
        if not all_checks:
            return [], verification_scope

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
            ordered = [
                by_name[_canonical_check_key(name)]
                for name in plan.check_names
                if _canonical_check_key(name) in by_name
            ]
        else:
            ordered = list(all_checks)
        return ordered, verification_scope

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
        pytest_focus_paths: Optional[List[str]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        _ = history, blocked_commands, budget_remaining, extra

        families = _infer_verification_targets(files_changed=files_changed, scope=scope, cwd=self.cwd)
        verification_scope = _scope_label_from_families(families)

        all_checks = self.get_applicable_checks(
            task_type=task_type or None,
            scope=scope or None,
            files_changed=files_changed,
            contract_spec=contract_spec,
        )
        missing_required_checks: List[str] = []
        if contract_spec is not None:
            required_plan = choose_verification_plan_from_contract_spec(
                contract_spec,
                task_type=task_type,
                scope=scope,
                files_changed=files_changed,
                budget_remaining=budget_remaining,
                failed_check_names=failed_check_names,
                repo_v2=repo_v2,
            )
            missing_required_checks = _missing_required_check_names(required_plan, all_checks)

        if skip_execution:
            return {
                "status": "incomplete",
                "checks": [],
                "steps_executed_count": 0,
                "skip_reason": skip_reason or "skip_execution",
                "verification_scope": verification_scope,
                "planned_check_cwds": {},
            }

        ordered, verification_scope = self.resolve_ordered_checks(
            task_type=task_type,
            scope=scope,
            files_changed=files_changed,
            budget_remaining=budget_remaining,
            failed_check_names=failed_check_names,
            contract_spec=contract_spec,
            repo_v2=repo_v2,
        )
        if not ordered and not missing_required_checks:
            return {
                "status": "skipped",
                "reason": "No supported stack detected for auto-verification.",
                "checks": [],
                "steps_executed_count": 0,
                "verification_scope": verification_scope,
                "planned_check_cwds": {},
            }

        planned_check_cwds = {
            str(check["name"]): str(check.get("cwd") or ".") for check in ordered if check.get("name")
        }
        default_check_cwd = next((str(check.get("cwd") or ".") for check in ordered if check.get("name")), ".")
        for missing_name in missing_required_checks:
            planned_check_cwds.setdefault(missing_name, default_check_cwd)

        if verify_batch_approval_fn and ordered:
            approval_cmds = [
                f"{check['command']} @ {check.get('cwd') or '.'}" for check in ordered if check.get("command")
            ]
            if approval_cmds and not verify_batch_approval_fn(approval_cmds):
                denied_checks = [
                    {
                        "name": check["name"],
                        "kind": check.get("kind"),
                        "command": check["command"],
                        "cwd": check.get("cwd") or ".",
                        "status": CHECK_DENIED,
                        "provenance": PROVENANCE_DENIED_BY_USER,
                        "cause": "approval_denied",
                    }
                    for check in ordered
                ]
                return {
                    "status": CHECK_BLOCKED,
                    "checks": denied_checks,
                    "steps_executed_count": 0,
                    "verification_scope": verification_scope,
                    "planned_check_cwds": planned_check_cwds,
                    "justification": "verification blocked: shell verify batch denied by user",
                }

        results: List[Dict[str, Any]] = []
        overall_success = True
        steps_executed = 0

        focus_list = [str(x).strip() for x in (pytest_focus_paths or []) if str(x).strip()]

        def _run_one_subprocess(
            run_cmd: str,
            *,
            abs_cwd_local: str,
            env_local: Dict[str, str],
        ) -> subprocess.CompletedProcess[str]:
            proc = subprocess.run(
                run_cmd,
                shell=True,
                capture_output=True,
                text=False,
                cwd=abs_cwd_local,
                timeout=300,
                env=env_local,
            )
            proc.stdout = _decode_subprocess_output(proc.stdout)
            proc.stderr = _decode_subprocess_output(proc.stderr)
            return proc

        for check in ordered:
            name = str(check["name"])
            cmd = str(check["command"])
            check_cwd = str(check.get("cwd") or ".")
            abs_cwd = _absolute_check_cwd(self.cwd, check_cwd)
            env = os.environ.copy()
            extra_env = check.get("env") or {}
            if isinstance(extra_env, dict):
                for key, value in extra_env.items():
                    if value is not None:
                        env[str(key)] = str(value)
            try:
                kind = _canonical_check_key(check.get("kind") or check.get("name"))
                base_py = str(check.get("pytest_base_command") or "").strip()
                pytest_narrow_escalate = (
                    kind == _CHECK_KIND_TESTS
                    and focus_list
                    and base_py
                    and "pytest" in base_py.lower()
                    and "unittest" not in cmd.lower()
                )
                combined_stdout = ""
                combined_stderr = ""
                last_code = 0
                executed_cmd = cmd
                if pytest_narrow_escalate:
                    narrow_cmd = base_py + " " + " ".join(_shell_quote_arg(p) for p in focus_list)
                    res_n = _run_one_subprocess(narrow_cmd, abs_cwd_local=abs_cwd, env_local=env)
                    combined_stdout += f"--- pytest focused ({len(focus_list)} target(s)) ---\n{res_n.stdout or ''}"
                    combined_stderr += f"--- pytest focused ---\n{res_n.stderr or ''}"
                    last_code = res_n.returncode
                    executed_cmd = narrow_cmd
                    if res_n.returncode != 0:
                        success = False
                        overall_success = False
                        steps_executed += 1
                        results.append(
                            {
                                "name": name,
                                "kind": check.get("kind"),
                                "command": executed_cmd,
                                "cwd": check_cwd,
                                "status": CHECK_FAILED,
                                "provenance": PROVENANCE_EXECUTED,
                                "stdout": combined_stdout,
                                "stderr": combined_stderr,
                                "exit_code": last_code,
                                "cause": "command_failed",
                                "pytest_phase": "narrow_failed",
                            }
                        )
                        continue
                    full_cmd = base_py
                    if full_cmd.strip() != narrow_cmd.strip():
                        res_f = _run_one_subprocess(full_cmd, abs_cwd_local=abs_cwd, env_local=env)
                        combined_stdout += f"\n--- pytest full suite ---\n{res_f.stdout or ''}"
                        combined_stderr += f"\n--- pytest full suite ---\n{res_f.stderr or ''}"
                        last_code = res_f.returncode
                        executed_cmd = f"{narrow_cmd} [then full: {full_cmd}]"
                        success = res_f.returncode == 0
                        if not success:
                            overall_success = False
                        steps_executed += 1
                        results.append(
                            {
                                "name": name,
                                "kind": check.get("kind"),
                                "command": executed_cmd,
                                "cwd": check_cwd,
                                "status": "passed" if success else CHECK_FAILED,
                                "provenance": PROVENANCE_EXECUTED,
                                "stdout": combined_stdout,
                                "stderr": combined_stderr,
                                "exit_code": last_code,
                                "cause": "" if success else "command_failed",
                                "pytest_phase": "narrow_ok_full" if success else "narrow_ok_full_failed",
                            }
                        )
                    else:
                        success = True
                        steps_executed += 1
                        results.append(
                            {
                                "name": name,
                                "kind": check.get("kind"),
                                "command": executed_cmd,
                                "cwd": check_cwd,
                                "status": "passed",
                                "provenance": PROVENANCE_EXECUTED,
                                "stdout": combined_stdout,
                                "stderr": combined_stderr,
                                "exit_code": last_code,
                                "cause": "",
                                "pytest_phase": "narrow_only",
                            }
                        )
                    continue

                res = _run_one_subprocess(cmd, abs_cwd_local=abs_cwd, env_local=env)
                success = res.returncode == 0
                if not success:
                    overall_success = False
                steps_executed += 1
                results.append(
                    {
                        "name": name,
                        "kind": check.get("kind"),
                        "command": cmd,
                        "cwd": check_cwd,
                        "status": "passed" if success else CHECK_FAILED,
                        "provenance": PROVENANCE_EXECUTED,
                        "stdout": res.stdout,
                        "stderr": res.stderr,
                        "exit_code": res.returncode,
                        "cause": "" if success else "command_failed",
                    }
                )
            except Exception as exc:
                overall_success = False
                steps_executed += 1
                logger.warning(
                    "verify check internal error name=%s cwd=%s: %s",
                    name,
                    check_cwd,
                    exc,
                    exc_info=True,
                )
                results.append(
                    {
                        "name": name,
                        "kind": check.get("kind"),
                        "command": cmd,
                        "cwd": check_cwd,
                        "status": CHECK_ERROR,
                        "provenance": PROVENANCE_EXECUTED,
                        "error": str(exc),
                        "cause": type(exc).__name__,
                    }
                )

        if missing_required_checks:
            overall_success = False
            for missing_name in missing_required_checks:
                results.append(
                    {
                        "name": missing_name,
                        "kind": _canonical_check_key(missing_name),
                        "command": "",
                        "cwd": planned_check_cwds.get(missing_name) or default_check_cwd,
                        "status": CHECK_BLOCKED,
                        "provenance": PROVENANCE_PLANNED_UNAVAILABLE,
                        "cause": "required_check_unavailable",
                        "error": "Required verification check is unavailable for this project.",
                    }
                )

        check_summary = ", ".join(
            f"{row['name']}@{row.get('cwd') or '.'}" for row in results[:6]
        )
        _shrink_verification_result_rows(results)
        final_status = "success" if overall_success else "failed"
        if missing_required_checks and not any(
            row.get("status") in (CHECK_FAILED, CHECK_ERROR) for row in results
        ):
            final_status = CHECK_BLOCKED
        justification = f"verification_scope={verification_scope}; checks={check_summary}"
        if missing_required_checks:
            justification += f"; missing_required={', '.join(missing_required_checks)}"
        return {
            "status": final_status,
            "checks": results,
            "steps_executed_count": steps_executed,
            "verification_scope": verification_scope,
            "planned_check_cwds": planned_check_cwds,
            "justification": justification,
        }
