"""Unit tests for structured Repair Specialist."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from apps.cli.runtime.nim_provider import ProviderResponse, ProviderUsage
from apps.cli.runtime.repair_specialist import (
    REPAIR_OUTCOME_APPLIED_PATCH,
    REPAIR_OUTCOME_INVALID_PATCH,
    REPAIR_OUTCOME_NO_EFFECTIVE_PATCH,
    REPAIR_OUTCOME_OUT_OF_ALLOWLIST,
    REPAIR_OUTCOME_PROVIDER_FAILURE,
    RepairSpecialistResult,
    build_repair_allowlist,
    build_repair_specialist_user_message,
    classify_structured_repair_outcome,
    filter_edits_to_allowlist,
    parse_repair_edits_json,
    repair_context_char_budget,
    repair_specialist_enabled,
    run_repair_specialist_llm,
)


class TestRepairSpecialist(unittest.TestCase):
    def test_repair_context_char_budget_fraction(self):
        with mock.patch.dict(os.environ, {"GHOST_REPAIR_CONTEXT_FRACTION": "0.25", "GHOST_REPAIR_BASELINE_CHARS": "40000"}):
            b = repair_context_char_budget(last_main_turn_prompt_chars=40_000)
            self.assertLessEqual(b, 14_000)
            self.assertGreaterEqual(b, 4096)

    def test_build_allowlist_union(self):
        failed = [
            {"name": "TypeCheck", "status": "failed", "stderr": "src/foo.ts(1,1): error TS2322"},
        ]
        diff = [{"file": "lib/bar.ts", "type": "Edit File"}]
        allow = build_repair_allowlist(failed, diff)
        self.assertIn("lib/bar.ts", allow)
        self.assertTrue(any("foo.ts" in p for p in allow))

    def test_parse_repair_edits_json(self):
        payload = {
            "root_cause": "missing semicolon",
            "edits": [{"path": "a.ts", "old_str": "x", "new_str": "y"}],
        }
        root, edits, ok = parse_repair_edits_json(json.dumps(payload))
        self.assertTrue(ok)
        self.assertEqual(root, "missing semicolon")
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["path"], "a.ts")

    def test_filter_edits_to_allowlist(self):
        allow = ["app/x.ts"]
        edits = [{"path": "other.ts", "old_str": "a", "new_str": "b"}, {"path": "app/x.ts", "old_str": "a", "new_str": "b"}]
        kept, dropped = filter_edits_to_allowlist(edits, allow)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["path"], "app/x.ts")
        self.assertEqual(dropped, ["other.ts"])

    def test_user_message_stays_under_max_chars(self):
        failed = [{"name": "Lint", "status": "failed", "stderr": "x" * 50_000}]
        diff = [{"file": "z.ts", "type": "Edit"}]
        text, meta = build_repair_specialist_user_message(
            failed_checks=failed,
            diff_summary=diff,
            previous_patch_text="",
            max_chars=2000,
        )
        self.assertLessEqual(len(text), 2000)
        self.assertEqual(meta["budget"], 2000)

    def test_repair_specialist_enabled_env(self):
        with mock.patch.dict(os.environ, {"GHOST_USE_REPAIR_SPECIALIST": "0"}):
            self.assertFalse(repair_specialist_enabled())
        with mock.patch.dict(os.environ, {"GHOST_USE_REPAIR_SPECIALIST": "1"}):
            self.assertTrue(repair_specialist_enabled())

    def test_run_repair_specialist_llm_mock_provider(self):
        ok_resp = ProviderResponse(
            ok=True,
            raw_text=json.dumps(
                {"root_cause": "fix", "edits": [{"path": "a.ts", "old_str": "1", "new_str": "2"}]}
            ),
            usage=ProviderUsage(prompt_tokens=10, completion_tokens=32, total_tokens=42),
        )

        with mock.patch.dict(os.environ, {"GHOST_USE_NVIDIA_NIM": "0"}):
            with mock.patch("apps.cli.runtime.repair_specialist.nim_chat_complete", return_value=ok_resp):
                r = run_repair_specialist_llm(
                    failed_checks=[{"name": "X", "status": "failed", "stderr": "./a.ts:1 error"}],
                    diff_summary=[{"file": "a.ts", "type": "Edit"}],
                    previous_patch_text="",
                    last_main_turn_prompt_chars=20_000,
                    ghost_server_url="http://127.0.0.1:9",
                    ghost_api_key="k",
                )
        self.assertTrue(r.provider_ok)
        self.assertTrue(r.parse_ok)
        self.assertTrue(r.ok)
        self.assertEqual(len(r.edits_to_apply), 1)
        self.assertEqual(classify_structured_repair_outcome(r, 1), REPAIR_OUTCOME_APPLIED_PATCH)

    def test_run_repair_specialist_provider_failure(self):
        bad = ProviderResponse(
            ok=False,
            raw_text="",
            error_message="rate_limited",
        )
        with mock.patch.dict(os.environ, {"GHOST_USE_NVIDIA_NIM": "0"}):
            with mock.patch("apps.cli.runtime.repair_specialist.nim_chat_complete", return_value=bad):
                r = run_repair_specialist_llm(
                    failed_checks=[{"name": "X", "status": "failed", "stderr": "./a.ts:1"}],
                    diff_summary=[{"file": "a.ts", "type": "Edit"}],
                    previous_patch_text="",
                    last_main_turn_prompt_chars=20_000,
                    ghost_server_url="http://127.0.0.1:9",
                    ghost_api_key="k",
                )
        self.assertFalse(r.provider_ok)
        self.assertEqual(classify_structured_repair_outcome(r, 0), REPAIR_OUTCOME_PROVIDER_FAILURE)

    def test_run_repair_specialist_invalid_json(self):
        ok_resp = ProviderResponse(
            ok=True,
            raw_text="NOT JSON {{{",
            usage=ProviderUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )
        with mock.patch.dict(os.environ, {"GHOST_USE_NVIDIA_NIM": "0"}):
            with mock.patch("apps.cli.runtime.repair_specialist.nim_chat_complete", return_value=ok_resp):
                r = run_repair_specialist_llm(
                    failed_checks=[{"name": "X", "status": "failed", "stderr": "./a.ts:1"}],
                    diff_summary=[{"file": "a.ts", "type": "Edit"}],
                    previous_patch_text="",
                    last_main_turn_prompt_chars=20_000,
                    ghost_server_url="http://127.0.0.1:9",
                    ghost_api_key="k",
                )
        self.assertFalse(r.parse_ok)
        self.assertEqual(classify_structured_repair_outcome(r, 0), REPAIR_OUTCOME_INVALID_PATCH)

    def test_run_repair_specialist_all_proposals_outside_allowlist(self):
        ok_resp = ProviderResponse(
            ok=True,
            raw_text=json.dumps(
                {
                    "root_cause": "fix",
                    "edits": [{"path": "nowhere/b.ts", "old_str": "1", "new_str": "2"}],
                }
            ),
            usage=ProviderUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        )
        with mock.patch.dict(os.environ, {"GHOST_USE_NVIDIA_NIM": "0"}):
            with mock.patch("apps.cli.runtime.repair_specialist.nim_chat_complete", return_value=ok_resp):
                r = run_repair_specialist_llm(
                    failed_checks=[{"name": "X", "status": "failed", "stderr": "error"}],
                    diff_summary=[{"file": "a.ts", "type": "Edit"}],
                    previous_patch_text="",
                    last_main_turn_prompt_chars=20_000,
                    ghost_server_url="http://127.0.0.1:9",
                    ghost_api_key="k",
                )
        self.assertTrue(r.all_proposals_outside_allowlist)
        self.assertEqual(classify_structured_repair_outcome(r, 0), REPAIR_OUTCOME_OUT_OF_ALLOWLIST)

    def test_run_repair_specialist_zero_edits_json_not_ok_to_apply(self):
        ok_resp = ProviderResponse(
            ok=True,
            raw_text=json.dumps({"root_cause": "nothing to do", "edits": []}),
            usage=ProviderUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        )
        with mock.patch.dict(os.environ, {"GHOST_USE_NVIDIA_NIM": "0"}):
            with mock.patch("apps.cli.runtime.repair_specialist.nim_chat_complete", return_value=ok_resp):
                r = run_repair_specialist_llm(
                    failed_checks=[{"name": "X", "status": "failed", "stderr": "./a.ts:1 error"}],
                    diff_summary=[{"file": "a.ts", "type": "Edit"}],
                    previous_patch_text="",
                    last_main_turn_prompt_chars=20_000,
                    ghost_server_url="http://127.0.0.1:9",
                    ghost_api_key="k",
                )
        self.assertFalse(r.ok)
        self.assertEqual(classify_structured_repair_outcome(r, 0), REPAIR_OUTCOME_NO_EFFECTIVE_PATCH)

    def test_classify_provider_failure(self):
        r = RepairSpecialistResult(ok=False, provider_ok=False, parse_ok=False)
        self.assertEqual(classify_structured_repair_outcome(r, 0), REPAIR_OUTCOME_PROVIDER_FAILURE)

    def test_classify_invalid_patch(self):
        r = RepairSpecialistResult(
            ok=False,
            provider_ok=True,
            parse_ok=False,
            edits_to_apply=[],
        )
        self.assertEqual(classify_structured_repair_outcome(r, 0), REPAIR_OUTCOME_INVALID_PATCH)

    def test_classify_out_of_allowlist(self):
        r = RepairSpecialistResult(
            ok=False,
            provider_ok=True,
            parse_ok=True,
            all_proposals_outside_allowlist=True,
            edits_to_apply=[],
        )
        self.assertEqual(classify_structured_repair_outcome(r, 0), REPAIR_OUTCOME_OUT_OF_ALLOWLIST)

    def test_classify_no_effective_after_failed_apply(self):
        r = RepairSpecialistResult(
            ok=True,
            provider_ok=True,
            parse_ok=True,
            edits_to_apply=[{"path": "a.ts", "old_str": "x", "new_str": "y"}],
        )
        self.assertEqual(classify_structured_repair_outcome(r, 0), REPAIR_OUTCOME_NO_EFFECTIVE_PATCH)

    def test_classify_applied_patch(self):
        r = RepairSpecialistResult(
            ok=True,
            provider_ok=True,
            parse_ok=True,
            edits_to_apply=[{"path": "a.ts", "old_str": "x", "new_str": "y"}],
        )
        self.assertEqual(classify_structured_repair_outcome(r, 1), REPAIR_OUTCOME_APPLIED_PATCH)


if __name__ == "__main__":
    unittest.main()
