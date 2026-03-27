"""Tests for structural repair gates (parse / failure blob)."""
from __future__ import annotations

import unittest

from apps.cli.runtime.repair_safety import failed_checks_indicate_structural_python_failure


class TestRepairSafety(unittest.TestCase):
    def test_blob_detects_indentation_in_stderr(self) -> None:
        failed = [
            {
                "name": "Python Tests",
                "status": "failed",
                "stderr": 'File "a.py", line 2\nIndentationError: unexpected indent',
            }
        ]
        self.assertTrue(failed_checks_indicate_structural_python_failure(failed))

    def test_blob_negative_on_generic_assert(self) -> None:
        failed = [{"name": "Python Tests", "status": "failed", "stderr": "AssertionError: assert 1 == 2"}]
        self.assertFalse(failed_checks_indicate_structural_python_failure(failed))


if __name__ == "__main__":
    unittest.main()
