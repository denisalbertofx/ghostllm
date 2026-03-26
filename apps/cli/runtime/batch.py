import os
import json
import uuid
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional

@dataclass
class BatchStep:
    step_id: int
    title: str
    task_id: Optional[str] = None
    artifact_id: Optional[str] = None
    status: str = "pending" # pending, executing, success, failed, skipped
    result: Optional[Dict[str, Any]] = None
    retry_count: int = 0

@dataclass
class Batch:
    batch_id: str
    steps: List[BatchStep]
    status: str = "idle" # idle, running, completed, failed, stopped
    current_step_index: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        # Handle recursive serialization of dataclasses
        d = asdict(self)
        return d

class BatchManager:
    def __init__(self, base_path: str):
        self.batches_dir = os.path.join(base_path, ".ghost", "batches")
        os.makedirs(self.batches_dir, exist_ok=True)
        self.current_batch: Optional[Batch] = None

    def create_batch(self, step_titles: List[str]) -> Batch:
        if len(step_titles) > 3:
            raise ValueError("Batch limited to maximum 3 steps in v0")
        
        batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}"
        steps = [BatchStep(step_id=i, title=title) for i, title in enumerate(step_titles)]
        
        batch = Batch(batch_id=batch_id, steps=steps)
        self.current_batch = batch
        self.save(batch)
        return batch

    def save(self, batch: Batch):
        batch.updated_at = datetime.now().isoformat()
        filepath = os.path.join(self.batches_dir, f"{batch.batch_id}.json")
        with open(filepath, "w") as f:
            json.dump(batch.to_dict(), f, indent=2)

    def load_batch(self, batch_id: str) -> Optional[Batch]:
        filepath = os.path.join(self.batches_dir, f"{batch_id}.json")
        if not os.path.exists(filepath):
            return None
        
        with open(filepath, "r") as f:
            data = json.load(f)
            # Reconstruct steps
            data["steps"] = [BatchStep(**s) for s in data["steps"]]
            batch = Batch(**data)
            self.current_batch = batch
            return batch

    def get_status(self, batch: Batch) -> str:
        completed = sum(1 for s in batch.steps if s.status == "success")
        total = len(batch.steps)
        return f"[{batch.status.upper()}] Step {batch.current_step_index + 1}/{total} ({batch.steps[batch.current_step_index].title if batch.current_step_index < total else 'Done'})"

    def stop_batch(self, batch: Batch):
        if batch.status == "running":
            batch.status = "stopped"
            self.save(batch)
            return True
        return False
