"""
Canonical keys for policy/tool denial deduplication (same operational intent → same key).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Mapping, Tuple

from apps.cli.runtime.paths import PathComposer


def _norm_path_key(p: str) -> str:
    s = PathComposer.normalize_path_segments(str(p or "").strip())
    s = s.strip("./")
    while s.startswith("./"):
        s = s[2:]
    while "//" in s:
        s = s.replace("//", "/")
    return os.path.normcase(s) if s else ""


def _normalize_read_file_args(args: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    p = args.get("path")
    if p is not None and str(p).strip() != "":
        out["path"] = _norm_path_key(str(p))
    sl = args.get("start_line")
    ml = args.get("max_lines")
    try:
        sl_i = int(sl) if sl is not None and str(sl).strip() != "" else None
    except (TypeError, ValueError):
        sl_i = None
    try:
        ml_i = int(ml) if ml is not None and str(ml).strip() != "" else None
    except (TypeError, ValueError):
        ml_i = None
    if sl_i == 1 and ml_i is None:
        sl_i = None
    if sl_i is not None:
        out["start_line"] = sl_i
    if ml_i is not None:
        out["max_lines"] = ml_i
    return out


def _normalize_run_shell_args(args: Mapping[str, Any]) -> Dict[str, Any]:
    cmd = str(args.get("command") or "").strip()
    cmd = re.sub(r"\s+", " ", cmd)
    return {"command": cmd} if cmd else {}


def _normalize_generic_tool_args(args: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in sorted(args.keys()):
        v = args[k]
        if v is None or v == "" or v == []:
            continue
        if k == "path" and isinstance(v, str):
            out[k] = _norm_path_key(v)
        else:
            out[k] = v
    return out


def canonical_tool_invocation_key(name: str, args: Mapping[str, Any]) -> Tuple[str, str]:
    """
    Returns (tool_name_lower, compact_json) for comparing blocked retries.
    """
    n = (name or "").strip().lower()
    a = args or {}
    if n == "read_file":
        body = _normalize_read_file_args(a)
    elif n == "run_shell":
        body = _normalize_run_shell_args(a)
    else:
        body = _normalize_generic_tool_args(a)
    try:
        s = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        s = str(body)
    return n, s[:1500]
