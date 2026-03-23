import sys
import os
import json
import asyncio
from typing import AsyncGenerator

# Add parent directory to sys.path to import modules
sys.path.append(os.path.abspath("apps/server/api"))

from adapters.tools import ToolUseMapper
from adapters.stream import StreamNormalizer
from adapters.anthropic import AnthropicAdapter
from adapters.openai import OpenAIAdapter

async def test_tool_mapping():
    print("--- Testing ToolUseMapper ---")
    anth_tools = [{"name": "read_file", "description": "read it", "input_schema": {"type": "object"}}]
    openai_tools = ToolUseMapper.anthropic_tools_to_openai(anth_tools)
    assert openai_tools[0]["function"]["name"] == "read_file"
    print("✅ Anthropic Definitions -> OpenAI: SUCCESS")

    openai_call = {"id": "call_123", "function": {"name": "write_file", "arguments": "{\"path\": \"test.py\"}"}}
    anth_use = ToolUseMapper.openai_call_to_anthropic_use(openai_call)
    assert anth_use["id"] == "call_123"
    assert anth_use["input"]["path"] == "test.py"
    print("✅ OpenAI Tool Call -> Anthropic Tool Use: SUCCESS")

async def test_scheduler():
    print("\n--- Testing Task-Aware Scheduler ---")
    # Case 1: Fast Tools
    fast_req = {"tools": [{"name": "ls"}, {"name": "grep"}], "messages": [], "system": ""}
    intent = AnthropicAdapter.detect_intent(fast_req)
    assert intent == "fast-tools"
    print("✅ Intent Detect: fast-tools (all explorers): SUCCESS")

    # Case 2: Planner
    plan_req = {"tools": [], "messages": [], "system": "Help me plan the architecture of the app."}
    intent = AnthropicAdapter.detect_intent(plan_req)
    assert intent == "planner"
    print("✅ Intent Detect: planner (system prompt): SUCCESS")

    # Case 3: Coder Default
    coding_req = {"tools": [{"name": "write_file"}], "messages": [], "system": ""}
    intent = AnthropicAdapter.detect_intent(coding_req)
    assert intent == "coder-default"
    print("✅ Intent Detect: coder-default (standard editing): SUCCESS")

async def test_stream_normalization():
    print("\n--- Testing StreamNormalizer ---")
    async def mock_stream():
        yield 'data: {"choices": [{"delta": {"content": "Hello"}, "index": 0}]}\n\n'
        yield 'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "tc1", "function": {"name": "read", "arguments": "{\\"path\\": \\"f.txt\\"}"}}]}, "index": 0}]}\n\n'
        yield 'data: {"choices": [{"finish_reason": "tool_calls", "index": 0}]}\n\n'
        yield 'data: [DONE]\n\n'

    normalizer = StreamNormalizer("claude-model")
    events = []
    async for event in normalizer.generate(mock_stream()):
        events.append(event)
        
    assert "message_start" in events[0]
    assert "content_block_start" in events[1]
    assert "text_delta" in events[2]
    assert "tool_use" in events[3]
    assert "message_stop" in events[-1]
    print("✅ Stream Sequence Normalization: SUCCESS")

async def main():
    try:
        await test_tool_mapping()
        await test_scheduler()
        await test_stream_normalization()
        print("\n🏆 ALL ARCHITECTURAL TESTS PASSED!")
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
