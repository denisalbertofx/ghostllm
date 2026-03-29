from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Dict, Tuple

from apps.cli.runtime.paths import PathComposer


def new_intent_id() -> str:
    return uuid.uuid4().hex


def summarize_filesystem_intent(intent: Dict[str, Any]) -> str:
    op_type = str(intent.get("op_type") or "operation")
    relpath = str(intent.get("relpath") or "").strip()
    payload = intent.get("payload") or {}
    if op_type == "run_shell":
        command = str(payload.get("command") or "").strip()
        if command:
            return f"{op_type}: {command[:120]}"
    if relpath:
        return f"{op_type}: {relpath}"
    return op_type


def likely_mutating_shell_command(command: str) -> bool:
    lowered = str(command or "").strip().lower()
    if not lowered:
        return False
    mutating_markers = (
        "npm install",
        "npm i ",
        "pnpm install",
        "yarn install",
        "uv sync",
        "uv add ",
        "pip install",
        "alembic upgrade",
        "alembic revision",
        "prisma migrate",
        "python manage.py migrate",
        "git apply",
        "git checkout",
        "git switch",
        "mkdir ",
        "new-item ",
        "copy-item ",
        "move-item ",
        "remove-item ",
        "touch ",
        "echo ",
        ">",
    )
    return any(marker in lowered for marker in mutating_markers)


def revert_filesystem_intent(repo_root: str, intent: Dict[str, Any]) -> Tuple[bool, str]:
    op_type = str(intent.get("op_type") or "")
    relpath = PathComposer.normalize_path_segments(str(intent.get("relpath") or ""))
    payload = intent.get("payload") or {}

    if op_type == "run_shell":
        return False, "Shell mutations cannot be reverted automatically."
    if not relpath:
        return False, "Incomplete intent payload: missing path."

    full_path = PathComposer.compose(repo_root, relpath)
    parent = str(Path(full_path).parent)
    if parent:
        os.makedirs(parent, exist_ok=True)

    if op_type in {"write_file", "edit_file"}:
        if bool(payload.get("had_existing_file")):
            old_content = str(payload.get("old_content") or "")
            with open(full_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(old_content)
            return True, f"Restored previous contents for {relpath}"
        if os.path.exists(full_path):
            os.remove(full_path)
        return True, f"Removed newly created file {relpath}"

    if op_type == "delete_file":
        old_content = str(payload.get("old_content") or "")
        with open(full_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(old_content)
        return True, f"Restored deleted file {relpath}"

    return False, f"Unsupported intent type: {op_type}"
