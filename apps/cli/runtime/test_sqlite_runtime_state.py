from __future__ import annotations

from pathlib import Path

from apps.cli.runtime.tasks import TaskManager
from apps.cli.runtime.workflow_continuity import (
    load_continuity_for_task,
    load_handoff_for_task,
    read_continuity_index,
    save_handoff_json,
    save_operational_plan,
    write_continuity_index,
)
from ghostllm_core.memory import MemoryStore


def test_memory_store_uses_versioned_schema(tmp_path: Path) -> None:
    store = MemoryStore(str(tmp_path / "ghost_memory.db"))
    assert store.get_schema_version() == 2


def test_task_manager_persists_tasks_in_sqlite(tmp_path: Path) -> None:
    tm = TaskManager(str(tmp_path))
    task = tm.create_task("crear demo", "do")

    db_path = tmp_path / "ghost_memory.db"
    assert db_path.is_file()
    assert not (tmp_path / ".ghost" / "tasks").exists()

    reloaded = TaskManager(str(tmp_path)).get_task(task.task_id)
    assert reloaded is not None
    assert reloaded.title == "crear demo"


def test_continuity_index_reads_from_sqlite_not_json_file(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    write_continuity_index(
        cwd,
        {
            "format_version": 2,
            "by_task_id": {
                "tid_sqlite": {
                    "task_id": "tid_sqlite",
                    "active_handoff_relpath": ".ghost/handoffs/tid_sqlite__latest.json",
                    "latest_plan_relpath": ".ghost/plans/tid_sqlite__latest.md",
                    "updated_at": "2099-01-01T00:00:00+00:00",
                    "updated_at_unix": 9999999999.0,
                }
            },
        },
    )

    idx = read_continuity_index(cwd)
    assert "tid_sqlite" in idx.get("by_task_id", {})
    assert not (tmp_path / ".ghost" / "continuity_index.json").exists()


def test_plan_and_handoff_load_from_sqlite_when_disk_output_missing(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    rel_plan = save_operational_plan(
        cwd,
        session_id="sess1",
        task_id="tid_live",
        plan_body="# Plan live",
        status="partial",
        task_title="demo",
    )
    rel_handoff = save_handoff_json(
        cwd,
        {
            "schema_version": 2,
            "task_id": "tid_live",
            "session_id": "sess1",
            "mode": "m",
            "task": "demo",
            "plan_reference": rel_plan,
        },
    )

    for fp in (tmp_path / ".ghost" / "plans").glob("*.md"):
        fp.unlink()
    for fp in (tmp_path / ".ghost" / "handoffs").glob("*.json"):
        fp.unlink()

    ctx = load_continuity_for_task(cwd, "tid_live")
    handoff, warnings, _src = load_handoff_for_task(cwd, "tid_live")

    assert "Plan live" in ctx.plan_body
    assert handoff.get("task") == "demo"
    assert "task_id_mismatch" not in warnings
