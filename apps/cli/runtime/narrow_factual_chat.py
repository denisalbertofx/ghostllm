"""
Narrow factual code questions (Chat / RO-2): reserve iteration budget for final synthesis.

Detects small-scope prompts (what does function X do, where defined, which file) and:
- nudges synthesis when tool evidence already contains the symbol;
- injects pressure nudges when the iteration budget is almost exhausted;
- supports disabling tools on the last iteration when exploration already ran.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple


def narrow_factual_synthesis_enabled() -> bool:
    v = os.getenv("GHOST_NARROW_FACTUAL_SYNTHESIS", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def synthesis_reserve_iterations() -> int:
    try:
        n = int(os.getenv("GHOST_SYNTHESIS_RESERVE_ITERATIONS", "2"))
    except ValueError:
        n = 2
    return max(0, min(n, 8))


def suppress_tools_on_last_factual_iteration() -> bool:
    v = os.getenv("GHOST_FACTUAL_LAST_ITER_NO_TOOLS", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


# Broad tasks — do not apply narrow-factual policy
_BROAD_REJECT = re.compile(
    r"(implementa|implement|refactor|escribe\s+el\s+c[oó]digo|write\s+the\s+code|"
    r"todos\s+los|every\s+file|migrate|add\s+feature|crea\s+un\s+nuevo|"
    r"tests?\s+for|unit\s+test|integrat)",
    re.I,
)

_WHAT_DOES = re.compile(
    r"(?i)qu[eé]\s+hace\s+(?:la\s+)?(?:funci[oó]n|function)\s+([a-zA-Z_][a-zA-Z0-9_]{1,80})\b",
)
_WHAT_DOES_EN = re.compile(
    r"(?i)what\s+does\s+(?:the\s+)?(?:function|method)\s+([a-zA-Z_][a-zA-Z0-9_]{1,80})\s+do\b",
)
_WHERE_FILE = re.compile(
    r"(?i)en\s+qu[eé]\s+archivo\s+(?:est[aá]\s+)?(?:la\s+)?(?:funci[oó]n|function)?\s*"
    r"([a-zA-Z_][a-zA-Z0-9_]{1,80})\b",
)
_WHERE_DEFINED = re.compile(
    r"(?i)(?:d[oó]nde\s+est[aá]\s+definid[ao]|where\s+is)\s+(?:la\s+)?(?:funci[oó]n|function)?\s*"
    r"([a-zA-Z_][a-zA-Z0-9_]{1,80})\b",
)
_WHICH_FILE = re.compile(
    r"(?i)which\s+file\s+(?:contains|has)\s+[`\"]?([a-zA-Z_][a-zA-Z0-9_]{1,80})[`\"]?\b",
)
# RO-2: "¿Qué hace la función X y en qué archivo está?" (symbol only before "y")
_COMPOUND_WHAT_AND_FILE = re.compile(
    r"(?i)qu[eé]\s+hace\s+(?:la\s+)?(?:funci[oó]n|function)\s+([a-zA-Z_][a-zA-Z0-9_]{1,80})\b"
    r".{0,160}?\ben\s+qu[eé]\s+archivo",
)


@dataclass(frozen=True)
class NarrowFactualScope:
    """Symbols the user asked about (typically one; multiple for compound questions)."""

    symbols: Tuple[str, ...]
    kinds: FrozenSet[str]


def detect_narrow_factual_code_question(task_text: str) -> Optional[NarrowFactualScope]:
    """
    RO-2 style: short factual questions about named functions / files.

    Returns None if the prompt looks like implementation work or is too long.
    """
    if not task_text or not isinstance(task_text, str):
        return None
    t = task_text.strip()
    if len(t) > 2500:
        return None
    if _BROAD_REJECT.search(t):
        return None

    symbols: Set[str] = set()
    kinds: Set[str] = set()
    for rx, kind in (
        (_WHAT_DOES, "what_does"),
        (_WHAT_DOES_EN, "what_does"),
        (_WHERE_FILE, "where_file"),
        (_WHERE_DEFINED, "where_defined"),
        (_WHICH_FILE, "which_file"),
    ):
        for m in rx.finditer(t):
            sym = m.group(1).strip()
            if len(sym) >= 2:
                symbols.add(sym)
                kinds.add(kind)

    for m in _COMPOUND_WHAT_AND_FILE.finditer(t):
        sym = m.group(1).strip()
        if len(sym) >= 2:
            symbols.add(sym)
            kinds.add("what_does")
            kinds.add("where_file")

    if not symbols:
        return None
    ordered = tuple(sorted(symbols, key=len, reverse=True))
    return NarrowFactualScope(symbols=ordered, kinds=frozenset(kinds))


def _parse_tool_json_content(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            o = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return o if isinstance(o, dict) else None
    return None


def read_file_texts_from_history(history: List[Dict[str, Any]]) -> List[str]:
    """Successful read_file tool bodies (content field)."""
    out: List[str] = []
    for m in history:
        if m.get("role") != "tool" or m.get("name") != "read_file":
            continue
        payload = _parse_tool_json_content(m.get("content"))
        if not payload or payload.get("error"):
            continue
        c = payload.get("content")
        if isinstance(c, str) and c.strip():
            out.append(c)
    return out


def content_defines_symbol(content: str, symbol: str) -> bool:
    if not content or not symbol:
        return False
    sym = re.escape(symbol)
    return bool(
        re.search(rf"\b(?:async\s+)?def\s+{sym}\b", content)
        or re.search(rf"\bclass\s+{sym}\b", content)
    )


def evidence_sufficient_for_narrow_factual(scope: NarrowFactualScope, history: List[Dict[str, Any]]) -> bool:
    """True if some read_file payload already defines or clearly names the symbol."""
    texts = read_file_texts_from_history(history)
    if not texts:
        return False
    combined = "\n".join(texts)
    for sym in scope.symbols:
        if content_defines_symbol(combined, sym):
            return True
    return False


def factual_explore_iterations_remaining(iterations: int, max_iter: int) -> int:
    """Iterations left including the current one (iterations is 1-based loop counter)."""
    return max(0, max_iter - iterations)
