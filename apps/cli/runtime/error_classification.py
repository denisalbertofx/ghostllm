from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ERROR_CATEGORY_TRANSIENT = "transient"
ERROR_CATEGORY_LOGIC = "logic"
ERROR_CATEGORY_PERMISSIONS = "permissions"
ERROR_CATEGORY_CONTEXT = "context"


@dataclass(frozen=True)
class ErrorClassification:
    category: str
    reason: str
    retryable: bool = False
    max_retries: int = 0


_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "temporary failure",
    "connection reset",
    "connection aborted",
    "remote protocol error",
    "broken pipe",
    "429",
    "rate limit",
    "too many requests",
    "503",
    "service unavailable",
)

_PERMISSION_MARKERS = (
    "permission denied",
    "access denied",
    "denied by policy",
    "denied by user",
    "action denied",
    "forbidden",
    "not authorized",
)

_CONTEXT_MARKERS = (
    "tool '",
    "tool not found",
    "missing 'path' argument",
    "missing 'command' argument",
    "missing 'query' argument",
    "missing 'path' or 'content' arguments",
    "missing 'path' or 'old_str' or 'new_str' arguments",
    "missing required",
    "unknown tool",
    "unsupported tool",
    "invalid tool",
)


def _lower_error(value: Any) -> str:
    return str(value or "").strip().lower()


def classify_tool_failure(
    tool_name: str,
    result: Mapping[str, Any] | None,
) -> ErrorClassification:
    err = _lower_error((result or {}).get("error"))
    if not err:
        return ErrorClassification(ERROR_CATEGORY_LOGIC, "no_error")

    if any(marker in err for marker in _PERMISSION_MARKERS):
        return ErrorClassification(ERROR_CATEGORY_PERMISSIONS, "permission_denied")

    if any(marker in err for marker in _CONTEXT_MARKERS):
        return ErrorClassification(ERROR_CATEGORY_CONTEXT, "invalid_tool_request")

    if any(marker in err for marker in _TRANSIENT_MARKERS):
        retries = 2 if tool_name == "run_shell" else 1
        return ErrorClassification(
            ERROR_CATEGORY_TRANSIENT,
            "transient_tool_failure",
            retryable=True,
            max_retries=retries,
        )

    return ErrorClassification(ERROR_CATEGORY_LOGIC, "tool_logic_error")


def classify_provider_failure(error: BaseException | str) -> ErrorClassification:
    err = _lower_error(error)
    if not err:
        return ErrorClassification(ERROR_CATEGORY_LOGIC, "provider_error")

    if "401" in err or "authentication failed" in err or "api key" in err:
        return ErrorClassification(ERROR_CATEGORY_PERMISSIONS, "provider_auth_failed")

    if "tool use has not been enabled" in err or "unsupported by" in err:
        return ErrorClassification(ERROR_CATEGORY_CONTEXT, "provider_tool_contract_mismatch")

    if any(marker in err for marker in _TRANSIENT_MARKERS):
        return ErrorClassification(
            ERROR_CATEGORY_TRANSIENT,
            "provider_transient_failure",
            retryable=True,
            max_retries=1,
        )

    return ErrorClassification(ERROR_CATEGORY_LOGIC, "provider_logic_error")
