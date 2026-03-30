"""
Heuristics for early EXPLORE exit on trivial read-only tasks (Plan / should_not_write).

Conservative: only shallow directory listings with explicit path + depth constraint cues.
Does not trigger on recursive search, multi-directory, or grep-style prompts (e.g. RO-1).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


def readonly_explore_early_exit_enabled() -> bool:
    v = os.getenv("GHOST_READONLY_EARLY_EXIT", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


# Broad / ambiguous: do not auto-nudge
_REJECT_SUBSTRINGS = re.compile(
    r"(contengan|contienen|containing|grep|find\s|walk|recursive|recursiv|"
    r"every\s+file|todos\s+los\s+archivos\s+bajo|bajo\s+apps|under\s+apps|"
    r"árbol|arbol|subdirector|subfolder|glob|pattern\s+match)",
    re.I,
)

_SHALLOW_CUE = re.compile(
    r"(primer\s+nivel|solo.{0,48}primer\s+nivel|directamente\s+dentro|sin\s+subcarpetas|"
    r"no\s+subfolders?|top-?level|first\s+level\s+only|only\s+top-?level|"
    r"s[oó]lo\s+el\s+primer\s+nivel|first-level)",
    re.I,
)

_LISTING_CUE = re.compile(
    r"(lista|listado|enumera|list)\s|nombres\s+de\s+archivo|file\s+names?|"
    r"which\s+files|qu[eé]\s+archivos|devuelve\s+solo\s+la\s+lista",
    re.I,
)

# Path-like segments (repo-relative)
_PATH_LIKE = re.compile(
    r"(?:[\w.-]+/)+[\w.-]+|(?:[\w.-]+\\)+[\w.-]+",
    re.I,
)


@dataclass(frozen=True)
class NarrowShallowListingScope:
    """Matched user intent: list entries in one directory, shallow only."""

    path_hint: str
    extension: Optional[str]  # e.g. ".py" — optional filter


def detect_narrow_shallow_dir_listing(task_text: str) -> Optional[NarrowShallowListingScope]:
    """
    Return scope only when the prompt is clearly a single-directory shallow listing.

    RO-3-style prompts match; RO-1 (files matching substring across a tree) does not.
    """
    if not task_text or not isinstance(task_text, str):
        return None
    t = task_text.strip()
    if len(t) > 4000:
        return None
    if _REJECT_SUBSTRINGS.search(t):
        return None
    if not _SHALLOW_CUE.search(t) or not _LISTING_CUE.search(t):
        return None

    ext_m = re.search(r"(\.(?:py|ts|tsx|js|jsx|json|md|yaml|yml|toml))\b", t, re.I)
    ext = ext_m.group(1).lower() if ext_m else None

    path_hint = _extract_single_dir_path_hint(t)
    if not path_hint:
        return None

    return NarrowShallowListingScope(path_hint=path_hint, extension=ext)


def _extract_single_dir_path_hint(text: str) -> str:
    """Pick one repo-relative directory path from the task (conservative)."""
    # After common prepositions (ES/EN) — includes "directamente dentro de …" (RO-3)
    for pat in (
        r"directamente\s+dentro\s+de\s+[`\"]?([^\s`\"]+)[`\"]?",
        r"(?:dentro\s+de|en\s+el\s+directorio|in\s+(?:the\s+)?(?:dir(?:ectory)?|folder))\s+[`\"]?([^\s`\"]+)[`\"]?",
        r"(?:under|inside)\s+[`\"]?([^\s`\"]+)[`\"]?",
        r"(?:directamente\s+en)\s+[`\"]?([^\s`\"]+)[`\"]?",
    ):
        m = re.search(pat, text, re.I)
        if m:
            cand = m.group(1).strip().rstrip(".,;:")
            if _looks_like_rel_path(cand):
                return _normalize_path_hint(cand)
    # Longest path-like token
    candidates = _PATH_LIKE.findall(text)
    best = ""
    for c in candidates:
        c = c.strip().rstrip(".,;:")
        if _looks_like_rel_path(c) and len(c) > len(best):
            best = c
    return _normalize_path_hint(best) if best else ""


def _looks_like_rel_path(s: str) -> bool:
    s = s.strip()
    if len(s) < 3 or ".." in s:
        return False
    if s.startswith("/") or re.match(r"^[a-zA-Z]:\\", s):
        return False
    return "/" in s or "\\" in s


def _normalize_path_hint(s: str) -> str:
    s = s.replace("\\", "/").strip().strip("/")
    parts = [p for p in s.split("/") if p and p != "."]
    return "/".join(parts)


def normalize_path_hint_for_listing(s: str) -> str:
    """Stable path key for ls scope alignment (same as internal hint normalization)."""
    return _normalize_path_hint(s)


def paths_align_for_listing(user_norm: str, ls_path: str) -> bool:
    """True if ls target is plausibly the directory the user asked for."""
    ls_n = _normalize_path_hint(ls_path or ".")
    if not user_norm:
        return False
    if not ls_n:
        return user_norm in ("", ".")
    if user_norm == ls_n:
        return True
    if ls_n.endswith(user_norm) or user_norm.endswith(ls_n):
        return True
    # Same last segment (e.g. user said .../adapters, ls used full path)
    u_last = user_norm.split("/")[-1]
    l_last = ls_n.split("/")[-1]
    if u_last and u_last == l_last and len(u_last) > 2:
        return True
    return False


def last_ls_invocation_from_history(history: List[Dict[str, Any]]) -> Optional[Tuple[str, List[str]]]:
    """
    From chat history, find the most recent completed ls call: (normalized_path, file_names).

    Returns None if not found or parse fails.
    """
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        ls_calls: List[Tuple[Optional[str], str]] = []
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
            ls_calls.append((tc.get("id"), p))
        if not ls_calls:
            continue
        tc_id, last_ls_path = ls_calls[-1]
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
                continue
            if not isinstance(payload, dict) or payload.get("error"):
                break
            files = payload.get("files")
            if not isinstance(files, list):
                break
            names = [str(x) for x in files]
            return (_normalize_path_hint(last_ls_path), names)
    return None


def evidence_sufficient_for_shallow_listing(
    scope: NarrowShallowListingScope,
    history: List[Dict[str, Any]],
    *,
    discovery_action_count: int,
) -> Tuple[bool, str]:
    """
    After at least one discovery action, check last ls matches scope and optional extension.
    """
    if discovery_action_count < 1:
        return False, "no_discovery_yet"
    inv = last_ls_invocation_from_history(history)
    if not inv:
        return False, "no_ls_in_history"
    ls_path, files = inv
    user_p = _normalize_path_hint(scope.path_hint)
    if not paths_align_for_listing(user_p, ls_path):
        return False, "path_mismatch"
    if scope.extension:
        # Every file entry must match extension (ignore subdirs without extension)
        for name in files:
            if not str(name).lower().endswith(scope.extension.lower()):
                return False, "extension_mismatch"
    return True, "shallow_listing_ok"


def evidence_sufficient_for_answer(
    session: Any,
    history: List[Dict[str, Any]],
    contract_spec: Optional[Dict[str, Any]],
    *,
    user_task_text: str,
    discovery_action_count: int,
    explore_act_blocked: bool,
) -> Tuple[bool, str]:
    """
    Entry point: read-only early exit when tool evidence already answers a narrow prompt.

    ``explore_act_blocked`` is True in Plan mode or should_not_write sessions.
    """
    _ = session
    if contract_spec is not None and contract_spec.get("readonly_early_exit") is False:
        return False, "spec_opt_out"
    if not readonly_explore_early_exit_enabled():
        return False, "disabled"
    if not explore_act_blocked:
        return False, "act_allowed"
    scope = detect_narrow_shallow_dir_listing(user_task_text)
    if scope:
        ok, why = evidence_sufficient_for_shallow_listing(
            scope, history, discovery_action_count=discovery_action_count
        )
        return ok, why
    return False, "no_narrow_scope"
