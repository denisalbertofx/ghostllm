import sqlite3
import json
import os
from datetime import datetime
from typing import List, Dict, Any, Optional

class MemoryStore:
    """
    Persistent SQLite-based storage for GhostLLM CLI sessions.
    Handles conversation history and project-specific metadata.
    """
    def __init__(self, db_path: str = "ghost_memory.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.commit()

    def add_message(self, session_id: str, role: str, content: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content)
            )
            conn.commit()

    def get_history(self, session_id: str, limit: int = 50) -> List[Dict[str, str]]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit)
            )
            rows = cursor.fetchall()
            # History should be in chronological order for LLM
            return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    def clear_history(self, session_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.commit()

    def set_metadata(self, key: str, value: Any):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                (key, json.dumps(value))
            )
            conn.commit()

    def get_metadata(self, key: str) -> Any:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,))
            row = cursor.fetchone()
            return json.loads(row[0]) if row else None
