from unittest import mock

from ghost import Agent


def test_agent_plan_dispatches_to_codex_assistant() -> None:
    with mock.patch("ghost.agent.CodexAssistant") as assistant_cls:
        agent = Agent(server_url="http://localhost:8000", api_key="key", cwd="C:/tmp/project")
        agent.plan("audita el repo")

    assistant_cls.assert_called_once()
    assistant = assistant_cls.return_value
    assistant.run.assert_called_once_with("/plan audita el repo", keep_open=False)
