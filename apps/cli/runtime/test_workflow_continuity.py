from pathlib import Path

from ghostllm_core.memory import MemoryStore

from apps.cli.runtime.workflow_continuity import (
    load_continuity_for_task,
    load_handoff_for_task,
    read_continuity_index,
)


def test_read_continuity_index_ignores_disk_fallback_when_db_empty(tmp_path: Path) -> None:
    ghost_dir = tmp_path / ".ghost"
    ghost_dir.mkdir()
    (ghost_dir / "continuity_index.json").write_text(
        '{"format_version":2,"by_task_id":{"task-x":{"task_id":"task-x"}}}',
        encoding="utf-8",
    )

    data = read_continuity_index(str(tmp_path))

    assert data["by_task_id"] == {}


def test_load_continuity_for_task_uses_sqlite_not_disk(tmp_path: Path) -> None:
    store = MemoryStore(str(tmp_path / "ghost_memory.db"))
    store.upsert_plan(
        "task-x",
        session_id="sess-1",
        plan_body="sqlite plan",
        relpath=".ghost/plans/task-x__sess-1.md",
        status="completed",
        task_title="task x",
    )
    store.upsert_handoff(
        "task-x",
        {
            "schema_version": 2,
            "session_id": "sess-1",
            "task_id": "task-x",
            "task": "sqlite task",
            "files_touched": ["sqlite.py"],
            "last_verify": {},
        },
        source=".ghost/handoffs/task-x__sess-1.json",
    )
    ghost_dir = tmp_path / ".ghost"
    (ghost_dir / "plans").mkdir(parents=True)
    (ghost_dir / "handoffs").mkdir(parents=True)
    (ghost_dir / "plans" / "task-x__latest.md").write_text("disk plan", encoding="utf-8")
    (ghost_dir / "handoffs" / "task-x__latest.json").write_text(
        '{"schema_version":2,"session_id":"disk","task_id":"task-x","task":"disk task"}',
        encoding="utf-8",
    )

    result = load_continuity_for_task(str(tmp_path), "task-x")

    assert result.plan_body == "sqlite plan"
    assert result.handoff["task"] == "sqlite task"


def test_load_handoff_for_task_does_not_fallback_to_disk(tmp_path: Path) -> None:
    ghost_dir = tmp_path / ".ghost" / "handoffs"
    ghost_dir.mkdir(parents=True)
    (ghost_dir / "task-x__latest.json").write_text(
        '{"schema_version":2,"session_id":"disk","task_id":"task-x","task":"disk task"}',
        encoding="utf-8",
    )

    handoff, warnings, source = load_handoff_for_task(str(tmp_path), "task-x")

    assert handoff == {}
    assert source == ""
    assert warnings == []


def test_load_continuity_for_task_fails_cleanly_when_sqlite_is_corrupt(tmp_path: Path) -> None:
    (tmp_path / "ghost_memory.db").write_bytes(b"not-a-sqlite-db")
    ghost_dir = tmp_path / ".ghost"
    (ghost_dir / "plans").mkdir(parents=True)
    (ghost_dir / "handoffs").mkdir(parents=True)
    (ghost_dir / "plans" / "task-x__latest.md").write_text("disk plan", encoding="utf-8")
    (ghost_dir / "handoffs" / "task-x__latest.json").write_text(
        '{"schema_version":2,"session_id":"disk","task_id":"task-x","task":"disk task"}',
        encoding="utf-8",
    )

    result = load_continuity_for_task(str(tmp_path), "task-x")

    assert result.plan_body == ""
    assert result.plan_path == ""
    assert result.handoff == {}
    assert result.handoff_warnings == ["sqlite_unavailable:DatabaseError"]
