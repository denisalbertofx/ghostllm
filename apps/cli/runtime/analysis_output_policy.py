"""
Enforced editorial policy for /plan, analysis, and review (read-only).

Post-processes model text: strip implementation CTAs, align language with evidence tier,
dedupe grounding notices, and optionally compact speculative “fix plans”.
"""
from __future__ import annotations

import re
from typing import Any, List

# --- Implementation CTAs (forbidden in read-only editorial mode) -----------------

_EXEC_TOUCH = (
    "fix",
    "patch",
    "correcciones",
    "corrección",
    "implement",
    "implementar",
    "aplicar el cambio",
    "aplicar los cambios",
    "aplicar la corrección",
    "aplicar las correcciones",
    "deploy",
    "merge",
    "pull request",
    "pr ",
)

_EXEC_VERB = (
    "proceder",
    "proceda",
    "procede",
    "aplicar",
    "arreglar",
    "arreglo",
    "corregir",
    "corrige",
    "implementar",
    "hago ",
    "hacer el fix",
    "apply ",
    "go ahead",
)


def _paragraphs(text: str) -> List[str]:
    if not (text or "").strip():
        return []
    parts = re.split(r"\n{2,}", text.rstrip())
    return [p for p in parts if p.strip()]


def _is_implementation_cta_paragraph(p: str) -> bool:
    """
    True for closings that ask permission to execute fixes (read-only must not end here).
    Conservative: requires both an execution touch and a question / “puedo …” offer.
    """
    s = p.strip()
    if not s:
        return False
    low = s.lower()
    touch = any(t in low for t in _EXEC_TOUCH)
    verb = any(t in low for t in _EXEC_VERB)
    question = "?" in s or "¿" in s
    offer = low.startswith("puedo ") or "puedo aplicar" in low or "puedo proceder" in low or "puedo corregir" in low
    if touch and verb and (question or offer):
        return True
    if question and touch and any(
        x in low for x in ("desea que", "quieres que", "quiere que", "te gustaría", "would you like", "want me to", "shall i")
    ):
        return True
    return False


def strip_trailing_implementation_ctas(text: str) -> str:
    """Drop trailing paragraphs that are execution CTAs (enforced, not prompt-only)."""
    if not (text or "").strip():
        return text
    paras = _paragraphs(text)
    if not paras:
        return text.rstrip()
    while paras and _is_implementation_cta_paragraph(paras[-1]):
        paras.pop()
    if not paras:
        return text.rstrip()
    return "\n\n".join(paras).rstrip()


# Phrases too strong for suspected / unverified without executed checks
_TIER_STRONG_ES = (
    (re.compile(r"(?i)\bbug\s+confirmado\b"), "posible bug (hipótesis; no confirmado por checks en esta sesión)"),
    (re.compile(r"(?i)\bbugs?\s+confirmados?\b"), "posibles bugs (hipótesis)"),
    (re.compile(r"(?i)\bestá\s+confirmado\b"), "no está confirmado con evidencia dura en esta sesión"),
    (re.compile(r"(?i)\bconfirmado\s+que\b"), "hipótesis de que"),
    (re.compile(r"(?i)\bimpacto\s+crítico\b"), "posible impacto grave (hipótesis)"),
    (re.compile(r"(?i)\bimpacto\s+critical\b"), "possible serious impact (hypothesis)"),
    (re.compile(r"(?i)\bbloquea\s+todo\b"), "podría afectar de forma amplia (hipótesis)"),
    (re.compile(r"(?i)\bbloquea\s+el\s+servicio\b"), "podría afectar el servicio (hipótesis)"),
    (re.compile(r"(?i)\bfallará\b"), "podría fallar (hipótesis)"),
    (re.compile(r"(?i)\bva\s+a\s+fallar\b"), "podría fallar (hipótesis)"),
    (re.compile(r"(?i)\bwill\s+fail\b"), "may fail (hypothesis)"),
    (re.compile(r"(?i)\bseguro\s+que\s+falla\b"), "podría fallar (hipótesis)"),
)

_GROUNDING_NOTE_PRIMARY = (
    "No cito el fragmento porque no quedó respaldado literalmente por una lectura válida en esta sesión."
)


def dedupe_grounding_prose(text: str) -> str:
    """Keep a single primary grounding note; strip legacy placeholder prose."""
    if not text:
        return text
    t = re.sub(
        r"(?is)\*\(fragmento de código omitido[^\)]*\)\*",
        "",
        text,
    )
    note = _GROUNDING_NOTE_PRIMARY
    first = t.find(note)
    if first != -1:
        tail = t[first + len(note) :]
        if note in tail:
            tail = tail.replace(note, "").strip()
            t = t[: first + len(note)] + ("\n\n" + tail if tail else "")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def soften_tier_language(text: str, tier: str) -> str:
    if tier not in ("suspected", "unverified") or not text:
        return text
    t = text
    for rx, repl in _TIER_STRONG_ES:
        t = rx.sub(repl, t)
    return t


_MARKDOWN_TABLE_LINE = re.compile(r"^\s*\|.+\|\s*$")


def _trim_speculative_fix_tables(text: str, *, tier: str, had_grounding_loss: bool) -> str:
    """
    When unconfirmed, drop long markdown “fix matrix” tables (common hallucination vehicle).
    """
    if tier not in ("suspected", "unverified") or not had_grounding_loss:
        return text
    lines = text.splitlines()
    if len(lines) < 8:
        return text
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _MARKDOWN_TABLE_LINE.match(line) and i + 1 < len(lines) and _MARKDOWN_TABLE_LINE.match(lines[i + 1]):
            start = i
            j = i
            while j < len(lines) and _MARKDOWN_TABLE_LINE.match(lines[j]):
                j += 1
            table_height = j - start
            if table_height >= 4:
                out.append(
                    "*[Tabla de plan de correcciones omitida: el hallazgo no está confirmado con evidencia dura "
                    "en esta sesión.]*"
                )
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _dedupe_similar_bug_sections(text: str, *, tier: str, had_grounding_loss: bool) -> str:
    """Cap long lists of ## sections when verification is weakest and grounding failed."""
    if tier != "unverified" or not had_grounding_loss:
        return text
    lines = text.splitlines()
    heading_idxs = [i for i, ln in enumerate(lines) if re.match(r"^#{1,3}\s+\S", ln)]
    if len(heading_idxs) <= 5:
        return text
    cut = heading_idxs[4]
    trimmed = "\n".join(lines[:cut]).rstrip()
    return (
        trimmed
        + "\n\n*[Se omitieron secciones adicionales: inspección superficial y citas no verificadas — "
        "prioriza validar lo anterior antes de acumular más hallazgos.]*"
    )


def _compact_unverified_wall(text: str, *, tier: str, had_grounding_loss: bool) -> str:
    if tier != "unverified" or not had_grounding_loss:
        return text
    max_chars = 4200
    if len(text) <= max_chars:
        return text
    head = text[:max_chars].rstrip()
    return (
        head
        + "\n\n*[Respuesta recortada: varias citas no quedaron verificadas; prioriza validar la hipótesis inicial "
        "con un read_file o un check mínimo antes de un plan de correcciones.]*"
    )


def finalize_readonly_editorial_reply(
    text: str,
    *,
    tier: str,
    grounding: Any,
    cli_mode: str = "",
) -> str:
    """
    Final pass on assistant-visible text for read-only harness (Plan / analysis / review).

    ``grounding`` is a GroundingEnforcementResult (duck-typed: fenced_replaced, inline_replaced).
    """
    if not (text or "").strip():
        return text
    tnorm = (tier or "").strip().lower()
    fenced = int(getattr(grounding, "fenced_replaced", 0) or 0)
    inline = int(getattr(grounding, "inline_replaced", 0) or 0)
    had_loss = fenced + inline > 0

    out = dedupe_grounding_prose(text)
    out = strip_trailing_implementation_ctas(out)
    out = soften_tier_language(out, tnorm)
    out = _trim_speculative_fix_tables(out, tier=tnorm, had_grounding_loss=had_loss)
    out = _dedupe_similar_bug_sections(out, tier=tnorm, had_grounding_loss=had_loss)
    out = _compact_unverified_wall(out, tier=tnorm, had_grounding_loss=had_loss)

    m = (cli_mode or "").strip().lower()
    if m == "plan" and tnorm != "confirmed" and had_loss:
        note = (
            "**Modo /plan (revisión):** no hay bug confirmado por evidencia dura en esta salida; "
            "trata los puntos como hipótesis hasta un check o cita literal."
        )
        if note.lower() not in out.lower():
            out = note + "\n\n" + out
    return out.rstrip()
