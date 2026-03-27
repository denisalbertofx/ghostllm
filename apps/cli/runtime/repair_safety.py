"""
Structural repair gates: parse/import sanity for Python files after proposed patches.
Used by REPAIR phase to avoid declaring progress when syntax/indentation worsens or stays broken.
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
from typing import Any, Dict, List, Optional, Tuple

# Failures that warrant ast.parse gate before accepting repair as "applied"
_STRUCTURAL_PY_RE = re.compile(
    r"\b(IndentationError|SyntaxError|TabError|ImportError|ModuleNotFoundError|AttributeError)\b",
    re.IGNORECASE,
)


def failed_checks_indicate_structural_python_failure(failed_checks: List[Dict[str, Any]]) -> bool:
    """True when verification output suggests parse/import/indent/syntax issues."""
    for c in failed_checks or []:
        if not isinstance(c, dict):
            continue
        blob = "\n".join(
            str(x or "")
            for x in (
                c.get("stderr"),
                c.get("stdout"),
                c.get("error"),
                c.get("output_summary"),
            )
        )
        if _STRUCTURAL_PY_RE.search(blob):
            return True
    return False


def parse_python_file_errors(repo_root: str, rel_path: str) -> Optional[str]:
    """
    Return normalized error text if ast.parse fails, else None.
    rel_path is repo-relative with forward slashes.
    """
    rel = str(rel_path or "").replace("\\", "/").strip().lstrip("./")
    if not rel.lower().endswith(".py"):
        return None
    full = os.path.join(repo_root, rel.replace("/", os.sep))
    if not os.path.isfile(full):
        return "missing_file"
    try:
        with open(full, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            src = f.read()
    except OSError as e:
        return f"read_error:{e}"
    try:
        ast.parse(src, filename=rel)
    except SyntaxError as e:
        parts = [type(e).__name__, str(e.msg or ""), str(e.filename or rel), str(e.lineno or "")]
        return "|".join(parts)
    except Exception as e:
        return f"{type(e).__name__}:{e}"
    return None


def parse_error_fingerprint(err: Optional[str]) -> str:
    if not err:
        return ""
    return hashlib.sha256(err.encode("utf-8", errors="replace")).hexdigest()[:32]


def collect_parse_state(
    repo_root: str,
    rel_paths: List[str],
) -> Tuple[Dict[str, Optional[str]], Dict[str, str]]:
    """Map rel_path -> error string (None if ok) and -> fingerprint."""
    errs: Dict[str, Optional[str]] = {}
    fps: Dict[str, str] = {}
    seen: set[str] = set()
    for p in rel_paths:
        rel = str(p or "").replace("\\", "/").strip()
        if not rel or rel in seen:
            continue
        seen.add(rel)
        if not rel.lower().endswith(".py"):
            continue
        e = parse_python_file_errors(repo_root, rel)
        errs[rel] = e
        fps[rel] = parse_error_fingerprint(e)
    return errs, fps


def validate_edited_python_parse(
    repo_root: str,
    edited_rel_paths: List[str],
    *,
    pre_errors: Dict[str, Optional[str]],
) -> Tuple[bool, str, Dict[str, Optional[str]], bool]:
    """
    Returns (ok, reason, post_errors, same_signature_as_pre).
    ok True only if every edited .py parses. same_signature if any file still has identical parse fp as pre.
    """
    post_errors: Dict[str, Optional[str]] = {}
    same_sig = False
    for rel in edited_rel_paths:
        rl = str(rel or "").replace("\\", "/").strip()
        if not rl.lower().endswith(".py"):
            continue
        err = parse_python_file_errors(repo_root, rl)
        post_errors[rl] = err
        pre = pre_errors.get(rl)
        if err and pre and parse_error_fingerprint(err) == parse_error_fingerprint(pre):
            same_sig = True
    py_paths = [p for p in edited_rel_paths if str(p).lower().endswith(".py")]
    if not py_paths:
        return True, "", post_errors, False
    bad = [p for p in py_paths if post_errors.get(p)]
    if bad:
        return (
            False,
            f"ast_parse_failed:{','.join(bad[:4])}",
            post_errors,
            same_sig,
        )
    return True, "", post_errors, False
