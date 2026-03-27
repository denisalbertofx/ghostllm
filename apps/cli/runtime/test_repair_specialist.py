"""Unit tests for structured Repair Specialist."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from apps.cli.runtime.nim_provider import ProviderResponse, ProviderUsage
from apps.cli.runtime.repair_specialist import (
    _check_blob,
    classify_error_signature_delta,
    classify_failure_set_delta,
    classify_failure_set_delta_meta,
    compute_causal_error_signature,
    snapshot_failed_verification,
    REPAIR_OUTCOME_APPLIED_PATCH,
    REPAIR_OUTCOME_INVALID_PATCH,
    REPAIR_OUTCOME_NO_EFFECTIVE_PATCH,
    REPAIR_OUTCOME_OUT_OF_ALLOWLIST,
    REPAIR_OUTCOME_PARSE_BLOCKED,
    REPAIR_OUTCOME_PROVIDER_FAILURE,
    RepairSpecialistResult,
    build_repair_allowlist,
    build_repair_specialist_user_message,
    classify_structured_repair_outcome,
    filter_edits_to_allowlist,
    parse_repair_edits_json,
    repair_context_char_budget,
    repair_applied_patch_deserves_operational_budget,
    repair_specialist_enabled,
    run_repair_specialist_llm,
    summarize_primary_failure,
)


class TestCheckBlobCap(unittest.TestCase):
    def test_huge_stderr_truncated(self) -> None:
        c = {"stderr": "e" * 400_000, "stdout": ""}
        with mock.patch.dict(os.environ, {"GHOST_RUNTIME_CHECK_BLOB_CAP": "60000"}, clear=False):
            b = _check_blob(c)
        self.assertLess(len(b), 65_000)
        self.assertIn("ghost_blob_truncated", b)


class TestFailureSetDelta(unittest.TestCase):
    def test_first_failure(self):
        failed = [{"name": "Lint", "status": "failed", "stderr": "x"}]
        self.assertEqual(classify_failure_set_delta(None, failed), "first_failure")

    def test_same_unchanged(self):
        failed = [{"name": "Lint", "status": "failed", "stderr": "same"}]
        prev = snapshot_failed_verification(failed)
        self.assertEqual(classify_failure_set_delta(prev, failed), "same_failure_unchanged")

    def test_reduced(self):
        prev = snapshot_failed_verification(
            [
                {
                    "name": "Lint",
                    "kind": "lint",
                    "status": "failed",
                    "stderr": 'File "pkg/a.py", line 1\nError: a',
                },
                {
                    "name": "Build",
                    "kind": "build",
                    "status": "failed",
                    "stderr": 'File "pkg/b.py", line 1\nError: b',
                },
            ]
        )
        cur = [
            {
                "name": "Lint",
                "kind": "lint",
                "status": "failed",
                "stderr": 'File "pkg/a.py", line 1\nError: still bad',
            },
        ]
        self.assertEqual(classify_failure_set_delta(prev, cur), "failure_reduced")

    def test_expanded(self):
        prev = snapshot_failed_verification([{"name": "Lint", "status": "failed", "stderr": "a"}])
        cur = [
            {"name": "Lint", "status": "failed", "stderr": "a"},
            {"name": "Build", "status": "failed", "stderr": "b"},
        ]
        self.assertEqual(classify_failure_set_delta(prev, cur), "failure_expanded")

    def test_renamed_check_same_location_same_failure(self):
        blob = 'File "pkg/mod.py", line 10\nValueError: nope'
        prev = snapshot_failed_verification(
            [{"name": "Python Tests (shard A)", "kind": "tests", "status": "failed", "stderr": blob}]
        )
        cur = [
            {"name": "pytest unit", "kind": "tests", "status": "failed", "stderr": blob + "\n-- note --"},
        ]
        self.assertEqual(classify_failure_set_delta(prev, cur), "same_failure_unchanged")

    def test_overlapping_not_subset_is_signature_changed(self):
        prev = snapshot_failed_verification(
            [
                {"name": "t1", "kind": "tests", "status": "failed", "stderr": 'File "a.py", line 1\nError: a'},
                {"name": "t2", "kind": "tests", "status": "failed", "stderr": 'File "b.py", line 1\nError: b'},
            ]
        )
        cur = [
            {"name": "t1", "kind": "tests", "status": "failed", "stderr": 'File "a.py", line 1\nError: a'},
            {"name": "t3", "kind": "tests", "status": "failed", "stderr": 'File "c.py", line 1\nError: c'},
        ]
        self.assertEqual(classify_failure_set_delta(prev, cur), "failure_signature_changed")

    def test_legacy_snapshot_without_structural_tokens(self):
        failed = [{"name": "Lint", "status": "failed", "stderr": "same"}]
        snap = snapshot_failed_verification(failed)
        prev_legacy = {"names": snap["names"], "count": snap["count"], "digest": snap["digest"]}
        self.assertEqual(classify_failure_set_delta(prev_legacy, failed), "same_failure_unchanged")

    def test_explicit_cur_matches_internal_snapshot(self):
        failed = [{"name": "Lint", "status": "failed", "stderr": "a"}]
        prev = snapshot_failed_verification(failed)
        cur_snap = snapshot_failed_verification(failed)
        self.assertEqual(
            classify_failure_set_delta(prev, failed),
            classify_failure_set_delta(prev, failed, cur=cur_snap),
        )

    def test_pytest_banner_noise_same_failure(self):
        core = "FAILED tests/unit/test_x.py::test_foo\nAssertionError: 1 != 2\n"
        a = "============================= test session starts ==============================\n" + core
        b = "----------------------------- Captured stdout call -----------------------------\n" + core
        prev = snapshot_failed_verification(
            [{"name": "Python Tests", "kind": "tests", "status": "failed", "stderr": a}]
        )
        cur = [{"name": "Python Tests", "kind": "tests", "status": "failed", "stderr": b}]
        self.assertEqual(classify_failure_set_delta(prev, cur), "same_failure_unchanged")

    def test_all_low_confidence_prev_blocks_subset_grant(self):
        prev = snapshot_failed_verification(
            [
                {"name": "Z1", "status": "failed", "stderr": "opaque blob one"},
                {"name": "Z2", "status": "failed", "stderr": "opaque blob two"},
            ]
        )
        cur = [{"name": "Z1", "status": "failed", "stderr": "opaque blob one"}]
        label, meta = classify_failure_set_delta_meta(prev, cur)
        self.assertEqual(label, "failure_signature_changed")
        self.assertEqual(meta.get("confidence_gate"), "prev_all_low_confidence")

    def test_repair_operational_budget_requires_parse_drift(self):
        res = RepairSpecialistResult(
            ok=True,
            parse_ok=True,
            provider_ok=True,
            edits_to_apply=[{"path": "pkg/x.py", "old_str": "a", "new_str": "b"}],
        )
        res.parse_validation_ok = True
        res.same_parse_signature_after_patch = True
        res.pre_parse_fingerprints = {"pkg/x.py": "sig_a"}
        res.post_parse_fingerprints = {"pkg/x.py": "sig_a"}
        ok, why = repair_applied_patch_deserves_operational_budget(
            structural_python_failure=True,
            applied=1,
            res=res,
        )
        self.assertFalse(ok)
        self.assertIn("divergence", why)
        res.post_parse_fingerprints = {"pkg/x.py": "sig_b"}
        ok2, why2 = repair_applied_patch_deserves_operational_budget(
            structural_python_failure=True,
            applied=1,
            res=res,
        )
        self.assertTrue(ok2)
        self.assertIn("fingerprint", why2)

    def test_repair_budget_denied_without_structural_python_context(self):
        res = RepairSpecialistResult(
            ok=True,
            parse_ok=True,
            provider_ok=True,
            edits_to_apply=[{"path": "a.ts", "old_str": "x", "new_str": "y"}],
        )
        res.parse_validation_ok = True
        ok, why = repair_applied_patch_deserves_operational_budget(
            structural_python_failure=False,
            applied=1,
            res=res,
        )
        self.assertFalse(ok)
        self.assertIn("structural_python", why)


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

    def test_primary_failure_summary_prefers_error_with_location(self):
        failed = [
            {
                "name": "Python Tests",
                "status": "failed",
                "stderr": 'File "apps/server/database.py", line 38\nIndentationError: expected an indented block',
            }
        ]
        summary = summarize_primary_failure(failed)
        self.assertIn("IndentationError", summary)
        self.assertIn("apps/server/database.py:38", summary)

    def test_user_message_includes_exact_active_failure_section(self):
        failed = [
            {
                "name": "Python Tests",
                "status": "failed",
                "stderr": 'File "apps/server/database.py", line 38\nIndentationError: expected an indented block',
            }
        ]
        text, meta = build_repair_specialist_user_message(
            failed_checks=failed,
            diff_summary=[{"file": "apps/server/database.py", "type": "Edit"}],
            previous_patch_text="",
            max_chars=2500,
        )
        self.assertIn("## Exact active failure to fix first", text)
        self.assertIn("IndentationError", text)
        self.assertIn("apps/server/database.py:38", meta["primary_failure"])

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

    def test_classify_parse_blocked_after_applied_edits(self):
        r = RepairSpecialistResult(
            ok=True,
            provider_ok=True,
            parse_ok=True,
            edits_to_apply=[{"path": "x.py", "old_str": "a", "new_str": "b"}],
            parse_validation_ok=False,
            parse_validation_reason="ast_parse_failed:x.py",
        )
        self.assertEqual(classify_structured_repair_outcome(r, 1), REPAIR_OUTCOME_PARSE_BLOCKED)

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


class TestCausalErrorSignature(unittest.TestCase):
    def test_compute_and_classify_delta(self) -> None:
        failed = [
            {"name": "Tests", "status": "failed", "stderr": "SyntaxError: invalid syntax\n"},
        ]
        t1, d1 = compute_causal_error_signature(failed)
        self.assertIn("SyntaxError", t1)
        self.assertEqual(len(d1), 32)
        self.assertEqual(classify_error_signature_delta("", d1), "first")
        self.assertEqual(classify_error_signature_delta(d1, d1), "same_error")
        failed2 = [
            {"name": "Tests", "status": "failed", "stderr": "IndentationError: unexpected indent\n"},
        ]
        _, d2 = compute_causal_error_signature(failed2)
        self.assertEqual(classify_error_signature_delta(d1, d2), "new_error")


if __name__ == "__main__":
    unittest.main()
