import json
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("ghostllm.adapters.tools")

class ToolUseMapper:
    """
    Bi-directional mapper for Anthropic tool_use and OpenAI tool_calls.
    Ensures that call IDs and data structures are consistent across protocols.
    """

    @staticmethod
    def anthropic_tools_to_openai(anthropic_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Converts Anthropic tool definitions to OpenAI function definitions with strict sanitization."""
        openai_tools = []
        if not anthropic_tools:
            return []
            
        logger.debug(f"Mapping {len(anthropic_tools)} tools.")
            
        for tool in anthropic_tools:
            if not isinstance(tool, dict):
                continue
            
            # Extract common elements with safe fallbacks
            func_data = tool.get("function")
            if not isinstance(func_data, dict):
                # If it's Anthropic format, the tool IS the function data
                func_data = tool
                
            # 1. Detect and sanitize name
            # Priority: tool['name'] (Anthropic) then func_data['name'] (OpenAI)
            name = tool.get("name") or func_data.get("name")
                
            if not isinstance(name, str) or not name.strip() or name.lower() == "none":
                logger.warning(f"Discarding tool with invalid name: '{name}'")
                continue

            name = name.strip()
            
            # 2. Extract description and parameters with strict typing
            description = tool.get("description") or func_data.get("description") or ""
            parameters = tool.get("input_schema") or func_data.get("parameters")
            
            if not isinstance(parameters, dict):
                parameters = {"type": "object", "properties": {}}
            
            # 3. Normalized Passthrough/Construction
            mapped = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(description),
                    "parameters": parameters
                }
            }
            openai_tools.append(mapped)
                 
        logger.debug(f"Mapped result: {[t['function']['name'] for t in openai_tools]}")
        return openai_tools

    @staticmethod
    def openai_call_to_anthropic_use(openai_call: Dict[str, Any]) -> Dict[str, Any]:
        """Converts a single OpenAI tool_call to Anthropic tool_use content block with hardening."""
        if not isinstance(openai_call, dict):
            raise ValueError("openai_call must be a dictionary")

        call_id = openai_call.get("id")
        func = openai_call.get("function")
        if not isinstance(func, dict):
            func = {}

        name = func.get("name")
        
        # Hard validation of identity
        if not isinstance(call_id, str) or not call_id.strip():
            raise ValueError("tool_call missing valid 'id'")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tool_call missing valid function 'name'")

        args_raw = func.get("arguments", "{}")
        args = {}

        # Safe Argument Parsing
        if isinstance(args_raw, dict):
            args = args_raw
        elif isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
                if not isinstance(args, dict):
                    # If it's valid JSON but not a dict (e.g. "[1,2,3]"), wrap it
                    args = {"_wrapped_val": args}
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"Failed to parse tool arguments as JSON: {args_raw}")
                args = {"_raw_arguments": str(args_raw)}
        else:
            args = {"_raw_val": str(args_raw)}

        return {
            "type": "tool_use",
            "id": call_id,
            "name": name,
            "input": args
        }

    @staticmethod
    def anthropic_result_to_openai_tool_msg(tool_result: Dict[str, Any]) -> Dict[str, Any]:
        """Converts Anthropic tool_result to OpenAI tool role message with deep serialization."""
        if not isinstance(tool_result, dict):
             return {"role": "tool", "content": "Error: Internal tool result mapping failed (not a dict)"}

        call_id = tool_result.get("tool_use_id")
        if not isinstance(call_id, str) or not call_id.strip():
            # OpenAI requires a valid tool_call_id
            logger.error(f"Missing tool_use_id in tool_result: {tool_result}")
            call_id = "unknown_id"

        content = tool_result.get("content", "")
        
        # Normalize list content (Anthropic style)
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    parts.append(str(block))
                    continue
                
                b_type = block.get("type")
                if b_type == "text":
                    parts.append(block.get("text", ""))
                else:
                    # Serialize complex blocks (json, images, etc) to string for OpenAI
                    parts.append(json.dumps(block, ensure_ascii=False))
            content = "\n".join(parts)
        elif not isinstance(content, str):
            # If content is a dict or other type directly
            content = json.dumps(content, ensure_ascii=False)
            
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": str(content)
        }
