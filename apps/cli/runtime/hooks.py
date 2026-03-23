from typing import Callable, Dict, List, Any
import time
import logging

class HookEvents:
    TASK_START = "TaskStart"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PRE_VERIFICATION = "PreVerification"
    POST_VERIFICATION = "PostVerification"
    TASK_COMPLETE = "TaskComplete"
    TASK_FAILED = "TaskFailed"

class HookManager:
    """
    Lightweight internal hook system for Ghost Core.
    Allows registering callbacks for runtime lifecycle events.
    """
    def __init__(self):
        self._hooks: Dict[str, List[Callable]] = {}
        self.logger = logging.getLogger("ghost.hooks")

    def register(self, event: str, callback: Callable):
        """Registers a callback for a specific event."""
        if event not in self._hooks:
            self._hooks[event] = []
        self._hooks[event].append(callback)

    def trigger(self, event: str, **kwargs):
        """
        Triggers all registered hooks for an event.
        Exceptions in hooks are caught to prevent runtime crashes.
        """
        if event not in self._hooks:
            return

        for hook in self._hooks[event]:
            try:
                hook(**kwargs)
            except Exception as e:
                self.logger.error(f"Error in hook {hook.__name__} for event {event}: {e}")

class RuntimeEvent:
    """Represents a single event occurrence for logging/artifacts."""
    def __init__(self, name: str, data: Dict[str, Any] = None):
        self.name = name
        self.timestamp = time.time()
        self.data = data or {}

    def to_dict(self):
        return {
            "name": self.name,
            "timestamp": self.timestamp,
            "data": self.data
        }
