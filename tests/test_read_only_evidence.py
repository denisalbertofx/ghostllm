"""Narrow read-only EXPLORE early exit heuristics (Plan / should_not_write)."""
from __future__ import annotations

import json
import unittest

from apps.cli.runtime.read_only_evidence import (
    detect_narrow_shallow_dir_listing,
    evidence_sufficient_for_answer,
    last_ls_invocation_from_history,
    paths_align_for_listing,
    readonly_explore_early_exit_enabled,
)


class TestReadOnlyEvidence(unittest.TestCase):
    def test_ro3_like_prompt_matches_scope(self) -> None:
        t = (
            "/plan Lista los .py directamente dentro de apps/cli/runtime/adapters "
            "(solo primer nivel, sin subcarpetas). Devuelve solo la lista de nombres de archivo, una por línea."
        )
        s = detect_narrow_shallow_dir_listing(t)
        self.assertIsNotNone(s)
        assert s is not None
        self.assertIn("adapters", s.path_hint.replace("\\", "/"))
        self.assertEqual(s.extension, ".py")

    def test_ro1_style_rejected(self) -> None:
        t = (
            "/plan En menos de 12 líneas, lista los archivos bajo apps/cli/runtime "
            'que contengan "verification" en el nombre'
        )
        self.assertIsNone(detect_narrow_shallow_dir_listing(t))

    def test_last_ls_from_history(self) -> None:
        hist = [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_ls_1",
                        "type": "function",
                        "function": {"name": "ls", "arguments": json.dumps({"path": "apps/cli/runtime/adapters"})},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_ls_1",
                "name": "ls",
                "content": json.dumps({"files": ["a.py", "b.py"]}),
            },
        ]
        inv = last_ls_invocation_from_history(hist)
        self.assertIsNotNone(inv)
        assert inv is not None
        path, files = inv
        self.assertTrue(path.endswith("adapters") or "adapters" in path)
        self.assertEqual(files, ["a.py", "b.py"])

    def test_evidence_sufficient_ro3(self) -> None:
        task = (
            "Lista los .py directamente dentro de apps/cli/runtime/adapters "
            "(solo primer nivel). Una por línea."
        )
        hist = [
            {"role": "user", "content": task},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "ls", "arguments": json.dumps({"path": "apps/cli/runtime/adapters"})},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "ls",
                "content": json.dumps({"files": ["x.py", "y.py"]}),
            },
        ]
        ok, why = evidence_sufficient_for_answer(
            None,
            hist,
            None,
            user_task_text=task,
            discovery_action_count=1,
            explore_act_blocked=True,
        )
        self.assertTrue(ok, why)

    def test_paths_align(self) -> None:
        self.assertTrue(paths_align_for_listing("apps/cli/runtime/adapters", "apps/cli/runtime/adapters"))
        self.assertTrue(paths_align_for_listing("cli/runtime/adapters", "apps/cli/runtime/adapters"))

    def test_readonly_early_exit_env(self) -> None:
        self.assertTrue(readonly_explore_early_exit_enabled())


if __name__ == "__main__":
    unittest.main(verbosity=2)
