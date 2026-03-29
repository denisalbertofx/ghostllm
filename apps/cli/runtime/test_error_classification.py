from __future__ import annotations

import unittest

from apps.cli.runtime.error_classification import (
    ERROR_CATEGORY_CONTEXT,
    ERROR_CATEGORY_LOGIC,
    ERROR_CATEGORY_PERMISSIONS,
    ERROR_CATEGORY_TRANSIENT,
    classify_provider_failure,
    classify_tool_failure,
)


class ErrorClassificationTests(unittest.TestCase):
    def test_tool_permission_error_classified(self) -> None:
        result = classify_tool_failure("read_file", {"error": "Access denied by policy."})
        self.assertEqual(result.category, ERROR_CATEGORY_PERMISSIONS)

    def test_tool_missing_arg_is_context_error(self) -> None:
        result = classify_tool_failure("read_file", {"error": "Missing 'path' argument."})
        self.assertEqual(result.category, ERROR_CATEGORY_CONTEXT)

    def test_tool_timeout_is_transient(self) -> None:
        result = classify_tool_failure("run_shell", {"error": "timeout while executing command"})
        self.assertEqual(result.category, ERROR_CATEGORY_TRANSIENT)
        self.assertTrue(result.retryable)

    def test_regular_tool_failure_is_logic(self) -> None:
        result = classify_tool_failure("edit_file", {"error": "Target string (old_str) not found."})
        self.assertEqual(result.category, ERROR_CATEGORY_LOGIC)

    def test_provider_auth_is_permission_error(self) -> None:
        result = classify_provider_failure("GhostLLM Upstream Error: [401] Authentication failed")
        self.assertEqual(result.category, ERROR_CATEGORY_PERMISSIONS)

    def test_provider_tool_contract_is_context_error(self) -> None:
        result = classify_provider_failure("Tool use has not been enabled, because it is unsupported by qwen/qwen2.5-coder-32b-instruct.")
        self.assertEqual(result.category, ERROR_CATEGORY_CONTEXT)

    def test_provider_timeout_is_transient(self) -> None:
        result = classify_provider_failure("timeout while reading response")
        self.assertEqual(result.category, ERROR_CATEGORY_TRANSIENT)


if __name__ == "__main__":
    unittest.main()
