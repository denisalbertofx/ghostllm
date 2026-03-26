import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "py-core"))

from apps.cli.assistant import CodexAssistant


def test_strip_tool_calls():
    a = CodexAssistant("http://localhost:11434", "k", "kimi")
    text = 'Hello <tool_call>{"name": "read_file", "arguments": {"path": "x"}}</tool_call> world'
    cleaned = a._strip_tool_calls_for_display(text)
    assert "<tool_call>" not in cleaned
    assert "read_file" not in cleaned
    assert "Hello" in cleaned and "world" in cleaned


def test_is_tool_call_chunk():
    a = CodexAssistant("http://localhost:11434", "k", "kimi")
    assert a._is_tool_call_chunk('{"name": "read_file"', False) is True
    assert a._is_tool_call_chunk("I'll check the file", False) is False


def test_extract_tool_calls_json_fallback():
    """JSON tool calls without XML tags should be extracted."""
    a = CodexAssistant("http://localhost:11434", "k", "kimi")
    text = '{"name": "read_file", "arguments": {"path": "db/schema.ts"}}'
    calls = a._extract_tool_calls_json(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"
    assert calls[0]["arguments"]["path"] == "db/schema.ts"


def test_extract_tool_calls_uses_json_fallback():
    """_extract_tool_calls should use JSON fallback when no XML."""
    a = CodexAssistant("http://localhost:11434", "k", "kimi")
    text = '{"name": "read_file", "arguments": {"path": "x"}}'
    calls = a._extract_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"


if __name__ == "__main__":
    test_strip_tool_calls()
    test_is_tool_call_chunk()
    test_extract_tool_calls_json_fallback()
    test_extract_tool_calls_uses_json_fallback()
    print("OK: tool strip tests passed")
