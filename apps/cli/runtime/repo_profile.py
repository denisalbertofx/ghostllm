"""
Repository profile: legacy RepoProfile + RepoProfile v2 semantic analyzer.

scan_repo_profile() builds RepoProfileV2 (with cache) and maps to RepoProfile
for TaskSpec compatibility. Full v2 is available as repo_profile.v2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from apps.cli.runtime.repo_profile_v2 import (
    RepoProfileV2,
    build_repo_profile,
    repo_profile_to_prompt_block,
    repo_profile_to_summary_text,
)

__all__ = [
    "RepoProfile",
    "RepoProfileV2",
    "scan_repo_profile",
    "build_repo_profile",
    "repo_profile_to_summary_text",
    "repo_profile_to_prompt_block",
]


@dataclass
class RepoProfile:
    """Snapshot for TaskSpec Engine (legacy fields + optional v2)."""

    root: Optional[Path] = None
    stack: str = "unknown"
    layers_detected: List[str] = field(default_factory=list)
    has_package_json: bool = False
    important_folders: List[str] = field(default_factory=list)
    v2: Optional[RepoProfileV2] = None


def _legacy_stack_from_v2(v2: RepoProfileV2) -> str:
    fw = list(v2.stack.framework or [])
    rt = v2.stack.runtime or "unknown"
    if rt == "polyglot":
        return "polyglot"
    if "nextjs" in fw:
        return "nextjs"
    if rt == "node":
        return "node"
    if rt == "python":
        return "python"
    return "unknown"


def scan_repo_profile(root: Path, *, cache: bool = True, force_refresh: bool = False) -> RepoProfile:
    """
    Build semantic RepoProfile v2 (cached) and map to legacy RepoProfile.
    """
    root = root.resolve()
    v2 = build_repo_profile(root, cache=cache, force_refresh=force_refresh)
    return RepoProfile(
        root=root,
        stack=_legacy_stack_from_v2(v2),
        layers_detected=list(v2.layers_detected),
        has_package_json=any(str(k).endswith("package.json") for k in (v2.key_files or [])),
        important_folders=list(v2.important_folders),
        v2=v2,
    )
