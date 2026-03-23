import re
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

@dataclass
class Intent:
    mode: str
    task: str
    task_type: str = "ask" # ask, architect, code, debug, review, fix, research
    is_slash_command: bool = False
    requires_tools: bool = False
    requires_shell: bool = False
    requires_file_scope: bool = False
    original_text: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

class IntentRouter:
    """
    Classifies user input and routes to the correct execution mode.
    GEP-1: Intent Router implementation.
    """
    
    MODES = {
        "/plan": "Plan",
        "/do": "Execute",
        "/edit": "Patch",
        "/fix": "Fix",
        "/review": "Review",
        "/chat": "Chat",
        "/mode": "ModeChange",
        "/debug": "DebugToggle",
        "/clear": "ClearHistory",
        "/help": "Help"
    }

    def __init__(self):
        pass

    def route(self, text: str) -> Intent:
        text = text.strip()
        if not text:
            return Intent(mode="Chat", task="", original_text=text)

        # 1. Handle Slash Commands
        if text.startswith("/"):
            parts = text.split(" ", 1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            
            if cmd in self.MODES:
                mode = self.MODES[cmd]
                intent = Intent(
                    mode=mode,
                    task=args,
                    is_slash_command=True,
                    original_text=text
                )
                self._enrich_intent(intent)
                return intent
            else:
                return Intent(mode="Invalid", task=cmd, is_slash_command=True, original_text=text)

        # 2. Natural Language Intent Detection
        text_lower = text.lower()
        
        # Default intent
        intent = Intent(mode="Chat", task=text, original_text=text)
        
        # Classification heuristics
        if any(w in text_lower for w in ["arregla", "fix", "corrige", "repara"]):
            intent.task_type = "fix"
            intent.requires_tools = True
        elif any(w in text_lower for w in ["error", "fallo", "falla", "no funciona", "broken", "bug", "crash"]):
            intent.task_type = "debug"
            intent.requires_tools = True
            intent.requires_file_scope = True
        elif any(w in text_lower for w in ["diseña", "arquitectura", "pantalla", "interfaz", "ui", "ux", "como construyo", "crea un diseño"]):
            intent.task_type = "architect"
        elif any(w in text_lower for w in ["escribe", "codifica", "implementa", "programa", "write a", "create a function"]):
            intent.task_type = "code"
            intent.requires_tools = True
        elif any(w in text_lower for w in ["revisa", "review", "chequea", "mira si"]):
            intent.task_type = "review"
            intent.requires_tools = True
            intent.requires_file_scope = True
        elif any(w in text_lower for w in ["busca", "encuentra", "donde esta", "investiga", "research"]):
            intent.task_type = "research"
            intent.requires_tools = True
        
        # Detection of 'bypass' intents (Local Extraction Triggers)
        analysis_triggers = ["total de líneas", "conteo", "qué dice la línea", "última línea", "primeras n líneas"]
        if any(t in text_lower for t in analysis_triggers):
            intent.mode = "Analyze"
            intent.task_type = "research"
            intent.requires_tools = False # Done locally

        self._enrich_intent(intent)
        return intent

    def _enrich_intent(self, intent: Intent):
        """Adds secondary flags based on task content."""
        task_lower = intent.task.lower()
        
        # Shell requirements
        shell_keywords = ["ejecuta", "run", "npm", "git", "bash", "shell", "build", "test"]
        if any(w in task_lower for w in shell_keywords):
            intent.requires_shell = True
            intent.requires_tools = True

        # File scope requirements
        file_keywords = ["archivo", "file", "en ", ".py", ".ts", ".js", ".tsx", ".json", "clase", "funcion"]
        if any(w in task_lower for w in file_keywords):
            intent.requires_file_scope = True
            intent.requires_tools = True

        # If it's a fix/debug/code/review, it almost certainly needs tools
        if intent.task_type in ["code", "debug", "fix", "review", "research"]:
            intent.requires_tools = True
