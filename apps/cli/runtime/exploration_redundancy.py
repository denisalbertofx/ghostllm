"""
Detect repeated low-signal exploration (e.g. identical `ls` on the same small directory).

Used by answer_now_policy for shallow listing tasks: two consecutive aligned `ls` calls that
return the same file set add no information — nudge synthesis / explore→closing.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

from apps.cli.runtime.read_only_evidence import normalize_path_hint_for_listing, paths_align_for_listing


def redundant_ls_answer_now_enabled() -> bool:
    v = os.getenv("GHOST_REDUNDANT_LS_POLICY", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def small_dir_redundancy_max_entries() -> int:
    try:
        n = int(os.getenv("GHOST_SMALL_DIR_REDUNDANCY_CAP", "48"))
    except ValueError:
        n = 48
    return max(4, min(n, 500))


def iter_ls_events_chronological(history: List[Dict[str, Any]]) -> List[Tuple[str, Tuple[str, ...]]]:
    """
    Completed ls tool calls in order: (normalized_path, sorted filenames tuple).
    """
    events: List[Tuple[str, Tuple[str, ...]]] = []
    for i, m in enumerate(history):
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if fn.get("name") != "ls":
                continue
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            p = str(args.get("path") or ".").strip() or "."
            tc_id = tc.get("id")
            for j in range(i + 1, len(history)):
                tm = history[j]
                if tm.get("role") != "tool" or tm.get("name") != "ls":
                    continue
                if tc_id is not None and tm.get("tool_call_id") != tc_id:
                    continue
                raw_c = tm.get("content") or ""
                try:
                    payload = json.loads(raw_c) if isinstance(raw_c, str) else raw_c
                except (json.JSONDecodeError, TypeError):
                    break
                if not isinstance(payload, dict) or payload.get("error"):
                    break
                files = payload.get("files")
                if not isinstance(files, list):
                    break
                names = tuple(sorted(str(x) for x in files))
                norm_path = normalize_path_hint_for_listing(p)
                events.append((norm_path, names))
                break
    return events


def shallow_scope_has_repeated_identical_ls(history: List[Dict[str, Any]], scope_path_hint: str) -> bool:
    """
    True when the last two ls invocations aligned with scope_path_hint produced the same listing
    and the directory is considered small (entry count cap).
    """
    if not scope_path_hint or not isinstance(scope_path_hint, str):
        return False
    scope_norm = normalize_path_hint_for_listing(scope_path_hint)
    if not scope_norm:
        return False

    aligned: List[Tuple[str, Tuple[str, ...]]] = []
    for ls_path, sig in iter_ls_events_chronological(history):
        if paths_align_for_listing(scope_norm, ls_path):
            aligned.append((ls_path, sig))

    if len(aligned) < 2:
        return False
    (_, last_sig) = aligned[-1]
    (_, prev_sig) = aligned[-2]
    if last_sig != prev_sig:
        return False
    cap = small_dir_redundancy_max_entries()
    if len(last_sig) > cap:
        return False
    return True
def get_previous_ls_result_payload(history: List[Dict[str, Any]], requested_path: str) -> Optional[Dict[str, Any]]:
    """
    Find the most recent successful ls result for the requested_path (normalized) 
    and return its full payload as a dict.
    """
    req = normalize_path_hint_for_listing(requested_path)
    # Search backwards for the latest match
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if m.get("role") != "tool" or m.get("name") != "ls":
            continue
        
        # Find the preceding assistant message to get the path
        prev_m = None
        for j in range(i - 1, -1, -1):
            if history[j].get("role") == "assistant":
                prev_m = history[j]
                break
        
        if not prev_m:
            continue
            
        # Check if the path matches
        match_path = None
        for tc in prev_m.get("tool_calls") or []:
            if tc.get("id") == m.get("tool_call_id"):
                fn = tc.get("function") or {}
                raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                    match_path = str(args.get("path") or ".").strip() or "."
                except:
                    pass
                break
        
        if match_path and paths_align_for_listing(req, normalize_path_hint_for_listing(match_path)):
            raw_c = m.get("content") or ""
            try:
                payload = json.loads(raw_c) if isinstance(raw_c, str) else raw_c
                if isinstance(payload, dict) and not payload.get("error"):
                    return payload
            except:
                pass
    return None
