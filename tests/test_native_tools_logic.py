import sys
import os
import json
from unittest.mock import MagicMock, patch

# Add project root and packages to path
root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, root)
sys.path.insert(0, os.path.join(root, "packages", "py-core"))

from apps.cli.assistant import CodexAssistant

def test_native_payload():
    """Verify that the payload includes tools and tool_choice."""
    assistant = CodexAssistant("http://localhost:11434", "key", "model")
    
    with patch("requests.post") as mock_post:
        # Mock a minimal SSE response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.__enter__.return_value = mock_response
        mock_response.iter_lines.return_value = [b"data: [DONE]"]
        mock_post.return_value = mock_response
        
        assistant._stream_completion()
        
        args, kwargs = mock_post.call_args
        payload = kwargs["json"]
        
        assert "tools" in payload
        assert len(payload["tools"]) == 4
        assert payload["tool_choice"] == "auto"
        print("✓ Payload contains native tools definition.")

def test_tool_call_parsing():
    """Verify that native tool calls are correctly extracted from stream."""
    assistant = CodexAssistant("http://localhost:11434", "key", "model")
    
    # Mock a stream with a tool call
    mock_chunks = [
        b'data: {"choices": [{"delta": {"role": "assistant", "content": "Let me check."}}]}',
        b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_123", "function": {"name": "ls", "arguments": "{\\"path\\":"}}]}}]}',
        b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": " \\".\\"}"}}]}}]}',
        b"data: [DONE]"
    ]
    
    with patch("requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.__enter__.return_value = mock_response
        mock_response.iter_lines.return_value = mock_chunks
        mock_post.return_value = mock_response
        
        msg = assistant._stream_completion()
        
        assert "tool_calls" in msg
        assert msg["tool_calls"][0]["id"] == "call_123"
        assert msg["tool_calls"][0]["function"]["name"] == "ls"
        assert msg["tool_calls"][0]["function"]["arguments"] == '{"path": "."}'
        print("✓ Tool call parsing from stream works correctly.")

if __name__ == "__main__":
    try:
        test_native_payload()
        test_tool_call_parsing()
        print("\n✨ ALL TESTS PASSED")
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
