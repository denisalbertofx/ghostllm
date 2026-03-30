from unittest import mock

from ghost import Agent, AgentRunResult


def test_agent_plan_dispatches_to_codex_assistant_and_returns_result() -> None:
    artifact = {
        "session_id": "task_123",
        "task_outcome": "read_only",
        "primary_output_path": ".ghost/review_packets/task_123.json",
        "artifact_json_path": ".ghost/artifacts/task_123.json",
    }
    with mock.patch("ghost.agent.CodexAssistant") as assistant_cls:
        assistant_cls.return_value.run.return_value = artifact
        agent = Agent(server_url="http://localhost:8000", api_key="key", cwd="C:/tmp/project")
        result = agent.plan("audita el repo")

    assistant_cls.assert_called_once()
    assistant = assistant_cls.return_value
    assistant.run.assert_called_once_with("/plan audita el repo", keep_open=False)
    assert isinstance(result, AgentRunResult)
    assert result.session_id == "task_123"
    assert result.primary_output_path == ".ghost/review_packets/task_123.json"


def test_agent_run_silent_captures_console_streams() -> None:
    artifact = {"session_id": "task_456", "task_outcome": "implemented"}
    with mock.patch("ghost.agent.CodexAssistant") as assistant_cls:
        def _run(task, keep_open=False):
            print("stdout marker")
            return artifact

        assistant_cls.return_value.run.side_effect = _run
        agent = Agent(server_url="http://localhost:8000", api_key="key", silent=True)
        result = agent.do("implementa algo")

    assert result.session_id == "task_456"
    assert "stdout marker" in result.captured_stdout
