"""
Conservative operational budget grants after REPAIR for non-Python failures (TS / lint / build).

Requires structured pre/post error sets; patch applied alone is never sufficient.
Probes are best-effort (timeout); missing probe or ambiguous comparison => no grant.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _cap_probe_parse_text(text: str) -> str:
    """Bound CPU on huge tool output when extracting structured keys."""
    try:
        cap = int(os.environ.get("GHOST_REPAIR_PROBE_PARSE_CAP", "120000"))
    except (TypeError, ValueError):
        cap = 120_000
    cap = max(30_000, min(400_000, cap))
    if len(text) <= cap:
        return text
    head = (cap * 2) // 3
    tail = cap - head - 72
    return text[:head] + f"\n...[ghost_probe_parse_truncated n={len(text)-head-tail}]...\n" + text[-tail:]


def _append_tool_unavailable_event(
    session: Any,
    *,
    probe: str,
    reason: str,
    impact: str,
) -> None:
    """One compact row per (probe,reason,write_epoch) to avoid spam in long sessions."""
    ev = getattr(session, "events", None)
    if not isinstance(ev, list):
        return
    ep = int(getattr(session, "write_epoch", 0) or 0)
    key = f"{probe}|{reason}|{ep}"
    if getattr(session, "_ghost_last_tool_unavail_key", "") == key:
        return
    try:
        session._ghost_last_tool_unavail_key = key
    except Exception:
        pass
    ev.append(
        {
            "event": "runtime_tool_unavailable",
            "probe": probe,
            "reason": reason,
            "impact": impact,
        }
    )


def _diagnose_missing_executable(name: str) -> bool:
    return shutil.which(name) is None


def _probe_cache_prune_if_needed(bucket: Dict[str, Any]) -> None:
    try:
        lim = int(os.environ.get("GHOST_REPAIR_PROBE_CACHE_MAX_KEYS", "24"))
    except (TypeError, ValueError):
        lim = 24
    lim = max(8, min(64, lim))
    while len(bucket) > lim:
        k = next(iter(bucket))
        del bucket[k]

from apps.cli.runtime.repair_specialist import (
    RepairSpecialistResult,
    _check_blob,
    infer_check_kind_bucket,
    repair_applied_patch_deserves_operational_budget,
)


def _strip_ansi(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _norm_rel(p: str) -> str:
    s = (p or "").replace("\\", "/").strip().strip("./")
    while s.startswith("./"):
        s = s[2:]
    while "//" in s:
        s = s.replace("//", "/")
    return s


def _edited_non_py_paths(res: RepairSpecialistResult) -> List[str]:
    out: List[str] = []
    for ed in res.edits_to_apply or []:
        rel = str(ed.get("path") or "").replace("\\", "/").strip()
        if not rel or rel.lower().endswith(".py"):
            continue
        out.append(_norm_rel(rel))
    return out


def infer_primary_non_py_domain(failed_checks: List[Dict[str, Any]]) -> str:
    """First failed check bucket among typecheck / lint / build."""
    for c in failed_checks or []:
        if not isinstance(c, dict) or c.get("status") not in ("failed", "error"):
            continue
        b = infer_check_kind_bucket(c)
        if b == "typecheck":
            return "typecheck"
        if b == "lint":
            return "lint"
        if b == "build":
            return "build"
    return ""


def _blobs_for_domain(failed_checks: List[Dict[str, Any]], domain: str) -> str:
    parts: List[str] = []
    for c in failed_checks or []:
        if not isinstance(c, dict) or c.get("status") not in ("failed", "error"):
            continue
        b = infer_check_kind_bucket(c)
        if domain == "typecheck" and b != "typecheck":
            continue
        if domain == "lint" and b != "lint":
            continue
        if domain == "build" and b != "build":
            continue
        parts.append(_check_blob(c))
    return "\n".join(parts)


def parse_typescript_error_signatures(text: str) -> frozenset[str]:
    """
    Stable keys: normpath|TScode|line (line anchors subset to edited files).
    """
    text = _cap_probe_parse_text(_strip_ansi(text or ""))
    if not text:
        return frozenset()
    keys: set[str] = set()
    for line in text.splitlines():
        m = re.search(
            r"([^\s(]+?\.(?:tsx?))\((\d+),\s*\d+\):\s*error\s*(TS\d{4,6})",
            line,
            re.I,
        )
        if m:
            keys.add(f"{_norm_rel(m.group(1)).lower()}|{m.group(3).upper()}|{m.group(2)}")
            continue
        m = re.search(r"\berror\s+(TS\d{4,6})\b.*?\(([^)]+?\.(?:tsx?))\)", line, re.I)
        if m:
            keys.add(f"{_norm_rel(m.group(2)).lower()}|{m.group(1).upper()}|0")
    return frozenset(keys)


def parse_ruff_error_signatures(text: str) -> frozenset[str]:
    text = _strip_ansi(text or "")
    if not text:
        return frozenset()
    keys: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"^(.+?):(\d+):\d+:\s+([A-Z]{1,3}\d{3,4})\b", line.strip())
        if m:
            keys.add(f"{_norm_rel(m.group(1)).lower()}|{m.group(3)}|{m.group(2)}")
    return frozenset(keys)


def parse_npm_build_signatures(text: str) -> frozenset[str]:
    """npm / lifecycle codes (coarse, conservative)."""
    text = _cap_probe_parse_text(_strip_ansi(text or ""))
    if not text:
        return frozenset()
    keys: set[str] = set()
    for line in text.splitlines():
        m = re.search(r"npm ERR!\s+code\s+(\S+)", line, re.I)
        if m:
            keys.add(f"npm|{m.group(1).strip()}")
        m = re.search(r"ERROR in\s+(.+)$", line, re.I)
        if m:
            keys.add(f"webpack|{_norm_rel(m.group(1))[:120].lower()}")
    return frozenset(keys)


def _removed_errors_touch_edits(
    pre: frozenset[str],
    post: frozenset[str],
    edited_norm: List[str],
    *,
    domain: str = "",
) -> bool:
    removed = pre - post
    if not removed:
        return False
    ed_lower = [e.lower() for e in edited_norm if e]
    if domain == "build":
        if any(e.endswith("package.json") or e.endswith("package-lock.json") for e in ed_lower):
            return True
        for r in removed:
            if r.startswith("webpack|"):
                wpath = r.split("|", 1)[1]
                if wpath and any(wpath in e or e in wpath for e in ed_lower):
                    return True
        return False
    for r in removed:
        fp = r.split("|", 1)[0].lower()
        base = fp.split("/")[-1]
        for e in ed_lower:
            if fp == e or e.endswith("/" + base) or fp.endswith("/" + e.split("/")[-1]):
                return True
    return False


def _probe_cache_enabled() -> bool:
    return os.environ.get("GHOST_REPAIR_PROBE_CACHE", "1").strip().lower() not in ("0", "false", "no")


def _probe_cache_bucket(session: Any) -> Dict[str, Any]:
    if session is None:
        return {}
    c = getattr(session, "repair_probe_cache", None)
    if not isinstance(c, dict):
        c = {}
        try:
            session.repair_probe_cache = c
        except Exception:
            return {}
    return c


def _probe_cache_get(session: Any, key: str, epoch: int) -> Optional[str]:
    """Return cached probe stdout/stderr for this write_epoch; None if miss."""
    if not _probe_cache_enabled() or session is None:
        return None
    bucket = _probe_cache_bucket(session)
    ent = bucket.get(key)
    if not isinstance(ent, dict) or int(ent.get("epoch", -1)) != int(epoch):
        return None
    if "payload" not in ent:
        return None
    p = ent.get("payload")
    return p if isinstance(p, str) else ""


def _probe_cache_set(session: Any, key: str, epoch: int, payload: Optional[str]) -> None:
    """Only cache successful probe text (never None) to avoid freezing transient tool failures."""
    if not _probe_cache_enabled() or session is None or payload is None:
        return
    bucket = _probe_cache_bucket(session)
    _probe_cache_prune_if_needed(bucket)
    bucket[key] = {"epoch": int(epoch), "payload": payload}


def _abs_cwd_key(cwd: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(cwd or "."))
    except (OSError, ValueError):
        return os.path.normcase((cwd or ".").replace("\\", "/"))


def _probe_tsc_output_raw(cwd: str) -> Optional[str]:
    if os.environ.get("GHOST_REPAIR_TSC_PROBE", "1").strip().lower() in ("0", "false", "no"):
        return None
    try:
        timeout = int(os.environ.get("GHOST_REPAIR_TSC_PROBE_TIMEOUT", "25"))
    except (TypeError, ValueError):
        timeout = 25
    timeout = max(5, min(90, timeout))
    try:
        proc = subprocess.run(
            ["npx", "--yes", "tsc", "--noEmit", "--pretty", "false"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError:
        logger.info("tsc probe FileNotFoundError (npx/tsc not invokable) cwd=%s", cwd)
        return None
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.info("tsc probe failed cwd=%s: %s", cwd, exc)
        return None
    except Exception as exc:
        logger.warning("tsc probe unexpected error cwd=%s: %s", cwd, exc, exc_info=True)
        return None
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _probe_tsc_output(cwd: str, session: Any = None) -> Optional[str]:
    epoch = int(getattr(session, "write_epoch", 0) or 0) if session is not None else -1
    key = f"tsc|{_abs_cwd_key(cwd)}"
    if session is not None and _probe_cache_enabled():
        hit = _probe_cache_get(session, key, epoch)
        if hit is not None:
            return hit
    out = _probe_tsc_output_raw(cwd)
    if out is None and session is not None:
        reason = "npx_not_on_path" if _diagnose_missing_executable("npx") else "tsc_probe_failed_or_timeout"
        _append_tool_unavailable_event(
            session,
            probe="tsc",
            reason=reason,
            impact="repair_operational_budget_probe_unavailable",
        )
    if session is not None and _probe_cache_enabled():
        _probe_cache_set(session, key, epoch, out)
    return out


def _probe_ruff_output_raw(cwd: str, rel_files: List[str]) -> Optional[str]:
    if os.environ.get("GHOST_REPAIR_RUFF_PROBE", "1").strip().lower() in ("0", "false", "no"):
        return None
    if not rel_files:
        return None
    try:
        timeout = int(os.environ.get("GHOST_REPAIR_RUFF_PROBE_TIMEOUT", "20"))
    except (TypeError, ValueError):
        timeout = 20
    timeout = max(5, min(60, timeout))
    cmd = ["ruff", "check", *rel_files[:12], "--output-format", "concise"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError:
        logger.info("ruff probe FileNotFoundError cwd=%s", cwd)
        return None
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.info("ruff probe failed cwd=%s: %s", cwd, exc)
        return None
    except Exception as exc:
        logger.warning("ruff probe unexpected error cwd=%s: %s", cwd, exc, exc_info=True)
        return None
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _probe_ruff_output(cwd: str, rel_files: List[str], session: Any = None) -> Optional[str]:
    epoch = int(getattr(session, "write_epoch", 0) or 0) if session is not None else -1
    tail = "|".join(sorted(rel_files[:12]))
    key = f"ruff|{_abs_cwd_key(cwd)}|{tail}"
    if session is not None and _probe_cache_enabled():
        hit = _probe_cache_get(session, key, epoch)
        if hit is not None:
            return hit
    out = _probe_ruff_output_raw(cwd, rel_files)
    if out is None and session is not None:
        reason = "ruff_not_on_path" if _diagnose_missing_executable("ruff") else "ruff_probe_failed_or_timeout"
        _append_tool_unavailable_event(
            session,
            probe="ruff",
            reason=reason,
            impact="repair_operational_budget_probe_unavailable",
        )
    if session is not None and _probe_cache_enabled():
        _probe_cache_set(session, key, epoch, out)
    return out


def _probe_build_output_raw(cwd: str) -> Optional[str]:
    cmd = os.environ.get("GHOST_REPAIR_BUILD_PROBE_CMD", "").strip()
    if not cmd:
        return None
    try:
        timeout = int(os.environ.get("GHOST_REPAIR_BUILD_PROBE_TIMEOUT", "90"))
    except (TypeError, ValueError):
        timeout = 90
    timeout = max(10, min(180, timeout))
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=True,
        )
    except FileNotFoundError:
        logger.info("build probe FileNotFoundError cwd=%s", cwd)
        return None
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.info("build probe failed cwd=%s: %s", cwd, exc)
        return None
    except Exception as exc:
        logger.warning("build probe unexpected error cwd=%s: %s", cwd, exc, exc_info=True)
        return None
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _probe_build_output(cwd: str, session: Any = None) -> Optional[str]:
    cmd = os.environ.get("GHOST_REPAIR_BUILD_PROBE_CMD", "").strip()
    epoch = int(getattr(session, "write_epoch", 0) or 0) if session is not None else -1
    key = f"build|{_abs_cwd_key(cwd)}|{hash(cmd) & 0xFFFFFFFF}"
    if session is not None and _probe_cache_enabled():
        hit = _probe_cache_get(session, key, epoch)
        if hit is not None:
            return hit
    out = _probe_build_output_raw(cwd)
    if out is None and session is not None and cmd:
        _append_tool_unavailable_event(
            session,
            probe="build",
            reason="build_probe_failed_or_timeout",
            impact="repair_operational_budget_probe_unavailable",
        )
    if session is not None and _probe_cache_enabled():
        _probe_cache_set(session, key, epoch, out)
    return out


def evaluate_non_python_repair_budget(
    *,
    domain: str,
    pre_set: frozenset[str],
    post_text: Optional[str],
    edited_norm: List[str],
    parse_post: Callable[[str], frozenset[str]],
) -> Tuple[bool, str, str, str]:
    """
    Returns: grant, detail, confidence, skip_reason (empty if grant).
    """
    if not domain or not edited_norm:
        return False, "", "none", "no_domain_or_no_non_py_edits"
    if not pre_set:
        return False, "", "none", "no_structured_pre_errors"
    if post_text is None:
        return False, "", "none", "probe_unavailable"
    post_set = parse_post(post_text)
    if post_set == pre_set:
        return False, "", "none", "post_equals_pre_no_reduction"
    if not (post_set < pre_set):
        return False, "", "none", "not_strict_subset_ambiguous"
    if not _removed_errors_touch_edits(pre_set, post_set, edited_norm, domain=domain):
        return False, "", "none", "removed_errors_not_on_edited_paths"
    return True, f"{domain}_error_set_strict_subset", "high", ""


def resolve_repair_operational_budget(
    *,
    cwd: str,
    structural_python_failure: bool,
    applied: int,
    res: RepairSpecialistResult,
    failed_checks: List[Dict[str, Any]],
    session: Any = None,
) -> Tuple[bool, str, str, str]:
    """
    Unified REPAIR apply budget: Python parse drift OR conservative non-Python probe + set reduction.

    Returns: granted, detail, confidence, skip_reason
    """
    try:
        return _resolve_repair_operational_budget_inner(
            cwd=cwd,
            structural_python_failure=structural_python_failure,
            applied=applied,
            res=res,
            failed_checks=failed_checks,
            session=session,
        )
    except Exception as exc:
        logger.warning("resolve_repair_operational_budget failed: %s", exc, exc_info=True)
        return False, "", "none", "internal_budget_resolution_error"


def _resolve_repair_operational_budget_inner(
    *,
    cwd: str,
    structural_python_failure: bool,
    applied: int,
    res: RepairSpecialistResult,
    failed_checks: List[Dict[str, Any]],
    session: Any = None,
) -> Tuple[bool, str, str, str]:
    if applied <= 0:
        return False, "", "none", "no_applied_edits"

    py_ok, py_detail = repair_applied_patch_deserves_operational_budget(
        structural_python_failure=structural_python_failure,
        applied=applied,
        res=res,
    )
    if py_ok:
        return True, py_detail, "high", ""

    edited = _edited_non_py_paths(res)
    domain = infer_primary_non_py_domain(failed_checks or [])
    if not domain or not edited:
        return False, "", "none", py_detail or "no_non_python_context"

    blob = _blobs_for_domain(failed_checks or [], domain)
    if domain == "typecheck":
        ts_edited = [p for p in edited if p.lower().endswith((".ts", ".tsx"))]
        if not ts_edited:
            return False, "", "none", "typecheck_but_no_ts_edits"
        pre = parse_typescript_error_signatures(blob)
        if not pre:
            return False, "", "none", "no_structured_pre_errors"
        post_raw = _probe_tsc_output(cwd, session)
        return evaluate_non_python_repair_budget(
            domain="typecheck",
            pre_set=pre,
            post_text=post_raw,
            edited_norm=ts_edited,
            parse_post=parse_typescript_error_signatures,
        )
    if domain == "lint":
        pre = parse_ruff_error_signatures(blob)
        if not pre:
            return False, "", "none", "lint_pre_not_ruff_shaped"
        lint_edited = [p for p in edited if p.lower().endswith((".py", ".pyi"))]
        if not lint_edited:
            return False, "", "none", "lint_but_no_py_edits"
        post_raw = _probe_ruff_output(cwd, lint_edited, session)
        return evaluate_non_python_repair_budget(
            domain="lint",
            pre_set=pre,
            post_text=post_raw,
            edited_norm=lint_edited,
            parse_post=parse_ruff_error_signatures,
        )
    if domain == "build":
        pre = parse_npm_build_signatures(blob)
        if not pre:
            return False, "", "none", "build_pre_not_structured"
        post_raw = _probe_build_output(cwd, session)
        return evaluate_non_python_repair_budget(
            domain="build",
            pre_set=pre,
            post_text=post_raw,
            edited_norm=edited,
            parse_post=parse_npm_build_signatures,
        )

    return False, "", "none", "unknown_domain"
