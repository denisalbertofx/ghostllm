import os
import re
from typing import List, Optional, Tuple

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
        
        # If we found markers, we are in a project
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
