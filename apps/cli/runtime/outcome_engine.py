"""
GEP-8: Task Outcome Engine.
Evidence-based classification of task results with explicit outcome types and scoring.
"""
import json
import re
try:
    from .implementation_quality import (
        evaluate_implementation_quality,
        QualityCheckResult,
        QUALITY_VALID,
        QUALITY_SUSPICIOUS,
        QUALITY_INCORRECT,
    )
except ImportError:
    from implementation_quality import (
        evaluate_implementation_quality,
        QualityCheckResult,
        QUALITY_VALID,
        QUALITY_SUSPICIOUS,
        QUALITY_INCORRECT,
    )
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from apps.cli.runtime.task_contract import get_task_contract_spec

# Outcome types
OUTCOME_IMPLEMENTED = "implemented"
OUTCOME_ALREADY_IMPLEMENTED = "already_implemented"
OUTCOME_PARTIALLY_IMPLEMENTED = "partially_implemented"
OUTCOME_BLOCKED = "blocked"
OUTCOME_READ_ONLY = "read_only"
OUTCOME_VERIFICATION_FAILED = "verification_failed"
OUTCOME_NO_OP = "no_op"

DISCOVERY_ACTIONS = frozenset(["summarize_repo", "ls", "read_file"])
WRITE_ACTIONS = frozenset(["write_file", "edit_file", "delete_file"])


def verification_integrity_stale(session: Any) -> bool:
    """
    True when file mutations (write_epoch) occurred after the last successful integrity verification
    checkpoint (last_integrity_ok_write_epoch). Used to avoid treating old pytest output as current.
    """
    we = int(getattr(session, "write_epoch", 0) or 0)
    li = getattr(session, "last_integrity_ok_write_epoch", None)
    if li is None:
        return False
    try:
        li_i = int(li)
    except (TypeError, ValueError):
        return False
    if li_i < 0:
        return False
    return we > li_i

# Intent categories for outcome logic
INTENT_IMPLEMENTATION = frozenset(["direct_edit", "code", "fix", "scaffold", "bug_fix", "refactor"])
INTENT_MODIFICATION = frozenset(["direct_edit", "fix", "bug_fix", "refactor"])
INTENT_INFORMATIONAL = frozenset(["research", "ask", "review"])

# Intents where missing-path tool errors are treated as informative read-only, not hard BLOCKED → ABORTED.
_INFORMATIVE_MISSING_PATH_INTENTS = frozenset({"analysis", "review"})


def _session_readonly_contract(session: Any) -> bool:
    ts = get_task_contract_spec(session)
    ce_spec = ""
    if isinstance(ts, Mapping):
        ce_spec = str(ts.get("change_expectation") or "").strip().lower()
    ce_sess = str(getattr(session, "change_expectation", "") or "").strip().lower()
    return ce_spec == "should_not_write" or ce_sess == "should_not_write"


def _session_prompt_mode(session: Any) -> str:
    tc = getattr(session, "task_contract", None)
    if isinstance(tc, Mapping):
        intent = tc.get("intent")
        if isinstance(intent, Mapping) and intent.get("is_slash_command"):
            return str(intent.get("mode") or "").strip().lower()
    return ""


def _session_is_readonly_plan_mode(session: Any) -> bool:
    return _session_prompt_mode(session) == "plan" and _session_readonly_contract(session)


def _session_is_scaffold_task(session: Any) -> bool:
    task_type = str(getattr(session, "task_type", "") or "").strip().lower()
    if task_type == "scaffold":
        return True
    tc = getattr(session, "task_contract", None)
    if isinstance(tc, Mapping):
        intent = tc.get("intent")
        if isinstance(intent, Mapping):
            if str(intent.get("task_type") or "").strip().lower() == "scaffold":
                return True
            if str(intent.get("scaffold_type") or "").strip().lower() == "bootstrap":
                return True
    return False


INFORMATIVE_KIND_EXPLORE_CHURN = "explore_churn_stop"
INFORMATIVE_KIND_MISSING_PATH_TOOLS = "missing_path_readonly"
INFORMATIVE_KIND_MISSING_SYMBOL_HINT = "missing_symbol_explore"
INFORMATIVE_KIND_EXPLICIT_PATH_ABSENT = "explicit_path_absent_explore"


def _explore_churn_graceful_stop(session: Any) -> Tuple[bool, str]:
    """True if EXPLORE closed early due to exploration churn (explore_v2_churn_abort)."""
    for e in reversed(getattr(session, "events", None) or []):
        if isinstance(e, dict) and e.get("event") == "explore_v2_churn_abort":
            return True, str(e.get("reason") or "")
    return False, ""


def _explore_explicit_paths_absent_event(session: Any) -> Tuple[bool, List[str]]:
    """True when EXPLORE closed because every user-listed directory target was missing under cwd."""
    for e in reversed(getattr(session, "events", None) or []):
        if isinstance(e, dict) and e.get("event") == "explore_explicit_paths_absent":
            raw = e.get("paths") or []
            if isinstance(raw, list):
                return True, [str(p) for p in raw if p is not None]
            return True, []
    return False, []


def _had_symbol_search_no_match_event(session: Any) -> bool:
    for e in getattr(session, "events", None) or []:
        if isinstance(e, dict) and e.get("event") == "explore_symbol_search_no_match":
            return True
    return False


def _informative_kind_from_churn_reason(churn_reason: str) -> str:
    cr = (churn_reason or "").lower()
    if "symbol_task_ls_loop" in cr or "identical_ls" in cr:
        return INFORMATIVE_KIND_MISSING_SYMBOL_HINT
    if "repo_mismatch" in cr:
        return "wrong_repo_churn"  # rare; repo_mismatch_detected usually wins first
    return INFORMATIVE_KIND_EXPLORE_CHURN


@dataclass
class TaskOutcomeResult:
    outcome: str
    confidence: float  # 0.0 - 1.0
    evidence_score: int  # raw score
    summary: str
    evidence_lines: List[str] = field(default_factory=list)
    recommended_next_action: str = ""
    # Informative read-only / blocked-explore (machine-readable; drives closure_reason UX)
    informative_readonly_kind: Optional[str] = None
    # analysis/review: grounding tier for operator UI (no NLP on model prose)
    findings_evidence_tier: Optional[str] = None


def _extract_tool_call_args_by_id(messages: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Build tool_call_id -> {name, arguments} from assistant messages."""
    out: Dict[str, Dict[str, Any]] = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls", []) or []:
            tc_id = tc.get("id")
            fn = tc.get("function") or tc.get("Function") or {}
            name = fn.get("name") or fn.get("Name") or ""
            args_raw = fn.get("arguments") or fn.get("Arguments") or "{}"
            args = {}
            if isinstance(args_raw, dict):
                args = args_raw
            elif isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw) if args_raw.strip() else {}
                except Exception:
                    pass
            if tc_id and name:
                out[tc_id] = {"name": name, "arguments": args}
    return out


def _extract_read_file_evidence(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract (path, content) for each read_file from message history."""
    tc_map = _extract_tool_call_args_by_id(messages)
    result: List[Dict[str, Any]] = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        tc_id = m.get("tool_call_id")
        name = m.get("name", "")
        if name != "read_file":
            continue
        info = tc_map.get(tc_id, {}) if tc_id else {}
        args = info.get("arguments", {})
        path = args.get("path", "")
        content = ""
        try:
            raw = m.get("content", "")
            res = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else {}
            content = res.get("content", "") or ""
        except Exception:
            pass
        if path or content:
            result.append({"path": path or "unknown", "content": content})
    # Legacy TOOL_RESULT (user messages): no path, use content only
    for m in messages:
        if m.get("role") != "user":
            continue
        raw = m.get("content", "")
        if not isinstance(raw, str) or not raw.strip().startswith("TOOL_RESULT:"):
            continue
        try:
            json_str = raw.replace("TOOL_RESULT:", "").strip()
            res = json.loads(json_str)
            if "content" not in res:
                continue
            c = res.get("content", "")
            if c and not any(r.get("content") == c for r in result):
                result.append({"path": "unknown", "content": c})
        except Exception:
            pass
    return result


def _describe_implementation_pattern(read_evidence: List[Dict[str, Any]]) -> str:
    """Generate a brief pattern description from read file contents."""
    patterns: List[str] = []
    for item in (read_evidence or []):
        c = (item.get("content") or "").replace("\r", "\n")
        if ".trim().toLowerCase()" in c or "trim().toLowerCase()" in c:
            patterns.append("status is normalized via trim().toLowerCase() before validation")
        elif "safeParse" in c and "issueStatusEnum" in c:
            patterns.append("status validated against issueStatusEnum before querying database")
        elif "parseResult.data" in c or "parsedStatus" in c:
            patterns.append("parsedStatus used in ORM filter")
        elif "z.enum(" in c or "issueStatusEnum" in c:
            patterns.append("Zod enum validation for status values")
    # Dedupe and join
    seen = set()
    unique = []
    for p in patterns:
        k = p.lower()
        if k not in seen:
            seen.add(k)
            unique.append(p)
    if not unique:
        return "implementation pattern found in inspected files"
    return ". ".join(unique[:3])


def _build_already_implemented_diagnostic(
    session: Any,
    messages: List[Dict[str, Any]],
    verification: Dict[str, Any],
    evidence_lines: List[str],
) -> str:
    """Build a specific diagnostic for already_implemented outcomes."""
    read_evidence = _extract_read_file_evidence(messages)
    files = [r.get("path", "") for r in read_evidence if r.get("path") and r.get("path") != "unknown"]
    files = list(dict.fromkeys(files))[:5]
    pattern = _describe_implementation_pattern(read_evidence)
    steps_executed = verification.get("steps_executed_count", 0)
    if steps_executed is None:
        checks = verification.get("checks", [])
        steps_executed = sum(1 for c in checks if c.get("provenance") in ("executed", "blocked_by_policy", "denied_by_user"))
    executed_passed = [c for c in verification.get("checks", []) if c.get("provenance") == "executed" and c.get("status") in ("passed", "success")]

    location = ""
    if files:
        location = f" in {', '.join(files)}"
    elif read_evidence:
        location = " in inspected files"

    base = f"Already implemented{location}: {pattern}. No code changes were needed."
    if steps_executed == 0 or not executed_passed:
        base += " Manual inspection only; no structured verification checks were executed."
    else:
        names = [c.get("name", "?") for c in executed_passed]
        base += f" Structured verification passed: {', '.join(names)}."
    return base


def _already_implemented_next_action() -> str:
    return "No changes needed. Use 'continua' only if you want a different behavior or wording."


def _normalize_tool_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract tool results from history (handles both tool role and legacy TOOL_RESULT)."""
    out = []
    for m in messages:
        if m.get("role") == "tool":
            out.append(m)
        elif m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str) and content.startswith("TOOL_RESULT:"):
                try:
                    json_str = content.replace("TOOL_RESULT:", "").strip()
                    res = json.loads(json_str)
                    name = "unknown"
                    if "files" in res:
                        name = "ls"
                    elif "content" in res:
                        name = "read_file"
                    elif "exit_code" in res:
                        name = "run_shell"
                    elif "summary" in res:
                        name = "summarize_repo"
                    out.append({"role": "tool", "name": name, "content": json_str})
                except Exception:
                    pass
    return out


def normalize_tool_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Public API: tool messages normalized for tier / evidence scoring (stable for callers)."""
    return _normalize_tool_history(messages)


def _readonly_findings_evidence_applies(session: Any, intent: str, made_changes: bool) -> bool:
    """
    True for sessions that should get findings_evidence_tier (read-only review discipline).

    Includes analysis/review intents and any task with change_expectation=should_not_write
    (e.g. /plan, misclassified modification that stays read-only).
    """
    if made_changes:
        return False
    if intent in ("analysis", "review"):
        return True
    return _session_readonly_contract(session)


def _is_recovered_tool_failure(err_str: str, diff_summary: List[Dict[str, Any]]) -> bool:
    """
    True if this tool error is likely recovered (edit failed but write succeeded on same target).
    Pattern: "Target string (old_str) not found" - edit_file failed to match, write fallback succeeded.
    """
    if not diff_summary:
        return False
    err_lower = err_str.lower()
    # Edit/patch specific failure - do not match generic "not found" or "tool not found"
    return "target string" in err_lower or ("old_str" in err_lower and "not found" in err_lower)


def _is_missing_path_or_file_error(err: str) -> bool:
    """
    True when the tool error means the path/file truly does not exist (not an edit_file substring miss).
    edit_file returns \"Target string (old_str) not found\" — that must NOT be classified as missing_files.
    """
    el = err.lower()
    if "not found" not in el and "path not found" not in el:
        return False
    # Patch did not match file contents; file was read successfully
    if "target string" in el or ("old_str" in el and "not found" in el):
        return False
    # Tool name / registration noise
    if "tool " in el and "not found" in el:
        return False
    return True


def _get_normalized_tool_errors(
    tool_history: List[Dict[str, Any]],
    diff_summary: List[Dict[str, Any]],
) -> tuple[List[str], int]:
    """
    Normalize tool errors: recovered failures (edit failed + write succeeded) are not surfaced.
    Returns (unresolved_error_lines, penalty_score).
    """
    unresolved = []
    penalty = 0
    for m in tool_history:
        if m.get("role") != "tool":
            continue
        try:
            content = m.get("content", "")
            res = json.loads(content) if isinstance(content, str) and content.strip().startswith("{") else {}
            if not isinstance(res, dict):
                continue
            err = res.get("error")
            if not err:
                continue
            err_str = str(err)[:120]
            if "already implemented fast-path" in err_str.lower():
                continue
            # Skip contradictory "Tool X not found" when writes succeeded (existing logic)
            if diff_summary and "tool " in err_str.lower() and "not found" in err_str.lower():
                continue
            # Recovered: edit failed, write fallback succeeded - do not surface as primary error
            if _is_recovered_tool_failure(err_str, diff_summary):
                continue
            # Classify and surface
            err_lower = err_str.lower()
            if "denied" in err_lower or "access denied" in err_lower:
                unresolved.append(f"Tool denied: {err_str[:60]}")
                penalty += 8
            elif "blocked" in err_lower or "policy" in err_lower or "unix-ism" in err_lower or "normalizer" in err_lower:
                unresolved.append(f"Policy block: {err_str[:60]}")
                penalty += 8
            elif "not found" in err_lower and "tool " in err_lower:
                unresolved.append(f"Tool lookup failure: {err_str[:60]}")
                penalty += 5
            else:
                unresolved.append(f"Tool error: {err_str[:60]}")
                penalty += 5
        except Exception:
            pass
    return unresolved, penalty


def _score_evidence(
    tool_history: List[Dict[str, Any]],
    session: Any,
    verification: Dict[str, Any],
) -> tuple[int, List[str]]:
    """
    Compute evidence score and evidence lines from session data.
    Returns (score, evidence_lines).
    """
    score = 0
    lines: List[str] = []

    # Relevant files read
    read_count = sum(1 for m in tool_history if m.get("role") == "tool" and m.get("name") == "read_file")
    if read_count > 0:
        score += min(read_count * 2, 10)  # cap 10
        lines.append(f"Relevant files read: {read_count}")

    # ls / summarize
    ls_count = sum(1 for m in tool_history if m.get("role") == "tool" and m.get("name") == "ls")
    sum_count = sum(1 for m in tool_history if m.get("role") == "tool" and m.get("name") == "summarize_repo")
    if ls_count or sum_count:
        score += 2
        lines.append(f"Exploration: ls({ls_count}), summarize_repo({sum_count})")

    # Files changed (deduplicate: count unique files, not operations)
    diff_summary = getattr(session, "diff_summary", []) or []
    if diff_summary:
        unique_files = list(dict.fromkeys(d.get("file", "?") for d in diff_summary))
        total_ops = len(diff_summary)
        if total_ops > len(unique_files):
            lines.append(f"Files changed: {len(unique_files)} ({', '.join(unique_files[:5])}) via {total_ops} operations")
        else:
            lines.append(f"Files changed: {len(unique_files)} ({', '.join(unique_files[:5])})")
        score += 15

    # Successful shell (build/typecheck) - compute early for matching-implementation heuristic
    shell_success = False
    for m in tool_history:
        if m.get("role") != "tool" or m.get("name") != "run_shell":
            continue
        try:
            content = m.get("content", "")
            res = json.loads(content) if isinstance(content, str) and content.strip().startswith("{") else {}
            if res.get("exit_code") == 0:
                shell_success = True
                break
        except Exception:
            pass

    # Matching implementation found (heuristic: reads + no writes)
    # 4+ reads = strong signal; 2-3 reads + exploration = light signal for already-implemented
    # NOTE: shell_success (run_shell exit 0) is NOT structured verification - do not say "verification passed"
    if read_count >= 4 and not diff_summary:
        if shell_success:
            score += 10
            lines.append("Matching implementation found (files inspected, shell check succeeded)")
        else:
            score += 6
            lines.append("Matching implementation found (relevant files inspected)")
    elif read_count >= 2 and not diff_summary and (ls_count or sum_count):
        # Light signal: 2+ reads + exploration => implementation possibly already present
        score += 4
        lines.append("Implementation explored (files read, scope inspected)")

    # Verification steps run: only count checks with provenance executed/blocked/denied/error
    checks = verification.get("checks", [])
    steps_executed = verification.get("steps_executed_count")
    if steps_executed is None:
        steps_executed = sum(1 for c in checks if c.get("provenance") in ("executed", "blocked_by_policy", "denied_by_user"))
    if steps_executed > 0:
        score += 3
        lines.append(f"Verification steps run: {steps_executed}")

    # Verification success/failure: only "passed" if provenance=executed + success
    v_status = verification.get("status", "")
    executed_passed = [c for c in checks if c.get("provenance") == "executed" and c.get("status") in ("passed", "success") and c.get("exit_code", 1) == 0]
    any_executed_passed = len(executed_passed) > 0
    blocked_checks = [c for c in checks if c.get("status") in ("blocked", "denied")]
    stale_integrity = verification_integrity_stale(session)

    if v_status in ("incomplete", "skipped") and steps_executed == 0:
        score -= 5
        lines.append("Verification: no checks executed (incomplete or skipped)")
    elif v_status == "blocked" or blocked_checks:
        score -= 10
        lines.append("Verification: blocked (policy/shell)")
    elif stale_integrity and v_status == "success" and any_executed_passed:
        score -= 8
        lines.append("Verification: stale (edits after last successful integrity run; re-run checks)")
    elif v_status == "success" and any_executed_passed:
        score += 10
        lines.append("Verification: passed")
    elif v_status == "failed":
        score -= 10
        lines.append("Verification: failed")
    elif steps_executed > 0 and not any_executed_passed:
        score -= 5
        lines.append("Verification: no checks passed")

    # Tool errors: use normalized logic (recovered edit+write not surfaced)
    tool_errors, tool_penalty = _get_normalized_tool_errors(tool_history, diff_summary)
    if tool_errors:
        score -= min(tool_penalty, 15)  # cap penalty
        lines.extend(tool_errors[:3])

    # Shell success (add to score if not already counted in matching-implementation)
    if shell_success:
        score += 5
        if "Shell commands succeeded" not in lines:
            lines.append("Shell commands succeeded")

    # Path not found / missing files
    for m in tool_history:
        if m.get("role") != "tool":
            continue
        try:
            content = m.get("content", "")
            res = json.loads(content) if isinstance(content, str) and content.strip().startswith("{") else {}
            err = str(res.get("error", ""))
            if _is_missing_path_or_file_error(err):
                score -= 3
                lines.append("Missing path/file reported")
                break
        except Exception:
            pass

    score = max(0, min(score, 50))  # clamp
    return score, list(dict.fromkeys(lines))


def _get_task_intent(session: Any) -> str:
    """
    Intent from TaskContract.spec (canonical); then session.task_intent; then task text.
    Returns: implementation | modification | analysis | review
    """
    ts = get_task_contract_spec(session)
    if isinstance(ts, Mapping):
        structured = (ts.get("intent") or "").strip()
        if structured == "implementation":
            return "implementation"
        if structured in ("modification", "refactor", "bugfix"):
            return "modification"
        if structured == "analysis":
            return "analysis"
        if structured in ("review", "verification"):
            return "review"
    structured = getattr(session, "task_intent", "") or ""
    if structured:
        # Map intent_classifier types to outcome_engine intent
        if structured == "implementation":
            return "implementation"
        if structured in ("modification", "refactor", "bugfix"):
            return "modification"
        if structured == "analysis":
            return "analysis"
        if structured in ("review", "verification"):
            return "review"
    # Contract-native: derive from task_type only (no free-text keyword inference)
    task_type = str(getattr(session, "task_type", "direct_edit") or "direct_edit")
    if task_type == "review":
        return "review"
    if task_type in INTENT_INFORMATIONAL:
        return "analysis"
    if task_type in INTENT_IMPLEMENTATION:
        return "implementation"
    if task_type in INTENT_MODIFICATION:
        return "modification"
    return "modification"


def _extract_verification_error_paths(text: str, *, limit: int = 8) -> List[str]:
    """Pull source paths from build/tsc/eslint stderr (not from diff_summary)."""
    if not text:
        return []
    out: List[str] = []
    patterns = [
        r"\./([^\s:`'\"]+\.(?:tsx?|jsx?|ts|js|mjs|cjs|css|md))",
        r"\b((?:app|components|lib|db|src|packages)/[A-Za-z0-9_./\\-]+\.(?:tsx?|jsx?|ts|js|mjs|cjs|css))(?:\(|:|\s|$)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            p = m.group(1).replace("\\", "/")
            while "//" in p:
                p = p.replace("//", "/")
            low = p.lower()
            if "node_modules" in low or p in out:
                continue
            out.append(p)
            if len(out) >= limit:
                return out
    return out


def _collect_paths_from_failed_checks(checks: List[Dict[str, Any]], *, limit: int = 6) -> List[str]:
    seen: List[str] = []
    failed = [c for c in checks if c.get("status") in ("failed", "error", "blocked", "denied")]
    for fc in failed:
        blob = (
            fc.get("stderr")
            or fc.get("stdout")
            or fc.get("error")
            or fc.get("output_summary")
            or fc.get("reason")
            or ""
        )
        for p in _extract_verification_error_paths(str(blob), limit=limit):
            if p not in seen:
                seen.append(p)
            if len(seen) >= limit:
                return seen
    return seen


def _first_compiler_line(err: str, *, limit: int = 220) -> str:
    """Prefer a concrete line (e.g. error TS2322: ...) over a flattened blob."""
    if not err:
        return ""
    for line in str(err).splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if "error ts" in low or "error ts" in s:
            return s[:limit]
    first = next((l.strip() for l in str(err).splitlines() if l.strip()), "")
    return first[:limit]


def _build_verification_failed_next_action(
    checks: List[Dict[str, Any]],
    diff_summary: List[Dict[str, Any]],
    repair_summary: str,
    repair_count: int,
    v_status: str,
) -> str:
    """Build specific, actionable next_action for verification_failed."""
    failed = [c for c in checks if c.get("status") in ("failed", "error", "blocked", "denied")]
    if not failed:
        if v_status == "incomplete":
            return "Verification was incomplete (stream interrupted). Run build/lint manually to confirm."
        return "Fix the verification failures. Run build/lint locally to debug."
    first_fail = failed[0]
    name = first_fail.get("name", "Check")
    cwd = str(first_fail.get("cwd") or ".")
    cause_code = str(first_fail.get("cause") or "")
    err = (
        first_fail.get("stderr") or first_fail.get("stdout")
        or first_fail.get("error") or first_fail.get("output_summary") or first_fail.get("reason") or ""
    )
    err_str = str(err)
    err_preview = err_str[:200].replace("\n", " ").strip()
    unique_files = [f for f in dict.fromkeys(d.get("file", "") for d in diff_summary if d.get("file"))][:3]
    err_paths = _collect_paths_from_failed_checks(failed, limit=6)
    if err_paths:
        files_str = ", ".join(err_paths[:3])
        diff_norm = [f.replace("\\", "/") for f in unique_files]
        path_blob = " ".join(err_paths).replace("\\", "/")
        if unique_files and not any(d in path_blob for d in diff_norm):
            files_str = f"{files_str} (el diff tocó: {', '.join(unique_files)})"
    else:
        files_str = ", ".join(unique_files) if unique_files else "changed files"
    parts = [f"{name} still failing in {files_str} (cwd: {cwd})."]
    if err_preview:
        err_low = err_str.lower()
        if "error ts" in err_low:
            cause = _first_compiler_line(err_str, limit=200) or err_preview
        elif "modulenotfounderror" in err_low or "no module named" in err_low:
            cause = _first_compiler_line(err_str, limit=200) or "python import path / test entrypoint mismatch"
        elif "unicode" in err_low and "decode" in err_low:
            cause = _first_compiler_line(err_str, limit=200) or "pytest collection is reading a non-Python text artifact"
        elif "enoent" in err_low and "package.json" in err_low:
            cause = _first_compiler_line(err_str, limit=200) or "workspace cwd is wrong for the Node check"
        elif "drizzle" in err_low or "drizzle-orm" in err_low:
            cause = _first_compiler_line(err_str, limit=200) or err_preview[:180]
        elif "not assignable" in err_low or "does not exist on type" in err_low:
            cause = _first_compiler_line(err_str, limit=200) or f"type mismatch: {err_preview[:160]}"
        elif "compil" in err_low:
            cause = _first_compiler_line(err_str, limit=200) or err_preview[:180]
        else:
            cause = err_preview[:180]
        parts.append(f"Likely cause: {cause[:200]}.")
    elif cause_code:
        parts.append(f"Likely cause: {cause_code}.")
    if repair_count > 0 and repair_summary:
        parts.append(f"A repair was applied ({repair_summary[:80]}).")
    parts.append(
        "Re-run the failing check in its real cwd, inspect only that output, and fix the reported root cause before broader verification."
    )
    return " ".join(parts)


def _readonly_read_metrics(tool_history: List[Dict[str, Any]]) -> Tuple[int, int]:
    """Count successful read_file tools and approximate chars from file bodies."""
    n = 0
    chars = 0
    for m in tool_history:
        if m.get("role") != "tool" or m.get("name") != "read_file":
            continue
        try:
            content = m.get("content", "")
            res = json.loads(content) if isinstance(content, str) and content.strip().startswith("{") else {}
        except Exception:
            res = {}
        if not isinstance(res, dict) or res.get("error"):
            continue
        c = res.get("content")
        if isinstance(c, str) and c.strip():
            n += 1
            chars += len(c)
        elif isinstance(res, dict) and not res.get("error"):
            n += 1
    return n, chars


def compute_findings_evidence_tier(
    tool_history: List[Dict[str, Any]],
    verification: Dict[str, Any],
) -> str:
    """
    Tier for read-only analysis/review (heuristic, tool-grounded only).

    - confirmed: at least one verification check was actually executed this session.
    - suspected: several substantive reads (still not proof of a specific bug without literal quotes).
    - unverified: light exploration — model must not claim concrete bugs as fact.
    """
    ver = verification or {}
    checks = ver.get("checks") or []
    steps = ver.get("steps_executed_count")
    if steps is None:
        steps = sum(1 for c in checks if str(c.get("provenance") or "") == "executed")
    if int(steps or 0) > 0:
        return "confirmed"
    rc, ch = _readonly_read_metrics(tool_history)
    if rc >= 4 and ch >= 10_000:
        return "suspected"
    if rc >= 2 and ch >= 2_000:
        return "suspected"
    return "unverified"


def _findings_tier_banner_line(tier: str) -> str:
    if tier == "confirmed":
        return (
            "Findings tier: confirmed — hay al menos un check de verificación ejecutado en esta sesión; "
            "los fallos de tests/lint/build pueden citarse como evidencia dura."
        )
    if tier == "suspected":
        return (
            "Findings tier: suspected — hay lecturas amplias pero sin check ejecutado; "
            "no afirmes bugs concretos (archivo/línea/código) salvo que cites texto literal devuelto por read_file en esta sesión."
        )
    return (
        "Findings tier: unverified — inspección superficial; NO afirmes bugs concretos ni muestres snippets como si fueran citas reales. "
        "Etiqueta todo como sospecha o «necesita verificación» y propón el siguiente read_file o check mínimo."
    )


def _has_final_nl_response(messages: List[Dict[str, Any]]) -> bool:
    """True if the last assistant message contains non-empty text content (final answer).
    Used to avoid misclassifying as no_op when the model produced a concluding response.
    """
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if content is None:
            continue
        if isinstance(content, str):
            if content.strip():
                return True
            continue
        if isinstance(content, list):
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    t = (p.get("text") or "").strip()
                    if t:
                        return True
        break
    return False


def determine_task_outcome(
    session: Any,
    messages: List[Dict[str, Any]],
    verification: Dict[str, Any],
) -> TaskOutcomeResult:
    """
    Determine the formal task outcome from session, messages (history), and verification.
    Extracts tool results from both role=tool and legacy TOOL_RESULT user messages.
    """
    tool_history = _normalize_tool_history(messages)
    diff_summary = getattr(session, "diff_summary", []) or []
    made_changes = bool(diff_summary)
    tools_executed = len([m for m in tool_history if m.get("role") == "tool"]) > 0
    has_final_response = _has_final_nl_response(messages)

    verification = verification or {}  # fail closed: treat None as empty
    evidence_score, evidence_lines = _score_evidence(tool_history, session, verification)
    intent = _get_task_intent(session)
    v_status = verification.get("status", "")

    # --- Decision logic ---

    # 0a. Explicit listing targets absent (Exploration Engine v2): informative read-only, not ABORTED
    ep_absent, ep_paths = _explore_explicit_paths_absent_event(session)
    if ep_absent:
        plist = ", ".join(ep_paths[:8]) if ep_paths else "(see session events)"
        summary = (
            "Read-only listing task: every path the operator asked to list is missing under the "
            f"current repository root ({plist})."
        )
        el = list(evidence_lines) + [f"explore_explicit_paths_absent: {plist}"]
        return TaskOutcomeResult(
            outcome=OUTCOME_READ_ONLY,
            confidence=0.81,
            evidence_score=max(int(evidence_score), 11),
            summary=summary[:800],
            evidence_lines=el[:22],
            recommended_next_action=(
                "Verify the repository root, fix the path in the prompt, or open the correct project."
            ),
            informative_readonly_kind=INFORMATIVE_KIND_EXPLICIT_PATH_ABSENT,
        )

    # 0. Repo mismatch (Exploration Engine v2 / INTAKE): prefer informative read-only DONE, not ABORTED
    #    from PARTIALLY_IMPLEMENTED due to expected tool errors in the wrong workspace.
    if getattr(session, "repo_mismatch_detected", False):
        op = (getattr(session, "repo_mismatch_operator_message", None) or "").strip()
        rr = (getattr(session, "repo_mismatch_reason", None) or "").strip()
        summary = op or (
            "The active repository does not match the paths or stack implied by this task "
            + (f"({rr})." if rr else ".")
        )
        el = list(evidence_lines)
        if op and not any(op[:80] in (x or "") for x in el):
            el = [f"repo_mismatch: {op[:500]}"] + el
        return TaskOutcomeResult(
            outcome=OUTCOME_READ_ONLY,
            confidence=0.82,
            evidence_score=max(int(evidence_score), 12),
            summary=summary[:800],
            evidence_lines=el[:20],
            recommended_next_action=(
                "Open the correct repository root (e.g. GhostLLM) and run the same prompt again."
            ),
            informative_readonly_kind="wrong_repo_mismatch",
        )

    # 1. No-op: no tools, no changes, and no final NL response
    # If the model produced a concluding answer (e.g. "already implemented"), never classify as no_op
    if not tools_executed and not made_changes and not has_final_response:
        return TaskOutcomeResult(
            outcome=OUTCOME_NO_OP,
            confidence=1.0,
            evidence_score=0,
            summary="No execution or answer produced.",
            evidence_lines=["No tool calls executed"],
            recommended_next_action="Retry with a clearer prompt, or use /do with explicit file paths.",
        )

    # 2. Blocked: permission/tool/path constraints (only when no successful writes)
    if made_changes:
        blocked_signals = []  # not blocked if writes succeeded
    else:
        blocked_signals = []
        for m in tool_history:
            if m.get("role") != "tool":
                continue
            try:
                content = m.get("content", "")
                res = json.loads(content) if isinstance(content, str) and content.strip().startswith("{") else {}
                err = str(res.get("error", ""))
                if "denied" in err.lower() or "Access denied" in err:
                    blocked_signals.append("shell_denied")
                if evidence_score < 5 and _is_missing_path_or_file_error(err):
                    blocked_signals.append("missing_files")
            except Exception:
                pass
    blocked_unique = list(dict.fromkeys(blocked_signals))
    if blocked_unique:
        spec_map = get_task_contract_spec(session)
        spec_ce = ""
        if isinstance(spec_map, Mapping):
            spec_ce = str(spec_map.get("change_expectation") or "").strip().lower()
        sess_ce = str(getattr(session, "change_expectation", "") or "").strip().lower()
        read_only_contract = spec_ce == "should_not_write" or sess_ce == "should_not_write"
        scaffold_task = _session_is_scaffold_task(session)
        if blocked_unique == ["missing_files"] and (
            intent in _INFORMATIVE_MISSING_PATH_INTENTS or read_only_contract
        ):
            return TaskOutcomeResult(
                outcome=OUTCOME_READ_ONLY,
                confidence=0.76,
                evidence_score=max(int(evidence_score), 8),
                summary=(
                    "Read-only / informational task: requested paths or files were not found under the "
                    "current workspace. This is an environmental/targeting issue, not a model crash."
                ),
                evidence_lines=evidence_lines + ["informative: missing path/file on read-only exploration"],
                recommended_next_action=(
                    "Verify paths exist in this repo, open the correct repository root, or narrow the question."
                ),
                informative_readonly_kind=INFORMATIVE_KIND_MISSING_PATH_TOOLS,
            )
        if blocked_unique == ["missing_files"] and scaffold_task:
            return TaskOutcomeResult(
                outcome=OUTCOME_BLOCKED,
                confidence=0.74,
                evidence_score=max(int(evidence_score), 6),
                summary=(
                    "Greenfield scaffold blocked early: the session tried to read paths that do not exist yet "
                    "instead of creating the initial project structure from the repo root."
                ),
                evidence_lines=evidence_lines + ["greenfield scaffold: missing paths are expected before first write"],
                recommended_next_action=(
                    "Resume the task from the current repo root and create the first files/directories directly "
                    "(for example pyproject.toml, README.md, src/ or app/) before reading nested paths."
                ),
            )
        return TaskOutcomeResult(
            outcome=OUTCOME_BLOCKED,
            confidence=0.8,
            evidence_score=evidence_score,
            summary=f"Task blocked: {', '.join(blocked_unique)}",
            evidence_lines=evidence_lines,
            recommended_next_action="Resolve access/permissions or missing files. Use 'continua' to retry.",
        )

    # 2b Tools ran but produced unresolved errors and no writes — not "already implemented"
    if not made_changes and tools_executed:
        readonly_plan_with_final_response = _session_is_readonly_plan_mode(session) and has_final_response
        cstop, creason = _explore_churn_graceful_stop(session)
        if cstop and not readonly_plan_with_final_response:
            kind = _informative_kind_from_churn_reason(creason)
            sym_hint = _had_symbol_search_no_match_event(session)
            if sym_hint and kind == INFORMATIVE_KIND_EXPLORE_CHURN:
                kind = INFORMATIVE_KIND_MISSING_SYMBOL_HINT
            summary = (
                "Exploration stopped early due to low-value repetition (churn policy). "
                f"The requested symbol or path may be absent in this repo, or tools repeated without new evidence. "
                f"Runtime detail: {creason[:300]}"
            )
            el = list(evidence_lines) + [f"explore_churn_abort: {creason[:400]}"]
            return TaskOutcomeResult(
                outcome=OUTCOME_READ_ONLY,
                confidence=0.78,
                evidence_score=max(int(evidence_score), 10),
                summary=summary[:800],
                evidence_lines=el[:22],
                recommended_next_action=(
                    "Use search_code/read_file on a concrete path, open the correct repo, or reformulate the question."
                ),
                informative_readonly_kind=kind,
            )
        unresolved, _pen_u = _get_normalized_tool_errors(tool_history, diff_summary)
        if unresolved and not readonly_plan_with_final_response:
            return TaskOutcomeResult(
                outcome=OUTCOME_PARTIALLY_IMPLEMENTED,
                confidence=0.82,
                evidence_score=evidence_score,
                summary="Tool execution failed before any file change. " + "; ".join(unresolved[:2]),
                evidence_lines=evidence_lines + unresolved[:5],
                recommended_next_action=(
                    "Ejecuta continua: lee el fichero con read_file y usa edit_file con un old_str copiado "
                    "exactamente del disco. Para branding/UI prioriza app/, components/ y estilos; evita tocar "
                    "rutas API salvo que el encargo lo requiera."
                ),
            )

    # 3. Verification failed, blocked, or incomplete: writes happened but verification failed/blocked/incomplete
    repair_count = getattr(session, "repair_attempt_count", 0)
    repair_summary = getattr(session, "repair_summary", "") or ""
    checks = verification.get("checks", [])
    steps_executed = verification.get("steps_executed_count", 0)
    if steps_executed is None or steps_executed == 0:
        steps_executed = sum(1 for c in checks if c.get("provenance") in ("executed", "blocked_by_policy", "denied_by_user"))
    blocked_checks = [c for c in checks if c.get("status") in ("blocked", "denied")]
    executed_passed = [c for c in checks if c.get("provenance") == "executed" and c.get("status") in ("passed", "success")]
    manual_notes = verification.get("manual_verification_notes", "").strip()
    # Manual-only: model did manual inspection, we skipped structured checks -> implemented, not verification_failed
    if made_changes and v_status == "incomplete" and steps_executed == 0 and manual_notes:
        return TaskOutcomeResult(
            outcome=OUTCOME_IMPLEMENTED,
            confidence=0.75,
            evidence_score=evidence_score,
            summary="Implementation completed. Manual inspection performed; no structured verification checks executed.",
            evidence_lines=evidence_lines,
            recommended_next_action="Run build/lint manually to confirm.",
        )
    if made_changes and (v_status == "failed" or v_status == "blocked" or v_status == "incomplete" or blocked_checks):
        failed_names = [c.get("name", "?") for c in checks if c.get("status") in ("failed", "error", "blocked", "denied")]
        what_failed = ", ".join(failed_names[:5]) if failed_names else ("build/lint/test" if v_status != "incomplete" else "verification incomplete (stream interrupted)")
        summary = f"Code changes were made but verification failed ({what_failed})."
        if v_status == "incomplete":
            summary = "Code changes were made but verification was incomplete (stream interrupted or skipped)."
        elif repair_count > 0:
            summary += f" Repair pass was attempted ({repair_count}x)."
        if repair_summary:
            evidence_lines = evidence_lines + [f"Repair: {repair_summary}"]
        rec = _build_verification_failed_next_action(
            checks=checks,
            diff_summary=diff_summary,
            repair_summary=repair_summary,
            repair_count=repair_count,
            v_status=v_status,
        )
        return TaskOutcomeResult(
            outcome=OUTCOME_VERIFICATION_FAILED,
            confidence=0.95 if v_status != "incomplete" else 0.7,
            evidence_score=evidence_score,
            summary=summary,
            evidence_lines=evidence_lines,
            recommended_next_action=rec,
        )

    # 3b. Structured verification snapshot is no longer valid after edits post-checkpoint
    if made_changes and v_status == "success" and executed_passed and steps_executed > 0 and verification_integrity_stale(session):
        el = list(evidence_lines) + [
            "Integrity evidence stale: edits after the last successful verification checkpoint; rerun VERIFY."
        ]
        rec = _build_verification_failed_next_action(
            checks=checks,
            diff_summary=diff_summary,
            repair_summary=repair_summary,
            repair_count=repair_count,
            v_status="failed",
        )
        return TaskOutcomeResult(
            outcome=OUTCOME_VERIFICATION_FAILED,
            confidence=0.88,
            evidence_score=evidence_score,
            summary=(
                "Code changed after the last successful integrity verification; "
                "Ghost VERIFY must run again before claiming tests or checks passed."
            ),
            evidence_lines=el[:22],
            recommended_next_action=rec or "Trigger VERIFY after latest edits (do not rely on prior pytest output).",
        )

    # 4. Implemented: writes + verification passed (at least one check provenance=executed and passed)
    if made_changes and v_status == "success" and executed_passed and steps_executed > 0:
        return TaskOutcomeResult(
            outcome=OUTCOME_IMPLEMENTED,
            confidence=0.95,
            evidence_score=evidence_score,
            summary="Implementation completed. Verification passed.",
            evidence_lines=evidence_lines,
            recommended_next_action="Review task outcome. Use 'continua' if needed.",
        )

    # 5. Implemented (no verification run): writes but verification skipped or empty; reduce confidence if incomplete
    if made_changes and v_status in ("skipped", "") and not blocked_checks and v_status != "incomplete":
        return TaskOutcomeResult(
            outcome=OUTCOME_IMPLEMENTED,
            confidence=0.85,
            evidence_score=evidence_score,
            summary="Implementation completed. Verification was skipped.",
            evidence_lines=evidence_lines,
            recommended_next_action="Review task outcome. Run build/lint manually if needed.",
        )

    # 6. Already implemented: implementation intent, no changes, evidence of exploration
    # Never treat implementation+no-writes as read_only; it's already_implemented when scope was explored
    if intent == "implementation" and not made_changes and tools_executed:
        evidence_str = " ".join(evidence_lines)
        strong_evidence = evidence_score >= 15 and (
            "Matching implementation" in evidence_str or "Verification: passed" in evidence_str
        )
        moderate_evidence = evidence_score >= 10
        # Light evidence: 2+ reads + exploration (ls/summarize) => implementation request resolved with no changes
        light_evidence = (
            evidence_score >= 5 or
            "Implementation explored" in evidence_str or
            "Matching implementation" in evidence_str
        )

        # Semantic quality check: block/downgrade already_implemented if implementation has defects
        quality = evaluate_implementation_quality(session, tool_history, messages)
        if quality.quality_status in (QUALITY_SUSPICIOUS, QUALITY_INCORRECT):
            merged_evidence = evidence_lines + quality.evidence_lines + quality.issues_found
            if quality.quality_status == QUALITY_INCORRECT:
                return TaskOutcomeResult(
                    outcome=OUTCOME_PARTIALLY_IMPLEMENTED,
                    confidence=quality.confidence,
                    evidence_score=evidence_score,
                    summary=f"Feature present but implementation has defects. {quality.recommended_action}",
                    evidence_lines=merged_evidence,
                    recommended_next_action=quality.recommended_action or "Fix the implementation and re-run.",
                )
            return TaskOutcomeResult(
                outcome=OUTCOME_PARTIALLY_IMPLEMENTED,
                confidence=0.7,
                evidence_score=evidence_score,
                summary="Feature present but semantic quality check found likely issues. Verify typed value usage.",
                evidence_lines=merged_evidence,
                recommended_next_action=quality.recommended_action or "Review and fix implementation.",
            )

        diagnostic = _build_already_implemented_diagnostic(session, messages, verification, evidence_lines)
        if strong_evidence:
            return TaskOutcomeResult(
                outcome=OUTCOME_ALREADY_IMPLEMENTED,
                confidence=0.9,
                evidence_score=evidence_score,
                summary=diagnostic,
                evidence_lines=evidence_lines,
                recommended_next_action=_already_implemented_next_action(),
            )
        if moderate_evidence:
            return TaskOutcomeResult(
                outcome=OUTCOME_ALREADY_IMPLEMENTED,
                confidence=0.75,
                evidence_score=evidence_score,
                summary=diagnostic,
                evidence_lines=evidence_lines,
                recommended_next_action=_already_implemented_next_action(),
            )
        if light_evidence:
            return TaskOutcomeResult(
                outcome=OUTCOME_ALREADY_IMPLEMENTED,
                confidence=0.7,
                evidence_score=evidence_score,
                summary=diagnostic,
                evidence_lines=evidence_lines,
                recommended_next_action=_already_implemented_next_action(),
            )

    # 7. Partially implemented: some evidence but incomplete
    if intent == "implementation" and not made_changes and 5 <= evidence_score < 15:
        return TaskOutcomeResult(
            outcome=OUTCOME_PARTIALLY_IMPLEMENTED,
            confidence=0.6,
            evidence_score=evidence_score,
            summary="Partial implementation may exist. Insufficient evidence for full coverage.",
            evidence_lines=evidence_lines,
            recommended_next_action="Continue with 'continua' to implement remaining pieces or verify scope.",
        )

    # 8. Read-only review discipline: analysis/review and any should_not_write contract (incl. /plan)
    if _readonly_findings_evidence_applies(session, intent, made_changes):
        tier = compute_findings_evidence_tier(tool_history, verification)
        banner = _findings_tier_banner_line(tier)
        merged = [banner] + list(evidence_lines)
        ev = ", ".join(evidence_lines[:3]) if evidence_lines else "Exploración con herramientas de lectura."
        conf = 0.78 if tier == "confirmed" else 0.58 if tier == "suspected" else 0.42
        return TaskOutcomeResult(
            outcome=OUTCOME_READ_ONLY,
            confidence=conf,
            evidence_score=evidence_score,
            summary=(
                f"[{tier}] Inspección solo lectura. No presentes bugs inventados: solo hallazgos con evidencia literal "
                f"o resultados de check. Contexto: {ev}"
            ),
            evidence_lines=merged[:22],
            recommended_next_action=(
                "Validación mínima siguiente: un `read_file` en la ruta sospechosa o un solo check reproducible; "
                "no se requiere implementación en esta sesión."
                if tier != "confirmed"
                else "Sin pasos de implementación en esta sesión; para cambios de código, abre un mensaje nuevo en modo ejecución."
            ),
            findings_evidence_tier=tier,
        )
    if not made_changes and tools_executed and intent not in ("implementation", "modification"):
        return TaskOutcomeResult(
            outcome=OUTCOME_READ_ONLY,
            confidence=0.7,
            evidence_score=evidence_score,
            summary=f"Task completed without code changes. {', '.join(evidence_lines[:2]) if evidence_lines else 'Exploration only.'}",
            evidence_lines=evidence_lines,
            recommended_next_action="Review task outcome. Use 'continua' if needed.",
        )

    # 9. Default: implementation+no-writes => already_implemented; else read_only
    # Never return read_only when user asked to implement and explored scope
    if (
        intent in ("implementation", "modification")
        and not made_changes
        and tools_executed
        and not _readonly_findings_evidence_applies(session, intent, made_changes)
    ):
        diagnostic = _build_already_implemented_diagnostic(session, messages, verification, evidence_lines)
        return TaskOutcomeResult(
            outcome=OUTCOME_ALREADY_IMPLEMENTED,
            confidence=0.65,
            evidence_score=evidence_score,
            summary=diagnostic,
            evidence_lines=evidence_lines,
            recommended_next_action=_already_implemented_next_action(),
        )
    return TaskOutcomeResult(
        outcome=OUTCOME_READ_ONLY,
        confidence=0.6,
        evidence_score=evidence_score,
        summary=f"Task completed without code changes. {', '.join(evidence_lines[:2]) or 'Exploration only.'}",
        evidence_lines=evidence_lines,
        recommended_next_action="Review task outcome. Use 'continua' if needed.",
    )
