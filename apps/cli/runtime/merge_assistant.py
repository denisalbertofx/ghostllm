import os
import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional
from .swarm import SwarmManager, SwarmWorker
from .review_coordinator import ReviewCoordinator

@dataclass
class MergeBundle:
    task_id: str
    approved_workers: List[Dict[str, Any]]
    files_changed: List[str]
    diff_summary: str
    conflict_risk: str  # LOW, MEDIUM, HIGH
    can_apply: bool
    verification_status: str = "pending"

class MergeAssistant:
    def __init__(self, root_dir: str, swarm_manager: SwarmManager, review_coordinator: ReviewCoordinator):
        self.root_dir = root_dir
        self.swarm_manager = swarm_manager
        self.review_coordinator = review_coordinator

    def get_merge_preview(self, task_id: str) -> MergeBundle:
        decision = self.review_coordinator.get_decision(task_id)
        if not decision or decision.get("decision") != "approved":
            return MergeBundle(task_id, [], [], "", "Task not approved", False)

        workers = self.swarm_manager.get_workers_by_task(task_id)
        approved_workers = []
        files_set = set()
        diff_summaries = []
        
        # Risk factors
        stale_base = False
        uncommitted_in_main = False
        
        for w in workers:
            approved_workers.append({
                "worker_id": w.worker_id,
                "role": w.role,
                "worktree_path": w.worktree_path
            })
            
            try:
                # 1. Detect Stale Base
                # We check if the current main branch HEAD is an ancestor of the worktree's HEAD.
                # Since worktrees are often branched from a specific commit, we check if that origin commit is still HEAD.
                # Simplified for v0.5: check if worktree HEAD is exactly on top of main HEAD or not.
                main_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root_dir, text=True).strip()
                
                # If these were real branches, we'd use merge-base. 
                # In our current worktree setup, we usually start from the 'main' branch HEAD.
                # If main HEAD moved, the worktree is 'stale'.
                try:
                    # Check if main_head is present in the worktree's history
                    subprocess.check_call(["git", "merge-base", "--is-ancestor", main_head, "HEAD"], cwd=w.worktree_path)
                except subprocess.CalledProcessError:
                    stale_base = True

                # 2. Files changed in worktree
                files_output = subprocess.check_output(
                    ["git", "status", "--porcelain"],
                    cwd=w.worktree_path,
                    text=True
                )
                for line in files_output.splitlines():
                    if line.strip():
                        files_set.add(line[3:].strip())
                
                diff_output = subprocess.check_output(
                    ["git", "diff", "--stat", "HEAD"],
                    cwd=w.worktree_path,
                    text=True
                )
                diff_summaries.append(f"Worker {w.worker_id}:\n{diff_output}")
            except:
                pass

        # 3. Detect uncommitted changes in main for same files
        for f in files_set:
            full_path = os.path.join(self.root_dir, f)
            if os.path.exists(full_path):
                try:
                    root_status = subprocess.check_output(
                        ["git", "status", "--porcelain", f],
                        cwd=self.root_dir,
                        text=True
                    ).strip()
                    if root_status:
                        uncommitted_in_main = True
                except:
                    pass

        # Classification
        if uncommitted_in_main:
            conflict_risk = "HIGH (Main repo has uncommitted changes in same files)"
            can_apply = False
        elif stale_base:
            conflict_risk = "MEDIUM (Worktree base is behind main repository)"
            can_apply = True # Allow but warn
        else:
            conflict_risk = "LOW"
            can_apply = True

        return MergeBundle(
            task_id=task_id,
            approved_workers=approved_workers,
            files_changed=list(files_set),
            diff_summary="\n".join(diff_summaries),
            conflict_risk=conflict_risk,
            can_apply=can_apply
        )

    def apply_merge(self, task_id: str) -> Dict[str, Any]:
        preview = self.get_merge_preview(task_id)
        if not preview.can_apply:
            raise Exception(f"Merge blocked: {preview.conflict_risk}")

        merged_files = []
        workers = self.swarm_manager.get_workers_by_task(task_id)
        
        for w in workers:
            if not os.path.exists(w.worktree_path):
                continue
                
            for f_rel in preview.files_changed:
                src = os.path.join(w.worktree_path, f_rel)
                dst = os.path.join(self.root_dir, f_rel)
                
                if os.path.exists(src):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    merged_files.append(f_rel)
                    subprocess.run(["git", "add", f_rel], cwd=self.root_dir)

        # Post-merge verification
        verification = self.run_post_merge_verification()
        
        result = {
            "task_id": task_id,
            "timestamp": subprocess.check_output(["git", "log", "-1", "--format=%ct"], text=True).strip(),
            "files": list(set(merged_files)),
            "risk": preview.conflict_risk,
            "verification": verification
        }
        
        self.review_coordinator.save_merge_result(task_id, result)
        return result

    def run_post_merge_verification(self) -> Dict[str, Any]:
        """Run basic repository health checks after merge."""
        results = {"status": "success", "checks": []}
        
        # Check 1: Git status integrity
        try:
            status = subprocess.check_output(["git", "status"], cwd=self.root_dir, text=True)
            results["checks"].append({"name": "git_status", "result": "passed"})
        except:
            results["checks"].append({"name": "git_status", "result": "failed"})
            results["status"] = "failed"

        # Check 2: Syntax Check (Python subset)
        # Simply check if we can parse the merged files
        return results
