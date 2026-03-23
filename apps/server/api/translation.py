import logging
from typing import Dict, Any, List, Optional, AsyncGenerator

from .adapters.anthropic import AnthropicAdapter
from .adapters.openai import OpenAIAdapter
from .adapters.tools import ToolUseMapper
from .adapters.stream import StreamNormalizer

logger = logging.getLogger("ghostllm.api.translation")

def translate_anthropic_to_openai(data: Dict[str, Any], model_mapping: Dict[str, str]) -> Dict[str, Any]:
    """
    High-level orchestrator for Anthropic-to-OpenAI translation.
    Uses modular adapters for request parsing, tool mapping, and payload construction.
    """
    # 1. Parse Anthropic Request and Detect Intent
    req = AnthropicAdapter.parse_request(data)
    profile = AnthropicAdapter.detect_intent(data)
    
    # 2. Determine Upstream Model
    local_model = req["model"]
    if local_model in ["sonnet", "claude-3-5-sonnet", "claude-3-5-sonnet-20241022", "claude-sonnet-4-6"]:
         local_model = "kimi"
    upstream_model = model_mapping.get(local_model, local_model)
    
    # 3. Process Messages with Truncation (Sliding Window)
    # Keeping 110k tokens (~330k chars) to fit Kimi's 128k limit
    max_chars = 330000 
    current_chars = 0
    truncated_messages = []
    
    # Keep system prompt if present
    system_prompt = AnthropicAdapter.clean_xml_tools(req["system"])
    
    # Convert and Truncate history (Sliding Window)
    history = req.get("messages", [])
    for msg in reversed(history):
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
            # Handle tool results
            for c in content:
                if c.get("type") == "tool_result":
                    text_parts.append(f"Tool Result ({c.get('tool_use_id')}): {c.get('content')}")
            content = "\n".join(text_parts)
            
        msg_len = len(str(content))
        if current_chars + msg_len > max_chars:
            logger.info(f"TRUNCATION: History cutoff at {len(truncated_messages)}/{len(history)} messages.")
            break
            
        current_chars += msg_len
        truncated_messages.insert(0, {
            "role": msg.get("role"),
            "content": content
        })

    # Add system prompt as a message for OpenAI
    if system_prompt:
        truncated_messages.insert(0, {"role": "system", "content": system_prompt})

    # 4. Handle Tools
    anth_tools = req.get("tools", [])
    has_tools = bool(anth_tools)
        
    # 5. Build OpenAI Request with Task-Aware Profile
    openai_req = OpenAIAdapter.build_request(
        model=upstream_model,
        messages=truncated_messages,
        tools=anth_tools if has_tools else None,
        temperature=req.get("temperature", 1.0),
        stream=req.get("stream", False),
        profile=profile
    )

    logger.info(f"MODULAR TRANSLATION: Anthropic -> OpenAI | upstream={upstream_model}, profile={profile}, tools={has_tools}")
    return openai_req

def translate_openai_to_anthropic(openai_resp: Dict[str, Any], local_model: str = "claude-3-5-sonnet") -> Dict[str, Any]:
    """Wrapper for modular response normalization."""
    return OpenAIAdapter.normalize_response(openai_resp, local_model)

async def stream_openai_to_anthropic(openai_stream: AsyncGenerator[str, None], requested_model: str = "claude-3-5-sonnet") -> AsyncGenerator[str, None]:
    """Wrapper for modular stateful SSE normalization."""
    normalizer = StreamNormalizer(requested_model)
    async for chunk in normalizer.generate(openai_stream):
        yield chunk
