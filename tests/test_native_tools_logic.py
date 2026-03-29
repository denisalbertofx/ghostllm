import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root and packages to path
root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, root)
sys.path.insert(0, os.path.join(root, "packages", "py-core"))

from apps.cli.assistant import CodexAssistant


async def _push_lines(chunks, on_line):
    for chunk in chunks:
        on_line(chunk)


def test_native_payload():
    """Verify that the payload includes tools and tool_choice."""
    assistant = CodexAssistant("http://localhost:11434", "key", "model")
    assistant._should_use_stream = MagicMock(return_value=False)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"choices": [{"message": {"content": ""}}]}

    with patch.object(assistant, "_post_chat_completions_async", AsyncMock(return_value=mock_response)) as mock_post:
        assistant._stream_completion()

    _, kwargs = mock_post.call_args
    payload = kwargs["json_payload"]

    assert "tools" in payload
    payload_tool_names = [tool["name"] for tool in payload["tools"]]
    expected_tool_names = [tool["name"] for tool in assistant.NATIVE_TOOLS]
    assert payload_tool_names == expected_tool_names
    assert len(payload["tools"]) == len(assistant.NATIVE_TOOLS)
    assert payload["tool_choice"] == "auto"


def test_tool_call_parsing():
    """Verify that native tool calls are correctly extracted from stream."""
    assistant = CodexAssistant("http://localhost:11434", "key", "model")

    mock_chunks = [
        'data: {"choices": [{"delta": {"role": "assistant", "content": "Let me check."}}]}',
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_123", "function": {"name": "ls", "arguments": "{\\"path\\":"}}]}}]}',
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": " \\".\\"}"}}]}}]}',
        "data: [DONE]",
    ]

    async def mock_stream(**kwargs):
        await _push_lines(mock_chunks, kwargs["on_line"])

    with patch.object(
        assistant,
        "_stream_chat_completions_async",
        new=AsyncMock(side_effect=mock_stream),
    ):
        msg = assistant._stream_completion()

    assert "tool_calls" in msg
    assert msg["tool_calls"][0]["id"] == "call_123"
    assert msg["tool_calls"][0]["function"]["name"] == "ls"
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"path": "."}'


if __name__ == "__main__":
    try:
        test_native_payload()
        test_tool_call_parsing()
        print("\nALL TESTS PASSED")
    except Exception as e:
        print(f"\nTEST FAILED: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
