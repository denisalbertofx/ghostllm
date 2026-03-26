"""Tests for sliding-window history and tool output shrinking."""
from __future__ import annotations

import json
import unittest

from apps.cli.runtime.chat_prompt import (
    build_sliding_window_history,
    shrink_tool_result_for_prompt,
    trim_leading_tool_orphans,
)


class TestChatPrompt(unittest.TestCase):
    def test_trim_leading_tool_orphans(self):
        msgs = [
            {"role": "tool", "name": "x", "content": "{}"},
            {"role": "assistant", "content": "ok"},
        ]
        self.assertEqual(trim_leading_tool_orphans(msgs)[0]["role"], "assistant")

    def test_sliding_keeps_first_user_and_summary(self):
        hist = [{"role": "user", "content": "task"}]
        for i in range(15):
            hist.append({"role": "assistant", "content": f"a{i}"})
            hist.append({"role": "tool", "name": "read_file", "content": "{}"})
        out, roll = build_sliding_window_history(
            hist,
            "",
            tail_messages=6,
            rollup_max_chars=500,
            keep_first_user=True,
        )
        self.assertEqual(out[0]["role"], "user")
        self.assertTrue(any("Earlier conversation" in m.get("content", "") for m in out if m.get("role") == "user"))
        self.assertTrue(len(roll) > 0)

    def test_shrink_read_file(self):
        big = "x" * 50_000
        d = shrink_tool_result_for_prompt("read_file", {"content": big}, max_json_chars=2000)
        s = json.dumps(d, ensure_ascii=False)
        self.assertLess(len(s), 4000)
        self.assertTrue(d.get("_ghost_truncated") or "ghost_trunc" in d.get("content", ""))


if __name__ == "__main__":
    unittest.main()
