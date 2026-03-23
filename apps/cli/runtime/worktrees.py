import os
import subprocess
from typing import List, Dict, Any, Optional

class WorktreeManager:
    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self.worktrees_dir = os.path.join(root_dir, ".ghost", "worktrees")
        os.makedirs(self.worktrees_dir, exist_ok=True)

    def create_worktree(self, name: str, branch: str = "main") -> str:
        """Creates a new git worktree."""
        path = os.path.join(self.worktrees_dir, name)
        if os.path.exists(path):
            return path # Already exists
            
        try:
            # Create a unique branch for the worker if needed, 
            # but for v0 we just use a detached HEAD or a specific branch
            worker_branch = f"ghost-worker-{name}"
            
            # git worktree add [-f] [--detach] [--checkout] [--lock] [-b <new-branch>] <path> [<commit-ish>]
            subprocess.run(
                ["git", "worktree", "add", "-b", worker_branch, path, branch],
                cwd=self.root_dir,
                check=True,
                capture_output=True,
                text=True
            )
            return path
        except subprocess.CalledProcessError as e:
            # If branch already exists, try without -b
            try:
                subprocess.run(
                    ["git", "worktree", "add", path, worker_branch],
                    cwd=self.root_dir,
                    check=True,
                    capture_output=True,
                    text=True
                )
                return path
            except:
                raise Exception(f"Failed to create worktree: {e.stderr}")

    def list_worktrees(self) -> List[Dict[str, str]]:
        """Lists active worktrees."""
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=self.root_dir,
                check=True,
                capture_output=True,
                text=True
            )
            
            worktrees = []
            current = {}
            for line in result.stdout.splitlines():
                if line.startswith("worktree "):
                    if current: worktrees.append(current)
                    current = {"path": line[9:]}
                elif line.startswith("branch "):
                    current["branch"] = line[7:]
            if current: worktrees.append(current)
            
            # Filter only those managed by ghost
            return [wt for wt in worktrees if self.worktrees_dir in wt["path"]]
        except:
            return []

    def remove_worktree(self, path: str):
        """Removes a worktree."""
        if not os.path.exists(path):
            return
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", path],
                cwd=self.root_dir,
                check=True,
                capture_output=True,
                text=True
            )
        except subprocess.CalledProcessError as e:
            raise Exception(f"Failed to remove worktree: {e.stderr}")
