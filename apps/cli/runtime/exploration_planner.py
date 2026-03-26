"""
Deterministic exploration ranking from operational contract spec + RepoProfile v2 + prompt hints.
Not a full Planner — operational intelligence layer for read/ls priority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from apps.cli.runtime.taskspec_engine import extract_target_files_from_prompt


def _norm_rel(p: str) -> str:
    return p.replace("\\", "/").strip().strip("./").lower()


def path_under_ui_roots(norm_path: str, ui_roots: List[str]) -> bool:
    """True if normalized repo-relative path is under any ui_root."""
    p = _norm_rel(norm_path)
    if not p:
        return False
    for root in ui_roots or []:
        r = _norm_rel(str(root))
        if not r:
            continue
        if p == r or p.startswith(r + "/"):
            return True
    return False


@dataclass
class ExplorationPlan:
    """Ranked exploration targets derived from RepoProfile v2."""

    ranked_files: List[str] = field(default_factory=list)
    ranked_dirs: List[str] = field(default_factory=list)
    evidence_lines: List[str] = field(default_factory=list)
    ui_exploration_blocked: bool = False


def build_exploration_plan(
    contract_spec: Optional[Dict[str, Any]],
    repo_v2: Optional[Dict[str, Any]],
    user_prompt: str,
) -> ExplorationPlan:
    """
    Produce deterministic ranked file/dir lists. Uses RepoProfile v2 when present;
    falls back to prompt target_files only when v2 is missing or empty.
    """
    plan = ExplorationPlan()
    if not contract_spec:
        contract_spec = {}
    scope_set = set(contract_spec.get("scope") or [])
    forbidden = list(contract_spec.get("forbidden_layers") or [])
    intent = str(contract_spec.get("intent") or "").lower()

    seen: Set[str] = set()
    files_out: List[str] = []
    dirs_out: List[str] = []

    def add_file(p: str, reason: str) -> None:
        p = p.replace("\\", "/").strip()
        if not p or p in seen:
            return
        seen.add(p)
        files_out.append(p)
        plan.evidence_lines.append(reason)

    def add_dir(d: str, reason: str) -> None:
        d = d.replace("\\", "/").strip().rstrip("/")
        if not d or d in seen:
            return
        seen.add(d)
        dirs_out.append(d)
        plan.evidence_lines.append(reason)

    for p in extract_target_files_from_prompt(user_prompt or ""):
        add_file(p, f"RepoProfile exploration: user prompt mentioned `{p}`.")

    v2_ok = bool(repo_v2) and isinstance(repo_v2, dict)
    api_routes: List[Any] = list(repo_v2.get("api_routes") or []) if v2_ok else []

    api_data_task = bool(scope_set & {"api", "data"}) or intent in (
        "implementation",
        "modification",
        "bugfix",
        "refactor",
        "verification",
    )

    if v2_ok:
        entry = repo_v2.get("entrypoints") or {}
        api_roots = list(entry.get("api_roots") or [])
        db_roots = list(entry.get("db_schema_roots") or [])
        ui_roots = list(entry.get("ui_roots") or [])

        if api_data_task or scope_set & {"api", "data"}:
            for r in api_routes[:20]:
                if isinstance(r, dict):
                    f = r.get("file")
                    if f:
                        meth = r.get("method") or "?"
                        pth = r.get("path") or ""
                        add_file(
                            str(f),
                            f"RepoProfile prioritized `{f}` (App Router route {meth} {pth}).",
                        )
            for f in repo_v2.get("db_schema_files") or []:
                if isinstance(f, str):
                    add_file(f, f"RepoProfile selected `{f}` as DB schema file.")
            for f in repo_v2.get("validation_files") or []:
                if isinstance(f, str):
                    add_file(f, f"RepoProfile selected `{f}` as validation layer.")

            for d in api_roots:
                if isinstance(d, str):
                    add_dir(d, f"RepoProfile api_roots: prefer `ls` under `{d}/`.")
            for d in db_roots:
                if isinstance(d, str):
                    add_dir(d, f"RepoProfile db_schema_roots: prefer `ls` under `{d}/`.")

        for f in (repo_v2.get("key_files") or [])[:10]:
            if isinstance(f, str) and f not in seen:
                add_file(f, f"RepoProfile key file: `{f}`.")

        vc = repo_v2.get("verification_commands") or {}
        if isinstance(vc, dict):
            src = vc.get("source")
            if isinstance(src, list) and src:
                plan.evidence_lines.append(
                    f"Verification commands derived from {', '.join(src)} (used at verify time)."
                )
    else:
        plan.evidence_lines.append(
            "RepoProfile v2 missing or incomplete — exploration falls back to prompt targets and generic discovery."
        )

    if "ui" in forbidden and v2_ok:
        ui_roots = list((repo_v2.get("entrypoints") or {}).get("ui_roots") or [])
        plan.ui_exploration_blocked = True
        if ui_roots:
            plan.evidence_lines.append(
                "TaskSpec forbids UI layer — read_file/ls under ui_roots are blocked; use API/data paths above."
            )
        else:
            plan.evidence_lines.append(
                "TaskSpec forbids UI layer — avoid components/pages/app UI unless strictly necessary for typing."
            )
        files_out = [f for f in files_out if not path_under_ui_roots(f, ui_roots)]
        dirs_out = [d for d in dirs_out if not path_under_ui_roots(d, ui_roots)]

    plan.ranked_files = files_out
    plan.ranked_dirs = dirs_out
    return plan


def exploration_plan_to_prompt_block(plan: ExplorationPlan) -> str:
    """Inject into system prompt (empty if nothing to say)."""
    if not plan.ranked_files and not plan.ranked_dirs and not plan.ui_exploration_blocked:
        return ""
    lines = [
        "[EXPLORATION PLAN — RepoProfile v2]",
        "Start with these targets before ad-hoc `ls` of the repo root:",
    ]
    if plan.ranked_files:
        lines.append("Priority read_file (in order):")
        for i, p in enumerate(plan.ranked_files[:25], 1):
            lines.append(f"  {i}. {p}")
    if plan.ranked_dirs:
        lines.append("Suggested ls (directories):")
        for d in plan.ranked_dirs[:12]:
            lines.append(f"  - {d}/")
    if plan.ui_exploration_blocked:
        lines.append(
            "UI LAYER: exploration blocked by TaskSpec — do not read or list under pages/, components/, or App Router UI-only trees."
        )
    if plan.evidence_lines[:8]:
        lines.append("Evidence (deterministic):")
        for e in plan.evidence_lines[:8]:
            lines.append(f"  - {e}")
    return "\n".join(lines) + "\n"
