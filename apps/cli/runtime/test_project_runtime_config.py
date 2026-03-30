from pathlib import Path

from apps.cli.runtime.execution_agent import resolve_model_role_map
from apps.cli.runtime.project_runtime_config import (
    apply_project_routing_to_role_map,
    load_and_validate_project_runtime_config,
)


def test_load_and_validate_project_runtime_config(tmp_path: Path) -> None:
    (tmp_path / "ghost.yaml").write_text(
        "routing:\n"
        "  explore: qwen-small\n"
        "  act: qwen-big\n"
        "  verify: llama-verify\n"
        "  fallback: llama-small\n",
        encoding="utf-8",
    )

    cfg = load_and_validate_project_runtime_config(str(tmp_path))

    assert cfg.exists is True
    assert cfg.valid is True
    assert cfg.routing["explore"] == "qwen-small"
    assert cfg.routing["act"] == "qwen-big"
    assert cfg.routing["verify"] == "llama-verify"
    assert cfg.routing["fallback"] == "llama-small"


def test_apply_project_routing_to_role_map_overlays_phase_models() -> None:
    base = {
        "planner": "env-plan",
        "execution": "env-exec",
        "repair": "env-repair",
        "general_fallback": "env-fallback",
        "execution_fallback": "env-exec-fallback",
        "provider_backend": "openai_compatible",
    }
    cfg = load_and_validate_project_runtime_config("does-not-exist")
    cfg = type(cfg)(
        path=cfg.path,
        exists=True,
        valid=True,
        routing={
            "explore": "project-explore",
            "act": "project-act",
            "verify": "project-verify",
            "fallback": "project-fallback",
        },
        errors=[],
        warnings=[],
    )

    resolved = apply_project_routing_to_role_map(base, cfg)

    assert resolved["explore"] == "project-explore"
    assert resolved["act"] == "project-act"
    assert resolved["verify"] == "project-verify"
    assert resolved["fallback"] == "project-fallback"
    assert resolved["planner"] == "project-explore"
    assert resolved["execution"] == "project-act"
    assert resolved["repair"] == "project-act"
    assert resolved["general_fallback"] == "project-fallback"


def test_resolve_model_role_map_reads_project_ghost_yaml(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "ghost.yaml").write_text(
        "routing:\n"
        "  explore: repo-explore\n"
        "  act: repo-act\n"
        "  verify: repo-verify\n"
        "  fallback: repo-fallback\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GHOST_PLANNER_MODEL", "env-plan")
    monkeypatch.setenv("GHOST_EXECUTION_MODEL", "env-exec")
    monkeypatch.setenv("GHOST_REPAIR_MODEL", "env-repair")

    resolved = resolve_model_role_map(str(tmp_path))

    assert resolved["planner"] == "repo-explore"
    assert resolved["execution"] == "repo-act"
    assert resolved["repair"] == "repo-act"
    assert resolved["general_fallback"] == "repo-fallback"


def test_load_and_validate_project_runtime_config_requires_all_routing_keys(tmp_path: Path) -> None:
    (tmp_path / "ghost.yaml").write_text(
        "routing:\n"
        "  explore: qwen-small\n",
        encoding="utf-8",
    )

    cfg = load_and_validate_project_runtime_config(str(tmp_path))

    assert cfg.valid is False
    assert "ghost.yaml:routing.act is required" in " ".join(cfg.errors)
    assert "ghost.yaml:routing.verify is required" in " ".join(cfg.errors)
    assert "ghost.yaml:routing.fallback is required" in " ".join(cfg.errors)
