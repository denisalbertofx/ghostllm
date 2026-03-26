"""
Exploration Engine v2 — policy layer for the EXPLORE phase.

Provides:
  - Task classification by exploration style (symbol_lookup, path_listing,
    factual_question, architecture_review, write_task)
  - Tool ranking hints per class (injected into system nudges / prompt)
  - Exploration churn detection (repeated ls, repeated missing paths)
  - "Unproductive explore" abort signal (distinct from max_iterations)
  - Repo-mismatch-aware graceful close suggestion

This module is ADVISORY / POLICY only.  It never writes files or modifies
session state directly; that responsibility stays in assistant.py.

All public helpers are deterministic and side-effect-free (suitable for tests).

Stable reason codes (do not rename without docs / migration note)
-----------------------------------------------------------------
CHURN_REASON_REPEATED_LS_SAME_DIR
CHURN_REASON_REPEATED_MISSING_PATH
CHURN_REASON_NO_NEW_EVIDENCE_AFTER_N_STEPS
CHURN_REASON_ONLY_SHADOW_LS
CHURN_REASON_REPEATED_IDENTICAL_LS_LISTING
CHURN_REASON_REPEATED_SAME_TOOL_INVOCATION

EXPLORE_CLASS_SYMBOL_LOOKUP
EXPLORE_CLASS_PATH_LISTING
EXPLORE_CLASS_FACTUAL_QUESTION
EXPLORE_CLASS_ARCHITECTURE_REVIEW
EXPLORE_CLASS_WRITE_TASK
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apps.cli.runtime.exploration_redundancy import iter_ls_events_chronological
from apps.cli.runtime.planning_task import detect_strategy_plan_request
from apps.cli.runtime.repo_overview_task import detect_repo_overview_question

# ── Constants ──────────────────────────────────────────────────────────────────

EXPLORE_CLASS_SYMBOL_LOOKUP = "symbol_lookup"
EXPLORE_CLASS_PATH_LISTING = "path_listing"
EXPLORE_CLASS_FACTUAL_QUESTION = "factual_question"
EXPLORE_CLASS_ARCHITECTURE_REVIEW = "architecture_review"
EXPLORE_CLASS_WRITE_TASK = "write_task"

CHURN_REASON_REPEATED_LS_SAME_DIR = "repeated_ls_same_dir"
CHURN_REASON_REPEATED_MISSING_PATH = "repeated_missing_path"
CHURN_REASON_NO_NEW_EVIDENCE = "no_new_evidence_after_n_steps"
CHURN_REASON_ONLY_SHADOW_LS = "only_shadow_ls_no_reads"
CHURN_REASON_REPEATED_IDENTICAL_LS_LISTING = "repeated_identical_ls_listing"
CHURN_REASON_REPEATED_SAME_TOOL_INVOCATION = "repeated_same_tool_invocation"

# Tool name constants (mirrors assistant.py NATIVE_TOOLS names)
TOOL_SEARCH_CODE = "search_code"
TOOL_READ_FILE = "read_file"
TOOL_LS = "ls"
TOOL_SUMMARIZE_REPO = "summarize_repo"

# ── Env flags ─────────────────────────────────────────────────────────────────

def explore_v2_enabled() -> bool:
    v = os.getenv("GHOST_EXPLORE_V2", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def churn_detection_enabled() -> bool:
    v = os.getenv("GHOST_EXPLORE_CHURN_DETECTION", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def churn_ls_repeat_threshold() -> int:
    try:
        return max(2, int(os.getenv("GHOST_CHURN_LS_REPEAT_THRESHOLD", "3")))
    except ValueError:
        return 3


def churn_no_evidence_steps() -> int:
    """How many explore steps with zero new read_file hits before 'no new evidence'."""
    try:
        return max(3, int(os.getenv("GHOST_CHURN_NO_EVIDENCE_STEPS", "5")))
    except ValueError:
        return 5


def identical_ls_churn_enabled() -> bool:
    """Detect consecutive ls on the same path with the same file listing (incl. shadow/cache replay)."""
    v = os.getenv("GHOST_CHURN_IDENTICAL_LS", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def churn_single_signal_abort_min_calls() -> int:
    """With exactly one churn signal, abort after this many explore tool calls (not max_iterations)."""
    try:
        return max(4, int(os.getenv("GHOST_CHURN_SINGLE_SIGNAL_ABORT_MIN_CALLS", "7")))
    except ValueError:
        return 7


def symbol_search_first_strict_enabled() -> bool:
    """Stricter search-first policy for symbol-oriented tasks (lower ls churn threshold, stronger nudges)."""
    v = os.getenv("GHOST_SYMBOL_SEARCH_FIRST_STRICT", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def symbol_omit_ls_first_turn_enabled() -> bool:
    """EXPLORE iter 1: hide `ls` for symbol_lookup so the model cannot open with directory listing."""
    v = os.getenv("GHOST_EXPLORE_SYMBOL_OMIT_LS_FIRST_TURN", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def path_listing_strict_ls_enabled() -> bool:
    """Path-listing tasks: treat a second ls on the same directory as churn (list once)."""
    v = os.getenv("GHOST_PATH_LISTING_STRICT_LS", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def same_tool_invocation_abort_min_streak() -> int:
    try:
        return max(3, int(os.getenv("GHOST_CHURN_SAME_TOOL_STREAK_ABORT", "4")))
    except ValueError:
        return 4


# ── Task classification ────────────────────────────────────────────────────────

# snake_case / long identifiers typical of code symbols (e.g. verification_diff_fingerprint)
_CODE_IDENTIFIER_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}\b", re.I)
# camelCase / PascalCase type names (e.g. TaskContract) — require an interior uppercase
# so plain English sentence starters like "List" / "Where" do not count as code identifiers.
_CODE_IDENTIFIER_CAMEL = re.compile(r"\b[A-Z][a-z0-9]*[A-Z][A-Za-z0-9]*\b")

# When these appear together with a code-like identifier, treat as symbol lookup.
_SYMBOL_CONTEXT = re.compile(
    r"\b("
    r"definida?|definido|definition|define|defined|archivo|file|files?|"
    r"function|función|class|method|método|symbol|símbolo|"
    r"where|dónde|ubicad[ao]|located|which\s+file|qué\s+archivo|en\s+qué\s+archivo|"
    r"contains|contiene|lookup|buscar|find|encuentr[ao]|"
    r"what\s+does|qué\s+hace|para\s+qué\s+sirve|how\s+does\s+\w+\s+work"
    r")\b",
    re.I,
)

_SYMBOL_PATTERNS = (
    re.compile(
        r"\b(función|function|def\s|class\s|método|method|symbol|símbolo|"
        r"defined?\s+in|where\s+is\s+defined?|dónde\s+(está|se\s+define)|"
        r"what\s+does\s+\w+\s+do|para\s+qué\s+(sirve|es)|how\s+does\s+\w+\s+work)\b",
        re.I,
    ),
    re.compile(
        r"[`'\"][\w_]{3,}[\w_()][`'\"]",   # backtick/quote wrapped symbol names
        re.I,
    ),
)

_LISTING_PATTERNS = (
    re.compile(
        r"\b(list\s+files?|show\s+files?|what\s+files?|listar?\s+(archivos|ficheros|carpetas)|"
        r"qué\s+(hay\s+en|archivos\s+hay|ficheros)|contenido\s+de|ls\s+|directorio)\b",
        re.I,
    ),
)

_FACTUAL_PATTERNS = (
    re.compile(
        r"\b(what\s+is|how\s+(does|do|is)|explain|describe|why\s+does|"
        r"qué\s+es|cómo\s+(funciona|se\s+usa)|explica|para\s+qué|"
        r"cuál\s+es)\b",
        re.I,
    ),
)

_ARCHITECTURE_PATTERNS = (
    re.compile(
        r"\b(architecture|overview|structure|flow|diagram|end\s+to\s+end|"
        r"arquitectura|flujo\s+completo|visión\s+general|cómo\s+funciona\s+todo|"
        r"dónde\s+están\s+los\s+módulos)\b",
        re.I,
    ),
)

_WRITE_PATTERNS = (
    re.compile(
        r"\b(create|add|implement|fix|refactor|update|modify|change|write|"
        r"crea|añade|implementa|corrige|refactoriza|actualiza|modifica|cambia|escribe)\b",
        re.I,
    ),
)

# Spanish / English "which file is X in?" — allows short identifiers (Foo, bar, X123).
_SYMBOL_FILE_LOC_QUESTION = re.compile(
    r"(?:¿\s*)?(?:en\s+qu[eé]|in\s+which)\s+archivo\s+est[áa]\s+"
    r"(?:la\s+|el\s+)?(?:funci[oó]n|clase|m[ée]todo|tipo|variable|constante|symbol|s[ií]mbolo)?\s*"
    r"[`\"']?([A-Za-z_][\w_]*)[`\"']?",
    re.I,
)
_SYMBOL_FILE_LOC_QUESTION_SHORT = re.compile(
    r"(?:¿\s*)?d[oó]nde\s+est[áa]\s+(?:definid[ao]\s+)?"
    r"(?:la\s+|el\s+)?(?:funci[oó]n|clase|m[ée]todo|tipo|variable)?\s*"
    r"[`\"']?([A-Za-z_][\w_]*)[`\"']?",
    re.I,
)

# Directory targets for path-listing style prompts (used to verify existence before ls loops).
_LISTING_DIR_PATH_RES: Tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:directorio|carpeta|folder|directory)\s+[`\"']?([\w./\\-]+)[`\"']?",
        re.I,
    ),
    re.compile(
        r"(?:list|listar|mostrar|show)\s+(?:files?|archivos|ficheros)?\s*(?:in|en|de|under|inside)\s+"
        r"[`\"']?([\w./\\-]+)[`\"']?",
        re.I,
    ),
    re.compile(r"(?:^|[\s,])(?:ls|lista)\s+[`\"']?([\w./\\-]+)[`\"']?", re.I),
    re.compile(
        r"qu[eé]\s+hay\s+en\s+(?:el\s+)?(?:directorio|carpeta)?\s*[`\"']?([\w./\\-]+)[`\"']?",
        re.I,
    ),
)

_SKIP_LISTING_TOKENS = frozenset(
    {
        "el",
        "la",
        "los",
        "las",
        "un",
        "una",
        "this",
        "that",
        "the",
        "a",
        "an",
        "mi",
        "tu",
        "este",
        "esta",
        "ese",
        "esa",
    }
)


def _has_code_like_identifier(t: str) -> bool:
    return bool(_CODE_IDENTIFIER_SNAKE.search(t) or _CODE_IDENTIFIER_CAMEL.search(t))


def classify_explore_task(task_text: str, session: Optional[Any] = None) -> str:
    """
    Classify the task's primary exploration style.

    Returns one of EXPLORE_CLASS_* constants.
    The classification is a heuristic; downstream callers must not hard-fail on it.
    """
    if not task_text or not isinstance(task_text, str):
        return EXPLORE_CLASS_FACTUAL_QUESTION

    t = task_text.strip()

    if detect_strategy_plan_request(t):
        return EXPLORE_CLASS_ARCHITECTURE_REVIEW

    # Write intent overrides everything
    if any(p.search(t) for p in _WRITE_PATTERNS):
        # Only if there are no question words too
        if not any(p.search(t) for p in _FACTUAL_PATTERNS + _SYMBOL_PATTERNS):
            return EXPLORE_CLASS_WRITE_TASK

    if any(p.search(t) for p in _SYMBOL_PATTERNS):
        return EXPLORE_CLASS_SYMBOL_LOOKUP

    mloc = _SYMBOL_FILE_LOC_QUESTION.search(t)
    if mloc and mloc.group(1) and mloc.group(1) not in _SKIP_LISTING_TOKENS:
        return EXPLORE_CLASS_SYMBOL_LOOKUP
    mloc2 = _SYMBOL_FILE_LOC_QUESTION_SHORT.search(t)
    if mloc2 and mloc2.group(1) and mloc2.group(1) not in _SKIP_LISTING_TOKENS:
        return EXPLORE_CLASS_SYMBOL_LOOKUP

    # Bare identifiers + definition/context wording (e.g. Spanish "definida la función foo_bar_baz")
    if _SYMBOL_CONTEXT.search(t) and _has_code_like_identifier(t):
        return EXPLORE_CLASS_SYMBOL_LOOKUP

    if any(p.search(t) for p in _LISTING_PATTERNS):
        return EXPLORE_CLASS_PATH_LISTING

    if detect_repo_overview_question(t):
        return EXPLORE_CLASS_ARCHITECTURE_REVIEW

    if any(p.search(t) for p in _ARCHITECTURE_PATTERNS):
        return EXPLORE_CLASS_ARCHITECTURE_REVIEW

    return EXPLORE_CLASS_FACTUAL_QUESTION


def is_symbol_oriented_question(task_text: str) -> bool:
    """
    True when the prompt is asking where/how a concrete code symbol is defined or what it does.

    Used for search-first EXPLORE policy (prefer search_code over ls).
    """
    return classify_explore_task(task_text, None) == EXPLORE_CLASS_SYMBOL_LOOKUP


def rank_explore_tools(task_class: str) -> List[str]:
    """
    Return an ordered list of tool names from most to least preferred
    for the given explore task class.

    The list is advisory; assistant.py still filters by READ_ONLY_TOOL_NAMES.
    """
    if task_class == EXPLORE_CLASS_SYMBOL_LOOKUP:
        return [TOOL_SEARCH_CODE, TOOL_READ_FILE, TOOL_LS, TOOL_SUMMARIZE_REPO]

    if task_class == EXPLORE_CLASS_PATH_LISTING:
        return [TOOL_LS, TOOL_SEARCH_CODE, TOOL_READ_FILE, TOOL_SUMMARIZE_REPO]

    if task_class == EXPLORE_CLASS_FACTUAL_QUESTION:
        return [TOOL_SEARCH_CODE, TOOL_READ_FILE, TOOL_LS, TOOL_SUMMARIZE_REPO]

    if task_class == EXPLORE_CLASS_ARCHITECTURE_REVIEW:
        return [TOOL_SUMMARIZE_REPO, TOOL_READ_FILE, TOOL_LS, TOOL_SEARCH_CODE]

    # WRITE_TASK — same as factual (EXPLORE is read-only anyway)
    return [TOOL_READ_FILE, TOOL_LS, TOOL_SEARCH_CODE, TOOL_SUMMARIZE_REPO]


def tool_ranking_nudge(task_class: str) -> str:
    """
    Returns a short human-readable hint to inject into the system prompt
    encouraging proper tool selection for the task class.
    """
    ranking = rank_explore_tools(task_class)
    ordered = " → ".join(ranking)
    if task_class == EXPLORE_CLASS_SYMBOL_LOOKUP:
        strict = (
            " Do NOT start with `ls` unless the user explicitly asked to list a directory. "
            "If search_code finds an exact match, read that file and answer — stop broad exploration."
        )
        if symbol_search_first_strict_enabled():
            strict += (
                " If search_code(mode=symbol) returns no matches, say clearly that the name was "
                "not found in this repo (do not run repeated ls hoping to discover it)."
            )
        return (
            f"[EXPLORE v2] SEARCH-FIRST symbol task. Tool order: {ordered}. "
            "Required first step: search_code(mode=symbol) with the symbol name as `query`. "
            "Then read_file on the top match. "
            "Do NOT use ls in a loop — directory listing does not locate definitions."
            + strict
        )
    if task_class == EXPLORE_CLASS_PATH_LISTING:
        return (
            f"[EXPLORE v2] Path listing task. Preferred tool order: {ordered}. "
            "Use ls once. If the listing answers the question, synthesize immediately. "
            "Do NOT re-list the same directory."
        )
    if task_class == EXPLORE_CLASS_ARCHITECTURE_REVIEW:
        return (
            f"[EXPLORE v2] Architecture question. Preferred tool order: {ordered}. "
            "Start with summarize_repo, then targeted read_file for key modules."
        )
    # FACTUAL_QUESTION / WRITE_TASK
    return (
        f"[EXPLORE v2] Preferred tool order: {ordered}. "
        "Use search_code or targeted read_file before generic ls."
    )


def extract_listing_directory_targets(task_text: str) -> List[str]:
    """
    Pull concrete relative directory/file path tokens from listing-style prompts.

    Used to verify ``path`` exists under repo root before the model runs repeated ``ls``.
    """
    if not task_text or not isinstance(task_text, str):
        return []
    seen: set[str] = set()
    out: List[str] = []
    for rx in _LISTING_DIR_PATH_RES:
        for m in rx.finditer(task_text):
            raw = (m.group(1) or "").strip().replace("\\", "/").strip("/")
            low = raw.lower()
            if not raw or low in _SKIP_LISTING_TOKENS:
                continue
            if raw in seen:
                continue
            seen.add(raw)
            out.append(raw)
    return out[:12]


def listing_targets_missing_under_cwd(task_text: str, cwd: str) -> List[str]:
    """Paths the user asked to list that do not exist under ``cwd`` (files or directories)."""
    if not cwd:
        return []
    root = Path(cwd).resolve()
    missing: List[str] = []
    for rel in extract_listing_directory_targets(task_text):
        p = (root / rel.replace("\\", "/")).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            missing.append(rel)
            continue
        if not p.exists():
            missing.append(rel)
    return missing


def explore_listing_targets_all_absent(task_text: str, cwd: str) -> Tuple[bool, List[str]]:
    """
    True when every directory/file token extracted from a listing-style prompt is absent under cwd.

    Partial hits (some exist, some not) returns (False, missing_subset) so the caller can nudge
    without auto-closing.
    """
    targets = extract_listing_directory_targets(task_text)
    if not targets:
        return False, []
    missing = listing_targets_missing_under_cwd(task_text, cwd)
    if not missing:
        return False, []
    if len(missing) != len(targets):
        return False, missing
    return True, missing


def build_listing_paths_absent_operator_message(paths: List[str], cwd: str) -> Tuple[str, str]:
    """(en, es) explanation: requested listing paths are not in this repo."""
    tail = Path(cwd).name if cwd else "this workspace"
    plist = ", ".join(repr(p) for p in paths[:6])
    en = (
        f"The path(s) you asked to list do not exist under the current repository root ({tail!r}): {plist}. "
        "State this clearly to the operator — do not repeat `ls` on the same missing path."
    )
    es = (
        f"Las rutas que pediste listar no existen en la raíz del repositorio actual ({tail!r}): {plist}. "
        "Dilo claramente al operador; no repitas `ls` sobre la misma ruta inexistente."
    )
    return en, es


def max_same_tool_invocation_streak(history: List[Dict[str, Any]]) -> Tuple[int, str, str]:
    """
    Longest run of consecutive identical (tool_name + normalized arguments) invocations.

    Returns (streak_length, tool_name, args_fingerprint).
    """
    calls = _iter_tool_calls_from_history(history)
    if not calls:
        return 0, "", ""

    def _sig(name: str, args: Dict[str, Any]) -> str:
        try:
            return json.dumps({"n": name, "a": args}, sort_keys=True, default=str)
        except TypeError:
            return f"{name}|{args!r}"

    best = 1
    best_name = calls[0][0]
    best_detail = _sig(calls[0][0], calls[0][1])
    cur = 1
    prev_sig = _sig(calls[0][0], calls[0][1])
    prev_name = calls[0][0]
    for name, args, _ok in calls[1:]:
        sig = _sig(name, args)
        if sig == prev_sig:
            cur += 1
            if cur > best:
                best = cur
                best_name = name
                best_detail = sig
        else:
            cur = 1
            prev_sig = sig
    return best, best_name, best_detail


# ── Churn detection ───────────────────────────────────────────────────────────

@dataclass
class ChurnResult:
    """Result of explore churn detection for one session state snapshot."""
    detected: bool = False
    reasons: List[str] = field(default_factory=list)
    churn_detail: str = ""
    steps_since_new_evidence: int = 0
    repeated_ls_count: int = 0
    repeated_missing_paths: List[str] = field(default_factory=list)
    suggested_next_tool: Optional[str] = None  # advisory: try this instead


def _iter_tool_calls_from_history(
    history: List[Dict[str, Any]]
) -> List[Tuple[str, Dict[str, Any], bool]]:
    """Yield (tool_name, args, ok) tuples in chronological order from history."""
    calls: List[Tuple[str, Dict[str, Any], bool]] = []
    for i, msg in enumerate(history):
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments") or "{}"
            try:
                args: Dict[str, Any] = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError):
                args = {}
            tc_id = tc.get("id")
            # find matching tool result
            ok = True
            for j in range(i + 1, len(history)):
                tm = history[j]
                if tm.get("role") != "tool":
                    continue
                if tc_id is not None and tm.get("tool_call_id") != tc_id:
                    continue
                raw_c = tm.get("content") or ""
                try:
                    payload = json.loads(raw_c) if isinstance(raw_c, str) else raw_c
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                ok = not bool(payload.get("error") if isinstance(payload, dict) else False)
                break
            calls.append((name, args, ok))
    return calls


def history_has_tool_call(history: List[Dict[str, Any]], tool_name: str) -> bool:
    """True if any completed tool call in history used the given tool name."""
    for name, _, _ in _iter_tool_calls_from_history(history):
        if name == tool_name:
            return True
    return False


def effective_ls_churn_threshold(explore_task_class: Optional[str]) -> int:
    """
    Repeat-ls count at or above this value triggers repeated-ls churn.

    Symbol-oriented tasks use a lower threshold when strict search-first is on.
    """
    base = churn_ls_repeat_threshold()
    if (
        explore_task_class == EXPLORE_CLASS_SYMBOL_LOOKUP
        and symbol_search_first_strict_enabled()
    ):
        return min(base, 2)
    if explore_task_class == EXPLORE_CLASS_PATH_LISTING and path_listing_strict_ls_enabled():
        return min(base, 2)
    return base


def detect_exploration_churn(
    history: List[Dict[str, Any]],
    session: Optional[Any] = None,
    explore_task_class: Optional[str] = None,
) -> ChurnResult:
    """
    Analyse the EXPLORE history for low-value repetition.

    Checks:
    1. Same ls path repeated ≥ threshold times.
    2. Same path repeatedly returning file-not-found errors.
    3. Many steps with no new read_file paths (no new evidence).
    4. Only ls/summarize, no file reads (often shadow listing loops).
    5. Consecutive ls on the same path with identical file sets (zero new info; includes cache/shadow replay).
    """
    if not churn_detection_enabled():
        return ChurnResult(detected=False)

    calls = _iter_tool_calls_from_history(history)
    if not calls:
        return ChurnResult(detected=False)

    reasons: List[str] = []
    details: List[str] = []
    suggested: Optional[str] = None

    # --- Check 0a: identical tool+args repeated (any explore tool) ----------------
    streak, st_name, _st_detail = max_same_tool_invocation_streak(history)
    _explore_tool_names = frozenset(
        {TOOL_LS, TOOL_READ_FILE, TOOL_SEARCH_CODE, TOOL_SUMMARIZE_REPO, "ls", "read_file", "summarize_repo"}
    )
    if streak >= same_tool_invocation_abort_min_streak() and st_name in _explore_tool_names:
        reasons.append(CHURN_REASON_REPEATED_SAME_TOOL_INVOCATION)
        details.append(f"Same `{st_name}` call with identical arguments repeated {streak} times")
        suggested = TOOL_SEARCH_CODE if explore_task_class == EXPLORE_CLASS_SYMBOL_LOOKUP else TOOL_READ_FILE

    # --- Check 0: consecutive identical listings (same path + same sorted names) -----
    if identical_ls_churn_enabled():
        ev_ls = iter_ls_events_chronological(history)
        prev_path: Optional[str] = None
        prev_sig: Optional[tuple] = None
        dup_paths: List[str] = []
        for path, sig in ev_ls:
            if prev_path is not None and prev_path == path and prev_sig == sig:
                if not dup_paths or dup_paths[-1] != path:
                    dup_paths.append(path)
            prev_path, prev_sig = path, sig
        if dup_paths:
            reasons.append(CHURN_REASON_REPEATED_IDENTICAL_LS_LISTING)
            details.append(
                "Identical ls listing repeated (no new filenames; often shadow/cache replay): "
                + ", ".join(repr(p) for p in dup_paths[:3])
            )
            suggested = TOOL_SEARCH_CODE if explore_task_class == EXPLORE_CLASS_SYMBOL_LOOKUP else TOOL_READ_FILE

    # --- Check 1: repeated ls on same directory --------------------------------
    ls_path_counts: Dict[str, int] = {}
    for name, args, ok in calls:
        if name == "ls":
            p = str(args.get("path") or ".").strip().replace("\\", "/").lower()
            ls_path_counts[p] = ls_path_counts.get(p, 0) + 1

    threshold = effective_ls_churn_threshold(explore_task_class)
    repeated_dirs = {p: cnt for p, cnt in ls_path_counts.items() if cnt >= threshold}
    repeated_ls_count = sum(repeated_dirs.values()) if repeated_dirs else 0

    if repeated_dirs:
        reasons.append(CHURN_REASON_REPEATED_LS_SAME_DIR)
        dirs_str = ", ".join(f"'{p}'×{c}" for p, c in list(repeated_dirs.items())[:3])
        details.append(f"Repeated ls: {dirs_str}")
        suggested = TOOL_SEARCH_CODE  # switch to search instead of ls

    # --- Check 2: repeated file-not-found on same paths -----------------------
    missing_path_counts: Dict[str, int] = {}
    for name, args, ok in calls:
        if not ok and name in ("ls", "read_file", TOOL_SEARCH_CODE):
            p = str(args.get("path") or args.get("query") or "").strip().replace("\\", "/").lower()
            if p:
                missing_path_counts[p] = missing_path_counts.get(p, 0) + 1

    repeated_missing = [p for p, c in missing_path_counts.items() if c >= 2]
    if repeated_missing:
        reasons.append(CHURN_REASON_REPEATED_MISSING_PATH)
        details.append(f"Repeated missing paths: {', '.join(repr(p) for p in repeated_missing[:3])}")
        suggested = suggested or TOOL_SEARCH_CODE

    # --- Check 3: many steps since a new read_file path -----------------------
    read_paths_seen: set[str] = set()
    steps_since_new = 0
    for name, args, ok in calls:
        if name == "read_file" and ok:
            p = str(args.get("path") or "").lower()
            if p not in read_paths_seen:
                read_paths_seen.add(p)
                steps_since_new = 0
                continue
        # increment if not a productive read
        if name in ("ls", "read_file", TOOL_SEARCH_CODE, "summarize_repo"):
            steps_since_new += 1

    no_evidence_threshold = churn_no_evidence_steps()
    if steps_since_new >= no_evidence_threshold:
        reasons.append(CHURN_REASON_NO_NEW_EVIDENCE)
        details.append(f"No new read_file hit in last {steps_since_new} explore steps")
        suggested = suggested or TOOL_SEARCH_CODE

    # --- Check 4: only ls/summarize, zero real reads in last N calls -----------
    last_n = min(8, max(4, churn_ls_repeat_threshold() + 1))
    tail = calls[-last_n:] if len(calls) >= last_n else calls
    has_real_reads = any(name == "read_file" and ok for name, args, ok in tail)
    only_listing = all(name in ("ls", "summarize_repo") for name, _, _ in tail) if tail else False
    if only_listing and not has_real_reads and len(tail) >= max(4, churn_ls_repeat_threshold()):
        reasons.append(CHURN_REASON_ONLY_SHADOW_LS)
        details.append(
            f"Last {len(tail)} actions were only ls/summarize — no file reads (low-value listing loop)"
        )
        suggested = suggested or (
            TOOL_SEARCH_CODE if explore_task_class == EXPLORE_CLASS_SYMBOL_LOOKUP else TOOL_READ_FILE
        )

    detected = bool(reasons)
    return ChurnResult(
        detected=detected,
        reasons=reasons,
        churn_detail="; ".join(details),
        steps_since_new_evidence=steps_since_new,
        repeated_ls_count=repeated_ls_count,
        repeated_missing_paths=repeated_missing[:6],
        suggested_next_tool=suggested,
    )


# ── Unproductive explore abort ────────────────────────────────────────────────

def should_abort_explore_as_unproductive(
    history: List[Dict[str, Any]],
    session: Optional[Any] = None,
    repo_mismatch_detected: bool = False,
    explore_task_class: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Return (should_abort, reason_message) for cases where continuing EXPLORE
    cannot produce new value — i.e., before hitting global max_iterations.

    This is distinct from StagnationDetector (which uses action/target pairs);
    this function focuses on semantic evidence churn and known-missing targets.

    Returns
    -------
    (True, reason) if EXPLORE should be closed early.
    (False, "") if exploration may continue.
    """
    if not explore_v2_enabled():
        return False, ""

    # Fast-path: repo mismatch is already known — abort immediately
    if repo_mismatch_detected:
        return True, "repo_mismatch_detected"

    churn = detect_exploration_churn(history, session, explore_task_class=explore_task_class)
    if not churn.detected:
        return False, ""

    if CHURN_REASON_REPEATED_SAME_TOOL_INVOCATION in churn.reasons:
        return True, f"exploration_churn_same_invocation: {churn.churn_detail}"

    # Symbol lookup + repeated ls alone is enough to abort (search-first policy)
    if (
        explore_task_class == EXPLORE_CLASS_SYMBOL_LOOKUP
        and symbol_search_first_strict_enabled()
        and CHURN_REASON_REPEATED_LS_SAME_DIR in churn.reasons
        and len(churn.reasons) >= 1
    ):
        return True, f"symbol_task_ls_loop: {churn.churn_detail}"

    # Same directory listing twice with zero new filenames (incl. shadow replay) → stop early
    if CHURN_REASON_REPEATED_IDENTICAL_LS_LISTING in churn.reasons:
        return True, f"exploration_churn_identical_ls: {churn.churn_detail}"

    # Need at least 2 churn signals to abort (single signal → strategy nudge first)
    if len(churn.reasons) >= 2:
        return True, f"exploration_churn: {churn.churn_detail}"

    # Single signal but enough tool volume → abort without waiting for max_iterations
    calls = _iter_tool_calls_from_history(history)
    if len(calls) >= churn_single_signal_abort_min_calls() and len(churn.reasons) == 1:
        return True, f"exploration_churn_single_signal_volume: {churn.churn_detail}"

    if len(calls) >= churn_no_evidence_steps() * 2 and churn.detected:
        return True, f"exploration_churn_prolonged: {churn.churn_detail}"

    return False, ""


def churn_nudge_message(churn: ChurnResult) -> str:
    """
    Returns a [SYSTEM] nudge message to inject into the conversation when churn
    is detected but not yet severe enough to abort.
    """
    if not churn.detected:
        return ""
    suggested = churn.suggested_next_tool or TOOL_SEARCH_CODE
    lines = [
        "[SYSTEM EXPLORE v2] Exploration churn detected:",
        churn.churn_detail or "(repeated low-value tool calls)",
        f"Strategy: switch to `{suggested}` instead of repeating the same tool.",
    ]
    if CHURN_REASON_REPEATED_LS_SAME_DIR in churn.reasons:
        lines.append("Do NOT run ls on directories you have already listed.")
    if CHURN_REASON_REPEATED_IDENTICAL_LS_LISTING in churn.reasons:
        lines.append(
            "You repeated ls and received the same file list again (runtime may inject a cached/shadow listing). "
            "That adds no information — use read_file, search_code, or synthesize."
        )
    if CHURN_REASON_ONLY_SHADOW_LS in churn.reasons:
        lines.append("Break the ls-only pattern: read a specific file or use search_code before listing again.")
    if CHURN_REASON_REPEATED_MISSING_PATH in churn.reasons:
        lines.append(
            "Paths: " + ", ".join(repr(p) for p in churn.repeated_missing_paths[:3])
            + " are not found.  Accept it and synthesize from what you know."
        )
    if CHURN_REASON_REPEATED_SAME_TOOL_INVOCATION in churn.reasons:
        lines.append(
            "You invoked the same tool with the same arguments repeatedly — change strategy "
            "(search_code, read_file, or explain) instead of repeating."
        )
    return " ".join(lines)


# ── Graceful closure message for mismatch / unproductive explore ──────────────

def build_graceful_blocked_closure_message(
    reason: str,
    task_text: str,
    repo_mismatch_message: str = "",
) -> str:
    """
    Construct a clear operator-facing explanation for INFORMATIVE_BLOCKED style
    closures, suitable for the assistant text in the CLOSING phase.
    """
    parts: List[str] = []

    if "repo_mismatch" in reason:
        if repo_mismatch_message:
            parts.append(repo_mismatch_message)
        else:
            parts.append(
                "The requested path or symbol does not exist in the current repository."
            )
        parts.append(
            "If you meant a different repository, open it and retry the same question."
        )
    elif "symbol_task_ls_loop" in reason:
        parts.append(
            "Exploration stopped: this was a symbol/definition question but the model repeated directory "
            "listing instead of using search_code(mode=symbol). Listing folders does not find function or class names."
        )
        parts.append(
            "Tell the operator whether the symbol was found; if not, say it was not found in this repository "
            "and suggest verifying the repo or spelling."
        )
    elif "identical_ls" in reason:
        parts.append(
            "Exploration stopped: directory listings were repeated with no new filenames "
            "(including cached/shadow replays of the same ls result)."
        )
        parts.append(
            "Summarize what is already known from prior tool output, use read_file or search_code on a concrete target, "
            "or explain if the task cannot proceed."
        )
    elif "same_invocation" in reason:
        parts.append(
            "Exploration stopped: the same tool was invoked repeatedly with identical arguments "
            "(no new information per call)."
        )
        parts.append(
            "Summarize what you already know, switch to search_code or read_file on a concrete path, "
            "or explain why the task cannot proceed in this workspace."
        )
    elif "churn" in reason:
        parts.append(
            "Exploration stopped because repeated tool calls produced no new evidence."
        )
        parts.append(
            "This typically means the target path or symbol does not exist in the current repo, "
            "or the task requires opening a different repository."
        )
    else:
        parts.append(
            "Exploration was unproductive and was stopped to avoid wasting the iteration budget."
        )

    if task_text:
        short_task = (task_text[:120] + "…") if len(task_text) > 120 else task_text
        parts.append(f'Original task: "{short_task}"')

    return "  ".join(parts)
