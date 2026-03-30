import os
import json
import uuid
import subprocess
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Any, Optional
from datetime import datetime
from .worktrees import WorktreeManager

@dataclass
class SwarmWorker:
    worker_id: str
    role: str
    task_id: str
    status: str
    worktree_path: str
    spawned_at: str = field(default_factory=lambda: datetime.now().isoformat())
    pid: Optional[int] = None

class SwarmManager:
    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self.state_dir = os.path.join(root_dir, ".ghost", "swarm")
        self.state_file = os.path.join(self.state_dir, "state.json")
        os.makedirs(self.state_dir, exist_ok=True)
        
        self.worktree_manager = WorktreeManager(root_dir)
        self.workers: List[SwarmWorker] = []
        self.load_state()

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.workers = [SwarmWorker(**w) for w in data.get("workers", [])]
            except:
                self.workers = []

    def save_state(self):
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump({"workers": [asdict(w) for w in self.workers]}, f, indent=2)

    def spawn_worker(self, role: str, task_id: str) -> SwarmWorker:
        # Check max 2 workers simultaneously
        active_workers = [w for w in self.workers if w.status == "active"]
        if len(active_workers) >= 2:
            raise Exception("Swarm limit reached: Maximum 2 active workers allowed.")

        valid_roles = ["architect", "coder", "reviewer"]
        if role not in valid_roles:
            raise Exception(f"Invalid role: {role}. Must be one of {valid_roles}")

        worker_id = f"worker_{uuid.uuid4().hex[:8]}"
        
        # 1. Create Worktree
        worktree_path = self.worktree_manager.create_worktree(worker_id)
        
        # 2. Spawn Subprocess (v0: detached background process)
        # We pass --mode SwarmWorker and --role <role>
        # Note: We need a way for the worker to know its identity
        cmd = [
            "python", "apps/cli/main.py", 
            "do", f"Task execution for {role} on {task_id}", 
            "--task-id", task_id,
            "--role", role,
            "--swarm-mode"
        ]
        
        # For v0, let's just simulate the spawn for the board 
        # unless we actually want to launch a real separate CLI session.
        # Given the "no shell libre" and "isolated worktree", 
        # launching a real process is more authentic.
        
        # try:
        #     p = subprocess.Popen(cmd, cwd=worktree_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        #     pid = p.pid
        # except:
        #     pid = None
        
        pid = 9999 # Placeholder for v0 simulation if we don't want to actually launch child yet
        
        worker = SwarmWorker(
            worker_id=worker_id,
            role=role,
            task_id=task_id,
            status="active",
            worktree_path=worktree_path,
            pid=pid
        )
        
        self.workers.append(worker)
        self.save_state()
        return worker

    def list_workers(self) -> List[SwarmWorker]:
        return self.workers

    def _pid_is_running(self, pid: Optional[int]) -> bool:
        if not pid or int(pid) <= 0:
            return False
        # v0 workers use a placeholder PID until real detached workers land.
        if int(pid) == 9999:
            return True
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {int(pid)}"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                return str(int(pid)) in (result.stdout or "")
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False

    def refresh_worker_statuses(self) -> List[SwarmWorker]:
        changed = False
        for worker in self.workers:
            if worker.status == "terminated":
                continue
            if worker.worktree_path and not os.path.exists(worker.worktree_path):
                worker.status = "terminated"
                changed = True
                continue
            if worker.pid and not self._pid_is_running(worker.pid):
                worker.status = "terminated"
                changed = True
        if changed:
            self.save_state()
        return self.workers

    def cleanup_workers(self) -> int:
        pruned = 0
        kept: List[SwarmWorker] = []
        for worker in self.workers:
            if worker.status != "terminated":
                kept.append(worker)
                continue
            pruned += 1
            if worker.worktree_path and os.path.exists(worker.worktree_path):
                try:
                    self.worktree_manager.remove_worktree(worker.worktree_path)
                except Exception:
                    pass
        if pruned:
            self.workers = kept
            self.save_state()
        return pruned

    def terminate_worker(self, worker_id: str):
        worker = next((w for w in self.workers if w.worker_id == worker_id), None)
        if worker:
            worker.status = "terminated"
            # In a real impl, we would kill the PID and remove the worktree
            # self.worktree_manager.remove_worktree(worker.worktree_path)
            self.save_state()
