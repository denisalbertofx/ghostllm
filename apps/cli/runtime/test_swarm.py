from __future__ import annotations

from pathlib import Path

from apps.cli.runtime.swarm import SwarmManager, SwarmWorker


def test_refresh_worker_statuses_marks_missing_worktree_terminated(tmp_path: Path) -> None:
    mgr = SwarmManager(str(tmp_path))
    mgr.workers = [
        SwarmWorker(
            worker_id="worker_a",
            role="coder",
            task_id="task_a",
            status="active",
            worktree_path=str(tmp_path / "missing-worktree"),
            pid=9999,
        )
    ]

    rows = mgr.refresh_worker_statuses()

    assert rows[0].status == "terminated"


def test_refresh_worker_statuses_keeps_placeholder_pid_alive_when_worktree_exists(tmp_path: Path) -> None:
    worktree = tmp_path / ".ghost" / "worktrees" / "worker_live"
    worktree.mkdir(parents=True)
    mgr = SwarmManager(str(tmp_path))
    mgr.workers = [
        SwarmWorker(
            worker_id="worker_live",
            role="reviewer",
            task_id="task_live",
            status="active",
            worktree_path=str(worktree),
            pid=9999,
        )
    ]

    rows = mgr.refresh_worker_statuses()

    assert rows[0].status == "active"


def test_cleanup_workers_prunes_terminated_rows(tmp_path: Path) -> None:
    live_dir = tmp_path / ".ghost" / "worktrees" / "worker_live"
    live_dir.mkdir(parents=True)
    mgr = SwarmManager(str(tmp_path))
    mgr.workers = [
        SwarmWorker(
            worker_id="worker_dead",
            role="coder",
            task_id="task_dead",
            status="terminated",
            worktree_path=str(tmp_path / "missing-dead"),
            pid=None,
        ),
        SwarmWorker(
            worker_id="worker_live",
            role="architect",
            task_id="task_live",
            status="active",
            worktree_path=str(live_dir),
            pid=9999,
        ),
    ]

    pruned = mgr.cleanup_workers()

    assert pruned == 1
    assert [w.worker_id for w in mgr.list_workers()] == ["worker_live"]
