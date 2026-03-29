import os
import re
from typing import List, Optional, Tuple

_TOOL_PATH_TRAILING_TAG_RE = re.compile(r"(?:\s*</?[A-Za-z][^>\r\n]*>\s*)+$")

class PathComposer:
    """
    Central utility for robust path composition and normalization.
    GEP-2: Path Composer implementation.
    """
    
    @staticmethod
    def normalize_path_segments(path: str) -> str:
        """Remove redundant separators (e.g. app/api/issues//route.ts -> app/api/issues/route.ts)."""
        if not path:
            return path
        path = str(path).strip()
        path = _TOOL_PATH_TRAILING_TAG_RE.sub("", path).strip()
        path = path.replace("\r", " ").replace("\n", " ").strip()
        scheme_match = re.match(r"^([A-Za-z0-9_.-]+)://([A-Za-z0-9_.-]+)(/.*)?$", path)
        if scheme_match:
            left = scheme_match.group(1)
            right = scheme_match.group(2)
            tail = scheme_match.group(3) or ""
            path = f"{left}-{right}{tail}"
        path = path.replace("\\", "/")
        while "//" in path:
            path = path.replace("//", "/")
        return path.strip("/")

    @staticmethod
    def compose(base: str, *parts: str) -> str:
        """
        Joins path parts, normalizes slashes, and removes redundant separators.
        Ensures compatibility with Windows and Unix. Handles app/api/issues//route.ts.
        """
        cleaned_parts = []
        for p in parts:
            if not p: continue
            p = PathComposer.normalize_path_segments(p)
            if not p: continue
            p = p.replace("/", os.sep).replace("\\", os.sep)
            p = p.strip(os.sep)
            cleaned_parts.append(p)

        full_path = os.path.join(base, *cleaned_parts)

        normalized = os.path.normpath(full_path)
        
        # 3. Final check for redundant separators (normpath usually handles this, but let's be safe)
        # Windows UNC paths start with \\, so we only remove if it's not a start
        if normalized.startswith(os.sep * 2) and os.name != 'nt':
            normalized = normalized[1:]
            
        return normalized

    @staticmethod
    def is_safe_relative(path: str) -> bool:
        """Checks if a path is relative and doesn't use .. to escape the root."""
        normalized = os.path.normpath(path)
        return not (os.path.isabs(normalized) or normalized.startswith(".."))

    @staticmethod
    def is_absolute(path: str) -> bool:
        """Returns True if the path is absolute."""
        return os.path.isabs(path)

class WorkingDirectoryGuard:
    """
    Detects project context and prevents invalid scaffolding decisions.
    GEP-3: Working Directory Guard implementation.
    """
    
    MARKERS = [
        "package.json", 
        "pyproject.toml", 
        "requirements.txt", 
        "app/", 
        "src/", 
        ".git", 
        "README.md",
        "next.config.js",
        "next.config.ts"
    ]
    STRONG_MARKERS = {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "app/",
        "src/",
        "next.config.js",
        "next.config.ts",
    }
    REPO_SHELL_ALLOWLIST = {
        ".git",
        ".ghost",
        "ghost_memory.db",
        "README",
        "README.md",
        "LICENSE",
        "LICENSE.md",
        "LICENSE.txt",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
    }
    BOOTSTRAP_PARTIAL_ALLOWLIST = REPO_SHELL_ALLOWLIST | {"README.txt"}
    BOOTSTRAP_SOURCE_SUFFIXES = (
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".php",
        ".cs",
    )
    BOOTSTRAP_PROJECT_ANCHORS = {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
    }
    BOOTSTRAP_MATURE_PATH_MARKERS = {
        "src",
        "app",
        "apps",
        "tests",
        "test",
        "__tests__",
        "pages",
        "components",
        "public",
        "migrations",
        "alembic",
        "prisma",
        "backend",
        "frontend",
        "server",
        "client",
    }
    NESTED_BOOTSTRAP_ANCHORS = {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "readme.md",
        "readme.txt",
        "next.config.js",
        "next.config.ts",
    }

    @classmethod
    def _looks_like_partial_bootstrap(cls, entries: List[str]) -> bool:
        visible = [str(name or "").strip() for name in entries if str(name or "").strip()]
        if not visible:
            return False
        non_shell_entries = [
            name for name in visible
            if name not in cls.BOOTSTRAP_PARTIAL_ALLOWLIST
        ]
        if not non_shell_entries or len(non_shell_entries) > 3:
            return False
        lowered = [name.lower() for name in non_shell_entries]
        if any(name in ("pyproject.toml", "package.json", "requirements.txt") for name in lowered):
            return False
        if any(
            name.startswith("tests")
            or name.startswith("test")
            or name.endswith("_test.py")
            or name.endswith(".test.ts")
            or name.endswith(".spec.ts")
            for name in lowered
        ):
            return False
        source_like = [name for name in lowered if name.endswith(cls.BOOTSTRAP_SOURCE_SUFFIXES)]
        return len(source_like) >= 1

    @classmethod
    def _looks_like_nested_partial_bootstrap(cls, cwd: str, entries: List[str]) -> bool:
        visible = [str(name or "").strip() for name in entries if str(name or "").strip()]
        non_shell_entries = [
            name for name in visible
            if name not in cls.REPO_SHELL_ALLOWLIST
        ]
        if len(non_shell_entries) != 1:
            return False
        nested_name = non_shell_entries[0]
        nested_path = os.path.join(cwd, nested_name)
        if not os.path.isdir(nested_path):
            return False
        try:
            nested_entries = [entry.name for entry in os.scandir(nested_path)]
        except OSError:
            return False
        nested_visible = [str(name or "").strip() for name in nested_entries if str(name or "").strip()]
        if not nested_visible or len(nested_visible) > 4:
            return False
        lowered = [name.lower() for name in nested_visible]
        if not any(name in cls.NESTED_BOOTSTRAP_ANCHORS for name in lowered):
            return False
        mature_markers = {"app", "src", "pages", "tests", "__tests__", "public"}
        if sum(1 for name in lowered if name in mature_markers) > 1:
            return False
        return True

    @classmethod
    def _looks_like_root_project_seed(cls, cwd: str, entries: List[str]) -> bool:
        visible = [str(name or "").strip() for name in entries if str(name or "").strip()]
        if not visible:
            return False
        non_shell_entries = [
            name for name in visible
            if name not in cls.REPO_SHELL_ALLOWLIST
        ]
        if not non_shell_entries or len(non_shell_entries) > 4:
            return False
        lowered = [name.lower() for name in non_shell_entries]
        anchors = [name for name in lowered if name in cls.BOOTSTRAP_PROJECT_ANCHORS]
        if not anchors:
            return False
        source_like = [name for name in lowered if name.endswith(cls.BOOTSTRAP_SOURCE_SUFFIXES)]
        if not source_like and len(non_shell_entries) > 2:
            return False
        for name in lowered:
            full = os.path.join(cwd, name)
            if os.path.isdir(full) and name in cls.BOOTSTRAP_MATURE_PATH_MARKERS:
                return False
            if name.startswith("test") or name in {"tests", "__tests__"}:
                return False
        return True

    @classmethod
    def detect_context(cls, cwd: str) -> Tuple[str, List[str]]:
        """
        Scans the directory for markers to determine context.
        Returns (context_type, found_markers).
        Context types: 'empty', 'project', 'nested'
        """
        found = []
        for marker in cls.MARKERS:
            # Check both as file and as directory prefix
            target = os.path.join(cwd, marker)
            if os.path.exists(target):
                found.append(marker)
        
        if not found:
            return "empty", []

        try:
            entries = [entry.name for entry in os.scandir(cwd)]
        except OSError:
            entries = []
        if any(marker in cls.STRONG_MARKERS for marker in found):
            if cls._looks_like_root_project_seed(cwd, entries):
                return "bootstrap_partial", found
            return "project", found
        non_shell_entries = [
            name for name in entries
            if name not in cls.REPO_SHELL_ALLOWLIST
        ]
        if not non_shell_entries:
            return "repo_shell", found
        if cls._looks_like_partial_bootstrap(entries):
            return "bootstrap_partial", found
        if cls._looks_like_nested_partial_bootstrap(cwd, entries):
            return "bootstrap_partial", found

        return "project", found

    @classmethod
    def validate_scaffold(cls, cwd: str, target_subfolder: Optional[str] = None) -> Tuple[str, str, List[str]]:
        """
        Validates if a bootstrap/scaffold action is safe.
        Returns (action, message, options).
        Action levels: 'ALLOW', 'WARN', 'HARD_BLOCK'
        """
        context, markers = cls.detect_context(cwd)
        
        options = [
            "Usar una carpeta nueva (ej: /do crea mi-app...)",
            "Usar una subcarpeta vacía",
            "Trabajar sobre el proyecto actual (sin bootstrap)"
        ]

        if context == "repo_shell":
            return "ALLOW", "Repositorio recién inicializado detectado; scaffold in-place permitido.", []

        if context == "bootstrap_partial":
            return "ALLOW", "Scaffold greenfield parcial detectado; se permite continuar construyendo el proyecto en este directorio.", []

        if context == "project":
            if not target_subfolder:
                msg = f"Ya te encuentras en un proyecto activo (detectado: {', '.join(markers)}). No se puede bootstrappear en el directorio actual para evitar sobrescribir configuraciones críticas."
                return "HARD_BLOCK", msg, options
            
            # If target is a subfolder, check if it's already a project
            target_path = os.path.join(cwd, target_subfolder)
            if os.path.exists(target_path) and os.path.isdir(target_path) and os.listdir(target_path):
                sub_context, sub_markers = cls.detect_context(target_path)
                if sub_context == "project":
                    msg = f"La subcarpeta '{target_subfolder}' ya contiene un proyecto ({', '.join(sub_markers)}). Evitando nesting accidental."
                    return "HARD_BLOCK", msg, options
                else:
                    return "WARN", f"La carpeta '{target_subfolder}' no está vacía. Procede con precaución.", options
        
        return "ALLOW", "Safe to proceed.", []
