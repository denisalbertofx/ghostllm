"""Tests for parallel planner + retrieval index preamble."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock

from apps.cli.runtime import pipeline_preamble as pp


class ParallelPreambleTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "GHOST_PARALLEL_PIPELINE",
            "GHOST_USE_RETRIEVAL",
        ):
            os.environ.pop(k, None)

    def test_parallel_pipeline_enabled_values(self) -> None:
        os.environ["GHOST_PARALLEL_PIPELINE"] = "partial"
        self.assertTrue(pp.parallel_pipeline_enabled())
        os.environ["GHOST_PARALLEL_PIPELINE"] = "off"
        self.assertFalse(pp.parallel_pipeline_enabled())

    def test_parallel_preamble_eligible_requires_contract_spec_and_retrieval(self) -> None:
        os.environ["GHOST_PARALLEL_PIPELINE"] = "on"
        os.environ["GHOST_USE_RETRIEVAL"] = "1"
        s = MagicMock()
        s.task_contract = {"spec": {"intent": "x"}}
        self.assertTrue(pp.parallel_preamble_eligible(s))

        s.task_contract = {"spec": {}}
        self.assertFalse(pp.parallel_preamble_eligible(s))
