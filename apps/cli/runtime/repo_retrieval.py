"""
Repo Semantic Search — deterministic chunking, symbols, hybrid ranking, optional embeddings.
Sprint 1: advisory index + prompt. Sprint 2: confidence, merged read order (GHOST_RETRIEVAL_INFLUENCES_RUNTIME).
See docs/REPO_SEMANTIC_SEARCH.md.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field

from apps.cli.runtime.exploration_planner import path_under_ui_roots
from apps.cli.runtime.task_contract import (
    contract_has_operational_spec,
    get_task_contract_decision_plan,
    get_task_contract_spec,
)

logger = logging.getLogger(__name__)

RETRIEVAL_CACHE_VERSION = "1.0.0"
ENV_USE_RETRIEVAL = "GHOST_USE_RETRIEVAL"
ENV_RETRIEVAL_INFLUENCES_RUNTIME = "GHOST_RETRIEVAL_INFLUENCES_RUNTIME"
ENV_USE_EMBEDDINGS = "GHOST_USE_EMBEDDINGS"
ENV_RETRIEVAL_AGGRESSION = "GHOST_RETRIEVAL_AGGRESSION"
ENV_EMBED_MODEL = "GHOST_EMBED_MODEL"
ENV_EMBED_BASE_URL = "GHOST_EMBED_BASE_URL"
ENV_EMBED_API_KEY = "GHOST_EMBED_API_KEY"

DEFAULT_MAX_FILE_BYTES = 256 * 1024
WINDOW_LINES = 72
WINDOW_OVERLAP = 16
MAX_CHUNKS_PER_FILE = 80

IGNORED_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        ".ghost",
        ".next",
        "dist",
        "build",
        "out",
        ".turbo",
        ".pnpm-store",
        "coverage",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
    }
)

INDEX_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx", ".py", ".json", ".md", ".sql"})
_PROJECT_MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt")
_MONOREPO_CONTAINER_DIRS = frozenset({"apps", "packages", "services", "libs", "modules", "crates"})
_COMMON_ROOT_DIRS = frozenset({"src", "tests", "test", "docs", "frontend", "backend", "web", "ui"})


# --- Pydantic models ---


class CodeChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    file_path: str
    language: str
    chunk_type: str
    symbol_names: List[str] = Field(default_factory=list)
    start_line: int
    end_line: int
    content: str
    fingerprint: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SymbolRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    file_path: str
    kind: str
    start_line: int
    end_line: int


class ImportEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_file: str
    target_ref: str
    edge_type: str
    normalized_target: str


class SkippedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    reason: str


class RetrievalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    allowed_roots: List[str] = Field(default_factory=list)
    forbidden_roots: List[str] = Field(default_factory=list)
    target_files: List[str] = Field(default_factory=list)
    preferred_files: List[str] = Field(default_factory=list)
    preferred_symbols: List[str] = Field(default_factory=list)
    max_hits: int = 12
    languages: List[str] = Field(default_factory=list)
    layer_boost_api: bool = False
    layer_boost_data: bool = False


class RetrievalHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    chunk_id: str
    score: float
    reasons: List[str] = Field(default_factory=list)
    start_line: int = 0
    end_line: int = 0
    preview: str = ""
    symbol_names: List[str] = Field(default_factory=list)


class RetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hits: List[RetrievalHit] = Field(default_factory=list)
    top_files: List[Tuple[str, float]] = Field(default_factory=list)
    top_symbols: List[Tuple[str, str]] = Field(default_factory=list)
    provenance: Dict[str, Any] = Field(default_factory=dict)
    embeddings_used: bool = False
    query_ms: float = 0.0
    chunks_scored: int = 0


class RetrievalIndexStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_count: int = 0
    chunk_count: int = 0
    symbol_count: int = 0
    edge_count: int = 0
    skipped_count: int = 0


class RetrievalCacheInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_path: str = ""
    rebuilt: bool = False
    incremental: bool = False
    files_updated: int = 0
    cache_version: str = RETRIEVAL_CACHE_VERSION


class RepoRetrievalIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = RETRIEVAL_CACHE_VERSION
    root: str
    chunks: List[CodeChunk] = Field(default_factory=list)
    symbols: List[SymbolRecord] = Field(default_factory=list)
    import_edges: List[ImportEdge] = Field(default_factory=list)
    file_fingerprints: Dict[str, str] = Field(default_factory=dict)
    file_mtimes_ns: Dict[str, int] = Field(default_factory=dict)


class RepoRetrievalBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: RepoRetrievalIndex
    stats: RetrievalIndexStats
    cache_info: RetrievalCacheInfo
    skipped: List[SkippedFile] = Field(default_factory=list)


# --- Embedding providers ---


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> List[List[float]]: ...

    @property
    def dimension(self) -> int: ...


class NullEmbeddingProvider:
    dimension = 0

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        return [[] for _ in texts]


class NvidiaNimEmbeddingProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "llama-3.2-nv-embedqa-1b-v2",
        timeout: float = 60.0,
    ):
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._dim = 0

    @property
    def dimension(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            import httpx
        except ImportError:
            logger.warning("httpx not available; embeddings skipped")
            return [[] for _ in texts]
        url = f"{self._base}/v1/embeddings"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        out: List[List[float]] = []
        for t in texts:
            payload = {"model": self._model, "input": t[:8000]}
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    r = client.post(url, headers=headers, json=payload)
                    r.raise_for_status()
                    data = r.json()
                    vec = data["data"][0]["embedding"]
                    if not self._dim:
                        self._dim = len(vec)
                    out.append([float(x) for x in vec])
            except Exception as e:
                logger.warning("NIM embedding failed: %s", e)
                out.append([])
        return out


def get_embedding_provider() -> EmbeddingProvider:
    if os.environ.get(ENV_USE_EMBEDDINGS, "0").strip().lower() not in ("1", "true", "yes", "on"):
        return NullEmbeddingProvider()
    base = os.environ.get(ENV_EMBED_BASE_URL, "").strip()
    key = os.environ.get(ENV_EMBED_API_KEY, "").strip()
    if not base or not key:
        return NullEmbeddingProvider()
    model = os.environ.get(ENV_EMBED_MODEL, "llama-3.2-nv-embedqa-1b-v2").strip()
    return NvidiaNimEmbeddingProvider(base_url=base, api_key=key, model=model)


def is_retrieval_enabled() -> bool:
    v = os.environ.get(ENV_USE_RETRIEVAL, "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def retrieval_influences_runtime_enabled() -> bool:
    """Both base retrieval and runtime-influence flags must be on."""
    if not is_retrieval_enabled():
        return False
    v = os.environ.get(ENV_RETRIEVAL_INFLUENCES_RUNTIME, "0").strip().lower()
    return v in ("1", "true", "yes", "on")


# --- Sprint 2: confidence, merge, guided prompt ---


def retrieval_confidence_score(result: RetrievalResult) -> float:
    """Deterministic 0..1 score from hit mass, top score, and strong match signals."""
    if not result.hits:
        return 0.0
    nh = len(result.hits)
    top = max(h.score for h in result.hits)
    part_a = min(1.0, nh / 8.0) * 0.35
    part_b = min(1.0, top / 100.0) * 0.45
    strong = 0
    for h in result.hits[:6]:
        for r in h.reasons:
            if r.startswith("target_file") or r.startswith("symbol:") or r in (
                "api_layer",
                "data_layer",
                "planner_preferred",
                "lexical",
            ):
                strong += 1
                break
    part_c = min(1.0, strong / 5.0) * 0.2
    return round(min(1.0, part_a + part_b + part_c), 4)


def _path_allowed_for_contract_spec(
    rel: str,
    query: RetrievalQuery,
    contract_spec: Dict[str, Any],
    repo_v2: Dict[str, Any],
) -> bool:
    forbidden = [_norm_rel(x) for x in query.forbidden_roots]
    if _under_forbidden_root(rel, forbidden):
        return False
    if "ui" in (contract_spec.get("forbidden_layers") or []):
        ep = repo_v2.get("entrypoints") or {}
        if isinstance(ep, dict):
            ui_roots = [str(x) for x in (ep.get("ui_roots") or []) if x]
            if ui_roots and path_under_ui_roots(rel, ui_roots):
                return False
    return True


# Deprecated alias.
_path_allowed_for_taskspec = _path_allowed_for_contract_spec


def _retrieval_hits_respect_task_constraints(
    result: RetrievalResult,
    query: RetrievalQuery,
    contract_spec: Dict[str, Any],
    repo_v2: Dict[str, Any],
) -> bool:
    if not result.hits:
        return False
    for h in result.hits[:6]:
        if not _path_allowed_for_contract_spec(h.file_path, query, contract_spec, repo_v2):
            return False
    return True


def _planner_paths(decision_plan: Optional[Dict[str, Any]]) -> List[str]:
    if not decision_plan:
        return []
    out: List[str] = []
    ex = decision_plan.get("exploration_plan") or {}
    for c in ex.get("ranked_candidates") or []:
        if isinstance(c, dict) and c.get("kind") == "file":
            p = c.get("path")
            if isinstance(p, str) and p.strip():
                out.append(_norm_rel(p))
    ep = decision_plan.get("execution_plan") or {}
    for f in ep.get("expected_files_changed") or []:
        if isinstance(f, str) and f.strip():
            out.append(_norm_rel(f))
    seen: Set[str] = set()
    uniq: List[str] = []
    for p in out:
        k = p.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _planner_overlap_bonus(result: RetrievalResult, decision_plan: Optional[Dict[str, Any]]) -> float:
    pp = {p.lower() for p in _planner_paths(decision_plan)}
    if not pp:
        return 0.0
    for path, _ in result.top_files[:5]:
        if _norm_rel(path).lower() in pp:
            return 0.08
    return 0.0


def retrieval_is_actionable(
    result: RetrievalResult,
    contract_spec: Dict[str, Any],
    repo_profile_v2: Dict[str, Any],
    query: RetrievalQuery,
    *,
    decision_plan: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    True when retrieval is strong enough to optionally influence runtime (subject to env flag).
    """
    if len(result.hits) < 2:
        return False
    top_score = result.hits[0].score
    if top_score < 38.0:
        return False
    conf = retrieval_confidence_score(result) + _planner_overlap_bonus(result, decision_plan)
    if conf < 0.45:
        return False
    if not _retrieval_hits_respect_task_constraints(result, query, contract_spec, repo_profile_v2):
        return False
    return True


def resolve_retrieval_mode(
    actionable: bool,
) -> str:
    if not actionable:
        return "fallback"
    if retrieval_influences_runtime_enabled():
        return "influencing"
    return "advisory"


def derive_likely_edit_targets(
    result: RetrievalResult,
    query: RetrievalQuery,
    contract_spec: Dict[str, Any],
    repo_v2: Dict[str, Any],
    *,
    actionable: bool,
) -> Tuple[List[str], List[str]]:
    """Returns (paths, parallel reasons). Empty when not actionable."""
    if not actionable:
        return [], []
    paths: List[str] = []
    reasons: List[str] = []
    seen: Set[str] = set()

    def add(p: str, reason: str) -> None:
        p = p.replace("\\", "/").strip()
        if not p or not _path_allowed_for_contract_spec(p, query, contract_spec, repo_v2):
            return
        k = _norm_rel(p).lower()
        if k in seen:
            return
        seen.add(k)
        paths.append(p)
        reasons.append(reason)

    for path, sc in result.top_files:
        if sc >= 30.0:
            add(path, f"top retrieval file (score={sc:.1f})")

    for h in result.hits[:8]:
        if h.score < 28.0:
            continue
        why: List[str] = []
        for r in h.reasons:
            if r.startswith("symbol:") or r in ("api_layer", "data_layer", "target_file", "planner_preferred"):
                why.append(r)
        if why:
            add(h.file_path, f"{h.file_path}:{h.start_line}-{h.end_line} :: " + ", ".join(why[:2]))

    return paths[:12], reasons[:12]


def build_retrieval_highlight_chunks(result: RetrievalResult, max_n: int = 8) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for h in result.hits[:max_n]:
        out.append(
            {
                "file_path": h.file_path,
                "start_line": h.start_line,
                "end_line": h.end_line,
                "reason": ", ".join(h.reasons[:2]) if h.reasons else "ranked",
            }
        )
    return out


def merge_exploration_and_retrieval(
    contract_spec: Dict[str, Any],
    exploration_plan: Dict[str, Any],
    decision_plan: Optional[Dict[str, Any]],
    repo_v2: Dict[str, Any],
    retrieval_result: RetrievalResult,
    retrieval_query: RetrievalQuery,
    *,
    include_retrieval_tier: bool,
) -> List[Dict[str, Any]]:
    """
    Deterministic merged read priority.
    Precedence: target_files → retrieval (if tier included) → planner files → exploration ranked_files → repo key_files.
    """
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    def add_row(path: str, source: str, reason: str) -> None:
        raw = path.replace("\\", "/").strip()
        if not raw:
            return
        p = _norm_rel(raw)
        if not p:
            return
        if not _path_allowed_for_contract_spec(p, retrieval_query, contract_spec, repo_v2):
            return
        k = p.lower()
        if k in seen:
            return
        seen.add(k)
        out.append({"path": p, "source": source, "reason": reason})

    for tf in contract_spec.get("target_files") or []:
        if isinstance(tf, str) and tf.strip():
            add_row(tf, "contract_spec_target", "contract_spec.target_files")

    if include_retrieval_tier:
        for path, sc in retrieval_result.top_files:
            add_row(path, "retrieval", f"retrieval ranked file (score={sc:.1f})")

    for p in _planner_paths(decision_plan):
        add_row(p, "planner", "Decision planner exploration / execution candidate")

    for f in exploration_plan.get("ranked_files") or []:
        if isinstance(f, str) and f.strip():
            add_row(f, "exploration", "RepoProfile v2 exploration plan")

    for kf in repo_v2.get("key_files") or []:
        if isinstance(kf, str) and kf.strip():
            add_row(kf, "repo_profile", "RepoProfile v2 key_files")

    return out


def merged_candidate_order_to_prompt_block(
    merged: List[Dict[str, Any]],
    *,
    ranked_dirs: List[str],
    ui_exploration_blocked: bool,
    evidence_lines: List[str],
    max_files: int = 25,
) -> str:
    if not merged and not ranked_dirs and not ui_exploration_blocked:
        return ""
    lines = [
        "[EXPLORATION PLAN — retrieval-guided merge]",
        "Priority read_file (deterministic order):",
    ]
    for i, row in enumerate(merged[:max_files], 1):
        p = row.get("path", "")
        src = row.get("source", "")
        lines.append(f"  {i}. {p}  — {src}")
    if ranked_dirs:
        lines.append("Suggested ls (directories):")
        for d in ranked_dirs[:12]:
            lines.append(f"  - {d}/")
    if ui_exploration_blocked:
        lines.append(
            "UI LAYER: exploration blocked by contract spec — do not read or list under ui_roots."
        )
    if evidence_lines[:8]:
        lines.append("Evidence (deterministic):")
        for e in evidence_lines[:8]:
            lines.append(f"  - {e}")
    return "\n".join(lines) + "\n"


# --- Path / walk ---


def _norm_rel(p: str) -> str:
    s = p.replace("\\", "/").strip().strip("./")
    while "//" in s:
        s = s.replace("//", "/")
    return s


def _under_forbidden_root(rel: str, roots: Sequence[str]) -> bool:
    r = _norm_rel(rel).lower()
    for root in roots:
        rr = _norm_rel(root).lower()
        if not rr:
            continue
        if r == rr or r.startswith(rr + "/"):
            return True
    return False


def _is_ghost_cache_dir(path: Path) -> bool:
    parts = path.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".ghost" and parts[i + 1] == "cache":
            return True
    return False


def _dir_has_project_manifest(path: Path) -> bool:
    return any((path / name).is_file() for name in _PROJECT_MANIFESTS)


def _derive_nested_project_forbidden_roots(repo_profile_v2: Dict[str, Any]) -> List[str]:
    root_raw = str((repo_profile_v2 or {}).get("root") or "").strip()
    if not root_raw:
        return []
    root = Path(root_raw)
    if not root.is_dir() or not _dir_has_project_manifest(root):
        return []
    folders = {
        str(x).strip().lower()
        for x in ((repo_profile_v2 or {}).get("important_folders") or [])
        if str(x).strip()
    }
    if folders & _MONOREPO_CONTAINER_DIRS:
        return []
    out: List[str] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        name = child.name.lower()
        if (
            name.startswith(".")
            or name in IGNORED_DIR_NAMES
            or name in _MONOREPO_CONTAINER_DIRS
            or name in _COMMON_ROOT_DIRS
        ):
            continue
        if (child / ".git").is_dir() or _dir_has_project_manifest(child):
            out.append(_norm_rel(str(child.relative_to(root)).replace("\\", "/")))
    return out


def discover_indexable_files(
    root: Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> Tuple[List[Path], List[SkippedFile]]:
    root = root.resolve()
    ok: List[Path] = []
    skipped: List[SkippedFile] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dp = Path(dirpath)
        if _is_ghost_cache_dir(dp.resolve()):
            dirnames[:] = []
            continue
        new_dirs: List[str] = []
        for d in dirnames:
            if d in IGNORED_DIR_NAMES:
                continue
            child = (dp / d).resolve()
            if _is_ghost_cache_dir(child):
                continue
            new_dirs.append(d)
        dirnames[:] = new_dirs

        for name in filenames:
            fp = dp / name
            if not fp.is_file():
                continue
            suf = fp.suffix.lower()
            if suf not in INDEX_EXTENSIONS:
                continue
            try:
                st = fp.stat()
            except OSError:
                skipped.append(SkippedFile(path=str(fp.relative_to(root)), reason="stat_error"))
                continue
            if st.st_size > max_file_bytes:
                skipped.append(
                    SkippedFile(
                        path=str(fp.relative_to(root)).replace("\\", "/"),
                        reason=f"size_exceeded>{max_file_bytes}",
                    )
                )
                continue
            ok.append(fp)
    return ok, skipped


def _language_for_suffix(suffix: str) -> str:
    s = suffix.lower()
    if s in (".ts", ".tsx"):
        return "typescript"
    if s in (".js", ".jsx"):
        return "javascript"
    if s == ".py":
        return "python"
    if s == ".json":
        return "json"
    if s == ".md":
        return "markdown"
    if s == ".sql":
        return "sql"
    return "unknown"


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --- Regex symbol / import extraction ---

_RE_TS_EXPORT_FN = re.compile(
    r"^\s*export\s+(?:async\s+)?function\s+(\w+)",
    re.MULTILINE,
)
_RE_TS_FN = re.compile(r"^\s*function\s+(\w+)\s*[\(]", re.MULTILINE)
_RE_TS_EXPORT_CLASS = re.compile(r"^\s*export\s+class\s+(\w+)", re.MULTILINE)
_RE_TS_CLASS = re.compile(r"^\s*class\s+(\w+)", re.MULTILINE)
_RE_TS_EXPORT_CONST = re.compile(r"^\s*export\s+const\s+(\w+)\s*=", re.MULTILINE)
_RE_TS_CONST = re.compile(r"^\s*const\s+(\w+)\s*=", re.MULTILINE)
_RE_TS_ROUTE = re.compile(
    r"^\s*export\s+(?:async\s+)?function\s+(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b",
    re.MULTILINE | re.IGNORECASE,
)
_RE_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)
_RE_PY_CLASS = re.compile(r"^\s*class\s+(\w+)\b", re.MULTILINE)
_RE_PY_FASTAPI = re.compile(r"^\s*@\w+\.(get|post|put|delete|patch)\s*\(\s*[\"']([^\"']+)[\"']", re.MULTILINE)
_RE_ZOD = re.compile(r"\bz\.object\s*\(|\.safeParse\s*\(", re.MULTILINE)
_RE_DRIZZLE = re.compile(
    r"\b(?:pgTable|sqliteTable|mysqlTable|pgEnum)\s*\(",
    re.MULTILINE,
)
_RE_PRISMA_MODEL = re.compile(r"^\s*model\s+(\w+)\s+\{", re.MULTILINE)

_RE_IMPORT_TS = re.compile(
    r"""import\s+(?:[\w*{}\s,]+\s+from\s+)?[\"']([^\"']+)[\"']""",
    re.MULTILINE,
)
_RE_REQUIRE = re.compile(r"""require\s*\(\s*[\"']([^\"']+)[\"']\s*\)""", re.MULTILINE)
_RE_IMPORT_PY = re.compile(r"^\s*import\s+([\w.]+)", re.MULTILINE)
_RE_FROM_PY = re.compile(r"^\s*from\s+([\w.]+)\s+import", re.MULTILINE)


def _line_of_index(content: str, pos: int) -> int:
    return content[:pos].count("\n") + 1


def extract_symbols_ts_py(content: str, rel_path: str, language: str) -> List[SymbolRecord]:
    syms: List[SymbolRecord] = []
    if language in ("typescript", "javascript"):
        for pat, kind in (
            (_RE_TS_EXPORT_FN, "function"),
            (_RE_TS_FN, "function"),
            (_RE_TS_EXPORT_CLASS, "class"),
            (_RE_TS_CLASS, "class"),
            (_RE_TS_EXPORT_CONST, "const"),
            (_RE_TS_CONST, "const"),
            (_RE_TS_ROUTE, "route_handler"),
        ):
            for m in pat.finditer(content):
                name = m.group(1)
                start = _line_of_index(content, m.start())
                end = min(start + 40, content.count("\n") + 1)
                syms.append(
                    SymbolRecord(name=name, file_path=rel_path, kind=kind, start_line=start, end_line=end)
                )
    elif language == "python":
        for m in _RE_PY_DEF.finditer(content):
            start = _line_of_index(content, m.start())
            syms.append(
                SymbolRecord(
                    name=m.group(1), file_path=rel_path, kind="function", start_line=start, end_line=start + 35
                )
            )
        for m in _RE_PY_CLASS.finditer(content):
            start = _line_of_index(content, m.start())
            syms.append(
                SymbolRecord(
                    name=m.group(1), file_path=rel_path, kind="class", start_line=start, end_line=start + 50
                )
            )
        for m in _RE_PY_FASTAPI.finditer(content):
            start = _line_of_index(content, m.start())
            path_bit = m.group(2)
            syms.append(
                SymbolRecord(
                    name=f"{m.group(1).upper()}:{path_bit}",
                    file_path=rel_path,
                    kind="route_handler",
                    start_line=start,
                    end_line=start + 5,
                )
            )
    if _RE_ZOD.search(content):
        syms.append(
            SymbolRecord(name="zod_schema", file_path=rel_path, kind="validation_hint", start_line=1, end_line=1)
        )
    if _RE_DRIZZLE.search(content) or _RE_PRISMA_MODEL.search(content):
        syms.append(
            SymbolRecord(name="schema_table", file_path=rel_path, kind="schema_hint", start_line=1, end_line=1)
        )
    return syms


def extract_imports(content: str, rel_path: str, language: str) -> List[ImportEdge]:
    edges: List[ImportEdge] = []
    if language in ("typescript", "javascript"):
        for m in _RE_IMPORT_TS.finditer(content):
            ref = m.group(1)
            norm = ref.split("/")[-1] if "/" in ref else ref
            edges.append(
                ImportEdge(
                    source_file=rel_path,
                    target_ref=ref,
                    edge_type="static",
                    normalized_target=norm,
                )
            )
        for m in _RE_REQUIRE.finditer(content):
            ref = m.group(1)
            edges.append(
                ImportEdge(
                    source_file=rel_path,
                    target_ref=ref,
                    edge_type="require",
                    normalized_target=ref.split("/")[-1],
                )
            )
    elif language == "python":
        for m in _RE_IMPORT_PY.finditer(content):
            ref = m.group(1)
            edges.append(
                ImportEdge(
                    source_file=rel_path,
                    target_ref=ref,
                    edge_type="import",
                    normalized_target=ref.split(".")[-1],
                )
            )
        for m in _RE_FROM_PY.finditer(content):
            ref = m.group(1)
            edges.append(
                ImportEdge(
                    source_file=rel_path,
                    target_ref=ref,
                    edge_type="from",
                    normalized_target=ref.split(".")[-1],
                )
            )
    return edges


def _make_chunks_from_lines(
    lines: List[str],
    rel_path: str,
    language: str,
    symbol_spans: List[Tuple[int, int, List[str]]],
) -> List[CodeChunk]:
    """symbol_spans: (start_line_1based, end_line_1based, names)."""
    n = len(lines)
    if n == 0:
        return []
    covered = [False] * n
    chunks: List[CodeChunk] = []

    def add_chunk(s0: int, s1: int, ctype: str, names: List[str]) -> None:
        s0 = max(1, min(s0, n))
        s1 = max(s0, min(s1, n))
        body = "\n".join(lines[s0 - 1 : s1])
        fp = _sha256_text(f"{rel_path}:{s0}:{s1}:{body}")
        cid = hashlib.sha256(fp.encode()).hexdigest()[:24]
        for i in range(s0 - 1, s1):
            if i < len(covered):
                covered[i] = True
        chunks.append(
            CodeChunk(
                chunk_id=cid,
                file_path=rel_path,
                language=language,
                chunk_type=ctype,
                symbol_names=names,
                start_line=s0,
                end_line=s1,
                content=body,
                fingerprint=fp,
                metadata={},
            )
        )

    for s0, s1, names in sorted(symbol_spans, key=lambda x: x[0]):
        if s1 - s0 > 400:
            s1 = s0 + 400
        add_chunk(s0, s1, "symbol", names)

    i = 0
    while i < n:
        if covered[i]:
            i += 1
            continue
        win = min(WINDOW_LINES, n - i)
        s0 = i + 1
        s1 = i + win
        add_chunk(s0, s1, "window", [])
        step = max(1, win - WINDOW_OVERLAP)
        i += step
        if len(chunks) > MAX_CHUNKS_PER_FILE:
            break
    return chunks


def _symbol_spans_from_records(symbols: List[SymbolRecord], n_lines: int) -> List[Tuple[int, int, List[str]]]:
    spans: List[Tuple[int, int, List[str]]] = []
    for s in symbols:
        if s.kind in ("validation_hint", "schema_hint"):
            continue
        e = min(s.end_line, n_lines)
        spans.append((s.start_line, max(e, s.start_line), [s.name]))
    return spans


def process_file(path: Path, root: Path) -> Tuple[List[CodeChunk], List[SymbolRecord], List[ImportEdge], Optional[str]]:
    rel = str(path.relative_to(root)).replace("\\", "/")
    try:
        raw = path.read_bytes()
    except OSError as e:
        return [], [], [], f"read_error:{e}"
    if len(raw) > DEFAULT_MAX_FILE_BYTES:
        return [], [], [], "size_exceeded"
    text = raw.decode("utf-8", errors="replace")
    language = _language_for_suffix(path.suffix)
    lines = text.splitlines()
    n = len(lines)

    symbols = extract_symbols_ts_py(text, rel, language)
    imports = extract_imports(text, rel, language)

    spans = _symbol_spans_from_records(symbols, n)
    if not spans and n > 0:
        spans = [(1, min(n, WINDOW_LINES), [])]

    chunks = _make_chunks_from_lines(lines, rel, language, spans)
    return chunks, symbols, imports, None


def _cache_path(root: Path) -> Path:
    return root / ".ghost" / "cache" / "repo_retrieval_index.json"


def build_retrieval_index(
    root: Path,
    repo_profile_v2: Optional[Dict[str, Any]] = None,
    *,
    force_refresh: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> RepoRetrievalBuildResult:
    root = root.resolve()
    cache_file = _cache_path(root)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    skipped: List[SkippedFile] = []
    cache_info = RetrievalCacheInfo(cache_path=str(cache_file), rebuilt=False, incremental=False, files_updated=0)

    old_fp: Dict[str, str] = {}
    old_mt: Dict[str, int] = {}
    old_chunks_by_file: Dict[str, List[CodeChunk]] = {}
    old_symbols: List[SymbolRecord] = []
    old_edges: List[ImportEdge] = []

    if not force_refresh and cache_file.is_file():
        try:
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
            if raw.get("version") == RETRIEVAL_CACHE_VERSION and raw.get("root") == str(root):
                old_fp = dict(raw.get("file_fingerprints") or {})
                old_mt = {k: int(v) for k, v in (raw.get("file_mtimes_ns") or {}).items()}
                for c in raw.get("chunks") or []:
                    ch = CodeChunk.model_validate(c)
                    old_chunks_by_file.setdefault(ch.file_path, []).append(ch)
                old_symbols = [SymbolRecord.model_validate(s) for s in raw.get("symbols") or []]
                old_edges = [ImportEdge.model_validate(e) for e in raw.get("import_edges") or []]
        except Exception as e:
            logger.debug("Cache load failed: %s", e)
            old_fp.clear()
            old_chunks_by_file.clear()

    files, walk_skipped = discover_indexable_files(root, max_file_bytes=max_file_bytes)
    skipped.extend(walk_skipped)

    new_chunks: List[CodeChunk] = []
    new_symbols: List[SymbolRecord] = []
    new_edges: List[ImportEdge] = []
    new_fp: Dict[str, str] = {}
    new_mt: Dict[str, int] = {}
    files_updated = 0

    for fp in files:
        rel = str(fp.relative_to(root)).replace("\\", "/")
        try:
            st = fp.stat()
            raw_b = fp.read_bytes()
        except OSError:
            skipped.append(SkippedFile(path=rel, reason="read_error"))
            continue
        if len(raw_b) > max_file_bytes:
            skipped.append(SkippedFile(path=rel, reason=f"size_exceeded>{max_file_bytes}"))
            continue
        digest = _sha256_bytes(raw_b)
        new_fp[rel] = digest
        new_mt[rel] = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))

        if (
            not force_refresh
            and old_fp.get(rel) == digest
            and rel in old_chunks_by_file
        ):
            new_chunks.extend(old_chunks_by_file[rel])
            for s in old_symbols:
                if s.file_path == rel:
                    new_symbols.append(s)
            for e in old_edges:
                if e.source_file == rel:
                    new_edges.append(e)
            continue

        files_updated += 1
        ch, sy, im, err = process_file(fp, root)
        if err:
            skipped.append(SkippedFile(path=rel, reason=err))
            continue
        new_chunks.extend(ch)
        new_symbols.extend(sy)
        new_edges.extend(im)

    if force_refresh:
        cache_info.rebuilt = True
    elif not old_fp:
        cache_info.rebuilt = True
    else:
        cache_info.incremental = files_updated > 0
    cache_info.files_updated = files_updated

    idx = RepoRetrievalIndex(
        root=str(root),
        chunks=new_chunks,
        symbols=new_symbols,
        import_edges=new_edges,
        file_fingerprints=new_fp,
        file_mtimes_ns=new_mt,
    )

    payload = idx.model_dump(mode="json")
    payload["version"] = RETRIEVAL_CACHE_VERSION
    try:
        cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write retrieval cache: %s", e)

    stats = RetrievalIndexStats(
        file_count=len(new_fp),
        chunk_count=len(new_chunks),
        symbol_count=len(new_symbols),
        edge_count=len(new_edges),
        skipped_count=len(skipped),
    )
    return RepoRetrievalBuildResult(index=idx, stats=stats, cache_info=cache_info, skipped=skipped)


def _query_tokens(q: str) -> Set[str]:
    return {t.lower() for t in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{1,}", q) if len(t) > 1}


def _lexical_score(content: str, tokens: Set[str]) -> float:
    low = content.lower()
    hits = sum(1 for t in tokens if t in low)
    return min(40.0, hits * 4.0)


def _path_token_score(rel: str, tokens: Set[str]) -> float:
    parts = re.split(r"[/_.\-]", rel.lower())
    hit = sum(1 for t in tokens if t in parts or t in rel.lower())
    return min(25.0, hit * 5.0)


def retrieve_code(
    query: RetrievalQuery,
    index: RepoRetrievalIndex,
    provider: Optional[EmbeddingProvider] = None,
) -> RetrievalResult:
    t0 = time.perf_counter()
    provider = provider or get_embedding_provider()
    tokens = _query_tokens(query.text)
    q_lower = query.text.lower()

    forbidden = [_norm_rel(x) for x in query.forbidden_roots]
    allowed = [_norm_rel(x) for x in query.allowed_roots] if query.allowed_roots else []

    chunk_vecs: Optional[List[List[float]]] = None
    q_vec: Optional[List[float]] = None
    use_emb = (
        os.environ.get(ENV_USE_EMBEDDINGS, "0").strip().lower() in ("1", "true", "yes", "on")
        and provider is not None
        and not isinstance(provider, NullEmbeddingProvider)
    )
    if use_emb and query.text.strip():
        try:
            q_vec_list = provider.embed([query.text[:4000]])
            q_vec = q_vec_list[0] if q_vec_list and q_vec_list[0] else None
        except Exception:
            q_vec = None
        if q_vec:
            texts = [c.content[:6000] for c in index.chunks]
            try:
                chunk_vecs = provider.embed(texts) if texts else []
            except Exception:
                chunk_vecs = None

    def cosine(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    preferred_norm = [_norm_rel(p).lower() for p in query.preferred_files]
    target_exact = {_norm_rel(p).lower() for p in query.target_files if p}

    def _allowed_path(rel: str) -> bool:
        if not allowed:
            return True
        r = _norm_rel(rel).lower()
        for a in allowed:
            al = _norm_rel(a).lower()
            if r == al or r.startswith(al + "/"):
                return True
        return False

    hits: List[RetrievalHit] = []
    for idx_i, chunk in enumerate(index.chunks):
        rel = chunk.file_path
        if _under_forbidden_root(rel, forbidden):
            continue
        if not _allowed_path(rel):
            continue
        if query.languages and chunk.language not in query.languages:
            continue

        score = 0.0
        reasons: List[str] = []

        rnl = _norm_rel(rel).lower()
        if rnl in target_exact:
            score += 100.0
            reasons.append("target_file")
        else:
            for te in target_exact:
                if te and (rnl.endswith("/" + te) or rnl == te):
                    score += 100.0
                    reasons.append("target_file")
                    break

        for pf in preferred_norm:
            if not pf:
                continue
            if rnl == pf or rnl.endswith("/" + pf):
                if "target_file" not in reasons:
                    score += 50.0
                    reasons.append("planner_preferred")
                break
            if pf in rnl:
                score += 35.0
                reasons.append("path_partial_preferred")
                break

        for sym in chunk.symbol_names:
            sl = sym.lower()
            if sl in tokens or sl in q_lower:
                score += 40.0
                reasons.append(f"symbol:{sym}")
                break

        if query.layer_boost_api and ("/api/" in rnl or "route.ts" in rnl or rnl.startswith("app/api")):
            score += 30.0
            reasons.append("api_layer")

        if query.layer_boost_data and any(x in rnl for x in ("schema", "drizzle", "prisma", "db/", "/db/")):
            score += 25.0
            reasons.append("data_layer")

        lx = _lexical_score(chunk.content, tokens)
        score += lx
        if lx > 0:
            reasons.append("lexical")

        score += _path_token_score(rel, tokens)

        if use_emb and q_vec and chunk_vecs and idx_i < len(chunk_vecs):
            cv = chunk_vecs[idx_i]
            if cv:
                sim = cosine(q_vec, cv)
                score += max(0.0, sim) * 50.0
                if sim > 0.2:
                    reasons.append("embedding")

        if score > 0:
            prev = "\n".join(chunk.content.splitlines()[:4])
            hits.append(
                RetrievalHit(
                    file_path=rel,
                    chunk_id=chunk.chunk_id,
                    score=score,
                    reasons=reasons,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    preview=prev[:240],
                    symbol_names=list(chunk.symbol_names),
                )
            )

    hits.sort(key=lambda h: -h.score)
    hits = hits[: query.max_hits]

    file_scores: Dict[str, float] = {}
    for h in hits:
        file_scores[h.file_path] = max(file_scores.get(h.file_path, 0), h.score)
    top_files = sorted(file_scores.items(), key=lambda x: -x[1])[:8]

    sym_seen: Dict[str, str] = {}
    for s in index.symbols:
        if any(t in s.name.lower() for t in tokens) or s.name.lower() in q_lower:
            sym_seen[s.name] = s.file_path
    top_symbols = list(sym_seen.items())[:12]

    ms = (time.perf_counter() - t0) * 1000
    return RetrievalResult(
        hits=hits,
        top_files=top_files,
        top_symbols=top_symbols,
        provenance={
            "method": "hybrid_lexical_path_symbol_priors",
            "chunks_scored": len(index.chunks),
            "hits_returned": len(hits),
        },
        embeddings_used=bool(use_emb and q_vec),
        query_ms=ms,
        chunks_scored=len(index.chunks),
    )


def build_retrieval_query_from_contract_spec(
    contract_spec: Dict[str, Any],
    repo_profile_v2: Dict[str, Any],
    *,
    user_text: str = "",
    decision_plan: Optional[Dict[str, Any]] = None,
) -> RetrievalQuery:
    parts: List[str] = []
    if user_text:
        parts.append(user_text)
    ac = contract_spec.get("acceptance_criteria") or []
    if isinstance(ac, list):
        parts.extend(str(x) for x in ac[:5])
    text = " ".join(parts).strip() or (contract_spec.get("intent") or "code")

    targets: List[str] = []
    for tf in contract_spec.get("target_files") or []:
        if isinstance(tf, str) and tf.strip():
            targets.append(_norm_rel(tf))

    preferred: List[str] = []
    if decision_plan:
        ex = decision_plan.get("exploration_plan") or {}
        for c in ex.get("ranked_candidates") or []:
            if isinstance(c, dict) and c.get("kind") == "file":
                p = c.get("path")
                if isinstance(p, str) and p.strip():
                    preferred.append(_norm_rel(p))
        ep = decision_plan.get("execution_plan") or {}
        for f in ep.get("expected_files_changed") or []:
            if isinstance(f, str):
                preferred.append(_norm_rel(f))

    repo = repo_profile_v2 or {}
    for r in repo.get("api_routes") or []:
        if isinstance(r, dict) and r.get("file"):
            preferred.append(_norm_rel(str(r["file"])))
    for vf in repo.get("validation_files") or []:
        if isinstance(vf, str):
            preferred.append(_norm_rel(vf))
    for sf in repo.get("db_schema_files") or []:
        if isinstance(sf, str):
            preferred.append(_norm_rel(sf))

    tgt_set = {t.lower() for t in targets}
    seen: Set[str] = set()
    pref_unique: List[str] = []
    for p in preferred:
        pl = _norm_rel(p).lower()
        if pl in tgt_set:
            continue
        if pl not in seen:
            seen.add(pl)
            pref_unique.append(_norm_rel(p))

    forbidden: List[str] = []
    if "ui" in (contract_spec.get("forbidden_layers") or []):
        ep = repo.get("entrypoints") or {}
        if isinstance(ep, dict):
            forbidden.extend(str(x) for x in (ep.get("ui_roots") or []) if x)
    for nested_root in _derive_nested_project_forbidden_roots(repo):
        if nested_root not in forbidden:
            forbidden.append(nested_root)

    scope = set(contract_spec.get("scope") or [])
    boost_api = bool(scope & {"api", "fullstack"}) or "api" in (repo.get("layers_detected") or [])
    boost_data = bool(scope & {"data"}) or "data" in (repo.get("layers_detected") or [])

    agg = os.environ.get(ENV_RETRIEVAL_AGGRESSION, "").strip().lower()
    if agg == "aggressive":
        max_hits = 24
        pref_cap = 60
    elif agg == "minimal":
        max_hits = 8
        pref_cap = 20
    else:
        max_hits = 14
        pref_cap = 40

    return RetrievalQuery(
        text=text,
        forbidden_roots=forbidden,
        target_files=targets,
        preferred_files=pref_unique[:pref_cap],
        max_hits=max_hits,
        layer_boost_api=boost_api,
        layer_boost_data=boost_data,
    )


# Deprecated public name (same callable).
build_retrieval_query_from_taskspec = build_retrieval_query_from_contract_spec


def retrieval_result_to_prompt_block(
    result: RetrievalResult,
    query_text: str = "",
    *,
    mode: str = "advisory",
    confidence: float = 0.0,
    actionable: bool = False,
    runtime_influenced: bool = False,
    likely_targets: Optional[List[str]] = None,
    likely_reasons: Optional[List[str]] = None,
    highlight_chunks: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Human-readable retrieval context (no raw JSON).
    Sprint 1 callers pass only (result, query_text) → advisory-style block.
    """
    likely_targets = likely_targets or []
    likely_reasons = likely_reasons or []
    highlight_chunks = highlight_chunks or []
    mode_l = (mode or "advisory").strip().lower()
    title = "[RETRIEVAL-GUIDED CONTEXT]" if mode_l in ("influencing", "fallback") else "[REPO RETRIEVAL — advisory]"
    lines = [
        title,
        f"Mode: {mode_l}",
        f"Actionable: {'yes' if actionable else 'no'} | Confidence: {confidence:.2f}",
        f"Runtime influenced (read priority): {'yes' if runtime_influenced else 'no'}",
        f"Query: {query_text[:200]}{'…' if len(query_text) > 200 else ''}",
        f"Embeddings used: {'yes' if result.embeddings_used else 'no'}",
        f"Latency: {result.query_ms:.1f} ms | chunks_scored: {result.chunks_scored}",
    ]
    if result.top_files:
        lines.append("Top files (retrieval-ranked):")
        for path, sc in result.top_files[:8]:
            lines.append(f"  - {path} :: score={sc:.1f} :: hybrid rank")
    if likely_targets:
        lines.append("Likely edit targets (candidates only, not auto-applied):")
        for i, p in enumerate(likely_targets[:8]):
            rs = likely_reasons[i] if i < len(likely_reasons) else ""
            lines.append(f"  - {p} :: {rs}")
    if result.top_symbols:
        lines.append("Top symbols:")
        for sym, fp in result.top_symbols[:10]:
            lines.append(f"  - {sym} :: {fp}")
    if highlight_chunks:
        lines.append("Relevant chunks (line ranges):")
        for ch in highlight_chunks[:8]:
            fp = ch.get("file_path", "")
            sl = ch.get("start_line", 0)
            el = ch.get("end_line", 0)
            r = ch.get("reason", "")
            lines.append(f"  - {fp}:{sl}-{el} :: {r}")
    elif result.hits:
        lines.append("Relevant chunks:")
        for h in result.hits[:6]:
            lines.append(
                f"  - {h.file_path}:{h.start_line}-{h.end_line} :: "
                f"{', '.join(h.reasons[:3]) or 'ranked'}"
            )
    lines.append("")
    return "\n".join(lines)


def apply_retrieval_to_session(
    session: Any,
    result: RetrievalResult,
    index_stats: RetrievalIndexStats,
    cache_info: RetrievalCacheInfo,
    *,
    query_dict: Optional[Dict[str, Any]] = None,
    retrieval_actionable: bool = False,
    retrieval_confidence: float = 0.0,
    retrieval_mode: str = "advisory",
    retrieval_runtime_influenced: bool = False,
    likely_edit_targets: Optional[List[str]] = None,
    likely_edit_target_reasons: Optional[List[str]] = None,
    merged_candidate_order: Optional[List[Dict[str, Any]]] = None,
    retrieval_highlight_chunks: Optional[List[Dict[str, Any]]] = None,
) -> None:
    session.retrieval_enabled = True
    session.retrieval_query = dict(query_dict or {})
    session.retrieval_result = result.model_dump(mode="json")
    session.retrieval_top_files = [{"path": p, "score": s} for p, s in result.top_files]
    session.retrieval_top_symbols = [{"symbol": s, "file": f} for s, f in result.top_symbols]
    session.retrieval_index_stats = index_stats.model_dump(mode="json")
    session.retrieval_cache_info = cache_info.model_dump(mode="json")
    session.retrieval_used_embeddings = result.embeddings_used
    session.retrieval_provenance = dict(result.provenance)
    session.retrieval_actionable = bool(retrieval_actionable)
    session.retrieval_confidence = float(retrieval_confidence)
    session.retrieval_mode = str(retrieval_mode or "advisory")
    session.retrieval_runtime_influenced = bool(retrieval_runtime_influenced)
    session.likely_edit_targets = list(likely_edit_targets or [])
    session.likely_edit_target_reasons = list(likely_edit_target_reasons or [])
    session.merged_candidate_order = list(merged_candidate_order or [])
    session.retrieval_highlight_chunks = list(retrieval_highlight_chunks or [])


def apply_retrieval_with_build_result(
    root: Path,
    session: Any,
    user_text: str,
    build_res: RepoRetrievalBuildResult,
) -> None:
    """
    Run query + merge + session apply using an index built earlier (e.g. in parallel with decision planner).
    Preconditions: same as run_retrieval_for_session (retrieval enabled, operational contract spec).
    """
    _ = root.resolve()
    ts = get_task_contract_spec(session)
    if not isinstance(ts, Mapping):
        session.retrieval_enabled = False
        return
    rpd = getattr(session, "repo_profile", None) or {}
    v2 = rpd.get("profile_v2") if isinstance(rpd, dict) else None
    repo_v2 = v2 if isinstance(v2, dict) else {}

    _dp = get_task_contract_decision_plan(session)
    dq = build_retrieval_query_from_contract_spec(
        ts,
        repo_v2,
        user_text=user_text,
        decision_plan=_dp or None,
    )
    rr = retrieve_code(dq, build_res.index)
    actionable = retrieval_is_actionable(
        rr, ts, repo_v2, dq, decision_plan=_dp or None
    )
    mode = resolve_retrieval_mode(actionable)
    conf = retrieval_confidence_score(rr) + _planner_overlap_bonus(rr, _dp or None)
    conf = round(min(1.0, conf), 4)
    include_tier = actionable
    merged = merge_exploration_and_retrieval(
        ts,
        getattr(session, "exploration_plan", None) or {},
        _dp or None,
        repo_v2,
        rr,
        dq,
        include_retrieval_tier=include_tier,
    )
    influenced = mode == "influencing"
    lt, lr = derive_likely_edit_targets(rr, dq, ts, repo_v2, actionable=actionable)
    highlights = build_retrieval_highlight_chunks(rr)
    prov = dict(rr.provenance)
    prov["retrieval_mode"] = mode
    prov["retrieval_actionable"] = actionable
    prov["retrieval_confidence"] = conf
    prov["retrieval_runtime_influenced"] = influenced
    rr2 = rr.model_copy(update={"provenance": prov})
    apply_retrieval_to_session(
        session,
        rr2,
        build_res.stats,
        build_res.cache_info,
        query_dict=dq.model_dump(mode="json"),
        retrieval_actionable=actionable,
        retrieval_confidence=conf,
        retrieval_mode=mode,
        retrieval_runtime_influenced=influenced,
        likely_edit_targets=lt,
        likely_edit_target_reasons=lr,
        merged_candidate_order=merged,
        retrieval_highlight_chunks=highlights,
    )


def run_retrieval_for_session(
    root: Path,
    session: Any,
    user_text: str,
    *,
    force_refresh: bool = False,
) -> None:
    if not is_retrieval_enabled():
        clear_retrieval_session_fields(session)
        return
    if not contract_has_operational_spec(session):
        session.retrieval_enabled = False
        return

    rpd = getattr(session, "repo_profile", None) or {}
    v2 = rpd.get("profile_v2") if isinstance(rpd, dict) else None
    repo_v2 = v2 if isinstance(v2, dict) else {}

    try:
        build_res = build_retrieval_index(root, repo_v2, force_refresh=force_refresh)
    except Exception as e:
        logger.warning("Retrieval index build failed: %s", e)
        session.retrieval_enabled = False
        return

    apply_retrieval_with_build_result(root, session, user_text, build_res)


def build_retrieval_prompt_block(session: Any) -> str:
    if getattr(session, "micro_task_kind", None):
        return ""
    agents_hint = ""
    ap = str(getattr(session, "agents_md_path", "") or "").strip()
    if ap:
        agents_hint = (
            f"[Retrieval context: project rules are loaded from `{ap}` in the system prompt — "
            f"prioritize paths and conventions described there when ranking files.]\n\n"
        )
    if not getattr(session, "retrieval_enabled", False):
        return agents_hint
    raw = getattr(session, "retrieval_result", None) or {}
    if not raw:
        return agents_hint
    try:
        rr = RetrievalResult.model_validate(raw)
    except Exception:
        return agents_hint
    q = getattr(session, "retrieval_query", None) or {}
    qt = ""
    if isinstance(q, dict):
        qt = str(q.get("text") or "")
    mode = str(getattr(session, "retrieval_mode", "advisory") or "advisory")
    return agents_hint + retrieval_result_to_prompt_block(
        rr,
        qt,
        mode=mode,
        confidence=float(getattr(session, "retrieval_confidence", 0.0) or 0.0),
        actionable=bool(getattr(session, "retrieval_actionable", False)),
        runtime_influenced=bool(getattr(session, "retrieval_runtime_influenced", False)),
        likely_targets=list(getattr(session, "likely_edit_targets", None) or []),
        likely_reasons=list(getattr(session, "likely_edit_target_reasons", None) or []),
        highlight_chunks=list(getattr(session, "retrieval_highlight_chunks", None) or []),
    )


def clear_retrieval_session_fields(session: Any) -> None:
    session.retrieval_enabled = False
    session.retrieval_query = {}
    session.retrieval_result = {}
    session.retrieval_top_files = []
    session.retrieval_top_symbols = []
    session.retrieval_index_stats = {}
    session.retrieval_cache_info = {}
    session.retrieval_used_embeddings = False
    session.retrieval_provenance = {}
    session.retrieval_actionable = False
    session.retrieval_confidence = 0.0
    session.retrieval_mode = ""
    session.retrieval_runtime_influenced = False
    session.likely_edit_targets = []
    session.likely_edit_target_reasons = []
    session.merged_candidate_order = []
    session.retrieval_highlight_chunks = []
