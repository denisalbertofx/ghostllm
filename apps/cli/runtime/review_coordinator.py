import os
import json
import subprocess
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional
from .swarm import SwarmManager, SwarmWorker

@dataclass
class WorkerReview:
    worker_id: str
    role: str
    worktree_path: str
    files_changed: List[str]
    diff_summary: str
    verification_status: Optional[str] = None

@dataclass
class ReviewBundle:
    task_id: str
    workers: List[WorkerReview]
    status: str
    recommendation: str

class ReviewCoordinator:
    def __init__(self, root_dir: str, swarm_manager: SwarmManager):
        self.root_dir = root_dir
        self.swarm_manager = swarm_manager
        self.reviews_dir = os.path.join(root_dir, ".ghost", "reviews")
        os.makedirs(self.reviews_dir, exist_ok=True)

    def get_review_bundle(self, task_id: str) -> ReviewBundle:
        workers = self.swarm_manager.get_workers_by_task(task_id)
        if not workers:
            raise Exception(f"No workers found for task {task_id}")

        worker_reviews = []
        for w in workers:
            files_changed, diff_summary = self._get_worker_changes(w)
            worker_reviews.append(WorkerReview(
                worker_id=w.worker_id,
                role=w.role,
                worktree_path=w.worktree_path,
                files_changed=files_changed,
                diff_summary=diff_summary
            ))

        # Generate recommendation based on roles present
        roles = [w.role for w in workers]
        if "coder" in roles and "architect" in roles:
            recommendation = "Ready for merge. Architect design verified by Coder implementation."
        elif "coder" in roles:
            recommendation = "Ready for review. Coder implementation complete."
        else:
            recommendation = "Incomplete. Missing implementation worker."

        return ReviewBundle(
            task_id=task_id,
            workers=worker_reviews,
            status="pending_review",
            recommendation=recommendation
        )

    def _get_worker_changes(self, worker: SwarmWorker) -> (List[str], str):
        if not os.path.exists(worker.worktree_path):
            return [], "Worktree path not found."

        try:
            # Get list of changed files relative to the worktree
            # We compare with the base repository (main/master)
            # In v0, we assume the base is the branch the worktree was created from
            
            # 1. Changed files (unstaged + staged)
            files_output = subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=worker.worktree_path,
                text=True
            )
            files_changed = []
            for line in files_output.splitlines():
                if line.strip():
                    files_changed.append(line[3:].strip())

            # 2. Diff Summary
            diff_output = subprocess.check_output(
                ["git", "diff", "--stat", "HEAD"],
                cwd=worker.worktree_path,
                text=True
            )
            diff_summary = diff_output if diff_output.strip() else "No changes detected."
            
            return files_changed, diff_summary
        except Exception as e:
            return [], f"Error reading git changes: {str(e)}"

    def save_decision(self, task_id: str, decision: str, comment: str = ""):
        if decision not in ["approved", "rejected"]:
            raise Exception(f"Invalid decision: {decision}")

        report_path = os.path.join(self.reviews_dir, f"{task_id}.json")
        data = {
            "task_id": task_id,
            "decision": decision,
            "comment": comment,
            "timestamp": datetime.now().isoformat()
        }
        
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        
        return report_path

    def get_decision(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve the persisted decision for a task."""
        report_path = os.path.join(self.reviews_dir, f"{task_id}.json")
        if os.path.exists(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def save_merge_result(self, task_id: str, merge_data: Dict[str, Any]):
        """Persist the result of a merge and its verification."""
        report_path = os.path.join(self.reviews_dir, f"{task_id}.json")
        
        # Merge with existing decision data if present
        data = self.get_decision(task_id) or {}
        data["merge_result"] = merge_data
        data["merge_timestamp"] = datetime.now().isoformat()
        
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

# Add missing import for datetime
from datetime import datetime
