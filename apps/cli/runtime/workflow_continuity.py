"""Continuidad entre sesiones: SQLite como fuente de verdad; `.ghost/` solo como salida auditiva."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Tuple

try:
    from ghostllm_core.memory import MemoryStore
except ModuleNotFoundError:
    py_core = Path(__file__).resolve().parents[3] / "packages" / "py-core"
    if str(py_core) not in sys.path:
        sys.path.insert(0, str(py_core))
    from ghostllm_core.memory import MemoryStore

_PLANS_DIR = "plans"
_HANDOFFS_DIR = "handoffs"
_REVIEW_PACKETS_DIR = "review_packets"
_INDEX_NAME = "continuity_index.json"

# Contrato handoff (schema_version 2). v1 (campo version=1) se normaliza al cargar.
HANDOFF_SCHEMA_VERSION = 2
HANDOFF_ENVELOPE_VERSION = 1

# Roles mínimos (contrato multiagente; sin orquestación aún).
ROLE_AUTHOR_DEFAULT = "ghost_model"
ROLE_OPERATOR_DEFAULT = "human_operator"
ROLE_REVIEWER_UNASSIGNED = ""


def ghost_subdir(cwd: str, name: str) -> Path:
    root = Path(cwd or ".").resolve()
    d = root / ".ghost" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _repo_root(cwd: str) -> Path:
    return Path(cwd or ".").resolve()


def _state_store(cwd: str) -> MemoryStore:
    return MemoryStore(str(_repo_root(cwd) / "ghost_memory.db"))


def _continuity_index_path(cwd: str) -> Path:
    return _repo_root(cwd) / ".ghost" / _INDEX_NAME


def _continuity_index_lock_path(cwd: str) -> Path:
    return _repo_root(cwd) / ".ghost" / f"{_INDEX_NAME}.lock"


def _session_file_tag(session_id: str) -> str:
    s = re.sub(r"[^\w.\-]+", "_", (session_id or "").strip())
    return (s[:56] or "unknown").strip("_") or "unknown"


def _clean_identifier(val: Any) -> str:
    """Evita `None`, `null`, etc. en rutas persistidas (nunca usar el literal Python ``None``)."""
    if val is None:
        return ""
    s = str(val).strip()
    if not s or s.lower() in ("none", "null", "undefined", "n/a", "na"):
        return ""
    return s


def _continuity_index_lookup_keys(task_id: str) -> List[str]:
    """Claves candidatas para `by_task_id` (espacios, aliases none/NA ya limpios)."""
    out: List[str] = []
    seen: set[str] = set()
    raw = str(task_id or "").strip()
    if raw and raw not in seen:
        out.append(raw)
        seen.add(raw)
    cl = _clean_identifier(task_id)
    if cl and cl not in seen:
        out.append(cl)
        seen.add(cl)
    return out


def _continuity_index_entry(by_t: Any, task_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(by_t, dict):
        return None
    for k in _continuity_index_lookup_keys(task_id):
        ent = by_t.get(k)
        if isinstance(ent, dict):
            return ent
    return None


def _plan_handoff_slug(*, task_id: str, session_id: str) -> str:
    tid = _clean_identifier(task_id)
    sid = _clean_identifier(session_id)
    return _safe_slug(tid or sid or "session")


def _safe_slug(s: str, max_len: int = 48) -> str:
    normalized = (s or "").strip().lower()
    raw = re.sub(r"[^\w\-]+", "_", normalized, flags=re.UNICODE)
    base = raw.strip("_") or "task"
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    head_limit = max(1, max_len - (len(digest) + 1))
    return f"{base[:head_limit]}_{digest}"


_CONTINUATION_RE = re.compile(
    r"^\s*(/(?:do|plan)\s+)?(continúa|continua|continue|siguiente\s*paso|sigue|go\s*on)\s*!?\s*$",
    re.I,
)


def is_continuation_user_message(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _CONTINUATION_RE.match(t):
        return True
    if t.lower() in ("continua", "continúa", "continue", "sigue", "siguiente paso"):
        return True
    return False


def plan_status_from_session(session: Any) -> str:
    phase = str(getattr(session, "phase", "") or "").upper()
    outcome = str(getattr(session, "task_outcome", "") or "").lower()
    if phase == "ABORTED":
        return "partial"
    if outcome in ("implemented", "already_implemented") and phase == "DONE":
        return "completed"
    if outcome in ("read_only", "no_op") and phase == "DONE":
        return "completed"
    if outcome in ("partially_implemented", "verification_failed", "blocked"):
        return "partial"
    if phase == "DONE":
        return "completed"
    return "partial"


def unwrap_handoff_raw(raw: Any) -> Any:
    """
    Si el fichero es un envelope de delegación, devuelve el payload interno para normalizar.
    """
    if not isinstance(raw, dict):
        return raw
    if raw.get("kind") == "ghost_handoff_delegation" and "handoff_payload" in raw:
        inner = raw.get("handoff_payload")
        return inner if isinstance(inner, dict) else raw
    if isinstance(raw.get("handoff_payload"), dict) and raw.get("envelope_version"):
        return raw["handoff_payload"]
    return raw


def strip_yaml_frontmatter(md: str) -> str:
    s = (md or "").strip()
    if not s.startswith("---"):
        return s
    parts = s.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return s


def normalize_handoff_document(raw: Any, *, expected_task_id: Optional[str] = None) -> Tuple[Dict[str, Any], List[str]]:
    """
    Valida y normaliza un handoff al contrato canónico (schema v2).
    Si está corrupto o vacío, devuelve {} o parcial con advertencias (degradación segura).
    """
    warnings: List[str] = []
    if raw is None:
        return {}, ["null_payload"]
    if not isinstance(raw, dict):
        return {}, ["not_object"]

    legacy_v = raw.get("schema_version")
    if legacy_v is None:
        legacy_v = raw.get("version")
    try:
        legacy_int = int(legacy_v) if legacy_v is not None else 1
    except (TypeError, ValueError):
        legacy_int = 1
        warnings.append("invalid_schema_version_coerced")

    mode = str(raw.get("mode") or raw.get("harness_mode_label") or "").strip()
    task = str(raw.get("task") or raw.get("task_text") or "").strip()[:4000]
    tid = str(raw.get("task_id") or "").strip()
    sid = str(raw.get("session_id") or "").strip()

    if expected_task_id and tid and str(expected_task_id).strip() not in ("", "N/A"):
        if tid != str(expected_task_id).strip():
            warnings.append("task_id_mismatch")
            return {}, warnings

    if not tid:
        warnings.append("missing_task_id")

    ft = raw.get("files_touched")
    if ft is None:
        files_touched: List[str] = []
    elif isinstance(ft, list):
        files_touched = [str(x).strip() for x in ft if str(x).strip()][:64]
    else:
        files_touched = []
        warnings.append("files_touched_coerced")

    lv = raw.get("last_verify")
    if lv is None or not isinstance(lv, dict):
        last_verify: Dict[str, Any] = {}
        if raw.get("last_verify") is not None:
            warnings.append("last_verify_coerced")
    else:
        last_verify = {
            "status": str(lv.get("status") or ""),
            "steps_executed_count": lv.get("steps_executed_count"),
            "check_names": [
                str(x) for x in (lv.get("check_names") or [])[:16] if isinstance(x, (str, int, float))
            ],
        }

    out: Dict[str, Any] = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "legacy_version_seen": legacy_int,
        "saved_at": str(raw.get("saved_at") or "")[:40],
        "session_id": sid,
        "task_id": tid,
        "mode": mode,
        "task": task,
        "intent": str(raw.get("intent") or "")[:120],
        "change_expectation": str(raw.get("change_expectation") or "")[:80],
        "files_touched": files_touched,
        "last_verify": last_verify,
        "causal_digest": str(raw.get("causal_digest") or "")[:128],
        "agents_md_path": str(raw.get("agents_md_path") or "")[:512],
        "agents_md_sha12": str(raw.get("agents_md_sha12") or "")[:24],
        "next_action": str(raw.get("next_action") or "")[:2000],
        "plan_reference": str(raw.get("plan_reference") or "")[:512],
        "status": str(raw.get("status") or "")[:120],
        "confidence": raw.get("confidence"),
        "harness_mode_label": str(raw.get("harness_mode_label") or mode)[:120],
        "plan_excerpt": str(raw.get("plan_excerpt") or "")[:4000],
    }
    if out["confidence"] is not None:
        try:
            out["confidence"] = float(out["confidence"])
        except (TypeError, ValueError):
            out["confidence"] = None
            warnings.append("confidence_invalid")

    if not task and not out["plan_excerpt"]:
        warnings.append("thin_task_and_plan")

    act_base: Dict[str, str] = {
        "author": ROLE_AUTHOR_DEFAULT,
        "operator": ROLE_OPERATOR_DEFAULT,
        "reviewer": ROLE_REVIEWER_UNASSIGNED,
        "repair_specialist": "",
    }
    actors = raw.get("actors")
    if isinstance(actors, dict):
        for k in act_base:
            v = actors.get(k)
            if v is not None and str(v).strip():
                act_base[k] = str(v).strip()[:120]
    out["actors"] = act_base

    return out, warnings


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


class _ContinuityIndexLock:
    def __init__(self, cwd: str, *, timeout_s: float = 5.0, poll_s: float = 0.05) -> None:
        self._path = _continuity_index_lock_path(cwd)
        self._timeout_s = timeout_s
        self._poll_s = poll_s
        self._fd: Optional[int] = None

    def __enter__(self) -> "_ContinuityIndexLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self._timeout_s
        while True:
            try:
                self._fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self._fd, str(os.getpid()).encode("ascii", errors="ignore"))
                return self
            except FileExistsError:
                if time.time() >= deadline:
                    raise TimeoutError(f"Timed out acquiring continuity index lock: {self._path}")
                time.sleep(self._poll_s)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    temp_path: Optional[Path] = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_path = Path(tmp.name)
        os.replace(temp_path, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def read_continuity_index(cwd: str) -> Dict[str, Any]:
    try:
        return _state_store(cwd).get_continuity_index()
    except Exception as exc:
        return {
            "format_version": 2,
            "updated_at": "",
            "by_task_id": {},
            "sqlite_error": f"{type(exc).__name__}: {exc}",
        }


def write_continuity_index(cwd: str, data: Dict[str, Any]) -> None:
    data = dict(data)
    data["format_version"] = int(data.get("format_version") or 2)
    by_task_id = data.get("by_task_id")
    if not isinstance(by_task_id, dict):
        by_task_id = {}
    store = _state_store(cwd)
    for task_id, row in by_task_id.items():
        if isinstance(row, dict):
            store.upsert_continuity_entry(str(task_id), row)


def _index_touch_task(
    cwd: str,
    *,
    task_id: str,
    session_id: str,
    active_handoff_relpath: str,
    latest_plan_relpath: str,
    active_envelope_relpath: str = "",
) -> None:
    idx = read_continuity_index(cwd)
    by_t: Dict[str, Any] = idx.setdefault("by_task_id", {})
    if not isinstance(by_t, dict):
        by_t = {}
        idx["by_task_id"] = by_t
    now = datetime.now(timezone.utc).isoformat()
    now_u = time.time()
    prev = by_t.get(task_id) if isinstance(by_t.get(task_id), dict) else {}
    prev_lp = str((prev or {}).get("latest_plan_relpath") or "").strip()
    final_plan = (latest_plan_relpath or "").strip() or prev_lp
    row = {
        "task_id": task_id,
        "last_session_id": session_id,
        "active_handoff_relpath": active_handoff_relpath,
        "latest_plan_relpath": final_plan,
        "updated_at": now,
        "updated_at_unix": now_u,
        **{k: v for k, v in (prev or {}).items() if k in ("notes",)},
    }
    env_p = (active_envelope_relpath or "").strip()
    if env_p:
        row["active_envelope_relpath"] = env_p.replace("\\", "/")
    elif isinstance(prev, dict) and prev.get("active_envelope_relpath"):
        row["active_envelope_relpath"] = str(prev.get("active_envelope_relpath") or "")
    _state_store(cwd).upsert_continuity_entry(task_id, row)


def build_handoff_payload(
    session: Any,
    *,
    plan_reference: str = "",
) -> Dict[str, Any]:
    """Payload handoff + contrato formal (schema v2)."""
    harness_mode = ""
    try:
        from apps.cli.runtime.harness_bundle import runtime_mode_label

        harness_mode = runtime_mode_label(
            str(getattr(session, "task_intent", "") or ""),
            str(getattr(session, "change_expectation", "") or ""),
            int(getattr(session, "repair_attempt_count", 0) or 0),
        )
    except Exception:
        harness_mode = str(getattr(session, "task_intent", "") or "")

    diff_summary = getattr(session, "diff_summary", None) or []
    files: List[str] = []
    if isinstance(diff_summary, list):
        for it in diff_summary[:40]:
            if isinstance(it, dict) and it.get("file"):
                files.append(str(it["file"]))

    ws = getattr(session, "active_workset", None) or {}
    if isinstance(ws, dict):
        for key in ("written_files", "edited_files", "deleted_files"):
            for p in ws.get(key) or []:
                s = str(p).strip()
                if s and s not in files:
                    files.append(s)

    ver = getattr(session, "verification", None) or {}
    verify_mini: Dict[str, Any] = {}
    if isinstance(ver, dict):
        verify_mini = {
            "status": str(ver.get("status") or ""),
            "steps_executed_count": ver.get("steps_executed_count"),
            "check_names": [
                str(c.get("name") or "")
                for c in (ver.get("checks") or [])[:12]
                if isinstance(c, dict)
            ],
        }

    causal = str(getattr(session, "last_repair_causal_digest", "") or "")[:64]
    phase = str(getattr(session, "phase", "") or "")
    outcome = str(getattr(session, "task_outcome", "") or "")
    try:
        conf = float(getattr(session, "task_completion_confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        conf = None

    task_body = (str(getattr(session, "task", "") or ""))[:2000]

    repair_prov = ""
    if str(getattr(session, "repair_specialist_outcome", "") or "").strip():
        repair_prov = str(getattr(session, "repair_specialist_outcome", "") or "")[:160]
    elif int(getattr(session, "repair_attempt_count", 0) or 0) > 0:
        repair_prov = f"repair_attempts={getattr(session, 'repair_attempt_count', 0)}"

    actors = {
        "author": str(getattr(session, "role_author", "") or ROLE_AUTHOR_DEFAULT)[:120],
        "operator": str(getattr(session, "role_operator", "") or ROLE_OPERATOR_DEFAULT)[:120],
        "reviewer": str(getattr(session, "role_reviewer", "") or ROLE_REVIEWER_UNASSIGNED)[:120],
        "repair_specialist": repair_prov,
    }

    sid_h = _clean_identifier(getattr(session, "session_id", None)) or str(
        getattr(session, "session_id", "") or ""
    )
    tid_h = _clean_identifier(getattr(session, "task_id", None))
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "session_id": sid_h,
        "task_id": tid_h,
        "actors": actors,
        "mode": harness_mode,
        "harness_mode_label": harness_mode,
        "task": task_body,
        "task_text": task_body,
        "intent": str(getattr(session, "task_intent", "") or ""),
        "change_expectation": str(getattr(session, "change_expectation", "") or ""),
        "files_touched": files[:48],
        "last_verify": verify_mini,
        "causal_digest": causal,
        "agents_md_path": str(getattr(session, "agents_md_path", "") or ""),
        "agents_md_sha12": str(getattr(session, "agents_md_sha12", "") or ""),
        "next_action": (str(getattr(session, "next_action", "") or ""))[:1200],
        "plan_excerpt": (str(getattr(session, "plan", "") or ""))[:4000],
        "plan_reference": (plan_reference or "").strip().replace("\\", "/"),
        "status": f"{phase}/{outcome}".strip("/")[:120],
        "confidence": conf,
    }


def save_operational_plan(
    cwd: str,
    *,
    session_id: str,
    task_id: str,
    plan_body: str,
    status: str,
    task_title: str = "",
) -> str:
    """Persiste primero en SQLite y luego emite markdown versionado; devuelve ruta relativa al repo."""
    d = ghost_subdir(cwd, _PLANS_DIR)
    tid_c = _clean_identifier(task_id)
    sid_c = _clean_identifier(session_id)
    slug = _plan_handoff_slug(task_id=tid_c, session_id=sid_c)
    tag = _session_file_tag(sid_c or session_id)
    name = f"{slug}__{tag}.md"
    path = d / name
    latest = d / f"{slug}__latest.md"
    yaml_tid = json.dumps(tid_c or sid_c or "session")
    yaml_sid = json.dumps(sid_c or "")
    header = (
        f"---\n"
        f"ghost_plan: true\n"
        f"status: {status}\n"
        f"task_id: {yaml_tid}\n"
        f"session_id: {yaml_sid}\n"
        f"updated_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"title: {json.dumps(task_title[:200])}\n"
        f"---\n\n"
    )
    body = (plan_body or "").strip()
    content = header + body
    root = _repo_root(cwd)
    try:
        rel = str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        rel = str(path).replace("\\", "/")
    _state_store(cwd).upsert_plan(
        tid_c or sid_c or "session",
        session_id=sid_c or session_id,
        plan_body=content,
        relpath=rel,
        status=status,
        task_title=task_title,
    )
    path.write_text(content, encoding="utf-8")
    try:
        latest.write_text(content, encoding="utf-8")
    except OSError:
        pass
    return rel


def _handoff_path_sort_key(p: Path) -> Tuple[float, str]:
    """Orden determinista: más reciente primero; empate por nombre de fichero."""
    try:
        return (-float(p.stat().st_mtime), p.name)
    except OSError:
        return (0.0, p.name)


def build_delegation_envelope(
    cwd: str,
    handoff_payload: Dict[str, Any],
    *,
    handoff_ref: str,
    state: str = "ready",
    recipient_role: str = "",
    recipient_actor_id: str = "",
) -> Dict[str, Any]:
    """Envelope explícito para transporte entre procesos/agentes (sin cola ni red)."""
    now = datetime.now(timezone.utc).isoformat()
    root = _repo_root(cwd)
    actors = handoff_payload.get("actors") if isinstance(handoff_payload.get("actors"), dict) else {}
    return {
        "envelope_version": HANDOFF_ENVELOPE_VERSION,
        "kind": "ghost_handoff_delegation",
        "created_at": now,
        "updated_at": now,
        "state": (state or "ready")[:48],
        "origin": {
            "session_id": str(handoff_payload.get("session_id") or ""),
            "task_id": str(handoff_payload.get("task_id") or ""),
            "repo_root_relpath": ".",
            "repo_label": str(root.name)[:120],
        },
        "recipient": {
            "role": (recipient_role or "")[:64],
            "actor_id": (recipient_actor_id or "")[:120],
        },
        "actors": dict(actors),
        "handoff_ref": (handoff_ref or "").replace("\\", "/"),
        "handoff_payload": handoff_payload,
    }


def save_delegation_envelope(cwd: str, envelope: Dict[str, Any]) -> str:
    hp = envelope.get("handoff_payload") if isinstance(envelope.get("handoff_payload"), dict) else {}
    tid = _clean_identifier(hp.get("task_id")) or "session"
    sid = _clean_identifier(hp.get("session_id"))
    slug = _plan_handoff_slug(task_id=tid, session_id=sid)
    tag = _session_file_tag(sid or "unknown")
    d = ghost_subdir(cwd, _HANDOFFS_DIR)
    path = d / f"{slug}__{tag}.envelope.json"
    path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8")
    root = _repo_root(cwd)
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def load_delegation_envelope(cwd: str, relpath: str) -> Tuple[Dict[str, Any], List[str]]:
    """Carga envelope; validación laxa con advertencias."""
    warns: List[str] = []
    p = _resolve_under_root(cwd, relpath)
    if not p.is_file():
        return {}, ["envelope_missing"]
    raw = _read_json_file(p)
    if not isinstance(raw, dict):
        return {}, ["envelope_invalid_json"]
    if raw.get("kind") != "ghost_handoff_delegation":
        warns.append("unexpected_envelope_kind")
    return raw, warns


def save_handoff_versioned(cwd: str, payload: Dict[str, Any], *, update_index: bool = True) -> Tuple[str, str]:
    """
    Escribe `.ghost/handoffs/<slug>__<session_tag>.json`, copia `__latest.json`,
    opcionalmente actualiza índice. Devuelve (ruta_versionada, ruta_latest).
    """
    d = ghost_subdir(cwd, _HANDOFFS_DIR)
    tid = _clean_identifier(payload.get("task_id")) or "session"
    sid = _clean_identifier(payload.get("session_id"))
    slug = _plan_handoff_slug(task_id=tid, session_id=sid)
    tag = _session_file_tag(sid or "unknown")
    versioned = d / f"{slug}__{tag}.json"
    latest = d / f"{slug}__latest.json"
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    root = _repo_root(cwd)
    try:
        vrel = str(versioned.relative_to(root)).replace("\\", "/")
        lrel = str(latest.relative_to(root)).replace("\\", "/")
    except ValueError:
        vrel = str(versioned).replace("\\", "/")
        lrel = str(latest).replace("\\", "/")

    _state_store(cwd).upsert_handoff(tid, payload, source=vrel)
    versioned.write_text(text, encoding="utf-8")
    try:
        latest.write_text(text, encoding="utf-8")
    except OSError:
        pass

    plan_ref = str(payload.get("plan_reference") or "")
    if update_index:
        _index_touch_task(
            cwd,
            task_id=tid,
            session_id=sid,
            active_handoff_relpath=vrel,
            latest_plan_relpath=plan_ref,
        )
    return vrel, lrel


def save_handoff_json(cwd: str, payload: Dict[str, Any]) -> str:
    """API estable: guarda handoff versionado; devuelve ruta del fichero versionado."""
    vrel, _ = save_handoff_versioned(cwd, payload)
    return vrel


@dataclass
class ContinuityLoadResult:
    plan_path: str
    plan_body: str
    handoff: Dict[str, Any]
    handoff_warnings: List[str] = field(default_factory=list)
    handoff_source: str = ""


def _resolve_under_root(cwd: str, relpath: str) -> Path:
    root = _repo_root(cwd)
    rel = (relpath or "").strip().replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return Path()
    p = root.joinpath(*rel.split("/")).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return Path()
    return p


def _try_load_handoff_file(
    cwd: str, path: Path, expected_task_id: str
) -> Tuple[Dict[str, Any], List[str], str]:
    if not path.is_file():
        return {}, [], ""
    raw = _read_json_file(path)
    if raw is None:
        return {}, ["json_decode_error"], str(path.name)
    raw = unwrap_handoff_raw(raw)
    norm, warnings = normalize_handoff_document(raw, expected_task_id=expected_task_id or None)
    if "task_id_mismatch" in warnings:
        return {}, warnings, str(path.name)
    return norm, warnings, str(path.name)


def load_handoff_for_task(cwd: str, task_id: str) -> Tuple[Dict[str, Any], List[str], str]:
    """Carga handoff activo para task_id solo desde SQLite."""
    warnings: List[str] = []
    task_id_clean = _clean_identifier(task_id)
    if not task_id_clean:
        return {}, ["missing_task_id"], ""
    try:
        db_handoff, db_source = _state_store(cwd).get_handoff(task_id_clean)
    except Exception as exc:
        return {}, [f"sqlite_unavailable:{type(exc).__name__}"], ""
    if isinstance(db_handoff, dict) and db_handoff:
        norm, hw = normalize_handoff_document(db_handoff, expected_task_id=task_id_clean or None)
        warnings.extend(hw)
        if norm:
            return norm, warnings, db_source or "sqlite"
    return {}, warnings, ""


def load_continuity_for_task(cwd: str, task_id: str) -> ContinuityLoadResult:
    """Plan + handoff resueltos solo desde SQLite; `.ghost/*` no se usa como fallback de lectura."""
    plan_body = ""
    rel_plan = ""
    task_id_clean = _clean_identifier(task_id)
    if task_id_clean:
        try:
            plan_row = _state_store(cwd).get_plan(task_id_clean)
        except Exception:
            plan_row = None
        if isinstance(plan_row, dict):
            plan_body = str(plan_row.get("plan_body") or "")
            rel_plan = str(plan_row.get("relpath") or "")

    h, hw, _src = load_handoff_for_task(cwd, task_id)
    return ContinuityLoadResult(
        plan_path=rel_plan,
        plan_body=plan_body,
        handoff=h,
        handoff_warnings=hw,
        handoff_source=_src,
    )


def load_latest_handoff_any(cwd: str) -> Tuple[Dict[str, Any], str]:
    """Sin task_id en memoria: elige el handoff más reciente solo desde SQLite."""
    try:
        db_handoff, db_source = _state_store(cwd).get_latest_handoff()
    except Exception:
        return {}, ""
    if isinstance(db_handoff, dict) and db_handoff:
        norm, _ = normalize_handoff_document(db_handoff, expected_task_id=None)
        if norm:
            return norm, db_source or "sqlite_latest"
    return {}, ""


def build_continuity_system_block(
    plan_excerpt: str,
    handoff: Dict[str, Any],
) -> str:
    """Bloque para inyectar en system prompt en mensajes /continua."""
    lines: List[str] = [
        "[CONTINUIDAD — no reinicies exploración salvo que falte contexto]",
        "- Prioriza el plan persistido, el handoff y los archivos ya tocados antes de nuevos ls amplios.",
        "- Si el último verify falló, continúa desde el fallo concreto (ruta/check) sin repetir pasos ya superados.",
    ]
    if handoff:
        mode = str(handoff.get("mode") or handoff.get("harness_mode_label") or "").strip()
        if mode:
            lines.append(f"- Modo harness: {mode}")
        ft = handoff.get("files_touched") or []
        if isinstance(ft, list) and ft:
            lines.append(f"- Archivos tocados recientemente: {', '.join(str(x) for x in ft[:20])}")
        lv = handoff.get("last_verify") or {}
        if isinstance(lv, dict) and lv.get("status"):
            lines.append(f"- Último verify: status={lv.get('status')} checks={lv.get('check_names')}")
        nx = str(handoff.get("next_action") or "").strip()
        if nx:
            lines.append(f"- Siguiente paso sugerido: {nx[:500]}")
        cd = str(handoff.get("causal_digest") or "").strip()
        if cd:
            lines.append(f"- Firma causal (repair): {cd}")
        ap = str(handoff.get("agents_md_path") or "").strip()
        if ap:
            lines.append(f"- AGENTS.md: `{ap}` (sha12={handoff.get('agents_md_sha12', '')})")
        pref = str(handoff.get("plan_reference") or "").strip()
        if pref:
            lines.append(f"- Plan persistido (ruta): `{pref}`")
        act = handoff.get("actors") if isinstance(handoff.get("actors"), dict) else {}
        if act:
            au = str(act.get("author") or "").strip()
            op = str(act.get("operator") or "").strip()
            rv = str(act.get("reviewer") or "").strip()
            if au or op or rv:
                lines.append(
                    f"- Roles (contrato): author={au or '—'} · operator={op or '—'} · reviewer={rv or '—'}"
                )
    pe = (plan_excerpt or "").strip()
    if pe:
        cap = pe[:6000] if len(pe) > 6000 else pe
        lines.append("\n[Plan persistido — extracto]\n" + cap)
    return "\n".join(lines) + "\n\n"


def save_change_proposal(cwd: str, packet: Dict[str, Any]) -> str:
    """Persiste paquete «change proposal» / review bajo .ghost/review_packets/."""
    d = ghost_subdir(cwd, _REVIEW_PACKETS_DIR)
    tid = _clean_identifier(packet.get("task_id")) or "session"
    sid_pk = _clean_identifier(packet.get("session_id"))
    slug = _plan_handoff_slug(task_id=tid, session_id=sid_pk)
    tag = _session_file_tag(sid_pk or "unknown")
    path = d / f"{slug}__{tag}.json"
    path.write_text(json.dumps(packet, indent=2, ensure_ascii=False), encoding="utf-8")
    root = _repo_root(cwd)
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def persist_plan_and_handoff(cwd: str, session: Any) -> Tuple[str, str]:
    """
    Al finalizar sesión: plan + handoff versionados + índice + review packet (harness).
    Devuelve (plan_rel_path, handoff_versioned_rel_path).
    """
    sid = _clean_identifier(getattr(session, "session_id", None)) or str(
        getattr(session, "session_id", "") or ""
    )
    tid = _clean_identifier(getattr(session, "task_id", None))
    plan_body = str(getattr(session, "plan", "") or "")
    status = plan_status_from_session(session)
    title = str(getattr(session, "task", "") or "")[:200]
    plan_rel = ""
    if plan_body.strip():
        plan_rel = save_operational_plan(
            cwd,
            session_id=sid,
            task_id=tid or sid or "session",
            plan_body=plan_body,
            status=status,
            task_title=title,
        )
        _state_store(cwd).upsert_plan(
            tid or sid or "session",
            session_id=sid,
            plan_body=plan_body,
            relpath=plan_rel,
            status=status,
            task_title=title,
        )
    payload = build_handoff_payload(session, plan_reference=plan_rel)
    handoff_vrel, _handoff_latest = save_handoff_versioned(cwd, payload, update_index=False)
    _state_store(cwd).upsert_handoff(tid or sid or "session", payload, source=handoff_vrel)
    env = build_delegation_envelope(
        cwd,
        payload,
        handoff_ref=handoff_vrel,
        state="ready",
        recipient_role=str(getattr(session, "delegation_recipient_role", "") or ""),
        recipient_actor_id=str(getattr(session, "delegation_recipient_actor_id", "") or ""),
    )
    envelope_rel = save_delegation_envelope(cwd, env)
    _index_touch_task(
        cwd,
        task_id=tid or sid or "session",
        session_id=sid,
        active_handoff_relpath=handoff_vrel,
        latest_plan_relpath=plan_rel,
        active_envelope_relpath=envelope_rel,
    )
    setattr(session, "persisted_plan_path", plan_rel)
    setattr(session, "persisted_handoff_path", handoff_vrel)
    setattr(session, "delegation_envelope_path", envelope_rel)

    try:
        from apps.cli.runtime.harness_bundle import (
            build_change_proposal_packet,
            format_review_packet_as_pr_body,
        )

        packet = build_change_proposal_packet(
            session,
            plan_relpath=plan_rel,
            handoff_relpath=handoff_vrel,
            envelope_relpath=envelope_rel,
        )
        rp_rel = save_change_proposal(cwd, packet)
        setattr(session, "review_packet_path", rp_rel)
        try:
            _rp_md = format_review_packet_as_pr_body(packet)
            if len(_rp_md) > 4500:
                _rp_md = _rp_md[:4500] + "\n…"
            setattr(session, "review_packet_pr_body_preview", _rp_md)
        except Exception:
            setattr(session, "review_packet_pr_body_preview", "")
        setattr(session, "review_ready_for_review", bool(packet.get("ready_for_review")))
        setattr(session, "review_readiness_code", str(packet.get("review_readiness") or ""))
        setattr(
            session,
            "review_readiness_detail_es",
            str(packet.get("review_readiness_detail_es") or ""),
        )
    except Exception:
        setattr(session, "review_packet_path", getattr(session, "review_packet_path", "") or "")
        setattr(session, "review_packet_pr_body_preview", "")

    try:
        session.events.append(
            {
                "event": "workflow_continuity_persisted",
                "plan_path": plan_rel,
                "handoff_path": handoff_vrel,
                "envelope_path": envelope_rel,
                "plan_status": status,
                "review_packet_path": str(getattr(session, "review_packet_path", "") or ""),
            }
        )
    except Exception:
        pass
    return plan_rel, handoff_vrel
