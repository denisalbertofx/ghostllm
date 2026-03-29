"""
Structured repair: compact prompt + repair-role model, no full conversation history.
REPAIR phase calls this module; results are applied via edit_file semantics, then ACT→VERIFY bridge.

Does not own session phases or terminal outcome; CodexAssistant._phase_repair orchestrates transitions.

Extension guide: docs/RUNTIME_EXTENSION_BOUNDARIES.md
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from apps.cli.runtime.execution_agent import (
    DEFAULT_REPAIR_MODEL,
    ENV_REPAIR_MODEL,
    nim_configured,
)
from apps.cli.runtime.nim_provider import ProviderRequest, ProviderResponse, nim_chat_complete
from apps.cli.runtime.outcome_engine import _collect_paths_from_failed_checks
from apps.cli.runtime.text_files import read_text_file_with_fallback

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
REPAIR_OUTCOME_PARSE_BLOCKED = "parse_blocked_after_patch"

REPAIR_OUTCOMES = frozenset(
    {
        REPAIR_OUTCOME_APPLIED_PATCH,
        REPAIR_OUTCOME_NO_EFFECTIVE_PATCH,
        REPAIR_OUTCOME_INVALID_PATCH,
        REPAIR_OUTCOME_PROVIDER_FAILURE,
        REPAIR_OUTCOME_OUT_OF_ALLOWLIST,
        REPAIR_OUTCOME_PARSE_BLOCKED,
    }
)

REPAIR_JSON_SYSTEM = """You are a minimal patch specialist for failed CI-style checks.
Output VALID JSON ONLY (no markdown fences, no commentary).
Return a JSON object with exactly these keys:
- "root_cause" (string, <= 400 chars): one-line diagnosis.
- "edits" (array): zero or more objects, each with:
  - "path" (string): repo-relative file path; MUST be one of the allowed_paths listed in the user message.
  - "old_str" (string): exact substring to replace once in that file (must exist verbatim). Use "" ONLY when creating
    a brand-new allowlisted file that does not exist yet.
  - "new_str" (string): replacement text.
Do not invent paths outside allowed_paths. Prefer the smallest edit that fixes the error.
If the log shows SyntaxError, IndentationError, ImportError, AttributeError, or module load/startup failures,
your edits MUST directly remove that exact failure (same file/line class). No refactors, renames, or cleanups
until that error class disappears from output.
If the log shows a TypeScript missing declaration error (for example TS7016 / "Could not find a declaration file"),
prefer creating a dedicated .d.ts file in an allowed path. Do NOT inline `declare module` hacks into runtime source
files like .ts/.tsx implementations when a declaration-file path is available."""


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


def _norm_cwd_path(p: str) -> str:
    return _norm_rel_path(p).strip("/")


def _sanitize_module_name_for_dts(module_name: str) -> str:
    raw = str(module_name or "").strip().lower()
    if raw.startswith("@"):
        raw = raw[1:]
    raw = raw.replace("\\", "/")
    raw = raw.replace("/", "__")
    raw = re.sub(r"[^a-z0-9._-]+", "-", raw)
    raw = raw.strip(".-_")
    return raw or "module"


def _extract_missing_type_declaration_modules(blob: str) -> List[str]:
    out: List[str] = []
    patterns = (
        r"Could not find a declaration file for module ['\"]([^'\"]+)['\"]",
        r"Cannot find module ['\"]([^'\"]+)['\"] or its corresponding type declarations",
    )
    for pat in patterns:
        for match in re.finditer(pat, blob or "", re.I):
            module_name = str(match.group(1) or "").strip()
            if not module_name:
                continue
            if module_name.startswith(".") or re.match(r"^[A-Za-z]:/", module_name):
                continue
            if module_name not in out:
                out.append(module_name)
    return out


def _ts_declaration_allowlist_candidates(
    failed_checks: List[Dict[str, Any]],
    *,
    limit: int,
) -> List[str]:
    seen: List[str] = []
    for check in failed_checks or []:
        if str(check.get("status") or "").strip().lower() not in ("failed", "error", "blocked", "denied"):
            continue
        blob = _check_blob(check)
        modules = _extract_missing_type_declaration_modules(blob)
        if not modules:
            continue
        cwd = _norm_cwd_path(str(check.get("cwd") or ""))
        for module_name in modules:
            safe_name = _sanitize_module_name_for_dts(module_name)
            candidates = [
                f"types/{safe_name}.d.ts",
                "global.d.ts",
            ]
            for candidate in candidates:
                rel = f"{cwd}/{candidate}" if cwd else candidate
                rel = _norm_rel_path(rel)
                if rel and rel not in seen:
                    seen.append(rel)
                if len(seen) >= limit:
                    return seen[:limit]
    return seen[:limit]


def _looks_like_jest_typescript_transform_issue(blob: str) -> bool:
    b = str(blob or "")
    bl = b.lower()
    if "jest encountered an unexpected token" not in bl:
        return False
    if "typescript" in bl or "ts-jest" in bl:
        return True
    return bool(re.search(r"\.(?:ts|tsx)(?::|\(|\b)", b))


def _jest_transform_allowlist_candidates(
    failed_checks: List[Dict[str, Any]],
    *,
    limit: int,
) -> List[str]:
    seen: List[str] = []
    for check in failed_checks or []:
        if str(check.get("status") or "").strip().lower() not in ("failed", "error", "blocked", "denied"):
            continue
        blob = _check_blob(check)
        if not _looks_like_jest_typescript_transform_issue(blob):
            continue
        cwd = _norm_cwd_path(str(check.get("cwd") or ""))
        for candidate in (
            "package.json",
            "jest.config.js",
            "jest.config.cjs",
            "jest.setup.js",
            "tsconfig.json",
        ):
            rel = f"{cwd}/{candidate}" if cwd else candidate
            rel = _norm_rel_path(rel)
            if rel and rel not in seen:
                seen.append(rel)
            if len(seen) >= limit:
                return seen[:limit]
    return seen[:limit]


def build_repair_allowlist(
    failed_checks: List[Dict[str, Any]],
    diff_summary: List[Dict[str, Any]],
    *,
    limit: int = 16,
) -> List[str]:
    """Union of failed-check paths plus touched files, prioritized by root-cause signals first."""
    seen: List[str] = []
    for candidate in _jest_transform_allowlist_candidates(list(failed_checks or []), limit=limit):
        if candidate and candidate not in seen:
            seen.append(candidate)
        if len(seen) >= limit:
            return seen[:limit]
    for p in _collect_paths_from_failed_checks(list(failed_checks or []), limit=max(limit, 12)):
        n = _norm_rel_path(p)
        if n and n not in seen:
            seen.append(n)
        if len(seen) >= limit:
            break
    for candidate in _ts_declaration_allowlist_candidates(list(failed_checks or []), limit=limit):
        if candidate and candidate not in seen:
            seen.append(candidate)
        if len(seen) >= limit:
            return seen[:limit]
    for d in diff_summary or []:
        fp = _norm_rel_path(str(d.get("file") or ""))
        if fp and fp not in seen:
            seen.append(fp)
        if len(seen) >= limit:
            return seen[:limit]
    return seen[:limit]


def _check_blob(c: Dict[str, Any]) -> str:
    parts = [
        c.get("stderr"),
        c.get("stdout"),
        c.get("error"),
        c.get("output_summary"),
        c.get("reason"),
    ]
    raw = "\n".join(str(x) for x in parts if x)
    try:
        cap = int(os.environ.get("GHOST_RUNTIME_CHECK_BLOB_CAP", "240000"))
    except (TypeError, ValueError):
        cap = 240_000
    cap = max(50_000, min(800_000, cap))
    if len(raw) <= cap:
        return raw
    reserve = 80
    each = max(1, (cap - reserve) // 2)
    lost = len(raw) - 2 * each
    return raw[:each] + f"\n...[ghost_blob_truncated n={lost}]...\n" + raw[-each:]


def _truncate(s: str, max_len: int) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def summarize_primary_failure(failed_checks: List[Dict[str, Any]]) -> str:
    """Compact signature for the active root failure so repair stays causal-first."""
    failed = [c for c in (failed_checks or []) if c.get("status") in ("failed", "error", "blocked", "denied")]
    for check in failed[:6]:
        blob = _check_blob(check)
        if not blob.strip():
            continue
        jest_syntax_loc = re.search(
            r"SyntaxError:\s+([A-Za-z0-9_./\\:-]+\.(?:ts|tsx|js|jsx)):[^\n]*\((\d+):\d+\)",
            blob,
        )
        py_loc = re.search(r'File "([^"]+)", line (\d+)', blob)
        generic_loc = re.search(r"([A-Za-z0-9_./\\-]+\.(?:py|ts|tsx|js|jsx|json|yaml|yml))[:(](\d+)", blob)
        location = ""
        if jest_syntax_loc:
            location = f"{_norm_rel_path(jest_syntax_loc.group(1))}:{jest_syntax_loc.group(2)}"
        elif py_loc:
            location = f"{_norm_rel_path(py_loc.group(1))}:{py_loc.group(2)}"
        elif generic_loc:
            location = f"{_norm_rel_path(generic_loc.group(1))}:{generic_loc.group(2)}"
        if _looks_like_jest_typescript_transform_issue(blob):
            summary = "Jest TypeScript transform/config missing"
            if location:
                summary += f" at {location}"
            summary += " - configure Jest for TS/TSX instead of editing the test source"
            return summary[:240]
        err = re.search(r"\b([A-Z][A-Za-z_]*(?:Error|Exception))\b(?::\s*([^\n]+))?", blob)
        if err:
            detail = str(err.group(2) or "").strip()
            summary = err.group(1)
            if location:
                summary += f" at {location}"
            if detail:
                summary += f" - {detail[:160]}"
            return summary[:240]
        first_line = next((line.strip() for line in blob.splitlines() if line.strip()), "")
        if first_line:
            if location and location not in first_line:
                return f"{location} - {first_line[:180]}"[:240]
            return first_line[:240]
    return ""


def infer_check_kind_bucket(check: Dict[str, Any]) -> str:
    """Stable kind bucket independent of display name churn."""
    k = str(check.get("kind") or "").strip().lower()
    if k:
        if k in ("tests", "test", "pytest"):
            return "tests"
        return k[:32]
    n = str(check.get("name") or "").lower()
    blob = _check_blob(check) if isinstance(check, dict) else ""
    b = (blob or "").lower()
    if "pytest" in n or "python test" in n or n.strip() in ("tests", "test"):
        return "tests"
    if "vitest" in n or "jest" in n or "mocha" in n:
        return "tests"
    if re.search(r"=\s*\d+\s+failed", b) or "short test summary" in b or "failures in" in b:
        return "tests"
    if "lint" in n or "eslint" in n or "ruff" in n or "biome" in n or "prettier" in n:
        return "lint"
    if "mypy" in n or "pyright" in n:
        return "typecheck"
    if "typecheck" in n or "type check" in n or "tsc" in n:
        return "typecheck"
    if "build" in n or "webpack" in n or "vite" in n or "rollup" in n:
        return "build"
    if "npm err" in b or "err!" in b or "error command failed" in b or "exited with code" in b:
        return "build"
    return "other"


def infer_normalized_error_type(blob: str) -> str:
    """Best-effort error class for signatures (tracebacks, pytest, tsc, lint, tool wrappers)."""
    if not blob:
        return "unknown"
    b = blob
    m = re.search(
        r"\b(AssertionError|TypeError|ValueError|KeyError|AttributeError|ImportError|"
        r"ModuleNotFoundError|SyntaxError|IndentationError|NameError|RuntimeError|OSError)\b",
        b,
    )
    if m:
        return str(m.group(1))[:48]
    m = re.search(r"\b(TS\d{4,6})\b", b, re.I)
    if m:
        return str(m.group(1)).upper()
    m = re.search(r"\b([A-Z][A-Za-z_]*(?:Error|Exception))\b", b)
    if m:
        return str(m.group(1))[:48]
    m = re.search(r"\berror\s+([A-Z]{2,10}\d{2,6})\b", b, re.I)
    if m:
        return str(m.group(1)).upper()[:48]
    if re.search(r"\beslint\b", b, re.I) and "error" in b.lower():
        return "eslint"
    if re.search(r"\bruff\b", b, re.I) and ("error" in b.lower() or "E" in b):
        return "ruff"
    if re.search(r"\bfailed\s+to\s+compile\b", b, re.I):
        return "compile_failed"
    if re.search(r"\bERROR\b", b) and re.search(r"^\s*>\s+", b, re.M):
        return "tool_wrapper_error"
    return "unknown"


def _extract_primary_error_type_token(blob: str) -> str:
    return infer_normalized_error_type(blob)


def _strip_cosmetic_failure_noise(blob: str) -> str:
    """Drop banner lines so the same failure keeps a stable signature."""
    out: List[str] = []
    for line in (blob or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if re.match(r"^=+$", s) or re.match(r"^-{3,}$", s):
            continue
        if re.match(r"^={5,}", s) or re.match(r"^-{5,}", s):
            continue
        sl = s.lower()
        if sl.startswith("collecting ") and "collected" in sl:
            continue
        if sl.startswith("platform ") and "python" in sl:
            continue
        if "seconds " in sl and re.search(r"\d+\.\d+s", sl):
            continue
        out.append(line)
    return "\n".join(out)


def _extract_pytest_node_id(blob: str) -> str:
    m = re.search(r"(?:FAILED|ERROR)\s+(\S+::\S+)", blob, re.I)
    if not m:
        return ""
    raw = m.group(1).strip().strip(",")
    raw = raw.replace("\\", "/")
    return _norm_rel_path(raw.split()[0]) if raw else ""


def _extract_module_not_found(blob: str) -> str:
    for pat in (
        r"No module named ['\"]([^'\"]+)['\"]",
        r"ModuleNotFoundError:\s*No module named ['\"]([^'\"]+)['\"]",
        r"ImportError:\s*cannot import name ['\"]([^'\"]+)['\"]",
    ):
        m = re.search(pat, blob, re.I)
        if m:
            return str(m.group(1)).strip()[:80]
    return ""


def _extract_ts_file_and_code(blob: str) -> Tuple[str, str]:
    m = re.search(r"\b(error\s+TS\d{4,6})\b", blob, re.I)
    code = m.group(1).replace(" ", "").upper() if m else ""
    m2 = re.search(r"([^\s()]+?\.(?:ts|tsx))[:(]\d+", blob, re.I)
    if m2:
        return _norm_rel_path(m2.group(1)), code
    m3 = re.search(r"\(([^(]*\.(?:ts|tsx))\)", blob, re.I)
    if m3 and code:
        return _norm_rel_path(m3.group(1)), code
    return "", code


def _last_resort_text_fingerprint(blob: str) -> str:
    """Last fallback: normalized text (cosmetic-resistant), not raw blob."""
    nb = _strip_cosmetic_failure_noise(blob)
    nb = re.sub(r"\b\d+\b", "N", nb)
    nb = re.sub(r"\s+", " ", nb).strip().lower()[:480]
    if not nb:
        return "empty"
    return hashlib.sha256(nb.encode("utf-8", errors="replace")).hexdigest()[:12]


def _finalize_structural_token(body: str, level: str) -> str:
    return f"{body}|@{level}"


def _structural_token_confidence(tok: str) -> str:
    if "|@" not in tok:
        return "L"
    suf = tok.rsplit("|@", 1)[-1]
    return suf if suf in ("H", "M", "L") else "L"


def _failure_row_tokens(blob: str, kind: str, et: str) -> List[str]:
    """
    One or more structural rows with confidence suffix (H/M/L).
    Prefer file:line, then pytest node, module/import, TS file+code, then text fallback.
    """
    locs = _extract_structural_locations(blob)
    if locs:
        return [_finalize_structural_token(f"{kind}|{et}|{loc}", "H") for loc in locs]
    pt = _extract_pytest_node_id(blob)
    if pt:
        return [_finalize_structural_token(f"{kind}|{et}|pt:{pt}", "H")]
    mod = _extract_module_not_found(blob)
    if mod:
        return [_finalize_structural_token(f"{kind}|{et}|mod:{mod}", "M")]
    tsf, tsc = _extract_ts_file_and_code(blob)
    if tsf and tsc:
        return [_finalize_structural_token(f"{kind}|{et}|{tsc}|{tsf}", "M")]
    if tsf:
        return [_finalize_structural_token(f"{kind}|{et}|tsf:{tsf}", "M")]
    fb = _last_resort_text_fingerprint(blob)
    return [_finalize_structural_token(f"{kind}|{et}|fb:{fb}", "L")]


def _snapshot_all_low_confidence_tokens(tokens: Iterable[str]) -> bool:
    tlist = list(tokens)
    if not tlist:
        return True
    return all(_structural_token_confidence(t) == "L" for t in tlist)


def _extract_structural_locations(blob: str) -> List[str]:
    out: List[str] = []
    for m in re.finditer(r'File "([^"]+)", line (\d+)', blob):
        loc = f"{_norm_rel_path(m.group(1))}:{m.group(2)}"
        if loc not in out:
            out.append(loc)
    for m in re.finditer(
        r"([A-Za-z0-9_./\\-]+\.(?:py|ts|tsx|js|jsx|mjs|cjs))[:(](\d+)",
        blob,
    ):
        loc = f"{_norm_rel_path(m.group(1))}:{m.group(2)}"
        if loc not in out:
            out.append(loc)
    return out[:6]


def failure_structural_token_set(failed_checks: List[Dict[str, Any]]) -> frozenset[str]:
    """
    One or more tokens per failed check: file:line / pytest node / module / TS / last-resort text.
    Each token ends with |@H |@M |@L for budget gating.
    """
    fc = [
        c
        for c in (failed_checks or [])
        if isinstance(c, dict) and c.get("status") in ("failed", "error")
    ]
    tokens: set[str] = set()
    for c in fc:
        blob = _check_blob(c)
        kind = infer_check_kind_bucket(c)
        et = infer_normalized_error_type(blob)
        for t in _failure_row_tokens(blob, kind, et):
            tokens.add(t)
    return frozenset(tokens)


def failure_structural_digest(tokens: frozenset[str]) -> str:
    if not tokens:
        return ""
    joined = "|".join(sorted(tokens))
    return hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()[:32]


def compute_causal_error_signature(
    failed_checks: List[Dict[str, Any]],
) -> Tuple[str, str]:
    """
    Human-readable primary line + stable short digest for repair loop / artifacts.
    """
    primary = summarize_primary_failure(failed_checks)
    primary = re.sub(r"\s+", " ", (primary or "").strip())[:400]
    if not primary:
        return "", ""
    digest = hashlib.sha256(primary.encode("utf-8")).hexdigest()[:32]
    return primary, digest


def classify_error_signature_delta(prev_digest: str, cur_digest: str) -> str:
    if not cur_digest:
        return "unclear"
    if not prev_digest:
        return "first"
    if prev_digest == cur_digest:
        return "same_error"
    return "new_error"


def snapshot_failed_verification(failed_checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compact state after a failed verify for comparing the next run (names, row count, causal digest,
    structural token set for conservative progress detection).
    """
    fc = [
        c
        for c in (failed_checks or [])
        if isinstance(c, dict) and c.get("status") in ("failed", "error")
    ]
    names = tuple(sorted({str(c.get("name") or "").strip() for c in fc if str(c.get("name") or "").strip()}))
    _, digest = compute_causal_error_signature(fc)
    st = failure_structural_token_set(fc)
    return {
        "names": names,
        "count": len(fc),
        "digest": digest,
        "structural_tokens": sorted(st),
        "structural_digest": failure_structural_digest(st),
    }


def _classify_failure_set_delta_legacy(
    prev: Mapping[str, Any],
    cur: Mapping[str, Any],
) -> str:
    """Name/digest-only comparison for snapshots stored before structural_tokens existed."""
    pnames = set(str(x) for x in (prev.get("names") or ()) if x)
    cnames = set(cur["names"])
    pdig = str(prev.get("digest") or "")
    cdig = str(cur.get("digest") or "")
    try:
        pcnt = int(prev.get("count") or 0)
    except (TypeError, ValueError):
        pcnt = 0
    ccnt = int(cur.get("count") or 0)

    if cdig == pdig and cnames == pnames and ccnt == pcnt:
        return "same_failure_unchanged"
    if cnames - pnames:
        return "failure_expanded"
    if ccnt < pcnt or (cnames < pnames and cnames <= pnames):
        return "failure_reduced"
    if cnames == pnames and cdig != pdig:
        return "failure_signature_changed"
    if cdig != pdig:
        return "failure_signature_changed"
    return "failure_signature_changed"


def classify_failure_set_delta_meta(
    prev: Optional[Mapping[str, Any]],
    failed_checks: List[Dict[str, Any]],
    *,
    cur: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Like classify_failure_set_delta but includes budget-confidence metadata for artifacts.
    """
    meta: Dict[str, Any] = {
        "confidence_gate": "",
        "prev_all_low_confidence": False,
    }
    cur_local = cur if cur is not None else snapshot_failed_verification(failed_checks)
    if not prev or not isinstance(prev, Mapping):
        return "first_failure", meta
    if not str(prev.get("digest") or "").strip() and not prev.get("names"):
        return "first_failure", meta

    prev_toks = prev.get("structural_tokens")
    if prev_toks is not None:
        prev_set = frozenset(str(x) for x in prev_toks)
        cur_set = frozenset(str(x) for x in (cur_local.get("structural_tokens") or []))

        if cur_set == prev_set:
            return "same_failure_unchanged", meta

        if not prev_set and cur_set:
            return "failure_signature_changed", meta

        if cur_set < prev_set:
            if _snapshot_all_low_confidence_tokens(prev_set):
                meta["confidence_gate"] = "prev_all_low_confidence"
                meta["prev_all_low_confidence"] = True
                return "failure_signature_changed", meta
            for t in cur_set:
                if _structural_token_confidence(t) == "L":
                    meta["confidence_gate"] = "remaining_row_low_confidence"
                    return "failure_signature_changed", meta
            meta["confidence_gate"] = "subset_trusted"
            return "failure_reduced", meta

        if prev_set < cur_set:
            return "failure_expanded", meta

        return "failure_signature_changed", meta

    return _classify_failure_set_delta_legacy(prev, cur_local), meta


def classify_failure_set_delta(
    prev: Optional[Mapping[str, Any]],
    failed_checks: List[Dict[str, Any]],
    *,
    cur: Optional[Mapping[str, Any]] = None,
) -> str:
    """
    Compare current failed checks to the last stored snapshot (same session).

    Primary signal: structural token set (kind + error type + file:line), not check labels.
    Conservative: overlapping but non-subset changes → failure_signature_changed (no budget grant).
    failure_reduced only when subset and confidence anchors are not low-only / low-remainder.
    """
    label, _ = classify_failure_set_delta_meta(prev, failed_checks, cur=cur)
    return label


def build_repair_specialist_user_message(
    *,
    failed_checks: List[Dict[str, Any]],
    diff_summary: List[Dict[str, Any]],
    previous_patch_text: str,
    max_chars: int,
    repo_root: str = "",
    causal_signature_text: str = "",
    error_signature_delta: str = "first",
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
    primary_failure = summarize_primary_failure(failed)
    if primary_failure:
        lines.append("")
        lines.append("## Exact active failure to fix first")
        lines.append(f"- {primary_failure}")
        lines.append("- Do not refactor unrelated logic until this exact failure stops occurring.")
    if causal_signature_text and causal_signature_text != primary_failure:
        lines.append("")
        lines.append("## Causal signature (must disappear after patch)")
        lines.append(f"- {causal_signature_text[:320]}")
    if error_signature_delta == "same_error":
        lines.append("")
        lines.append("## Same signature as prior repair attempt")
        lines.append(
            "- Read the exact failing lines from disk; patch ONLY that root cause. "
            "No lateral refactors or style edits."
        )
    lines.append("")
    lines.append("## allowed_paths (edit ONLY these; JSON path must match exactly one of these)")
    if not allow:
        lines.append("(none — use diff files below)")
    else:
        for p in allow:
            lines.append(f"- {p}")
    declaration_targets = [p for p in allow if p.lower().endswith(".d.ts")]
    if declaration_targets:
        lines.append("")
        lines.append("## TypeScript declaration guidance")
        lines.append(
            "- For TS7016 / missing declaration errors, prefer creating one of these .d.ts files with old_str set to \"\"."
        )
        lines.append("- Do not put `declare module` blocks inside runtime implementation files when a declaration target exists.")
        for p in declaration_targets[:4]:
            lines.append(f"- declaration target: {p}")
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
    excerpt_budget = max(800, body_budget // 2)

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

    if allow and repo_root:
        lines.append("")
        lines.append("## Current file excerpts (copy old_str exactly from here when possible)")
        remaining = excerpt_budget
        excerpt_paths: List[str] = []
        for rel_path in allow[:4]:
            if remaining < 240:
                break
            per_file = min(2500, max(240, remaining // max(1, 4 - len(excerpt_paths))))
            excerpt = _load_repair_file_excerpt(repo_root, rel_path, max_chars=per_file)
            if not excerpt:
                continue
            excerpt_paths.append(rel_path)
            lines.append(f"### {rel_path}")
            lines.append(excerpt)
            remaining -= len(excerpt)
    else:
        excerpt_paths = []

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
        "primary_failure": primary_failure,
        "error_signature_delta": error_signature_delta,
        "file_excerpt_paths": excerpt_paths,
    }
    return text, meta


def _diff_lines(diff_summary: List[Dict[str, Any]]) -> Tuple[List[str], Dict[str, List[str]], int]:
    from apps.cli.runtime.artifacts import deduplicated_diff_summary

    return deduplicated_diff_summary(diff_summary)


def _load_repair_file_excerpt(repo_root: str, rel_path: str, *, max_chars: int) -> str:
    path = _norm_rel_path(rel_path)
    if not repo_root or not path or max_chars <= 0:
        return ""
    abs_path = os.path.join(repo_root, path.replace("/", os.sep))
    if not os.path.isfile(abs_path):
        return ""
    content, _encoding, error = read_text_file_with_fallback(abs_path)
    if content is None or error:
        return ""
    content = content.strip()
    if not content:
        return ""
    return _truncate(content, max_chars)


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
    parse_validation_ok: bool = True
    parse_validation_reason: str = ""
    pre_parse_fingerprints: Dict[str, str] = field(default_factory=dict)
    post_parse_fingerprints: Dict[str, str] = field(default_factory=dict)
    same_parse_signature_after_patch: bool = False


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
    if edits_applied > 0 and not res.parse_validation_ok:
        return REPAIR_OUTCOME_PARSE_BLOCKED
    return REPAIR_OUTCOME_APPLIED_PATCH


def repair_applied_patch_deserves_operational_budget(
    *,
    structural_python_failure: bool,
    applied: int,
    res: RepairSpecialistResult,
) -> Tuple[bool, str]:
    """
    Non-VERIFY operational budget: patch alone is insufficient — require monitored .py parse drift.
    """
    if applied <= 0:
        return False, "no_applied_edits"
    if not structural_python_failure:
        return False, "no_structural_python_failure_context"
    py_edited = [
        str(ed.get("path") or "").replace("\\", "/")
        for ed in (res.edits_to_apply or [])
        if str(ed.get("path") or "").lower().endswith(".py")
    ]
    if not py_edited:
        return False, "no_python_files_in_applied_patch"
    if not res.parse_validation_ok:
        return False, "parse_validation_not_ok"
    pre = dict(res.pre_parse_fingerprints or {})
    post = dict(res.post_parse_fingerprints or {})
    if pre != post:
        return True, "python_error_fingerprint_changed"
    if not res.same_parse_signature_after_patch:
        return True, "python_parse_signature_changed"
    return False, "python_parse_no_divergence"


def run_repair_specialist_llm(
    *,
    failed_checks: List[Dict[str, Any]],
    diff_summary: List[Dict[str, Any]],
    previous_patch_text: str,
    last_main_turn_prompt_chars: int,
    ghost_server_url: str,
    ghost_api_key: str,
    repo_root: str = "",
    causal_signature_text: str = "",
    error_signature_delta: str = "first",
) -> RepairSpecialistResult:
    budget_total = repair_context_char_budget(last_main_turn_prompt_chars=last_main_turn_prompt_chars)
    sys_len = len(REPAIR_JSON_SYSTEM)
    user_cap = max(DEFAULT_ABS_FLOOR, budget_total - sys_len)
    user_msg, _meta = build_repair_specialist_user_message(
        failed_checks=failed_checks,
        diff_summary=diff_summary,
        previous_patch_text=previous_patch_text,
        max_chars=user_cap,
        repo_root=repo_root,
        causal_signature_text=causal_signature_text,
        error_signature_delta=error_signature_delta,
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
