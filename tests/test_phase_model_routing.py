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
