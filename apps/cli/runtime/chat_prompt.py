"""
Sliding-window chat history + tool output shaping for smaller prompts (tokens / TTFT).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def trim_leading_tool_orphans(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = list(messages)
    while out and out[0].get("role") == "tool":
        out = out[1:]
    return out


def rollup_dropped_messages(dropped: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for m in dropped:
        r = m.get("role")
        if r == "tool":
            lines.append(f"- tool:{m.get('name')} chars={len(str(m.get('content') or ''))}")
        elif r == "assistant":
            tc = m.get("tool_calls") or []
            lines.append(
                f"- assistant chars={len(str(m.get('content') or ''))} tool_calls={len(tc)}"
            )
        else:
            lines.append(f"- {r} chars={len(str(m.get('content') or ''))}")
    return "\n".join(lines)


def merge_rollup(existing: str, chunk: str, max_chars: int) -> str:
    s = (existing.strip() + "\n" + chunk.strip()).strip()
    if len(s) <= max_chars:
        return s
    return s[-max_chars:]


def _suffix_from_rest(rest: List[Dict[str, Any]], tail_messages: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Take a suffix of rest (at least tail_messages long, growing until a non-tool head exists).
    Returns (suffix, dropped). dropped includes leading tool orphans cut from the raw tail window.
    """
    if not rest or tail_messages <= 0:
        return [], rest
    if len(rest) <= tail_messages:
        suf = trim_leading_tool_orphans(rest)
        return (suf if suf else rest), []
    for expand in range(tail_messages, len(rest) + 1):
        raw = rest[-expand:]
        j = 0
        while j < len(raw) and raw[j].get("role") == "tool":
            j += 1
        suffix = raw[j:]
        if suffix:
            dropped = rest[: len(rest) - expand] + raw[:j]
            return suffix, dropped
    raw_window = rest[-tail_messages:]
    j = 0
    while j < len(raw_window) and raw_window[j].get("role") == "tool":
        j += 1
    suf = raw_window[j:]
    if suf:
        dropped = rest[: len(rest) - tail_messages] + raw_window[:j]
        return suf, dropped
    return rest[-tail_messages:], rest[: max(0, len(rest) - tail_messages)]


def build_sliding_window_history(
    history: List[Dict[str, Any]],
    rollup: str,
    *,
    tail_messages: int,
    rollup_max_chars: int,
    keep_first_user: bool,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Returns (history_for_api, updated_rollup). Caller stores updated_rollup on the assistant.
    """
    if not history:
        return [], rollup
    h = history
    prefix: List[Dict[str, Any]] = []
    rest = h
    if keep_first_user and h[0].get("role") == "user":
        prefix = [h[0]]
        rest = h[1:]
    if tail_messages <= 0:
        return list(h), rollup

    suffix, dropped = _suffix_from_rest(rest, tail_messages)
    if not dropped:
        return prefix + trim_leading_tool_orphans(rest), rollup

    chunk = rollup_dropped_messages(dropped)
    new_rollup = merge_rollup(rollup, chunk, rollup_max_chars * 3)
    summary = new_rollup[-rollup_max_chars:] if len(new_rollup) > rollup_max_chars else new_rollup
    mid: List[Dict[str, Any]] = []
    if summary.strip():
        mid = [
            {
                "role": "user",
                "content": (
                    "[Earlier conversation — rolled up. Large tool outputs stored in session artifact "
                    "under tool_output_archive / _ghost_full_output_ref.]\n" + summary
                ),
            }
        ]
    return prefix + mid + suffix, new_rollup


def sliding_window_enabled() -> bool:
    v = os.environ.get("GHOST_PROMPT_SLIDING_WINDOW", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def shrink_tool_result_for_prompt(name: str, result: Dict[str, Any], max_json_chars: int) -> Dict[str, Any]:
    """Return a copy/dict safe to json.dumps for the model; may truncate large strings."""
    try:
        raw = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"_ghost_shrink_error": True, "preview": str(result)[: max_json_chars - 80]}
    if len(raw) <= max_json_chars:
        return result

    out = dict(result)
    budget = max(400, max_json_chars - 400)

    if name == "read_file" and isinstance(out.get("content"), str):
        c = out["content"]
        if len(c) > budget:
            head = budget * 55 // 100
            tail = budget * 25 // 100
            out["content"] = (
                c[:head]
                + f"\n… [ghost_trunc {len(c) - head - tail} chars omitted] …\n"
                + c[-tail:]
            )
            out["_ghost_truncated"] = True
    elif name == "run_shell":
        for k in ("stdout", "stderr"):
            v = out.get(k)
            if isinstance(v, str) and len(v) > budget // 3:
                out[k] = v[: budget // 3] + f"…[ghost_trunc {len(v) - budget // 3} chars]"
                out["_ghost_truncated"] = True
    elif name == "ls" and isinstance(out.get("files"), list):
        files = out["files"]
        cap = _env_int("GHOST_TOOL_LS_MAX_FILES_PROMPT", 400)
        if len(files) > cap:
            out["files"] = list(files[:cap]) + [f"…[ghost_trunc {len(files) - cap} more entries]"]
            out["_ghost_truncated"] = True

    try:
        again = json.dumps(out, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"_ghost_truncated": True, "preview": raw[:budget]}
    if len(again) <= max_json_chars:
        return out

    return {
        "_ghost_truncated": True,
        "_ghost_tool": name,
        "preview": again[: max_json_chars - 120] + "…",
    }
