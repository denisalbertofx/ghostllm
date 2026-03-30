from pathlib import Path

from apps.cli.assistant import CodexAssistant
from apps.cli.runtime.session_phase import SessionPhase


def test_assistant_resolves_phase_models_from_project_ghost_yaml(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "ghost.yaml").write_text(
        "routing:\n"
        "  explore: qwen-explore\n"
        "  act: qwen-act\n"
        "  verify: llama-verify\n"
        "  fallback: llama-fallback\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assistant = CodexAssistant("http://localhost:11434", "key", "coder")

    assert assistant._effective_phase_model(SessionPhase.EXPLORE) == "qwen-explore"
    assert assistant._effective_phase_model(SessionPhase.ACT) == "qwen-act"
    assert assistant._effective_phase_model(SessionPhase.VERIFY) == "llama-verify"


def test_assistant_uses_explicit_model_when_project_routing_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assistant = CodexAssistant("http://localhost:11434", "key", "explicit-model")

    assert assistant._effective_phase_model(SessionPhase.EXPLORE) == "explicit-model"
    assert assistant._effective_phase_model(SessionPhase.ACT) == "explicit-model"
    assert assistant._effective_phase_model(SessionPhase.VERIFY) == "explicit-model"


def test_invalid_project_runtime_contract_blocks_mutating_tools(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "ghost.yaml").write_text(
        "routing:\n"
        "  explore: ok\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assistant = CodexAssistant("http://localhost:11434", "key", "coder", auto_approve=True)
    result = assistant._execute_tool(
        {"name": "write_file", "arguments": {"path": "x.txt", "content": "hello"}},
        is_authorized=True,
    )

    assert "ghost.yaml" in result["error"]
    assert "Blocked mutating action" in result["error"]
