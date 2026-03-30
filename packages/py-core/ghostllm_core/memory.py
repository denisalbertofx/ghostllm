from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_VERSION = 3


class MemoryStore:
    """
    SQLite-backed runtime state for the CLI.

    Disk artifacts under `.ghost/` are outputs for humans.
    State that the runtime may read back to make decisions survives here.
    """

    def __init__(self, db_path: str = "ghost_memory.db"):
        self.db_path = str(Path(db_path))
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS continuity_entries (
                    task_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_at_unix REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS continuity_handoffs (
                    task_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS continuity_plans (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL DEFAULT '',
                    plan_body TEXT NOT NULL DEFAULT '',
                    relpath TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    task_title TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS filesystem_intents (
                    intent_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    op_type TEXT NOT NULL,
                    relpath TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    reversible INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)
                """,
                (str(SCHEMA_VERSION),),
            )
            conn.commit()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _load(raw: Any, default: Any) -> Any:
        if raw in (None, ""):
            return default
        try:
            return json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return default

    def get_schema_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        try:
            return int((row["value"] if row else SCHEMA_VERSION) or SCHEMA_VERSION)
        except (TypeError, ValueError):
            return SCHEMA_VERSION

    def add_message(self, session_id: str, role: str, content: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content),
            )
            conn.commit()

    def get_history(self, session_id: str, limit: int = 50) -> List[Dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [{"role": str(r["role"]), "content": str(r["content"])} for r in reversed(rows)]

    def clear_history(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.commit()

    def set_metadata(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                (key, self._dump(value)),
            )
            conn.commit()

    def get_metadata(self, key: str) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return self._load(row["value"] if row else None, None)

    def save_task_payload(self, task_id: str, payload: Dict[str, Any]) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (task_id, self._dump(payload), now),
            )
            conn.commit()

    def get_task_payload(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        payload = self._load(row["payload"] if row else None, None)
        return payload if isinstance(payload, dict) else None

    def list_task_payloads(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM tasks ORDER BY updated_at DESC"
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            payload = self._load(row["payload"], None)
            if isinstance(payload, dict):
                out.append(payload)
        return out

    def upsert_continuity_entry(self, task_id: str, payload: Dict[str, Any]) -> None:
        now = self._now_iso()
        updated_at_unix = payload.get("updated_at_unix")
        try:
            unix_val = float(updated_at_unix) if updated_at_unix is not None else datetime.now(timezone.utc).timestamp()
        except (TypeError, ValueError):
            unix_val = datetime.now(timezone.utc).timestamp()
        row = dict(payload)
        row["task_id"] = task_id
        row["updated_at"] = str(row.get("updated_at") or now)
        row["updated_at_unix"] = unix_val
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO continuity_entries(task_id, payload, updated_at, updated_at_unix)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at,
                    updated_at_unix=excluded.updated_at_unix
                """,
                (task_id, self._dump(row), row["updated_at"], unix_val),
            )
            conn.commit()

    def get_continuity_index(self) -> Dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, payload, updated_at FROM continuity_entries ORDER BY updated_at_unix DESC, updated_at DESC"
            ).fetchall()
        by_task_id: Dict[str, Any] = {}
        updated_at = ""
        for row in rows:
            payload = self._load(row["payload"], {})
            if not isinstance(payload, dict):
                continue
            task_id = str(row["task_id"] or payload.get("task_id") or "").strip()
            if not task_id:
                continue
            by_task_id[task_id] = payload
            if not updated_at:
                updated_at = str(row["updated_at"] or "")
        return {"format_version": 2, "schema_version": SCHEMA_VERSION, "updated_at": updated_at, "by_task_id": by_task_id}

    def upsert_handoff(self, task_id: str, payload: Dict[str, Any], *, source: str = "") -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO continuity_handoffs(task_id, payload, source, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    payload=excluded.payload,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (task_id, self._dump(payload), source, now),
            )
            conn.commit()

    def get_handoff(self, task_id: str) -> Tuple[Dict[str, Any], str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, source FROM continuity_handoffs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        payload = self._load(row["payload"] if row else None, {})
        source = str(row["source"] if row else "")
        return (payload if isinstance(payload, dict) else {}), source

    def get_latest_handoff(self) -> Tuple[Dict[str, Any], str]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT h.payload, h.source
                FROM continuity_handoffs h
                LEFT JOIN continuity_entries e ON e.task_id = h.task_id
                ORDER BY COALESCE(e.updated_at_unix, 0) DESC, COALESCE(e.updated_at, h.updated_at) DESC
                LIMIT 1
                """
            ).fetchone()
        payload = self._load(row["payload"] if row else None, {})
        source = str(row["source"] if row else "")
        return (payload if isinstance(payload, dict) else {}), source

    def upsert_plan(
        self,
        task_id: str,
        *,
        session_id: str,
        plan_body: str,
        relpath: str,
        status: str,
        task_title: str,
    ) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO continuity_plans(task_id, session_id, plan_body, relpath, status, task_title, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    plan_body=excluded.plan_body,
                    relpath=excluded.relpath,
                    status=excluded.status,
                    task_title=excluded.task_title,
                    updated_at=excluded.updated_at
                """,
                (task_id, session_id, plan_body, relpath, status, task_title, now),
            )
            conn.commit()

    def get_plan(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT session_id, plan_body, relpath, status, task_title, updated_at
                FROM continuity_plans
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "session_id": str(row["session_id"] or ""),
            "plan_body": str(row["plan_body"] or ""),
            "relpath": str(row["relpath"] or ""),
            "status": str(row["status"] or ""),
            "task_title": str(row["task_title"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def begin_filesystem_intent(
        self,
        *,
        intent_id: str,
        session_id: str,
        op_type: str,
        relpath: str,
        payload: Dict[str, Any],
        reversible: bool,
    ) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO filesystem_intents(
                    intent_id,
                    session_id,
                    op_type,
                    relpath,
                    payload,
                    reversible,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    intent_id,
                    session_id,
                    op_type,
                    relpath,
                    self._dump(payload),
                    1 if reversible else 0,
                    now,
                    now,
                ),
            )
            conn.commit()

    def update_filesystem_intent_status(self, intent_id: str, status: str) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE filesystem_intents
                SET status = ?, updated_at = ?
                WHERE intent_id = ?
                """,
                (status, now, intent_id),
            )
            conn.commit()

    def list_pending_filesystem_intents(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT intent_id, session_id, op_type, relpath, payload, reversible, status, created_at, updated_at
                FROM filesystem_intents
                WHERE status = 'pending'
                ORDER BY updated_at DESC, created_at DESC
                """
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            payload = self._load(row["payload"], {})
            out.append(
                {
                    "intent_id": str(row["intent_id"] or ""),
                    "session_id": str(row["session_id"] or ""),
                    "op_type": str(row["op_type"] or ""),
                    "relpath": str(row["relpath"] or ""),
                    "payload": payload if isinstance(payload, dict) else {},
                    "reversible": bool(row["reversible"]),
                    "status": str(row["status"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "updated_at": str(row["updated_at"] or ""),
                }
            )
        return out
