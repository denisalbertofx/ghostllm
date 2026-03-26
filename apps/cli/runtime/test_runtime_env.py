"""Tests for runtime_env: autoload precedence, flags, startup data, wrong-repo hints."""
from __future__ import annotations

import os

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime import runtime_env as re


def test_parse_env_lines_basic():
    text = """
# c
FOO=1
export BAR=two
BAZ="quoted"
"""
    pairs = re.parse_env_lines(text)
    assert dict(pairs) == {"FOO": "1", "BAR": "two", "BAZ": "quoted"}


def test_forbidden_autoload_rejects_example_suffix(tmp_path):
    p = tmp_path / "x.example"
    p.write_text("A=1")
    assert re._is_forbidden_autoload_path(p)


def test_forbidden_autoload_rejects_nim_flow_name(tmp_path):
    p = tmp_path / "ghost_nim_flow.env"
    p.write_text("A=1")
    assert re._is_forbidden_autoload_path(p)


def test_ghost_local_env_path_allowed(tmp_path):
    p = tmp_path / "ghost.local.env"
    p.write_text("A=1")
    assert not re._is_forbidden_autoload_path(p)


def test_autoload_prefers_dot_ghost_env_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GHOST_AUTOLOAD_ENV", "1")
    monkeypatch.delenv("GHOST_FROM_DOT", raising=False)
    monkeypatch.delenv("GHOST_FROM_CONFIGS", raising=False)
    (tmp_path / ".ghost.env").write_text("GHOST_FROM_DOT=1\n", encoding="utf-8")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "ghost.local.env").write_text("GHOST_FROM_CONFIGS=1\n", encoding="utf-8")
    try:
        attempted, rel, n, _ = re.autoload_local_env(str(tmp_path))
        assert attempted
        assert rel == ".ghost.env"
        assert os.environ.get("GHOST_FROM_DOT") == "1"
        assert os.environ.get("GHOST_FROM_CONFIGS") is None
    finally:
        os.environ.pop("GHOST_FROM_DOT", None)


def test_shell_env_overrides_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GHOST_AUTOLOAD_ENV", "1")
    monkeypatch.setenv("GHOST_USE_TASKSPEC", "0")
    (tmp_path / ".ghost.env").write_text("GHOST_USE_TASKSPEC=1\n", encoding="utf-8")
    re.prepare_runtime(str(tmp_path))
    assert os.environ.get("GHOST_USE_TASKSPEC") == "0"


def test_autoload_off_skips_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GHOST_AUTOLOAD_ENV", raising=False)
    monkeypatch.delenv("GHOST_ONLY_IN_FILE", raising=False)
    (tmp_path / ".ghost.env").write_text("GHOST_ONLY_IN_FILE=1\n", encoding="utf-8")
    re.prepare_runtime(str(tmp_path))
    assert os.environ.get("GHOST_ONLY_IN_FILE") is None


def test_weak_project_root_warning_empty_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = re.weak_project_root_warnings(str(tmp_path))
    assert len(w) == 1
    assert "package.json" in w[0].lower() or "pyproject" in w[0].lower()


def test_weak_project_root_no_warning_with_package_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert re.weak_project_root_warnings(str(tmp_path)) == []


def test_wrong_repo_warnings_outside_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apps").mkdir()
    task = "edit apps/missing/file.tsx"
    w = re.wrong_repo_warnings(str(tmp_path), task)
    assert w


def test_prepare_runtime_sets_flags_and_config_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GHOST_AUTOLOAD_ENV", raising=False)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    prep = re.prepare_runtime(str(tmp_path))
    assert prep.cwd == str(tmp_path.resolve())
    assert "environment" in prep.config_source.lower()
    assert "GHOST_USE_TASKSPEC" in prep.active_feature_flags


def test_artifact_session_cli_fields_roundtrip():
    s = ArtifactSession("sid", "t")
    s.config_source = "configs/ghost.local.env + environment"
    s.autoload_env_used = True
    s.env_file_loaded = "configs/ghost.local.env"
    s.active_feature_flags = {"GHOST_USE_TASKSPEC": "1"}
    s.startup_warnings = ["hint"]
    s.effective_repo_root = "/tmp/proj"
    s.cli_command_mode = "dev"
    d = s.to_dict()
    assert d["config_source"] == s.config_source
    assert d["autoload_env_used"] is True
    assert d["active_feature_flags"]["GHOST_USE_TASKSPEC"] == "1"
    assert d["startup_warnings"] == ["hint"]
    md = s.to_markdown()
    assert "CLI runtime" in md
    assert "GHOST_USE_TASKSPEC" in md
