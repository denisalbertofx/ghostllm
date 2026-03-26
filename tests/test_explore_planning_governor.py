"""EXPLORE planning governor: small-scope detection, planning text, redundant ls paths."""
from __future__ import annotations

import json
import unittest

from apps.cli.runtime.explore_planning_governor import (
    assistant_text_suggests_protracted_planning,
    ls_target_already_listed_successfully,
    normalize_ls_path_key,
    session_is_small_explore_scope,
)


class _Sess:
    def __init__(self, **kw: object) -> None:
        self.micro_task_kind = kw.get("micro_task_kind")
        self.iteration_budget = kw.get("iteration_budget") or {}


class TestExplorePlanningGovernor(unittest.TestCase):
    def test_session_small_micro(self) -> None:
        self.assertTrue(session_is_small_explore_scope(_Sess(micro_task_kind="shallow_list")))

    def test_session_small_iter_budget(self) -> None:
        self.assertTrue(
            session_is_small_explore_scope(
                _Sess(iteration_budget={"category": "factual_code_question"})
            )
        )

    def test_session_not_small(self) -> None:
        self.assertFalse(session_is_small_explore_scope(_Sess(iteration_budget={"category": "complex_multi_file"})))
        self.assertFalse(session_is_small_explore_scope(None))

    def test_planning_text_long(self) -> None:
        self.assertTrue(assistant_text_suggests_protracted_planning("x" * 500))

    def test_planning_text_markers(self) -> None:
        self.assertTrue(assistant_text_suggests_protracted_planning("Step 1: explore\nStep 2: read"))
        self.assertTrue(assistant_text_suggests_protracted_planning("My plan is to list files first."))

    def test_planning_text_short(self) -> None:
        self.assertFalse(assistant_text_suggests_protracted_planning("OK."))

    def test_ls_redundant_same_path(self) -> None:
        task = "list"
        hist = [
            {"role": "user", "content": task},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "ls", "arguments": json.dumps({"path": "apps/cli"})},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "ls",
                "content": json.dumps({"files": ["a.py"]}),
            },
        ]
        self.assertTrue(ls_target_already_listed_successfully(hist, "apps/cli", set()))
        self.assertFalse(ls_target_already_listed_successfully([], "apps/cli", set()))

    def test_ls_dispatch_set(self) -> None:
        hist: list = []
        seen = {normalize_ls_path_key("apps/cli")}
        self.assertTrue(ls_target_already_listed_successfully(hist, "apps/cli", seen))
