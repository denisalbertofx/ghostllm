import logging
from typing import Dict, Any, List

logger = logging.getLogger("ghostllm.adapters.scheduler")

class TaskScheduler:
    """
    Intelligently detects task intent to choose the best Kimi profile.
    Supports both Anthropic and OpenAI request formats.
    """
    
    @staticmethod
    def detect_intent(data: Dict[str, Any], is_openai: bool = False) -> str:
        if is_openai:
            return TaskScheduler._detect_openai(data)
        else:
            return TaskScheduler._detect_anthropic(data)

    @staticmethod
    def _detect_anthropic(data: Dict[str, Any]) -> str:
        tools = data.get("tools", [])
        system = str(data.get("system", "")).lower()
        messages = data.get("messages", [])
        return TaskScheduler._analyze(tools, system, messages)

    @staticmethod
    def _detect_openai(data: Dict[str, Any]) -> str:
        tools = data.get("tools", [])
        messages = data.get("messages", [])
        # Extract system prompt from messages
        system = ""
        for m in messages:
            if m.get("role") == "system":
                system = str(m.get("content", "")).lower()
                break
        return TaskScheduler._analyze(tools, system, messages)

    @staticmethod
    def _analyze(tools: List[Any], system: str, messages: List[Any]) -> str:
        # 1. Check for Fast Tools (ReadOnly / Explorer)
        fast_tool_names = {"glob", "grep", "ls", "read_file", "cat", "view_file", "list_dir"}
        if tools:
            # If it's tools-only or all tools are fast
            tool_names = []
            for t in tools:
                name = t.get("name") if "name" in t else t.get("function", {}).get("name")
                if name: tool_names.append(name)
            
            if tool_names and all(name in fast_tool_names for name in tool_names):
                return "fast-tools"

        # 2. Check for Planning (Reasoning)
        planning_keywords = {"plan", "architecture", "design", "refactor", "complex", "strategy", "analyze", "pensar", "analiza"}
        if any(word in system for word in planning_keywords):
            return "planner"
            
        # 3. Check for multi-turn history suggesting planning
        if len(messages) > 12:
            return "planner"

        # Default: Standard coding
        return "coder-default"
