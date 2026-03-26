import os
import json
import uuid
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any

@dataclass
class Task:
    task_id: str
    title: str
    mode: str
    status: str  # created, planned, approved, executing, verifying, blocked, done, failed, rolled_back
    owner: str = "denis"
    scope: List[str] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    status_history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Extended fields (used by newer assistant.py)
    task_type: str = "ask"
    inferred_type: str = ""
    type_source: str = "inferred"
    batch_id: Optional[str] = None

    def to_dict(self):
        return asdict(self)

class TaskManager:
    def __init__(self, base_path: str):
        self.tasks_dir = os.path.join(base_path, ".ghost", "tasks")
        os.makedirs(self.tasks_dir, exist_ok=True)
        self.current_task: Optional[Task] = None

    def create_task(
        self,
        title: str,
        mode: str,
        owner: str = "denis",
        task_type: str = "ask",
        inferred_type: str = "",
        type_source: str = "inferred",
        batch_id: Optional[str] = None,
    ) -> Task:
        task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}"
        task = Task(
            task_id=task_id,
            title=title,
            mode=mode,
            status="created",
            owner=owner,
            task_type=task_type,
            inferred_type=inferred_type,
            type_source=type_source,
            batch_id=batch_id,
            status_history=[{"status": "created", "timestamp": datetime.now().isoformat()}]
        )
        self.current_task = task
        self.save(task)
        return task

    def update_status(self, status: str, detail: Optional[str] = None, event: Optional[str] = None):
        if self.current_task:
            previous_status = self.current_task.status
            # Allow same-status updates if there's a detail or specific event
            if previous_status != status or detail or event:
                self.current_task.status = status
                self.current_task.updated_at = datetime.now().isoformat()
                history_entry = {
                    "status": status,
                    "timestamp": datetime.now().isoformat()
                }
                if event:
                    history_entry["event"] = event
                if detail:
                    history_entry["detail"] = detail
                self.current_task.status_history.append(history_entry)
                self.save(self.current_task)

    def resume_task(self, task_id: str) -> Optional[Task]:
        task = self.get_task(task_id)
        if task:
            self.current_task = task
            # Record explicit RESUME event without changing the underlying status yet
            self.update_status(task.status, event="resumed", detail="Task session resumed via /resume")
            
            # Record reopening/re-executing transition
            if task.status not in ["done", "failed", "rolled_back"]:
                self.update_status("reopened")
            else:
                self.update_status("executing", detail="Re-opening finished task for additional work")
            return task
        return None

    def list_tasks(self) -> List[Dict[str, Any]]:
        tasks = []
        for filename in os.listdir(self.tasks_dir):
            if filename.endswith(".json"):
                task_id = filename.replace(".json", "")
                task = self.get_task(task_id)
                if task:
                    tasks.append({
                        "id": task.task_id,
                        "title": task.title,
                        "status": task.status,
                        "updated_at": task.updated_at
                    })
        return sorted(tasks, key=lambda x: x["updated_at"], reverse=True)

    def add_artifact(self, artifact_id: str):
        if self.current_task and artifact_id not in self.current_task.artifacts:
            self.current_task.artifacts.append(artifact_id)
            self.save(self.current_task)

    def save(self, task: Task):
        file_path = os.path.join(self.tasks_dir, f"{task.task_id}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(task.to_dict(), f, indent=2)

    def get_task(self, task_id: str) -> Optional[Task]:
        # Handle full filenames or just IDs
        clean_id = task_id.replace(".json", "")
        file_path = os.path.join(self.tasks_dir, f"{clean_id}.json")
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return Task(**data)
        return None
