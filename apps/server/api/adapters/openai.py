import logging
from typing import Dict, Any, List, Optional
from .tools import ToolUseMapper

logger = logging.getLogger("ghostllm.adapters.openai")

class OpenAIAdapter:
    """
    Formats requests for OpenAI-compatible providers and normalizes responses.
    """

    @staticmethod
    def build_request(
        model: str, 
        messages: List[Dict[str, Any]], 
        tools: List[Dict[str, Any]] = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stream: bool = False,
        profile: str = "coder-default"
    ) -> Dict[str, Any]:
        """
        Builds a complete OpenAI chat completion request with task-aware scheduling profiles.
        """
        # 1. Apply Profile Defaults
        thinking = False
        final_temp = temperature
        final_max_tokens = max_tokens

        if profile == "fast-tools":
            thinking = False
            final_temp = 0.6
            final_max_tokens = 2048 # High speed for file ops
        elif profile == "planner":
            thinking = True
            final_temp = 1.0
            final_max_tokens = 16384 # Large budget for reasoning
        elif profile == "coder-default":
            thinking = False
            final_temp = 0.6
            final_max_tokens = 8192 # Standard safe budget for coding

        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "max_tokens": final_max_tokens,
            "temperature": final_temp
        }
        
        if tools:
            mapped_tools = ToolUseMapper.anthropic_tools_to_openai(tools)
            if mapped_tools:
                payload["tools"] = mapped_tools
            
        # Optional provider-specific thinking toggle can be added here if a model family needs it.
        # if "qwen3-coder" in model.lower():
        #     logger.info(f"QWEN3 SCHEDULER: Profile={profile}, Thinking={thinking}, Temp={final_temp}")
        #     payload["extra_body"] = {"chat_template_kwargs": {"thinking": thinking}}
        #     if thinking:
        #          # Override some params for thinking mode stability
        #          payload["top_p"] = 1.0
        #     else:
        #          payload["top_p"] = 0.95
            
        return payload

    @staticmethod
    def normalize_response(openai_resp: Dict[str, Any], local_model: str) -> Dict[str, Any]:
        """Converts a standard OpenAI JSON response to Anthropic message format."""
        import uuid
        message_id = f"msg_{uuid.uuid4().hex}"
        content = []
        finish_reason = "end_turn"
        
        choice = openai_resp.get("choices", [{}])[0]
        msg = choice.get("message", {})
        
        # 1. Handle Thinking/Reasoning
        reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        if reasoning:
            content.append({
                "type": "thinking",
                "thinking": reasoning
            })

        # 2. Handle Text
        if msg.get("content"):
            content.append({
                "type": "text",
                "text": msg["content"]
            })
            
        # 3. Handle Tool Calls
        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            finish_reason = "tool_use"
            for tc in tool_calls:
                content.append(ToolUseMapper.openai_call_to_anthropic_use(tc))
        
        usage = openai_resp.get("usage", {})
        
        return {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": local_model,
            "content": content,
            "stop_reason": finish_reason,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0)
            }
        }
