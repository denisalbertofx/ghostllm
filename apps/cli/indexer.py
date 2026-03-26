import os
from pathlib import Path
from typing import List

from apps.cli.runtime.repo_profile_v2 import build_repo_profile


class RepoIndexer:
    """
    Scans the repository to provide compact context to the LLM.
    Respects a small ignore list and keeps summaries deterministic.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.ignored_dirs = {
            ".git",
            "node_modules",
            "__pycache__",
            ".venv",
            ".next",
            "dist",
            "build",
            ".ghost",
        }

    def get_project_tree(self, max_depth: int = 3) -> str:
        """Returns a string representation of the project structure."""
        tree: List[str] = []
        self._build_tree(self.root, "", tree, 0, max_depth)
        return "\n".join(tree)

    def _build_tree(self, current_dir: str, prefix: str, tree: List[str], depth: int, max_depth: int):
        if depth > max_depth:
            return

        try:
            items = sorted(os.listdir(current_dir))
        except PermissionError:
            return

        visible = [name for name in items if name not in self.ignored_dirs]
        for i, name in enumerate(visible):
            path = os.path.join(current_dir, name)
            is_last = i == len(visible) - 1
            connector = "\\-- " if is_last else "|-- "
            tree.append(f"{prefix}{connector}{name}")

            if os.path.isdir(path):
                new_prefix = prefix + ("    " if is_last else "|   ")
                self._build_tree(path, new_prefix, tree, depth + 1, max_depth)

    def get_all_files(self) -> List[str]:
        """Flattened list of all relevant files."""
        files: List[str] = []
        for root, dirs, filenames in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in self.ignored_dirs]
            for filename in filenames:
                rel_path = os.path.relpath(os.path.join(root, filename), self.root)
                files.append(rel_path.replace("\\", "/"))
        return files

    def get_project_summary(self) -> str:
        """High-level summary for repo overview and architecture questions."""
        tree = self.get_project_tree(max_depth=2)
        all_files = self.get_all_files()

        try:
            profile = build_repo_profile(Path(self.root), cache=True)
            stack = profile.stack
            frameworks = ", ".join(stack.framework or []) or "unknown"
            languages = ", ".join(stack.language or []) or "unknown"
            package_managers = ", ".join(stack.package_manager or []) or "unknown"
            entry = profile.entrypoints
            ui_roots = ", ".join(entry.ui_roots or []) or "-"
            api_roots = ", ".join(entry.api_roots or []) or "-"
            workspaces: List[str] = []
            for rel_path, label in (
                ("apps/server", "FastAPI gateway"),
                ("apps/cli", "CLI runtime"),
                ("apps/web", "Next.js web app"),
                ("packages/py-core", "shared Python core"),
            ):
                if os.path.exists(os.path.join(self.root, rel_path)):
                    workspaces.append(f"- {rel_path}: {label}")

            key_files = ", ".join((profile.key_files or [])[:8]) or "-"
            return (
                f"Repo root: {self.root}\n"
                f"Runtime: {stack.runtime}\n"
                f"Languages: {languages}\n"
                f"Frameworks: {frameworks}\n"
                f"Package managers: {package_managers}\n"
                f"Layers: {', '.join(profile.layers_detected or []) or '-'}\n"
                f"UI roots: {ui_roots}\n"
                f"API roots: {api_roots}\n"
                f"Key files: {key_files}\n"
                f"Workspaces:\n{os.linesep.join(workspaces) if workspaces else '- none'}\n\n"
                f"Project Tree:\n{tree}\n\n"
                f"Total Files: {len(all_files)}"
            )
        except Exception:
            return f"Project Tree:\n{tree}\n\nTotal Files: {len(all_files)}"

    def invalidate_project_tree_cache(self) -> None:
        """Compatibility no-op: tree is built on demand."""
        return None
