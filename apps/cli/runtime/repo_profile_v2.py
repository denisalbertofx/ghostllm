"""
Repo Semantic Analyzer — RepoProfile v2.
Disk-only, deterministic, no shell/network.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

PROFILE_VERSION = "2.1.0"

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

DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_BYTES = 256 * 1024

_SIGNATURE_FILES = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "tsconfig.json",
    "tsconfig.base.json",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "drizzle.config.ts",
    "drizzle.config.js",
    "AGENTS.md",
    "agents.md",
)

_EXPORT_HANDLER_RE = re.compile(
    r"(?:export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS))\b",
    re.IGNORECASE,
)

_ROUTE_GROUP_RE = re.compile(r"^\([^/)]+\)$")


class StackBlock(BaseModel):
    runtime: str = "unknown"
    language: List[str] = Field(default_factory=list)
    framework: List[str] = Field(default_factory=list)
    orm: List[str] = Field(default_factory=list)
    database: List[str] = Field(default_factory=list)
    package_manager: List[str] = Field(default_factory=list)
    test_runner: List[str] = Field(default_factory=list)


class EntrypointsBlock(BaseModel):
    api_roots: List[str] = Field(default_factory=list)
    ui_roots: List[str] = Field(default_factory=list)
    db_schema_roots: List[str] = Field(default_factory=list)


class ApiRouteEntry(BaseModel):
    method: Optional[str] = None
    path: Optional[str] = None
    file: str
    kind: str


class VerificationCommandsBlock(BaseModel):
    typecheck: Optional[str] = None
    build: Optional[str] = None
    lint: Optional[str] = None
    tests: Optional[str] = None
    source: List[str] = Field(default_factory=list)


class ConstraintsBlock(BaseModel):
    ignored_dirs: List[str] = Field(default_factory=list)
    max_files_scanned: int = DEFAULT_MAX_FILES
    max_bytes_per_file: int = DEFAULT_MAX_BYTES


class CacheMeta(BaseModel):
    used: bool = False
    cache_key: str = ""
    source: str = "fresh"


class RepoProfileV2(BaseModel):
    version: str = PROFILE_VERSION
    root: str
    generated_at: datetime
    cache: CacheMeta = Field(default_factory=CacheMeta)
    stack: StackBlock = Field(default_factory=StackBlock)
    layers_detected: List[str] = Field(default_factory=list)
    key_files: List[str] = Field(default_factory=list)
    important_folders: List[str] = Field(default_factory=list)
    entrypoints: EntrypointsBlock = Field(default_factory=EntrypointsBlock)
    api_routes: List[ApiRouteEntry] = Field(default_factory=list)
    db_schema_files: List[str] = Field(default_factory=list)
    validation_files: List[str] = Field(default_factory=list)
    verification_commands: VerificationCommandsBlock = Field(default_factory=VerificationCommandsBlock)
    constraints: ConstraintsBlock = Field(default_factory=ConstraintsBlock)
    confidence: float = 0.0
    evidence_lines: List[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


def _read_text_limited(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as f:
            raw = f.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _safe_toml_loads(text: str) -> Any:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _rel_posix(root: Path, path: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return str(path).replace("\\", "/")
    return "." if not rel.parts else str(rel).replace("\\", "/")


def _discover_node_package_roots(root: Path) -> List[Path]:
    seen: Set[str] = set()
    out: List[Path] = []

    def add(candidate: Path) -> None:
        if not candidate.is_dir() or not (candidate / "package.json").is_file():
            return
        rel = _rel_posix(root, candidate)
        if rel in seen:
            return
        seen.add(rel)
        out.append(candidate)

    add(root)
    for name in ("apps", "packages"):
        base = root / name
        if not base.is_dir():
            continue
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.name in IGNORED_DIR_NAMES or child.name.startswith("."):
                continue
            add(child)
            try:
                grandchildren = sorted(child.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                grandchildren = []
            for grandchild in grandchildren:
                if grandchild.is_dir() and grandchild.name not in IGNORED_DIR_NAMES and not grandchild.name.startswith("."):
                    add(grandchild)
    for name in ("web", "frontend", "ui"):
        add(root / name)
    return out


def _dependency_name(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return re.split(r"[<>=!~\s\[]", raw, maxsplit=1)[0].strip().lower()


def _pyproject_dependency_names(root: Path, max_bytes: int) -> List[str]:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    text = _read_text_limited(pyproject, max_bytes)
    doc = _safe_toml_loads(text)
    if not isinstance(doc, dict):
        return []
    project = doc.get("project") or {}
    deps = list(project.get("dependencies") or [])
    opt = project.get("optional-dependencies") or {}
    if isinstance(opt, dict):
        for values in opt.values():
            if isinstance(values, list):
                deps.extend(values)
    names = [_dependency_name(dep) for dep in deps]
    return [name for name in names if name]


def _detect_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "package-lock.json").is_file():
        return "npm"
    return "unknown"


def _pm_run(pm: str, script: str) -> str:
    if pm == "pnpm":
        return f"pnpm -s run {script}"
    if pm == "yarn":
        return f"yarn {script}"
    if pm == "npm":
        return f"npm run {script}"
    return f"npm run {script}"


def _parse_agents_md_commands(text: str) -> Dict[str, str]:
    """Extract typecheck/build/lint/test commands from AGENTS.md (backticks + labeled lines)."""
    out: Dict[str, str] = {}
    if not text.strip():
        return out
    # `npm run typecheck` style after a keyword line (allow leading whitespace / markdown)
    labeled = re.compile(
        r"(?im)^\s*(?:[-*]\s+)?(?:#+\s*)?(typecheck|build|lint|tests?)\s*[:：]\s*[`\"']([^`\"'\n\r]+)[`\"']",
    )
    for m in labeled.finditer(text):
        key = m.group(1).lower()
        if key == "test":
            key = "tests"
        cmd = m.group(2).strip()
        if cmd and "|" not in cmd and "head" not in cmd and "tail" not in cmd:
            out[key] = cmd
    # Fallback: first bare `npm run X` / `pnpm run X` per category in sections
    if "typecheck" not in out:
        m = re.search(r"`(npm run typecheck|pnpm(?:\s+-s)?\s+run\s+typecheck|yarn\s+typecheck)`", text, re.I)
        if m:
            out["typecheck"] = m.group(1).strip("`")
    return out


def _deps_list(pkg: Dict[str, Any]) -> List[str]:
    deps = pkg.get("dependencies") or {}
    dev = pkg.get("devDependencies") or {}
    return [str(k).lower() for k in {**deps, **dev}.keys()]


def _build_verification_commands(
    root: Path,
    pkg: Optional[Dict[str, Any]],
    pm: str,
    has_typescript: bool,
    agents_cmds: Dict[str, str],
    evidence: List[str],
) -> VerificationCommandsBlock:
    sources: List[str] = []
    typecheck: Optional[str] = None
    build: Optional[str] = None
    lint: Optional[str] = None
    tests: Optional[str] = None
    scripts: Dict[str, str] = {}
    if isinstance(pkg, dict):
        scripts = pkg.get("scripts") or {}
        if not isinstance(scripts, dict):
            scripts = {}

    def add_source(s: str) -> None:
        if s not in sources:
            sources.append(s)

    if agents_cmds:
        add_source("agents_md")
        if "typecheck" in agents_cmds:
            typecheck = agents_cmds["typecheck"].strip()
            evidence.append(f"Derived typecheck command from AGENTS.md: {typecheck}")
        if "build" in agents_cmds:
            build = agents_cmds["build"].strip()
            evidence.append(f"Derived build command from AGENTS.md: {build}")
        if "lint" in agents_cmds:
            lint = agents_cmds["lint"].strip()
            evidence.append(f"Derived lint command from AGENTS.md: {lint}")
        if "tests" in agents_cmds:
            tests = agents_cmds["tests"].strip()
            evidence.append(f"Derived tests command from AGENTS.md: {tests}")

    add_source("package_json")
    if not build and scripts.get("build"):
        build = _pm_run(pm, "build") if pm != "unknown" else "npm run build"
        evidence.append(f"Derived build command from package.json scripts.build: {build}")
    if not lint and scripts.get("lint"):
        lint = _pm_run(pm, "lint") if pm != "unknown" else "npm run lint"
        evidence.append(f"Derived lint command from package.json scripts.lint: {lint}")
    if not tests and scripts.get("test"):
        tests = _pm_run(pm, "test") if pm != "unknown" else "npm run test"
        evidence.append(f"Derived tests command from package.json scripts.test: {tests}")

    if not typecheck:
        if scripts.get("typecheck"):
            typecheck = _pm_run(pm, "typecheck") if pm != "unknown" else "npm run typecheck"
            evidence.append(f"Derived typecheck from package.json scripts.typecheck: {typecheck}")
        elif has_typescript:
            typecheck = "npx tsc --noEmit"
            add_source("defaults")
            evidence.append("Default typecheck for TypeScript: npx tsc --noEmit")

    if not typecheck and not build and not lint and not tests:
        add_source("unknown")

    return VerificationCommandsBlock(
        typecheck=typecheck,
        build=build,
        lint=lint,
        tests=tests,
        source=sources,
    )


def _next_config_exists(root: Path) -> bool:
    for name in ("next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs"):
        if (root / name).is_file():
            return True
    return False


def _strip_route_groups(parts: List[str]) -> List[str]:
    return [p for p in parts if p and not _ROUTE_GROUP_RE.match(p)]


def _app_router_url(root: Path, route_file: Path) -> str:
    rel = route_file.relative_to(root)
    parts = list(rel.parts[:-1])
    try:
        idx = max(i for i, p in enumerate(parts) if p == "app")
    except ValueError:
        parts = _strip_route_groups(parts)
        return "/" + "/".join(parts) if parts else "/"
    rest = parts[idx + 1 :]
    rest = _strip_route_groups(rest)
    if not rest:
        return "/"
    return "/" + "/".join(rest)


def _pages_api_url(root: Path, f: Path) -> str:
    rel = f.relative_to(root)
    parts = list(rel.parts)
    if "pages" in parts:
        i = parts.index("pages")
        rest = parts[i + 1 :]
        if rest and rest[-1].endswith((".ts", ".tsx", ".js", ".jsx")):
            rest = list(rest[:-1])
        rest = [p for p in rest if not p.startswith("_")]
        if not rest:
            return "/api"
        return "/api/" + "/".join(rest).replace("\\", "/")


def _extract_route_methods(content: str) -> List[str]:
    seen: Set[str] = set()
    order: List[str] = []
    for m in _EXPORT_HANDLER_RE.finditer(content):
        name = m.group(1).upper()
        if name not in seen:
            seen.add(name)
            order.append(name)
    return order


def _iter_files_os_walk(root: Path, rel_prefix: str, ignored: Set[str], max_files: int) -> Iterator[Path]:
    """Walk root/rel_prefix with depth-first cap."""
    base = root / rel_prefix
    if not base.is_dir():
        return
    count = 0
    for dirpath, dirnames, filenames in base.walk(top_down=True):
        dirnames[:] = [d for d in dirnames if d not in ignored and not d.startswith(".")]
        for fn in filenames:
            if count >= max_files:
                return
            yield Path(dirpath) / fn
            count += 1


def _compute_cache_key(root: Path, max_bytes: int) -> str:
    parts: List[str] = [PROFILE_VERSION, str(root.resolve())]
    for name in sorted(_SIGNATURE_FILES):
        p = root / name
        if p.is_file():
            try:
                st = p.stat()
                parts.append(f"{name}:{st.st_size}:{int(st.st_mtime_ns)}")
            except OSError:
                parts.append(f"{name}:missing")
    # top-level directories
    try:
        entries = sorted((x.name, x.is_dir()) for x in root.iterdir())
        for name, is_dir in entries:
            if is_dir and name not in IGNORED_DIR_NAMES and not name.startswith("."):
                try:
                    st = (root / name).stat()
                    parts.append(f"dir:{name}:{int(st.st_mtime_ns)}")
                except OSError:
                    parts.append(f"dir:{name}:0")
    except OSError:
        pass
    h = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return h


def _cache_path(root: Path) -> Path:
    ghost = root / ".ghost" / "cache"
    return ghost / "repo_profile_v2.json"


def _load_cache(root: Path) -> Optional[Tuple[str, RepoProfileV2]]:
    cp = _cache_path(root)
    if not cp.is_file():
        return None
    try:
        raw = _read_text_limited(cp, 2 * 1024 * 1024)
        data = json.loads(raw)
        key = data.get("cache_key", "")
        prof = data.get("profile")
        if not prof or not key:
            return None
        return key, RepoProfileV2.model_validate(prof)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _save_cache(root: Path, cache_key: str, profile: RepoProfileV2) -> None:
    cp = _cache_path(root)
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_key": cache_key,
            "profile": json.loads(profile.model_dump_json()),
        }
        cp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def _count_ts_files(root: Path, ignored: Set[str], max_count: int = 40) -> int:
    n = 0
    for dirpath, dirnames, filenames in root.walk(top_down=True):
        dirnames[:] = [d for d in dirnames if d not in ignored and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith((".ts", ".tsx")):
                n += 1
                if n >= max_count:
                    return n
    return n


def _scan_validation_files(root: Path, ignored: Set[str], max_bytes: int, evidence: List[str]) -> List[str]:
    found: List[str] = []
    candidates = [
        "lib/validations.ts",
        "lib/validation.ts",
        "src/lib/validations.ts",
        "src/lib/validation.ts",
    ]
    for rel in candidates:
        p = root / rel
        if p.is_file():
            found.append(rel.replace("\\", "/"))
            txt = _read_text_limited(p, max_bytes)
            if "zod" in txt.lower() or re.search(r"\bz\.", txt):
                evidence.append(f"Validation layer with Zod signals: {rel}")
    # light scan lib/ src/lib for *validat*
    for prefix in ("lib", "src/lib"):
        base = root / prefix
        if not base.is_dir():
            continue
        try:
            for f in base.iterdir():
                if f.is_file() and "validat" in f.name.lower() and f.suffix in (".ts", ".tsx", ".js"):
                    rel = str(f.relative_to(root)).replace("\\", "/")
                    if rel not in found:
                        found.append(rel)
                        txt = _read_text_limited(f, max_bytes)
                        if "zod" in txt.lower():
                            evidence.append(f"Zod validation file: {rel}")
        except OSError:
            pass
    return sorted(dict.fromkeys(found))


def _discover_schema_files(root: Path, evidence: List[str]) -> Tuple[List[str], List[str]]:
    """Returns (db_schema_files, db_schema_roots)."""
    files: List[str] = []
    roots: Set[str] = set()
    patterns = [
        "db/schema.ts",
        "src/db/schema.ts",
        "drizzle/schema.ts",
        "lib/db/schema.ts",
        "prisma/schema.prisma",
    ]
    for rel in patterns:
        p = root / rel
        if p.is_file():
            files.append(rel.replace("\\", "/"))
            roots.add(str(Path(rel).parts[0]))
            evidence.append(f"DB schema file: {rel}")
    drizzle_cfg = list(root.glob("drizzle.config.*"))
    for p in drizzle_cfg:
        if p.suffix in (".ts", ".js", ".mjs"):
            rel = str(p.relative_to(root)).replace("\\", "/")
            if rel not in files:
                files.append(rel)
            roots.add("drizzle")
            evidence.append(f"Drizzle config: {rel}")
    if (root / "prisma").is_dir():
        roots.add("prisma")
    return sorted(files), sorted(roots)


def _build_profile_fresh(root: Path, max_files: int, max_bytes: int) -> RepoProfileV2:
    root = root.resolve()
    evidence: List[str] = []
    ignored = set(IGNORED_DIR_NAMES)
    constraints = ConstraintsBlock(
        ignored_dirs=sorted(IGNORED_DIR_NAMES),
        max_files_scanned=max_files,
        max_bytes_per_file=max_bytes,
    )

    stack = StackBlock()
    layers: Set[str] = set()
    key_files: Set[str] = set()
    important: Set[str] = set()
    entry = EntrypointsBlock()
    api_routes: List[ApiRouteEntry] = []
    files_scanned = 0

    pkg: Optional[Dict[str, Any]] = None
    pm = "unknown"
    pm_list: List[str] = []
    node_package_roots = _discover_node_package_roots(root)
    pkg_records: List[Tuple[Path, str, Dict[str, Any], str]] = []
    all_node_deps: Set[str] = set()
    has_typescript = False
    next_cfg = False
    app_dir = False
    src_app = False
    pages_dir = False
    route_patterns_base: List[str] = []
    pages_api_bases: List[str] = []

    for candidate in node_package_roots:
        rel_root = _rel_posix(root, candidate)
        pkg_path = candidate / "package.json"
        pkg_data = _safe_json_loads(_read_text_limited(pkg_path, max_bytes))
        if not isinstance(pkg_data, dict):
            continue
        local_pm = _detect_package_manager(candidate)
        pkg_records.append((candidate, rel_root, pkg_data, local_pm))
        key_files.add("package.json" if rel_root == "." else f"{rel_root}/package.json")
        if local_pm != "unknown" and local_pm not in pm_list:
            pm_list.append(local_pm)

        candidate_deps = _deps_list(pkg_data)
        all_node_deps.update(candidate_deps)
        rel_prefix = "" if rel_root == "." else f"{rel_root}/"
        if rel_root != ".":
            important.add(rel_root)

        local_tsconfig_name = ""
        if (candidate / "tsconfig.json").is_file():
            local_tsconfig_name = "tsconfig.json"
        elif (candidate / "tsconfig.base.json").is_file():
            local_tsconfig_name = "tsconfig.base.json"
        local_has_typescript = bool(local_tsconfig_name) or _count_ts_files(candidate, ignored) >= 3
        has_typescript = has_typescript or local_has_typescript
        if local_tsconfig_name:
            key_files.add(local_tsconfig_name if rel_root == "." else f"{rel_root}/{local_tsconfig_name}")

        local_next_cfg = _next_config_exists(candidate)
        local_app_dir = (candidate / "app").is_dir()
        local_src_app = (candidate / "src" / "app").is_dir()
        local_pages_dir = (candidate / "pages").is_dir()
        next_cfg = next_cfg or local_next_cfg
        app_dir = app_dir or local_app_dir
        src_app = src_app or local_src_app
        pages_dir = pages_dir or local_pages_dir

        if local_next_cfg or local_app_dir or local_src_app or local_pages_dir:
            if "nextjs" not in stack.framework:
                stack.framework.append("nextjs")
            layers.add("ui")
            layers.add("api")
            markers = []
            if local_next_cfg:
                markers.append("next.config.*")
            if local_app_dir or local_src_app:
                markers.append("app router")
            if local_pages_dir:
                markers.append("pages router")
            evidence.append(
                f"Detected Next.js in {rel_root if rel_root != '.' else 'root'} via {', '.join(markers)}"
            )
            if local_app_dir:
                entry.ui_roots.append(f"{rel_prefix}app")
                entry.api_roots.append(f"{rel_prefix}app/api")
                route_patterns_base.append(f"{rel_prefix}app")
            if local_src_app:
                entry.ui_roots.append(f"{rel_prefix}src/app")
                entry.api_roots.append(f"{rel_prefix}src/app/api")
                route_patterns_base.append(f"{rel_prefix}src/app")
            if local_pages_dir:
                entry.ui_roots.append(f"{rel_prefix}pages")
                entry.api_roots.append(f"{rel_prefix}pages/api")
                pages_api_bases.append(f"{rel_prefix}pages/api")

    if pkg_records:
        ranked = sorted(
            pkg_records,
            key=lambda rec: (
                0 if rec[1] == "." else 1,
                0
                if (
                    _next_config_exists(rec[0])
                    or (rec[0] / "app").is_dir()
                    or (rec[0] / "src" / "app").is_dir()
                )
                else 1,
                -len((rec[2].get("scripts") or {})) if isinstance(rec[2].get("scripts"), dict) else 0,
                len(rec[1]),
            ),
        )
        pkg_root, _pkg_rel, pkg, pm = ranked[0]
    else:
        pkg_root = root

    py_deps = _pyproject_dependency_names(root, max_bytes)
    if (root / "pyproject.toml").is_file():
        key_files.add("pyproject.toml")
        important.add("pyproject.toml")
        evidence.append("Python project (pyproject.toml or requirements.txt)")
        pyproject_text = _read_text_limited(root / "pyproject.toml", max_bytes)
        if "[tool.uv]" in pyproject_text.lower() and "uv" not in pm_list:
            pm_list.append("uv")
    elif (root / "requirements.txt").is_file():
        key_files.add("requirements.txt")
        important.add("requirements.txt")
        evidence.append("Python project (pyproject.toml or requirements.txt)")

    stack.package_manager = pm_list or ["unknown"]

    if has_typescript:
        stack.language.append("typescript")
        layers.add("config")
    elif pkg_records:
        stack.language.append("javascript")

    deps: List[str] = sorted(set(all_node_deps) | set(py_deps))

    if any("drizzle" in d for d in deps) or (root / "drizzle.config.ts").is_file() or (root / "drizzle.config.js").is_file():
        if "drizzle" not in stack.orm:
            stack.orm.append("drizzle")
        evidence.append("Detected Drizzle ORM (dependency or drizzle.config.*)")
        layers.add("data")
        if (root / "drizzle").is_dir():
            entry.db_schema_roots.append("drizzle")

    if (root / "prisma").is_dir() or any(d.startswith("prisma") or d == "@prisma/client" for d in deps):
        if "prisma" not in stack.orm:
            stack.orm.append("prisma")
        evidence.append("Detected Prisma")
        layers.add("data")
        entry.db_schema_roots.append("prisma")

    if "sqlmodel" in py_deps and "sqlmodel" not in stack.orm:
        stack.orm.append("sqlmodel")
        layers.add("data")
        evidence.append("Detected SQLModel dependency")

    if any(x in deps for x in ("better-sqlite3", "sqlite3", "@libsql/client", "sqlmodel")):
        stack.database.append("sqlite")
        evidence.append("Detected SQLite-related dependency")
    elif "pg" in deps or "@vercel/postgres" in deps:
        stack.database.append("postgres")
        evidence.append("Detected Postgres-related dependency")
    elif "mysql2" in deps or "mysql" in deps:
        stack.database.append("mysql")
    else:
        stack.database.append("unknown")

    has_python = (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file()
    has_node = bool(pkg_records)
    if has_python:
        stack.language.append("python")
        layers.add("api")
        if (root / "apps" / "server").is_dir():
            entry.api_roots.append("apps/server")
            if (root / "apps" / "server" / "api").is_dir():
                entry.api_roots.append("apps/server/api")
            if (root / "apps" / "server" / "main.py").is_file():
                key_files.add("apps/server/main.py")
        if (root / "apps" / "cli").is_dir() and (root / "apps" / "cli" / "main.py").is_file():
            key_files.add("apps/cli/main.py")
        if (root / "apps" / "server" / "database.py").is_file():
            key_files.add("apps/server/database.py")
            layers.add("data")

    if has_python and has_node:
        stack.runtime = "polyglot"
    elif has_node:
        stack.runtime = "node"
    elif has_python:
        stack.runtime = "python"

    if any("fastapi" in d for d in deps):
        if "fastapi" not in stack.framework:
            stack.framework.append("fastapi")
        evidence.append("Detected FastAPI dependency")

    if not stack.framework:
        stack.framework.append("none")

    if not stack.orm:
        stack.orm.append("none")

    # pytest / jest / vitest
    scripts = (pkg or {}).get("scripts") or {}
    if isinstance(scripts, dict):
        test_script = str(scripts.get("test", "")).lower()
        if "vitest" in test_script or any("vitest" in d for d in deps):
            stack.test_runner.append("vitest")
        elif "jest" in test_script or any("jest" in d for d in deps):
            stack.test_runner.append("jest")
    if "pytest" in py_deps or (root / "pytest.ini").is_file():
        stack.test_runner.append("pytest")
    if (root / "pytest.ini").is_file():
        stack.test_runner.append("pytest")
    if not stack.test_runner:
        stack.test_runner.append("unknown")

    for name in ("components", "lib", "db", "tests", "test", "docs", "apps", "packages"):
        if (root / name).is_dir():
            important.add(name)
            if name == "docs":
                layers.add("docs")
            if name in ("tests", "test"):
                layers.add("tests")

    agents_text = ""
    for an in ("AGENTS.md", "agents.md"):
        ap = root / an
        if ap.is_file():
            key_files.add(an)
            agents_text = _read_text_limited(ap, max_bytes)
            evidence.append(f"Read {an} for verification hints")
            break

    agents_cmds = _parse_agents_md_commands(agents_text)
    vc = _build_verification_commands(pkg_root, pkg, pm, has_typescript, agents_cmds, evidence)

    # Route discovery (Next.js)
    for base in route_patterns_base:
        api_base = root / base / "api"
        if api_base.is_dir():
            for f in _iter_files_os_walk(root, f"{base}/api", ignored, max_files):
                files_scanned += 1
                if f.name not in ("route.ts", "route.js", "route.tsx", "route.jsx"):
                    continue
                rel = str(f.relative_to(root)).replace("\\", "/")
                url = _app_router_url(root, f)
                if not url.startswith("/api"):
                    url = "/api" + url if url == "/" else "/api" + url
                content = _read_text_limited(f, max_bytes)
                methods = _extract_route_methods(content)
                if methods:
                    for meth in methods:
                        api_routes.append(
                            ApiRouteEntry(method=meth, path=url, file=rel, kind="app_router")
                        )
                else:
                    api_routes.append(ApiRouteEntry(method=None, path=url, file=rel, kind="app_router"))
                evidence.append(f"App Router API: {url} ({rel})")

    for pages_base in pages_api_bases:
        pages_api = root / pages_base
        if not pages_api.is_dir():
            continue
        for f in _iter_files_os_walk(root, pages_base, ignored, max_files):
            files_scanned += 1
            if f.suffix not in (".ts", ".tsx", ".js", ".jsx"):
                continue
            rel = str(f.relative_to(root)).replace("\\", "/")
            url = _pages_api_url(root, f)
            api_routes.append(ApiRouteEntry(method=None, path=url, file=rel, kind="pages_api"))
            evidence.append(f"Pages API route: {url} ({rel})")

    db_files, db_roots = _discover_schema_files(root, evidence)
    for r in db_roots:
        if r not in entry.db_schema_roots:
            entry.db_schema_roots.append(r)
    validation_files = _scan_validation_files(root, ignored, max_bytes, evidence)

    for df in db_files:
        key_files.add(df)
    for vf in validation_files:
        key_files.add(vf)

    # Confidence
    conf = 0.35
    if "nextjs" in stack.framework:
        conf += 0.25
    if next_cfg and (app_dir or src_app):
        conf += 0.15
    if pkg and isinstance((pkg.get("scripts") or {}), dict) and (pkg.get("scripts") or {}):
        conf += 0.1
    if api_routes:
        conf += 0.1
    if "drizzle" in stack.orm or "prisma" in stack.orm:
        conf += 0.1
    if db_files:
        conf += 0.05
    conf = max(0.0, min(1.0, conf))

    if not stack.runtime or stack.runtime == "unknown":
        if (root / "package.json").is_file():
            stack.runtime = "node"
        elif (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
            stack.runtime = "python"

    stack.language = list(dict.fromkeys(stack.language))
    stack.framework = list(dict.fromkeys(stack.framework))
    stack.orm = list(dict.fromkeys(stack.orm))
    stack.database = list(dict.fromkeys(stack.database))
    stack.package_manager = list(dict.fromkeys(stack.package_manager))
    stack.test_runner = list(dict.fromkeys(stack.test_runner))
    entry.api_roots = list(dict.fromkeys(entry.api_roots))
    entry.ui_roots = list(dict.fromkeys(entry.ui_roots))
    entry.db_schema_roots = list(dict.fromkeys(entry.db_schema_roots))

    profile = RepoProfileV2(
        root=str(root),
        generated_at=datetime.now(timezone.utc),
        cache=CacheMeta(used=False, cache_key="", source="fresh"),
        stack=stack,
        layers_detected=sorted(layers),
        key_files=sorted(key_files),
        important_folders=sorted(important),
        entrypoints=entry,
        api_routes=api_routes,
        db_schema_files=db_files,
        validation_files=validation_files,
        verification_commands=vc,
        constraints=constraints,
        confidence=round(conf, 3),
        evidence_lines=sorted(set(evidence)),
    )
    return profile


def build_repo_profile(
    root: Path,
    *,
    cache: bool = True,
    force_refresh: bool = False,
    max_files_scanned: int = DEFAULT_MAX_FILES,
    max_bytes_per_file: int = DEFAULT_MAX_BYTES,
) -> RepoProfileV2:
    root = root.resolve()
    key = _compute_cache_key(root, max_bytes_per_file)
    if cache and not force_refresh:
        cached = _load_cache(root)
        if cached and cached[0] == key:
            prof = cached[1]
            prof.cache = CacheMeta(used=True, cache_key=key, source="cache")
            return prof

    profile = _build_profile_fresh(root, max_files_scanned, max_bytes_per_file)
    profile.cache = CacheMeta(used=False, cache_key=key, source="fresh")
    if cache:
        _save_cache(root, key, profile)
    return profile


def repo_profile_to_summary_text(profile: RepoProfileV2) -> str:
    lines = [
        f"RepoProfile v{profile.version} (confidence {profile.confidence:.0%})",
        f"Runtime: {profile.stack.runtime} | Languages: {', '.join(profile.stack.language) or '—'}",
        f"Framework: {', '.join(profile.stack.framework)} | ORM: {', '.join(profile.stack.orm)} | DB: {', '.join(profile.stack.database)}",
        f"Package manager: {', '.join(profile.stack.package_manager)}",
        f"Layers: {', '.join(profile.layers_detected) or '—'}",
        f"API roots: {', '.join(profile.entrypoints.api_roots) or '—'}",
        f"Key files (sample): {', '.join((profile.key_files or [])[:8])}",
        f"Verification: typecheck={profile.verification_commands.typecheck!r} build={profile.verification_commands.build!r} lint={profile.verification_commands.lint!r}",
    ]
    if profile.api_routes:
        sample = profile.api_routes[:5]
        lines.append("API routes (sample): " + "; ".join(f"{r.path} [{r.method or '?'}] {r.file}" for r in sample))
    return "\n".join(lines)


def repo_profile_to_prompt_block(profile: RepoProfileV2) -> str:
    """Compact block for system prompt / LLM context."""
    vc = profile.verification_commands
    return (
        "[REPO PROFILE v2]\n"
        f"root: {profile.root}\n"
        f"stack: runtime={profile.stack.runtime} languages={profile.stack.language} "
        f"framework={profile.stack.framework} orm={profile.stack.orm} db={profile.stack.database}\n"
        f"layers: {profile.layers_detected}\n"
        f"entrypoints api={profile.entrypoints.api_roots} ui={profile.entrypoints.ui_roots} db={profile.entrypoints.db_schema_roots}\n"
        f"verification_commands: typecheck={vc.typecheck} build={vc.build} lint={vc.lint} tests={vc.tests} "
        f"source={vc.source}\n"
        f"confidence: {profile.confidence:.2f}\n"
        f"evidence (deterministic):\n"
        + "\n".join(f"  - {e}" for e in profile.evidence_lines[:12])
    )
