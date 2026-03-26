"""
Structured repair: compact prompt + repair-role model, no full conversation history.
REPAIR phase calls this module; results are applied via edit_file semantics, then ACT→VERIFY bridge.

Does not own session phases or terminal outcome; CodexAssistant._phase_repair orchestrates transitions.

Extension guide: docs/RUNTIME_EXTENSION_BOUNDARIES.md
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from apps.cli.runtime.execution_agent import (
    DEFAULT_REPAIR_MODEL,
    ENV_REPAIR_MODEL,
    nim_configured,
)
from apps.cli.runtime.nim_provider import ProviderRequest, ProviderResponse, nim_chat_complete
from apps.cli.runtime.outcome_engine import _collect_paths_from_failed_checks

ENV_USE_REPAIR_SPECIALIST = "GHOST_USE_REPAIR_SPECIALIST"
ENV_REPAIR_CONTEXT_FRACTION = "GHOST_REPAIR_CONTEXT_FRACTION"
ENV_REPAIR_BASELINE_CHARS = "GHOST_REPAIR_BASELINE_CHARS"
ENV_REPAIR_CONTEXT_MAX_CHARS = "GHOST_REPAIR_CONTEXT_MAX_CHARS"
ENV_NIM_BASE_URL = "GHOST_NIM_BASE_URL"
ENV_NIM_API_KEY = "GHOST_NIM_API_KEY"

DEFAULT_CONTEXT_FRACTION = 0.25
DEFAULT_BASELINE_CHARS = 32_000
DEFAULT_ABS_CAP = 14_000
DEFAULT_ABS_FLOOR = 4_096

# Machine-readable outcomes after structured repair (apply phase may change applied_patch vs no_effective)
REPAIR_OUTCOME_APPLIED_PATCH = "applied_patch"
REPAIR_OUTCOME_NO_EFFECTIVE_PATCH = "no_effective_patch"
REPAIR_OUTCOME_INVALID_PATCH = "invalid_patch"
REPAIR_OUTCOME_PROVIDER_FAILURE = "provider_failure"
REPAIR_OUTCOME_OUT_OF_ALLOWLIST = "out_of_allowlist"

REPAIR_OUTCOMES = frozenset(
    {
        REPAIR_OUTCOME_APPLIED_PATCH,
        REPAIR_OUTCOME_NO_EFFECTIVE_PATCH,
        REPAIR_OUTCOME_INVALID_PATCH,
        REPAIR_OUTCOME_PROVIDER_FAILURE,
        REPAIR_OUTCOME_OUT_OF_ALLOWLIST,
    }
)

REPAIR_JSON_SYSTEM = """You are a minimal patch specialist for failed CI-style checks.
Output VALID JSON ONLY (no markdown fences, no commentary).
Return a JSON object with exactly these keys:
- "root_cause" (string, <= 400 chars): one-line diagnosis.
- "edits" (array): zero or more objects, each with:
  - "path" (string): repo-relative file path; MUST be one of the allowed_paths listed in the user message.
  - "old_str" (string): exact substring to replace once in that file (must exist verbatim).
  - "new_str" (string): replacement text.
Do not invent paths outside allowed_paths. Prefer the smallest edit that fixes the error."""


def repair_specialist_enabled() -> bool:
    v = os.environ.get(ENV_USE_REPAIR_SPECIALIST, "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def repair_context_char_budget(*, last_main_turn_prompt_chars: int) -> int:
    """Target repair user+system prompt size as a fraction of a normal turn (capped/floored)."""
    try:
        frac = float(os.environ.get(ENV_REPAIR_CONTEXT_FRACTION, str(DEFAULT_CONTEXT_FRACTION)))
    except (TypeError, ValueError):
        frac = DEFAULT_CONTEXT_FRACTION
    frac = max(0.05, min(0.45, frac))
    try:
        baseline = int(os.environ.get(ENV_REPAIR_BASELINE_CHARS, str(DEFAULT_BASELINE_CHARS)))
    except (TypeError, ValueError):
        baseline = DEFAULT_BASELINE_CHARS
    baseline = max(8_000, baseline)
    ref = max(last_main_turn_prompt_chars, baseline)
    raw = int(ref * frac)
    try:
        cap = int(os.environ.get(ENV_REPAIR_CONTEXT_MAX_CHARS, str(DEFAULT_ABS_CAP)))
    except (TypeError, ValueError):
        cap = DEFAULT_ABS_CAP
    cap = max(DEFAULT_ABS_FLOOR, cap)
    return max(DEFAULT_ABS_FLOOR, min(cap, raw))


def _norm_rel_path(p: str) -> str:
    s = (p or "").replace("\\", "/").strip().strip("./")
    while "//" in s:
        s = s.replace("//", "/")
    return s


def build_repair_allowlist(
    failed_checks: List[Dict[str, Any]],
    diff_summary: List[Dict[str, Any]],
    *,
    limit: int = 16,
) -> List[str]:
    """Union of files touched in session diff and paths extracted from failed check output."""
    seen: List[str] = []
    for d in diff_summary or []:
        fp = _norm_rel_path(str(d.get("file") or ""))
        if fp and fp not in seen:
            seen.append(fp)
        if len(seen) >= limit:
            return seen[:limit]
    for p in _collect_paths_from_failed_checks(list(failed_checks or []), limit=max(limit, 12)):
        n = _norm_rel_path(p)
        if n and n not in seen:
            seen.append(n)
        if len(seen) >= limit:
            break
    return seen[:limit]


def _check_blob(c: Dict[str, Any]) -> str:
    parts = [
        c.get("stderr"),
        c.get("stdout"),
        c.get("error"),
        c.get("output_summary"),
        c.get("reason"),
    ]
    return "\n".join(str(x) for x in parts if x)


def _truncate(s: str, max_len: int) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def build_repair_specialist_user_message(
    *,
    failed_checks: List[Dict[str, Any]],
    diff_summary: List[Dict[str, Any]],
    previous_patch_text: str,
    max_chars: int,
) -> Tuple[str, Dict[str, Any]]:
    """Assemble a compact user message; returns (text, meta) with meta for telemetry."""
    failed = [c for c in (failed_checks or []) if c.get("status") in ("failed", "error", "blocked", "denied")]
    allow = build_repair_allowlist(failed, diff_summary or [])
    lines: List[str] = [
        "## Failed checks",
    ]
    for c in failed[:6]:
        name = str(c.get("name") or "?")
        lines.append(f"- {name}: {c.get('status')}")
    lines.append("")
    lines.append("## allowed_paths (edit ONLY these; JSON path must match exactly one of these)")
    if not allow:
        lines.append("(none — use diff files below)")
    else:
        for p in allow:
            lines.append(f"- {p}")
    lines.append("")
    lines.append("## Session diff summary (files touched)")
    uq, by_file, total = _diff_lines(diff_summary or [])
    lines.append(f"unique_files={len(uq)} total_ops={total}")
    for f in uq[:12]:
        ops = ",".join(by_file.get(f, [])[:4])
        lines.append(f"- {f} :: {ops}")
    body_budget = max(800, max_chars - 400)
    stderr_budget = max(400, body_budget // 3)
    prev_budget = max(200, body_budget // 4)

    lines.append("")
    lines.append("## Tool / check output (truncated)")
    blob_parts: List[str] = []
    for c in failed[:4]:
        name = str(c.get("name") or "?")
        blob = _truncate(_check_blob(c), stderr_budget // max(1, min(4, len(failed))))
        if blob.strip():
            blob_parts.append(f"### {name}\n{blob}")
    stderr_combined = _truncate("\n\n".join(blob_parts), stderr_budget * 2)
    lines.append(stderr_combined or "(no stderr captured)")

    if previous_patch_text.strip():
        lines.append("")
        lines.append("## Previous repair model output (truncated; do not repeat mistakes)")
        lines.append(_truncate(previous_patch_text, prev_budget))

    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        text = _truncate(text, max_chars)
    meta = {
        "allowlist_len": len(allow),
        "failed_count": len(failed),
        "built_chars": len(text),
        "budget": max_chars,
    }
    return text, meta


def _diff_lines(diff_summary: List[Dict[str, Any]]) -> Tuple[List[str], Dict[str, List[str]], int]:
    from apps.cli.runtime.artifacts import deduplicated_diff_summary

    return deduplicated_diff_summary(diff_summary)


def parse_repair_edits_json(raw_text: str) -> Tuple[str, List[Dict[str, str]], bool]:
    """
    Parse model output into (root_cause, edits, ok).
    Each edit: path, old_str, new_str.
    """
    text = (raw_text or "").strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "", [], False
    if not isinstance(data, dict):
        return "", [], False
    root = str(data.get("root_cause") or data.get("cause") or "")[:400]
    edits_raw = data.get("edits")
    if edits_raw is None and isinstance(data.get("path"), str):
        edits_raw = [data]
    if not isinstance(edits_raw, list):
        return root, [], bool(root)
    out: List[Dict[str, str]] = []
    for item in edits_raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("file_path") or "").strip()
        old_s = item.get("old_str")
        new_s = item.get("new_str")
        if old_s is None or new_s is None or not path:
            continue
        old_str = str(old_s)
        new_str = str(new_s)
        if len(old_str) > 12_000 or len(new_str) > 12_000:
            continue
        out.append({"path": path, "old_str": old_str, "new_str": new_str})
    return root, out, True


def _allowlist_normalized(allow: List[str]) -> Dict[str, str]:
    """Map normalized path -> canonical allowlist entry."""
    m: Dict[str, str] = {}
    for a in allow:
        key = _norm_rel_path(a).lower()
        if key and key not in m:
            m[key] = _norm_rel_path(a)
    return m


def filter_edits_to_allowlist(
    edits: List[Dict[str, str]],
    allowlist: List[str],
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Drop edits whose path is not in allowlist (normalized)."""
    m = _allowlist_normalized(allowlist)
    kept: List[Dict[str, str]] = []
    dropped: List[str] = []
    for e in edits:
        p = _norm_rel_path(e["path"])
        key = p.lower()
        if key not in m:
            dropped.append(p)
            continue
        ee = dict(e)
        ee["path"] = m[key]
        kept.append(ee)
    return kept, dropped


def resolve_repair_model_id() -> str:
    return os.environ.get(ENV_REPAIR_MODEL, DEFAULT_REPAIR_MODEL).strip() or DEFAULT_REPAIR_MODEL


def call_repair_model(
    *,
    system: str,
    user: str,
    model_id: str,
    ghost_server_url: str,
    ghost_api_key: str,
    max_tokens: int = 2048,
) -> ProviderResponse:
    """Route to NIM when configured, else Ghost OpenAI-compatible gateway."""
    if nim_configured():
        base = os.environ.get(ENV_NIM_BASE_URL, "").strip().rstrip("/")
        key = os.environ.get(ENV_NIM_API_KEY, "").strip()
        req = ProviderRequest(
            model=model_id,
            system=system,
            user=user,
            temperature=0.1,
            max_tokens=max_tokens,
        )
        return nim_chat_complete(base, key, req)
    req = ProviderRequest(
        model=model_id,
        system=system,
        user=user,
        temperature=0.1,
        max_tokens=max_tokens,
    )
    return nim_chat_complete(ghost_server_url.rstrip("/"), ghost_api_key, req)


@dataclass
class RepairSpecialistResult:
    """ok = LLM returned parseable JSON with at least one in-allowlist edit to attempt (not yet applied)."""
    ok: bool
    root_cause: str = ""
    edits_proposed: int = 0
    edits_to_apply: List[Dict[str, str]] = field(default_factory=list)
    edits_applied: int = 0
    edits_skipped_allowlist: int = 0
    all_proposals_outside_allowlist: bool = False
    parse_ok: bool = False
    provider_ok: bool = False
    error: str = ""
    raw_model_text: str = ""
    prompt_user_chars: int = 0
    prompt_budget: int = 0
    model_id: str = ""
    usage_total_tokens: int = 0
    apply_errors: List[str] = field(default_factory=list)


def classify_structured_repair_outcome(res: RepairSpecialistResult, edits_applied: int) -> str:
    """
    Final outcome after provider + parse + allowlist + filesystem apply.
    Only REPAIR_OUTCOME_APPLIED_PATCH authorizes REPAIR -> VERIFY bridge.
    """
    if not res.provider_ok:
        return REPAIR_OUTCOME_PROVIDER_FAILURE
    if not res.parse_ok:
        return REPAIR_OUTCOME_INVALID_PATCH
    if res.all_proposals_outside_allowlist:
        return REPAIR_OUTCOME_OUT_OF_ALLOWLIST
    if not res.edits_to_apply:
        return REPAIR_OUTCOME_NO_EFFECTIVE_PATCH
    if edits_applied <= 0:
        return REPAIR_OUTCOME_NO_EFFECTIVE_PATCH
    return REPAIR_OUTCOME_APPLIED_PATCH


def run_repair_specialist_llm(
    *,
    failed_checks: List[Dict[str, Any]],
    diff_summary: List[Dict[str, Any]],
    previous_patch_text: str,
    last_main_turn_prompt_chars: int,
    ghost_server_url: str,
    ghost_api_key: str,
) -> RepairSpecialistResult:
    budget_total = repair_context_char_budget(last_main_turn_prompt_chars=last_main_turn_prompt_chars)
    sys_len = len(REPAIR_JSON_SYSTEM)
    user_cap = max(DEFAULT_ABS_FLOOR, budget_total - sys_len)
    user_msg, _meta = build_repair_specialist_user_message(
        failed_checks=failed_checks,
        diff_summary=diff_summary,
        previous_patch_text=previous_patch_text,
        max_chars=user_cap,
    )
    model_id = resolve_repair_model_id()
    resp = call_repair_model(
        system=REPAIR_JSON_SYSTEM,
        user=user_msg,
        model_id=model_id,
        ghost_server_url=ghost_server_url,
        ghost_api_key=ghost_api_key,
    )
    usage = int(resp.usage.total_tokens or 0) if resp.usage else 0
    if not resp.ok:
        return RepairSpecialistResult(
            ok=False,
            provider_ok=False,
            error=resp.error_message or "repair_provider_failed",
            raw_model_text="",
            prompt_user_chars=len(user_msg),
            prompt_budget=budget_total,
            model_id=model_id,
            usage_total_tokens=usage,
        )
    root, edits, parse_ok = parse_repair_edits_json(resp.raw_text)
    allow = build_repair_allowlist(
        [c for c in failed_checks if c.get("status") in ("failed", "error", "blocked", "denied")],
        diff_summary,
    )
    filtered, dropped = filter_edits_to_allowlist(edits, allow)
    stripped_all = bool(parse_ok and edits and not filtered)
    can_attempt_apply = parse_ok and bool(filtered) and not stripped_all
    return RepairSpecialistResult(
        ok=can_attempt_apply,
        root_cause=root,
        edits_proposed=len(edits),
        edits_to_apply=filtered,
        edits_applied=0,
        edits_skipped_allowlist=len(dropped),
        all_proposals_outside_allowlist=stripped_all,
        parse_ok=parse_ok,
        provider_ok=True,
        error=("all_edits_outside_allowlist" if stripped_all else ("" if parse_ok else "parse_failed")),
        raw_model_text=resp.raw_text[:24_000],
        prompt_user_chars=len(user_msg),
        prompt_budget=budget_total,
        model_id=model_id,
        usage_total_tokens=usage,
        apply_errors=[f"not_allowlisted:{p}" for p in dropped[:8]] if dropped else [],
    )
