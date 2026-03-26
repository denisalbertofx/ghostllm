"""
Repo mismatch detection for Exploration Engine v2.

Provides an early-intake / early-explore signal when the user's task
references paths or concepts that strongly suggest a *different* repository
than the one currently active under ``cwd``.

Integration points
------------------
- Called from INTAKE (``assistant._run_task_contract_intake``) and on the first
  EXPLORE turn (``assistant._explore_v2_precheck``), the latter with chat history.
- Result is written to ArtifactSession.repo_mismatch_* fields.
- When detected, _phase_explore can close gracefully instead of looping.

Machine-readable constants
--------------------------
MISMATCH_REASON_GHOST_PATHS_IN_NON_GHOST_REPO
MISMATCH_REASON_REQUESTED_PATHS_ABSENT
MISMATCH_REASON_STACK_INCONSISTENCY
MISMATCH_REASON_REPEATED_FILE_NOT_FOUND
MISMATCH_REASON_WRONG_REPO_STRUCTURE

All reason codes are stable snake_case strings (do not rename without docs).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from apps.cli.runtime.session_trace import (
    paths_outside_repo_from_task,
    wrong_repo_note_from_task,
)

# ── Reason codes ──────────────────────────────────────────────────────────────

MISMATCH_REASON_GHOST_PATHS_IN_NON_GHOST_REPO = "ghost_paths_in_non_ghost_repo"
MISMATCH_REASON_REQUESTED_PATHS_ABSENT = "requested_paths_absent"
MISMATCH_REASON_STACK_INCONSISTENCY = "stack_inconsistency"
MISMATCH_REASON_REPEATED_FILE_NOT_FOUND = "repeated_file_not_found"
MISMATCH_REASON_WRONG_REPO_STRUCTURE = "wrong_repo_structure_note"
MISMATCH_REASON_GHOST_PRODUCT_IN_FOREIGN_REPO = "ghost_product_question_foreign_repo"

# Threshold: if ≥ this many concrete path tokens from the task are missing, flag (unless strong path / note).
_MIN_MISSING_FOR_MISMATCH = 2

# Ghost-specific internal path markers (only meaningful inside GhostLLM repo)
# Natural-language references to the GhostLLM product / runtime (without file paths).
_GHOST_PRODUCT_PHRASES = (
    "ghostllm",
    "ghost llm",
    "ghost-cli",
    "ghost cli",
    "exploration engine v2",
    "explore_engine_v2",
    "explore engine v2",
    "codexassistant",
    "codex assistant",
    "task_contract",
    "task contract",
    "artifact_manager",
    "session_phase",
    "runtime explore",
    "ghost gateway",
)

_GHOST_INTERNAL_MARKERS = (
    "apps/cli/runtime",
    "apps/cli/assistant",
    "apps/server/api/translation",
    "packages/py-core/ghostllm_core",
    "ghostllm_core",
    "verification_diff_fingerprint",
    "task_confidence_engine",
    "session_phase",
    "explore_promotion",
    "repair_specialist",
    "taskspec",
)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class RepoMismatchResult:
    """
    Outcome of a single repo mismatch probe.

    Fields are additive to ArtifactSession (do not remove or rename).
    """
    detected: bool = False
    confidence: float = 0.0          # 0.0–1.0 heuristic confidence
    reasons: List[str] = field(default_factory=list)   # stable reason codes
    missing_paths: List[str] = field(default_factory=list)
    operator_message_es: str = ""    # human-readable Spanish explanation
    operator_message_en: str = ""

    def to_dict(self) -> dict:
        return {
            "detected": self.detected,
            "confidence": round(self.confidence, 2),
            "reasons": list(self.reasons),
            "missing_paths": list(self.missing_paths),
            "operator_message_en": self.operator_message_en,
        }


# ── Env flag ──────────────────────────────────────────────────────────────────

def repo_mismatch_detection_enabled() -> bool:
    v = os.getenv("GHOST_REPO_MISMATCH_DETECTION", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


# ── Detection helpers ─────────────────────────────────────────────────────────

def _is_ghost_repo(cwd: str) -> bool:
    """Heuristic: is the current working directory a GhostLLM repo?"""
    root = Path(cwd).resolve()
    ghost_markers = (root / "apps" / "cli", root / "apps" / "server", root / "CLAUDE.md")
    return sum(1 for m in ghost_markers if m.exists()) >= 2


def _task_references_ghost_internals(task_text: str) -> bool:
    """True if the task text references GhostLLM-internal path/symbol markers."""
    t = task_text.lower().replace("\\", "/")
    return any(m.lower() in t for m in _GHOST_INTERNAL_MARKERS)


def _task_references_ghost_product_language(task_text: str) -> bool:
    """True when the prompt clearly names GhostLLM runtime concepts (not generic 'ghost' wording)."""
    t = task_text.lower().replace("\\", "/")
    return any(p in t for p in _GHOST_PRODUCT_PHRASES)


def _collect_missing_paths(task_text: str, cwd: str) -> List[str]:
    """Concrete path-like tokens from the task that do NOT exist under cwd."""
    return paths_outside_repo_from_task(task_text, cwd)


def _is_strong_ghost_style_path(p: str) -> bool:
    """Single missing path that strongly suggests GhostLLM / Python monorepo layout."""
    x = p.replace("\\", "/").lower().strip()
    return x.startswith(
        ("apps/cli/", "apps/server/", "packages/py-core/", "packages/py_core/")
    )


def _session_stack(session: Optional[Any]) -> str:
    if session is None:
        return ""
    rp = getattr(session, "repo_profile", None) or {}
    if not isinstance(rp, dict):
        return ""
    return str(rp.get("stack") or "").strip().lower()


def _looks_like_frontend_only_repo(cwd: str) -> bool:
    """package.json present, no Python workspace markers typical of GhostLLM."""
    root = Path(cwd).resolve()
    if not (root / "package.json").is_file():
        return False
    if (root / "pyproject.toml").is_file():
        return False
    if (root / "apps" / "cli").is_dir():
        return False
    return True


def _task_implies_ghost_python_monorepo(task_text: str) -> bool:
    t = task_text.replace("\\", "/").lower()
    return any(
        m in t
        for m in (
            "apps/cli/runtime",
            "apps/cli/assistant",
            "apps/server/",
            "packages/py-core",
            "ghostllm_core",
            "verification_diff_fingerprint",
        )
    )


def _count_explore_tool_not_found_errors(history: Optional[List[Dict[str, Any]]]) -> int:
    """Tool payloads whose error looks like missing path / file (EXPLORE churn signal)."""
    if not history:
        return 0
    n = 0
    for m in history:
        if m.get("role") != "tool":
            continue
        name = str(m.get("name") or "")
        if name not in ("ls", "read_file", "search_code"):
            continue
        raw = m.get("content") or ""
        try:
            payload = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            continue
        err = str(payload.get("error") or "").lower()
        if any(
            s in err
            for s in (
                "not found",
                "no such file",
                "does not exist",
                "path not found",
                "enoent",
            )
        ):
            n += 1
    return n


def _build_operator_message(
    reasons: List[str],
    missing_paths: List[str],
    cwd: str,
) -> tuple[str, str]:
    cwd_tail = Path(cwd).name if cwd else "(desconocido)"
    parts_es: list[str] = []
    parts_en: list[str] = []

    if MISMATCH_REASON_GHOST_PATHS_IN_NON_GHOST_REPO in reasons:
        parts_es.append(
            "La pregunta hace referencia a rutas o símbolos internos de GhostLLM, "
            f"pero el repo activo parece ser '{cwd_tail}' (no GhostLLM)."
        )
        parts_en.append(
            f"The task references GhostLLM-internal paths/symbols, but the active repo appears "
            f"to be '{cwd_tail}' (not GhostLLM)."
        )

    if MISMATCH_REASON_REQUESTED_PATHS_ABSENT in reasons and missing_paths:
        listed = ", ".join(missing_paths[:4])
        parts_es.append(
            f"Las siguientes rutas no existen en este repo: {listed}. "
            "Si la tarea apunta a otro repositorio, ábrelo y reintenta."
        )
        parts_en.append(
            f"These requested paths are absent from the current repo: {listed}. "
            "If the task targets a different repository, open it first."
        )

    if MISMATCH_REASON_REPEATED_FILE_NOT_FOUND in reasons:
        parts_es.append(
            "Se detectaron múltiples errores consecutivos 'fichero no encontrado' "
            "en rutas concretas mencionadas en la tarea."
        )
        parts_en.append(
            "Multiple consecutive 'file not found' errors on paths mentioned in the task."
        )

    if MISMATCH_REASON_STACK_INCONSISTENCY in reasons:
        parts_es.append(
            "El stack del repo activo (p. ej. Next.js/Node) no encaja con rutas o módulos típicos de "
            "GhostLLM (Python: apps/cli, apps/server, packages/py-core)."
        )
        parts_en.append(
            "The active repo stack (e.g. Next.js/Node) does not match GhostLLM-style Python paths "
            "(apps/cli, apps/server, packages/py-core)."
        )

    if MISMATCH_REASON_WRONG_REPO_STRUCTURE in reasons:
        parts_es.append(
            "La tarea menciona carpetas tipo `apps/` o `packages/` que no existen en la raíz de este repo."
        )
        parts_en.append(
            "The task references `apps/` or `packages/` layouts that are not present at this repo root."
        )

    if MISMATCH_REASON_GHOST_PRODUCT_IN_FOREIGN_REPO in reasons:
        parts_es.append(
            "La pregunta parece referirse al producto o runtime GhostLLM, pero el repo abierto no coincide "
            f"(carpeta activa: {cwd_tail!r}). Lo que buscas no está en este árbol de archivos."
        )
        parts_en.append(
            "The question appears to target the GhostLLM product or runtime, but the open repository "
            f"does not match (active folder: {cwd_tail!r}). What you are asking for is not in this tree."
        )

    es = " ".join(parts_es) or "El repositorio activo puede no coincidir con el objetivo de la tarea."
    en = " ".join(parts_en) or "The active repository may not match the task's target."
    return es, en


# ── Public API ────────────────────────────────────────────────────────────────

def detect_repo_mismatch(
    task_text: str,
    cwd: str,
    session: Optional[Any] = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> RepoMismatchResult:
    """
    Probe whether the task references a different repo than ``cwd``.

    Parameters
    ----------
    task_text : the raw user task / question text.
    cwd       : the repo root being operated on.
    session   : optional ArtifactSession (repo_profile, trace_notes).
    history   : optional chat history for repeated tool "not found" signals.

    Returns
    -------
    RepoMismatchResult with detected=True when mismatch is likely.
    """
    if not repo_mismatch_detection_enabled():
        return RepoMismatchResult(detected=False)

    if not task_text or not cwd:
        return RepoMismatchResult(detected=False)

    reasons: List[str] = []
    confidence = 0.0

    # Signal 1: Ghost-internal paths asked inside non-Ghost repo
    if _task_references_ghost_internals(task_text) and not _is_ghost_repo(cwd):
        reasons.append(MISMATCH_REASON_GHOST_PATHS_IN_NON_GHOST_REPO)
        confidence += 0.7

    # Signal 1b: explicit GhostLLM product/runtime wording in a non-Ghost workspace (e.g. Next.js app folder)
    if (
        _task_references_ghost_product_language(task_text)
        and not _is_ghost_repo(cwd)
        and (
            _looks_like_frontend_only_repo(cwd)
            or not (Path(cwd).resolve() / "apps" / "cli").is_dir()
        )
    ):
        if MISMATCH_REASON_GHOST_PRODUCT_IN_FOREIGN_REPO not in reasons:
            reasons.append(MISMATCH_REASON_GHOST_PRODUCT_IN_FOREIGN_REPO)
        confidence += 0.48

    # Signal 2: concrete paths from the task are absent
    missing = _collect_missing_paths(task_text, cwd)
    strong_missing = [p for p in missing if _is_strong_ghost_style_path(p)]
    if len(missing) >= _MIN_MISSING_FOR_MISMATCH or strong_missing:
        if MISMATCH_REASON_REQUESTED_PATHS_ABSENT not in reasons:
            reasons.append(MISMATCH_REASON_REQUESTED_PATHS_ABSENT)
        confidence += min(0.5, 0.14 * max(len(missing), len(strong_missing) or 0))
        if strong_missing:
            confidence += 0.15

    # Signal 2b: structural wrong-repo hint (apps/ expected but missing under cwd)
    note = wrong_repo_note_from_task(task_text, cwd)
    if note:
        if MISMATCH_REASON_WRONG_REPO_STRUCTURE not in reasons:
            reasons.append(MISMATCH_REASON_WRONG_REPO_STRUCTURE)
        confidence += 0.52

    # Signal 3: stack / domain — JS frontend-only tree vs Ghost Python monorepo task
    stack = _session_stack(session)
    if (
        _looks_like_frontend_only_repo(cwd)
        and _task_implies_ghost_python_monorepo(task_text)
        and not _is_ghost_repo(cwd)
    ):
        if MISMATCH_REASON_STACK_INCONSISTENCY not in reasons:
            reasons.append(MISMATCH_REASON_STACK_INCONSISTENCY)
        confidence += 0.55
    elif (
        stack in ("nextjs", "node")
        and _task_implies_ghost_python_monorepo(task_text)
        and not _is_ghost_repo(cwd)
    ):
        if MISMATCH_REASON_STACK_INCONSISTENCY not in reasons:
            reasons.append(MISMATCH_REASON_STACK_INCONSISTENCY)
        confidence += 0.45

    # Signal 4: session-level repeated file-not-found (trace notes)
    if session is not None:
        trace_notes = getattr(session, "trace_notes", [])
        fnf_count = sum(
            1 for n in (trace_notes or [])
            if "file-not-found" in str(n).lower() or "path not found" in str(n).lower()
        )
        if fnf_count >= 2:
            reasons.append(MISMATCH_REASON_REPEATED_FILE_NOT_FOUND)
            confidence += 0.3

    # Signal 5: explore history — repeated tool errors (missing paths)
    hist_errs = _count_explore_tool_not_found_errors(history)
    if hist_errs >= 2:
        if MISMATCH_REASON_REPEATED_FILE_NOT_FOUND not in reasons:
            reasons.append(MISMATCH_REASON_REPEATED_FILE_NOT_FOUND)
        confidence += min(0.55, 0.2 * hist_errs)
    if hist_errs >= 3 and missing:
        confidence += 0.1

    detected = bool(reasons) and confidence >= 0.4
    confidence = min(1.0, confidence)
    missing_for_msg = missing[:8]

    if not detected:
        return RepoMismatchResult(detected=False, confidence=round(confidence, 2))

    msg_es, msg_en = _build_operator_message(reasons, missing_for_msg, cwd)
    return RepoMismatchResult(
        detected=True,
        confidence=round(confidence, 2),
        reasons=reasons,
        missing_paths=missing_for_msg,
        operator_message_es=msg_es,
        operator_message_en=msg_en,
    )


def inject_mismatch_into_session(result: RepoMismatchResult, session: Any) -> None:
    """
    Write mismatch result fields into ArtifactSession (fail-safe).
    Only overwrites if detected=True (preserves prior detection).
    """
    if session is None:
        return
    try:
        if result.detected:
            session.repo_mismatch_detected = True
            existing_reasons = list(getattr(session, "repo_mismatch_reasons", None) or [])
            for r in result.reasons:
                if r not in existing_reasons:
                    existing_reasons.append(r)
            session.repo_mismatch_reasons = existing_reasons
            session.repo_mismatch_reason = ", ".join(existing_reasons)
            existing_paths = list(getattr(session, "requested_paths_missing", None) or [])
            for p in result.missing_paths:
                if p not in existing_paths:
                    existing_paths.append(p)
            session.requested_paths_missing = existing_paths[:24]
            msg = (result.operator_message_en or result.operator_message_es or "").strip()
            if msg:
                session.repo_mismatch_operator_message = msg
        else:
            if not getattr(session, "repo_mismatch_detected", False):
                session.repo_mismatch_detected = False
    except Exception:
        pass
