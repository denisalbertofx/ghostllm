"""
Fast-path detection and evidence checks for read-only repo overview questions.

Examples:
- "dime el stack tecnologico de esta app"
- "listame la estructura del repo y las tecnologias"
- "show me the project tree and tech stack"
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

_LEADING_SLASH = re.compile(r"^\s*/[a-zA-Z0-9_-]+\b\s*")

_WRITE_REJECT = re.compile(
    r"(implement|implementa|fix|arregla|repara|repair|update|actualiza|modify|modifica|"
    r"refactor|refactoriza|write\s+code|escribe\s+c[oó]digo|create|crea|delete|borra|edita?)",
    re.I,
)

_STACK_CUE = re.compile(
    r"(tech\s+stack|stack\s+tecnol[oó]gic[oa]|stack\s+tecnologic[oa]|"
    r"tecnolog[ií]as?|frameworks?|lenguajes?|stack\b)",
    re.I,
)

_STRUCTURE_CUE = re.compile(
    r"(estructura(?:\s+del?\s+(?:repo|repositorio|proyecto|app|codebase))?|"
    r"repo\s+tree|project\s+tree|tree\s+of\s+the\s+(?:repo|project)|"
    r"archivos?\b|files?\b|carpetas?\b|directorios?\b|m[oó]dulos?\b|"
    r"c[oó]mo\s+est[aá]\s+organizado)",
    re.I,
)

_REPO_NOUN = re.compile(
    r"\b(repo|repositorio|project|proyecto|app|aplicaci[oó]n|codebase|monorepo)\b",
    re.I,
)

_LIST_CUE = re.compile(r"\b(lista(?:me|r)?|enumera|muestra|show|list)\b", re.I)


def _normalize_task_text(task_text: str) -> str:
    t = (task_text or "").strip()
    return _LEADING_SLASH.sub("", t, count=1).strip()


def detect_repo_overview_question(task_text: str) -> bool:
    """
    True when the task is a read-only repo/app overview request.

    Conservative by design: if the prompt smells like implementation or fix work, reject.
    """
    if not task_text or not isinstance(task_text, str):
        return False
    t = _normalize_task_text(task_text)
    if not t or len(t) > 2500:
        return False
    if _WRITE_REJECT.search(t):
        return False

    has_stack = bool(_STACK_CUE.search(t))
    has_structure = bool(_STRUCTURE_CUE.search(t))
    has_repo_noun = bool(_REPO_NOUN.search(t))
    has_list = bool(_LIST_CUE.search(t))

    if has_stack and (has_repo_noun or has_structure):
        return True
    if has_structure and has_repo_noun and has_list:
        return True
    return False


def _successful_tool_payloads(history: List[Dict[str, Any]], tool_name: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for msg in history:
        if msg.get("role") != "tool" or msg.get("name") != tool_name:
            continue
        raw = msg.get("content") or ""
        try:
            payload = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else raw
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and not payload.get("error"):
            out.append(payload)
    return out


def evidence_sufficient_for_repo_overview(
    history: List[Dict[str, Any]],
    *,
    discovery_action_count: int,
) -> Tuple[bool, str]:
    """
    One good summarize_repo is usually enough for a repo overview answer.

    Fallback accepts a small amount of direct evidence when summarize_repo was skipped.
    """
    if discovery_action_count < 1:
        return False, "no_discovery_yet"

    summaries = _successful_tool_payloads(history, "summarize_repo")
    if any(str(p.get("summary") or "").strip() for p in summaries):
        return True, "repo_summary_ok"

    ls_payloads = _successful_tool_payloads(history, "ls")
    read_payloads = _successful_tool_payloads(history, "read_file")
    if ls_payloads and len(read_payloads) >= 2:
        return True, "listing_and_manifest_reads_ok"

    if ls_payloads:
        return False, "missing_manifest_reads"
    return False, "no_repo_summary"
