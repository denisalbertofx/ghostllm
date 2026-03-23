import re
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("ghostllm.adapters.anthropic")

class AnthropicAdapter:
    """
    Parses Anthropic /v1/messages requests into high-level bridge objects.
    """

    @staticmethod
    def parse_request(data: Dict[str, Any]) -> Dict[str, Any]:
        """Extracts structured data from an Anthropic request."""
        return {
            "model": data.get("model"),
            "system": AnthropicAdapter._extract_system(data.get("system")),
            "messages": data.get("messages", []),
            "tools": data.get("tools", []),
            "stream": data.get("stream", False),
            "max_tokens": data.get("max_tokens", 4096),
            "temperature": data.get("temperature", 1.0),
            "top_p": data.get("top_p"),
            "stop": data.get("stop_sequences")
        }

    @staticmethod
    def _extract_system(system: Any) -> str:
        """Normalizes Anthropic system prompt (string or list)."""
        if not system:
            return ""
        if isinstance(system, str):
            return system
        if isinstance(system, list):
            text_parts = [c.get("text", "") for c in system if c.get("type") == "text"]
            return "\n".join(text_parts)
        return str(system)

    @staticmethod
    def detect_intent(data: Dict[str, Any]) -> str:
        """Proxies to the centralized TaskScheduler."""
        from .scheduler import TaskScheduler
        return TaskScheduler.detect_intent(data, is_openai=False)

    @staticmethod
    def clean_xml_tools(text: str) -> str:
        """Strips Claude-specific XML tool instructions."""
        if not text:
            return ""
        # Strip <tools>...</tools> and the preamble
        text = re.sub(r"<tools>.*?</tools>", "", text, flags=re.DOTALL)
        text = re.sub(r"In this environment, you have access to a set of tools.*?</tool_code>", "", text, flags=re.DOTALL)
        return text.strip()
