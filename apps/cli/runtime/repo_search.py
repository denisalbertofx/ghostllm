"""
Safe read-only code search for EXPLORE phase (Exploration Engine v2).

Backends (in order):
  1. ripgrep (rg) if available — fast, supports multi-file symbol/text search.
  2. Python-based recursive search — zero external dependency fallback.

All searches stay inside repo_root; path traversal outside is silently denied.
The interface is intentionally minimal so the model/runtime can reason over results
without large unstructured blobs.

Public API
----------
search_code(query, repo_root, mode, extensions, max_results) -> SearchCodeResult
is_ripgrep_available() -> bool
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

SEARCH_MODE_SYMBOL = "symbol"        # find definition / usage of a name
SEARCH_MODE_FILENAME = "filename"    # filenames containing the pattern
SEARCH_MODE_PATH_EXISTS = "path_exists"  # does a concrete path exist in repo?
SEARCH_MODE_TEXT = "text"            # raw text / regex search in code files

_DEFAULT_MAX_RESULTS = 20
_DEFAULT_CONTEXT_LINES = 1
_RG_TIMEOUT_S = 8

# Extensions scanned by Python fallback (conservative but covers typical repos)
_CODE_EXTENSIONS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".md",
    ".html", ".css", ".sh", ".bat",
})

# Dirs excluded from recursive Python search
_SKIP_DIRS = frozenset({
    ".git", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", "dist", "build", "out", ".next",
    ".ghost", ".cursor",
})


# ── Result model ───────────────────────────────────────────────────────────────

@dataclass
class SearchMatch:
    """A single matched location (file + optional line range)."""
    path: str                        # relative to repo_root
    lines: List[str] = field(default_factory=list)   # matched / context lines
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    exact: bool = True               # False = fuzzy / partial match


@dataclass
class SearchCodeResult:
    mode: str
    query: str
    matches: List[SearchMatch] = field(default_factory=list)
    truncated: bool = False
    backend: str = "python"          # "ripgrep" | "python"
    error: Optional[str] = None
    total_files_scanned: int = 0

    # --- convenience -------------------------------------------------------
    def found(self) -> bool:
        return bool(self.matches)

    def summary(self) -> str:
        """Short human-readable summary (for prompt injection / artifact)."""
        if self.error:
            return f"search_error: {self.error}"
        if not self.matches:
            return f"No matches for {self.mode}={self.query!r}"
        n = len(self.matches)
        trunc = " (truncated)" if self.truncated else ""
        first_paths = ", ".join(m.path for m in self.matches[:3])
        if n > 3:
            first_paths += f" (+{n - 3} more)"
        return f"{n} match(es){trunc}: {first_paths}"

    def to_tool_payload(self) -> dict:
        """JSON-serialisable payload returned as tool result to the model."""
        return {
            "mode": self.mode,
            "query": self.query,
            "found": self.found(),
            "match_count": len(self.matches),
            "truncated": self.truncated,
            "backend": self.backend,
            "matches": [
                {
                    "path": m.path,
                    "line_start": m.line_start,
                    "line_end": m.line_end,
                    "lines": m.lines[:8],   # cap per-match lines in payload
                    "exact": m.exact,
                }
                for m in self.matches
            ],
            **({"error": self.error} if self.error else {}),
        }


# ── Availability ──────────────────────────────────────────────────────────────

def is_ripgrep_available() -> bool:
    """True if `rg` binary is on PATH."""
    return shutil.which("rg") is not None


# ── Safety helpers ────────────────────────────────────────────────────────────

def _safe_repo_root(repo_root: str) -> Optional[Path]:
    """Resolve repo_root; return None if not an existing directory."""
    try:
        p = Path(repo_root).resolve()
        return p if p.is_dir() else None
    except Exception:
        return None


def _rel_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


# ── Backend: ripgrep ──────────────────────────────────────────────────────────

def _rg_search_symbol(
    pattern: str,
    root: Path,
    max_results: int,
    context_lines: int = _DEFAULT_CONTEXT_LINES,
) -> SearchCodeResult:
    """Use rg to find a symbol (word-boundary match) across code files."""
    try:
        cmd = [
            "rg",
            "--line-number",
            "--no-heading",
            "--word-regexp",
            f"--context={context_lines}",
            f"--max-count={max_results * 2}",
            "--type-add", "code:*.{py,ts,tsx,js,jsx,json,yaml,yml,toml,md,sh}",
            "--type", "code",
            pattern,
            str(root),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_RG_TIMEOUT_S, cwd=str(root)
        )
        return _parse_rg_output(result.stdout, root, SEARCH_MODE_SYMBOL, pattern, max_results, "ripgrep")
    except subprocess.TimeoutExpired:
        return SearchCodeResult(mode=SEARCH_MODE_SYMBOL, query=pattern, error="rg timeout", backend="ripgrep")
    except Exception as e:
        return SearchCodeResult(mode=SEARCH_MODE_SYMBOL, query=pattern, error=str(e), backend="ripgrep")


def _rg_search_text(
    pattern: str,
    root: Path,
    max_results: int,
    context_lines: int = _DEFAULT_CONTEXT_LINES,
) -> SearchCodeResult:
    """Use rg to search raw text/regex across code files."""
    try:
        cmd = [
            "rg",
            "--line-number",
            "--no-heading",
            f"--context={context_lines}",
            f"--max-count={max_results * 2}",
            "--type-add", "code:*.{py,ts,tsx,js,jsx,json,yaml,yml,toml,md,sh}",
            "--type", "code",
            pattern,
            str(root),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_RG_TIMEOUT_S, cwd=str(root)
        )
        return _parse_rg_output(result.stdout, root, SEARCH_MODE_TEXT, pattern, max_results, "ripgrep")
    except subprocess.TimeoutExpired:
        return SearchCodeResult(mode=SEARCH_MODE_TEXT, query=pattern, error="rg timeout", backend="ripgrep")
    except Exception as e:
        return SearchCodeResult(mode=SEARCH_MODE_TEXT, query=pattern, error=str(e), backend="ripgrep")


def _parse_rg_output(
    stdout: str,
    root: Path,
    mode: str,
    query: str,
    max_results: int,
    backend: str,
) -> SearchCodeResult:
    """Parse `rg --line-number --no-heading` output into SearchCodeResult."""
    # Format: filepath:lineno:content  or filepath-lineno-content (context)
    matches_by_file: dict[str, list[tuple[int, str]]] = {}
    for raw_line in stdout.splitlines():
        # match line: file:lineno:content
        m = re.match(r"^(.*?):(\d+):(.*)", raw_line)
        if m:
            fpath, lineno, content = m.group(1), int(m.group(2)), m.group(3)
            rel = fpath.replace("\\", "/")
            # strip repo_root prefix if present
            try:
                rel = str(Path(fpath).relative_to(root)).replace("\\", "/")
            except ValueError:
                pass
            matches_by_file.setdefault(rel, []).append((lineno, content))

    matches: List[SearchMatch] = []
    for rel_path, line_tuples in list(matches_by_file.items())[:max_results]:
        if not line_tuples:
            continue
        line_nos = [lt[0] for lt in line_tuples]
        lines_text = [lt[1] for lt in line_tuples]
        matches.append(SearchMatch(
            path=rel_path,
            lines=lines_text[:8],
            line_start=min(line_nos),
            line_end=max(line_nos),
        ))

    truncated = len(matches_by_file) > max_results
    return SearchCodeResult(
        mode=mode, query=query,
        matches=matches,
        truncated=truncated,
        backend=backend,
    )


def _rg_search_filename(pattern: str, root: Path, max_results: int) -> SearchCodeResult:
    """Use rg --files to list filenames, then filter by pattern."""
    try:
        cmd = ["rg", "--files", str(root)]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_RG_TIMEOUT_S, cwd=str(root)
        )
        pat_lower = pattern.lower().replace("*", "")
        matches: List[SearchMatch] = []
        for fpath in result.stdout.splitlines():
            rel = fpath.replace("\\", "/")
            try:
                rel = str(Path(fpath).relative_to(root)).replace("\\", "/")
            except ValueError:
                pass
            if pat_lower in Path(rel).name.lower():
                matches.append(SearchMatch(path=rel, exact=True))
            if len(matches) >= max_results:
                break
        truncated = len(result.stdout.splitlines()) > max_results
        return SearchCodeResult(
            mode=SEARCH_MODE_FILENAME, query=pattern,
            matches=matches, truncated=truncated, backend="ripgrep",
        )
    except Exception as e:
        return SearchCodeResult(mode=SEARCH_MODE_FILENAME, query=pattern, error=str(e), backend="ripgrep")


# ── Backend: Python recursive search ──────────────────────────────────────────

def _py_search_symbol(pattern: str, root: Path, max_results: int) -> SearchCodeResult:
    """Python fallback: search symbol (word boundary) across code files."""
    try:
        word_re = re.compile(r"\b" + re.escape(pattern) + r"\b")
    except re.error:
        word_re = re.compile(re.escape(pattern))
    return _py_text_search(pattern, root, max_results, word_re, SEARCH_MODE_SYMBOL)


def _py_search_text(pattern: str, root: Path, max_results: int) -> SearchCodeResult:
    """Python fallback: raw text/regex search across code files."""
    try:
        pat_re = re.compile(pattern, re.IGNORECASE)
    except re.error:
        pat_re = re.compile(re.escape(pattern), re.IGNORECASE)
    return _py_text_search(pattern, root, max_results, pat_re, SEARCH_MODE_TEXT)


def _py_text_search(
    pattern: str,
    root: Path,
    max_results: int,
    pat_re: "re.Pattern[str]",
    mode: str,
) -> SearchCodeResult:
    matches: List[SearchMatch] = []
    total_scanned = 0
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext not in _CODE_EXTENSIONS:
                continue
            fpath = Path(dirpath) / fname
            rel = _rel_path(root, fpath)
            total_scanned += 1
            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            hit_lines: list[tuple[int, str]] = []
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pat_re.search(line):
                    hit_lines.append((lineno, line.rstrip()))
            if hit_lines:
                line_nos = [lt[0] for lt in hit_lines]
                lines_text = [lt[1] for lt in hit_lines]
                matches.append(SearchMatch(
                    path=rel,
                    lines=lines_text[:8],
                    line_start=min(line_nos),
                    line_end=max(line_nos),
                ))
                if len(matches) >= max_results:
                    return SearchCodeResult(
                        mode=mode, query=pattern,
                        matches=matches, truncated=True,
                        backend="python", total_files_scanned=total_scanned,
                    )
    return SearchCodeResult(
        mode=mode, query=pattern,
        matches=matches, truncated=False,
        backend="python", total_files_scanned=total_scanned,
    )


def _py_search_filename(pattern: str, root: Path, max_results: int) -> SearchCodeResult:
    matches: List[SearchMatch] = []
    pat_lower = pattern.lower().replace("*", "")
    total = 0
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            total += 1
            if pat_lower in fname.lower():
                fpath = Path(dirpath) / fname
                rel = _rel_path(root, fpath)
                matches.append(SearchMatch(path=rel, exact=pat_lower == fname.lower()))
                if len(matches) >= max_results:
                    return SearchCodeResult(
                        mode=SEARCH_MODE_FILENAME, query=pattern,
                        matches=matches, truncated=True,
                        backend="python", total_files_scanned=total,
                    )
    return SearchCodeResult(
        mode=SEARCH_MODE_FILENAME, query=pattern,
        matches=matches, truncated=False,
        backend="python", total_files_scanned=total,
    )


def _check_path_exists(path: str, root: Path) -> SearchCodeResult:
    """Resolve a path against repo_root and check existence."""
    # Normalize: strip leading slash / backslash
    clean = path.lstrip("/\\").replace("\\", "/")
    candidate = (root / clean).resolve()
    # Safety: must stay inside repo root
    try:
        candidate.relative_to(root)
    except ValueError:
        return SearchCodeResult(
            mode=SEARCH_MODE_PATH_EXISTS, query=path,
            error="Path would escape repo root — denied",
        )
    exists = candidate.exists()
    rel = _rel_path(root, candidate)
    if exists:
        match = SearchMatch(path=rel, exact=True)
        return SearchCodeResult(
            mode=SEARCH_MODE_PATH_EXISTS, query=path,
            matches=[match], backend="python",
        )
    return SearchCodeResult(
        mode=SEARCH_MODE_PATH_EXISTS, query=path,
        matches=[], backend="python",
    )


# ── Public API ────────────────────────────────────────────────────────────────

def search_code(
    query: str,
    repo_root: str,
    mode: str = SEARCH_MODE_SYMBOL,
    extensions: Optional[List[str]] = None,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> SearchCodeResult:
    """
    Search for symbols, filenames, paths, or text inside ``repo_root``.

    Parameters
    ----------
    query       : the symbol name, filename fragment, path, or text/regex pattern.
    repo_root   : absolute path to the repo root (enforced; no traversal outside).
    mode        : one of SEARCH_MODE_* constants.
    extensions  : optional list of file extensions to restrict (Python fallback only).
    max_results : maximum number of matched files returned.

    Returns
    -------
    SearchCodeResult with structured match list + metadata.
    """
    if not query or not isinstance(query, str):
        return SearchCodeResult(mode=mode, query=str(query or ""), error="empty query")

    root = _safe_repo_root(repo_root)
    if root is None:
        return SearchCodeResult(mode=mode, query=query, error=f"repo_root not found: {repo_root}")

    max_results = max(1, min(int(max_results or _DEFAULT_MAX_RESULTS), 50))

    if mode == SEARCH_MODE_PATH_EXISTS:
        return _check_path_exists(query, root)

    if mode == SEARCH_MODE_FILENAME:
        if is_ripgrep_available():
            r = _rg_search_filename(query, root, max_results)
            if r.error is None:
                return r
        return _py_search_filename(query, root, max_results)

    if mode == SEARCH_MODE_SYMBOL:
        if is_ripgrep_available():
            r = _rg_search_symbol(query, root, max_results)
            if r.error is None:
                return r
        return _py_search_symbol(query, root, max_results)

    if mode == SEARCH_MODE_TEXT:
        if is_ripgrep_available():
            r = _rg_search_text(query, root, max_results)
            if r.error is None:
                return r
        return _py_search_text(query, root, max_results)

    return SearchCodeResult(mode=mode, query=query, error=f"unknown mode: {mode}")
