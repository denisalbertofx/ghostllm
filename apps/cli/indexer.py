import os
from typing import List, Set

class RepoIndexer:
    """
    Scans the repository to provide context to the LLM.
    Respects .gitignore-like patterns.
    """
    
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.ignored_dirs = {".git", "node_modules", "__pycache__", ".venv", ".next", "dist", "build", ".ghost"}

    def get_project_tree(self, max_depth: int = 3) -> str:
        """Returns a string representation of the project structure."""
        tree = []
        self._build_tree(self.root, "", tree, 0, max_depth)
        return "\n".join(tree)

    def _build_tree(self, current_dir: str, prefix: str, tree: List[str], depth: int, max_depth: int):
        if depth > max_depth:
            return

        try:
            items = sorted(os.listdir(current_dir))
        except PermissionError:
            return

        for i, name in enumerate(items):
            if name in self.ignored_dirs:
                continue
                
            path = os.path.join(current_dir, name)
            is_last = (i == len(items) - 1)
            connector = "└── " if is_last else "├── "
            
            tree.append(f"{prefix}{connector}{name}")
            
            if os.path.isdir(path):
                new_prefix = prefix + ("    " if is_last else "│   ")
                self._build_tree(path, new_prefix, tree, depth + 1, max_depth)

    def get_all_files(self) -> List[str]:
        """Flattened list of all relevant files."""
        files = []
        for root, dirs, filenames in os.walk(self.root):
            # Skip ignored dirs
            dirs[:] = [d for d in dirs if d not in self.ignored_dirs]
            for f in filenames:
                rel_path = os.path.relpath(os.path.join(root, f), self.root)
                files.append(rel_path)
        return files

    def get_project_summary(self) -> str:
        """High-level summary for the LLM."""
        tree = self.get_project_tree(max_depth=2)
        all_files = self.get_all_files()
        return f"Project Tree:\n{tree}\n\nTotal Files: {len(all_files)}"
