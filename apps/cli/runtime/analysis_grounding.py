"""
Programmatic read-only grounding: ledger from read_file + conservative snippet/claim checks.

Only for analysis/review/plan-style sessions — not Execute/Patch/Fix or must_write implementation.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apps.cli.runtime.artifacts import append_compacted_session_event

_MAX_LEDGER_FILES = 16
_MAX_STORED_CHARS_PER_READ = 6000
_MIN_FENCED_BODY = 14
_MIN_INLINE_CHARS = 24
_SQL_INJECTION_RE = re.compile(r"(?i)\b(sql\s+injection|inyecci.n\s+sql)\b")
_CONCURRENCY_RE = re.compile(
    r"(?i)\b(concurrenc(?:y|ia)|race\s+condition|condici[oó]n\s+de\s+carrera|multihilo|multi-?thread)\b"
)
_STRONG_CLAIM_RE = re.compile(
    r"(?i)(bug\s+cr[ií]tico|bug\s+critical|fallar[áa]\b|will\s+fail\b|"
    r"NameError\b|SyntaxError\b|TypeError\b|AttributeError\b|"
    r"l[ií]nea\s+\d+\b|line\s+\d+\b)"
)
_CODEISH_INLINE_RE = re.compile(
    r"[={}();`\[\]]|def\s+\w+|class\s+\w+|import\s+\w+|->|::|=>|\w+\.\w+\("
)


def readonly_analysis_grounding_enabled(session: Any, cli_mode: str) -> bool:
    if os.getenv("GHOST_ANALYSIS_GROUNDING", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    m = (cli_mode or "").strip().lower()
    if m in ("execute", "patch", "fix"):
        return False
    if m == "plan":
        return True
    if session is None:
        return False
    tc = getattr(session, "task_contract", None)
    spec_body: Dict[str, Any] = {}
    if isinstance(tc, dict):
        sp = tc.get("spec")
        if isinstance(sp, dict):
            spec_body = sp
    intent = str(spec_body.get("intent") or getattr(session, "task_intent", "") or "").strip().lower()
    ce = str(spec_body.get("change_expectation") or getattr(session, "change_expectation", "") or "").strip().lower()
    if intent in ("implementation", "modification", "bugfix") and ce in ("must_write", "may_write"):
        return False
    if intent in ("analysis", "review"):
        return True
    if ce == "should_not_write":
        return True
    if m == "plan":
        return True
    return False


def _window_excerpts(content: str) -> List[str]:
    c = content.replace("\r\n", "\n")
    if not c.strip():
        return []
    if len(c) <= _MAX_STORED_CHARS_PER_READ:
        return [c]
    n = _MAX_STORED_CHARS_PER_READ // 3
    mid = len(c) // 2
    return [c[:n], c[mid - n // 2 : mid + n // 2], c[-n:]]


def record_read_file_ledger(session: Any, path: str, content: str) -> None:
    if session is None or not isinstance(content, str) or not content.strip():
        return
    ledger = getattr(session, "read_grounding_ledger", None)
    if not isinstance(ledger, list):
        session.read_grounding_ledger = []
        ledger = session.read_grounding_ledger
    p = str(path or "unknown").strip() or "unknown"
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:12]
    for e in ledger:
        if isinstance(e, dict) and e.get("path") == p and e.get("sha12") == digest:
            return
    ledger.append(
        {
            "path": p,
            "sha12": digest,
            "windows": _window_excerpts(content),
            "total_len": len(content),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
    )
    while len(ledger) > _MAX_LEDGER_FILES:
        ledger.pop(0)


def _ledger_corpus(ledger: List[Any]) -> str:
    parts: List[str] = []
    for e in ledger:
        if not isinstance(e, dict):
            continue
        for w in e.get("windows") or []:
            if isinstance(w, str) and w:
                parts.append(w)
    return "\n".join(parts)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def snippet_is_grounded(snippet: str, corpus: str) -> bool:
    s = (snippet or "").strip()
    if len(s) < _MIN_FENCED_BODY:
        return True
    if not corpus.strip():
        return False
    if s in corpus:
        return True
    if len(s) >= 28 and _norm_ws(s) in _norm_ws(corpus):
        return True
    return False


def _looks_codeish(s: str) -> bool:
    t = s.strip()
    if len(t) < _MIN_INLINE_CHARS:
        return False
    return bool(_CODEISH_INLINE_RE.search(t))


# Prefer ```lang\nbody```; then ```...``` when body spans lines (avoids matching inline ```x```).
_FENCE_RE_LANG = re.compile(r"```(?:[a-zA-Z0-9_.+-]*)\s*\r?\n(.*?)```", re.DOTALL)
_FENCE_RE_PLAIN = re.compile(r"```(?![a-zA-Z0-9_.+-]+\s*\r?\n)(.*?)```", re.DOTALL)

# Plain prose (no fenced placeholder): first ungrounded block gets one honest sentence; further blocks drop silently.
_UNGROUNDED_SNIPPET_NOTE = (
    "No cito el fragmento porque no quedó respaldado literalmente por una lectura válida en esta sesión."
)


def _replace_fenced_blocks(text: str, corpus: str) -> Tuple[str, int]:
    count = 0

    def repl_lang(m: re.Match[str]) -> str:
        nonlocal count
        body = m.group(1)
        if len(body.strip()) < _MIN_FENCED_BODY:
            return m.group(0)
        if snippet_is_grounded(body, corpus):
            return m.group(0)
        count += 1
        if count == 1:
            return "\n\n" + _UNGROUNDED_SNIPPET_NOTE + "\n\n"
        return "\n\n"

    out = _FENCE_RE_LANG.sub(repl_lang, text)

    def repl_plain(m: re.Match[str]) -> str:
        nonlocal count
        body = m.group(1)
        if "\n" not in body:
            return m.group(0)
        if len(body.strip()) < _MIN_FENCED_BODY:
            return m.group(0)
        if snippet_is_grounded(body, corpus):
            return m.group(0)
        count += 1
        if count == 1:
            return "\n\n" + _UNGROUNDED_SNIPPET_NOTE + "\n\n"
        return "\n\n"

    out = _FENCE_RE_PLAIN.sub(repl_plain, out)
    return out, count


_INLINE_TICK_RE = re.compile(r"`([^`\n]+)`")


def _replace_inline_ticks(text: str, corpus: str) -> Tuple[str, int]:
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        inner = m.group(1)
        if len(inner) < _MIN_INLINE_CHARS or not _looks_codeish(inner):
            return m.group(0)
        if snippet_is_grounded(inner, corpus):
            return m.group(0)
        count += 1
        # Sin backticks: evita “código” falso; primera sustitución deja marca breve, el resto colapsa.
        if count == 1:
            return "⟨cita no verificada en lecturas de esta sesión⟩"
        return "⟨⋯⟩"

    return _INLINE_TICK_RE.sub(repl, text), count


def _soften_strong_claims(text: str, weak_evidence: bool) -> Tuple[str, bool]:
    if not weak_evidence or not text:
        return text, False
    if not _STRONG_CLAIM_RE.search(text):
        return text, False
    softened = text
    softened = re.sub(r"(?i)bug\s+cr[ií]tico", "posible problema grave (no verificado automáticamente)", softened)
    softened = re.sub(r"(?i)bug\s+critical", "possible serious issue (not auto-verified)", softened)
    softened = re.sub(
        r"(?i)\b(NameError|SyntaxError|TypeError|AttributeError)\b",
        r"posible \1 (no confirmada por salida de herramientas)",
        softened,
    )
    softened = re.sub(r"(?i)\bfallar[áa]\b", "podría fallar (hipótesis)", softened)
    softened = re.sub(r"(?i)\bwill\s+fail\b", "may fail (hypothesis)", softened)
    softened = re.sub(
        r"(?i)(l[ií]nea\s+)(\d+)(\b)",
        r"línea ~\2 (referencia no verificada por lectura)",
        softened,
    )
    softened = re.sub(
        r"(?i)\b(line\s+)(\d+)(\b)",
        r"line ~\2 (unverified reference)",
        softened,
    )
    return softened, softened != text


def _soften_speculative_categories(text: str, corpus: str) -> Tuple[str, List[Dict[str, Any]]]:
    if not text:
        return text, []
    out = text
    events: List[Dict[str, Any]] = []
    corpus_low = str(corpus or "").lower()

    has_unsafe_sql_signal = bool(
        re.search(r"(select|insert|update|delete).*(\+|%|format\(|f\"|f')", corpus_low, re.DOTALL)
    )
    if _SQL_INJECTION_RE.search(out) and not has_unsafe_sql_signal:
        out = _SQL_INJECTION_RE.sub(
            "riesgo SQL no confirmado por evidencia literal (no se observó concatenación insegura)",
            out,
        )
        events.append({"action": "soften_sql_injection_without_literal_signal"})

    has_concurrency_signal = any(
        token in corpus_low
        for token in ("thread", "threading", "async ", "await ", "lock", "multiprocessing", "concurrent")
    )
    if _CONCURRENCY_RE.search(out) and not has_concurrency_signal:
        out = _CONCURRENCY_RE.sub(
            "riesgo concurrente no confirmado por lectura literal",
            out,
        )
        events.append({"action": "soften_concurrency_without_literal_signal"})

    return out, events


def _ledger_weak_evidence(ledger: List[Any]) -> bool:
    total = 0
    for e in ledger:
        if isinstance(e, dict):
            total += int(e.get("total_len") or 0)
    return total < 800


@dataclass
class GroundingEnforcementResult:
    text: str
    events: List[Dict[str, Any]] = field(default_factory=list)
    fenced_replaced: int = 0
    inline_replaced: int = 0
    claims_softened: bool = False


def enforce_readonly_assistant_message(text: str, session: Any) -> GroundingEnforcementResult:
    if not text or not isinstance(text, str):
        return GroundingEnforcementResult(text=text)
    ledger = getattr(session, "read_grounding_ledger", None) if session else None
    if not isinstance(ledger, list):
        ledger = []
    corpus = _ledger_corpus(ledger)
    weak = _ledger_weak_evidence(ledger)
    events: List[Dict[str, Any]] = []

    t, fc = _replace_fenced_blocks(text, corpus)
    t, ic = _replace_inline_ticks(t, corpus)
    t, cs = _soften_strong_claims(t, weak_evidence=weak or not corpus.strip())
    t, speculative_events = _soften_speculative_categories(t, corpus)
    t = re.sub(r"(?m)^\s*```\s*$", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()

    ts = datetime.now().isoformat(timespec="seconds")
    if fc:
        events.append(
            {
                "event": "analysis_grounding",
                "action": "fenced_snippet_ungrounded",
                "count": fc,
                "ts": ts,
            }
        )
    if ic:
        events.append(
            {
                "event": "analysis_grounding",
                "action": "inline_snippet_ungrounded",
                "count": ic,
                "ts": ts,
            }
        )
    if cs:
        events.append(
            {
                "event": "analysis_grounding",
                "action": "strong_claim_softened",
                "ts": ts,
            }
        )
    for evt in speculative_events:
        events.append(
            {
                "event": "analysis_grounding",
                "action": evt["action"],
                "ts": ts,
            }
        )

    if events and session is not None:
        digest_parts = [f"{e.get('action')}:{e.get('count', 1)}" for e in events]
        new_d = ";".join(digest_parts)[:240]
        prev = str(getattr(session, "analysis_grounding_digest", "") or "").strip()
        if prev:
            session.analysis_grounding_digest = (prev + " | " + new_d)[:400]
        else:
            session.analysis_grounding_digest = new_d
        for e in events:
            append_compacted_session_event(session, e)

    return GroundingEnforcementResult(
        text=t,
        events=events,
        fenced_replaced=fc,
        inline_replaced=ic,
        claims_softened=cs,
    )
