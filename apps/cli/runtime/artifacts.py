import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional

class ArtifactSession:
    """
    GEP-6: Artifact Contract Session.
    Structured container for task evidence.
    """
    def __init__(self, session_id: str, task: str, task_id: str = "N/A"):
        self.session_id = session_id
        self.task_id = task_id
        self.timestamp = datetime.now().isoformat()
        self.task = task
        self.plan: str = ""
        self.root_cause: str = ""
        self.diff_summary: List[Dict[str, str]] = []
        self.verification: Dict[str, Any] = {}
        self.rollback_status: str = "none" # none, available, executed
        self.next_action: str = ""
        self.events: List[Dict[str, Any]] = [] # Runtime audit trail

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "task": self.task,
            "plan": self.plan,
            "root_cause": self.root_cause,
            "diff_summary": self.diff_summary,
            "verification": self.verification,
            "rollback_status": self.rollback_status,
            "next_action": self.next_action,
            "events": self.events
        }

    def to_markdown(self) -> str:
        md = f"# 👻 Ghost Task Artifact: {self.session_id}\n\n"
        md += f"**Task ID:** {self.task_id}\n"
        md += f"**Timestamp:** {self.timestamp}\n\n"
        md += f"## 🎯 Task\n{self.task}\n\n"
        md += f"## 📝 Plan\n{self.plan or 'N/A'}\n\n"
        md += f"## 🔍 Root Cause\n{self.root_cause or 'N/A'}\n\n"
        
        md += "## 🛠️ Diff Summary\n"
        if not self.diff_summary:
            md += "No files changed.\n"
        for item in self.diff_summary:
            md += f"- **{item.get('file')}**: {item.get('type')} ({item.get('status')})\n"
        md += "\n"
        
        if self.verification:
            md += "## 🛡️ Verification\n"
            md += f"**Status:** {self.verification.get('status', 'N/A').upper()}\n"
            for check in self.verification.get("checks", []):
                md += f"- {check.get('name')}: {check.get('status')}\n"
            md += "\n"

        if self.events:
            md += "## 📂 Runtime Audit Trail\n"
            # Structured table-like header for better readability
            md += "| Timestamp | Event | Details |\n"
            md += "| :--- | :--- | :--- |\n"
            for event in self.events:
                ts = datetime.fromtimestamp(event.get('timestamp', 0)).strftime('%H:%M:%S')
                name = event.get('event', 'Unknown')
                details = event.get('details', '')
                # Ensure details is a string and clean it up if it's multiline
                if isinstance(details, dict):
                    details = json.dumps(details)
                details = str(details).replace("\n", " ").strip()
                md += f"| `{ts}` | **{name}** | {details} |\n"
            md += "\n"
            
        md += f"## 🔄 Rollback\nStatus: {self.rollback_status}\n\n"
        md += f"## 🚀 Next Action\n{self.next_action or 'N/A'}\n"
        
        return md

class ArtifactManager:
    """
    Manages the lifecycle and persistence of ArtifactSessions.
    """
    def __init__(self, project_root: str):
        self.project_root = project_root
        self.artifacts_dir = os.path.join(project_root, ".ghost", "artifacts")
        os.makedirs(self.artifacts_dir, exist_ok=True)
        self.current_session: Optional[ArtifactSession] = None

    def start_session(self, task: str, task_id: str = "N/A") -> ArtifactSession:
        session_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.current_session = ArtifactSession(session_id, task, task_id)
        return self.current_session

    def persist(self):
        if not self.current_session:
            return

        session_id = self.current_session.session_id
        
        # Save JSON
        json_path = os.path.join(self.artifacts_dir, f"{session_id}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(self.current_session.to_dict(), f, indent=2)
            
        # Save Markdown
        md_path = os.path.join(self.artifacts_dir, f"{session_id}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(self.current_session.to_markdown())

    def add_diff(self, file_path: str, edit_type: str, status: str = "success"):
        if self.current_session:
            self.current_session.diff_summary.append({
                "file": file_path,
                "type": edit_type,
                "status": status
            })
