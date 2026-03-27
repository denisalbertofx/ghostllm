"""Regression tests for non-Python REPAIR operational budget policy."""
from __future__ import annotations

import unittest
from unittest import mock

from apps.cli.runtime.autonomy import BudgetManager
from apps.cli.runtime.artifacts import append_compacted_session_event, build_operator_runtime_digest
from apps.cli.runtime.repair_budget_nonpy import (
    _probe_tsc_output,
    evaluate_non_python_repair_budget,
    parse_npm_build_signatures,
    parse_ruff_error_signatures,
    parse_typescript_error_signatures,
    resolve_repair_operational_budget,
)
from apps.cli.runtime.repair_specialist import RepairSpecialistResult


class TestParseTypescriptSignatures(unittest.TestCase):
    def test_cosmetic_wrapper_same_set(self):
        core = "src/a.ts(1,1): error TS2322: Type 'x' is not assignable to 'y'.\n"
        a = "\x1b[31m" + core + "\x1b[0m"
        b = "======= banner =======\n" + core + "\n trailing noise"
        self.assertEqual(parse_typescript_error_signatures(a), parse_typescript_error_signatures(b))


class TestNonPyRepairBudget(unittest.TestCase):
    def test_ts_strict_subset_high_confidence_grant(self):
        pre = frozenset(
            {
                "src/a.ts|TS2322|1",
                "src/b.ts|TS100|2",
            }
        )
        post_txt = "src/b.ts(2,3): error TS100: still bad\n"
        ok, detail, conf, skip = evaluate_non_python_repair_budget(
            domain="typecheck",
            pre_set=pre,
            post_text=post_txt,
            edited_norm=["src/a.ts"],
            parse_post=parse_typescript_error_signatures,
        )
        self.assertTrue(ok)
        self.assertIn("subset", detail)
        self.assertEqual(conf, "high")
        self.assertEqual(skip, "")

    def test_ts_same_parsed_set_no_grant(self):
        line = "src/a.ts(1,1): error TS2322: msg one\n"
        pre = parse_typescript_error_signatures(line)
        post = line.replace("msg one", "msg two totally different words")
        ok, _, conf, skip = evaluate_non_python_repair_budget(
            domain="typecheck",
            pre_set=pre,
            post_text=post,
            edited_norm=["src/a.ts"],
            parse_post=parse_typescript_error_signatures,
        )
        self.assertFalse(ok)
        self.assertEqual(skip, "post_equals_pre_no_reduction")
        self.assertEqual(conf, "none")

    def test_ts_not_strict_subset_ambiguous(self):
        pre = frozenset({"src/a.ts|TS2322|1"})
        post_txt = (
            "src/a.ts(1,1): error TS2322: x\n"
            "src/c.ts(9,9): error TS9999: new\n"
        )
        ok, _, _, skip = evaluate_non_python_repair_budget(
            domain="typecheck",
            pre_set=pre,
            post_text=post_txt,
            edited_norm=["src/a.ts"],
            parse_post=parse_typescript_error_signatures,
        )
        self.assertFalse(ok)
        self.assertEqual(skip, "not_strict_subset_ambiguous")

    def test_removed_errors_must_touch_edited_files(self):
        pre = frozenset({"src/other.ts|TS2322|5"})
        post_txt = ""
        ok, _, _, skip = evaluate_non_python_repair_budget(
            domain="typecheck",
            pre_set=pre,
            post_text=post_txt,
            edited_norm=["src/a.ts"],
            parse_post=parse_typescript_error_signatures,
        )
        self.assertFalse(ok)
        self.assertEqual(skip, "removed_errors_not_on_edited_paths")

    def test_lint_ruff_subset_grant(self):
        pre = parse_ruff_error_signatures(
            "pkg/x.py:1:1: E402 Module level import not at top\npkg/x.py:2:1: F401\n"
        )
        post_txt = "pkg/x.py:2:1: F401 'os' imported but unused\n"
        ok, detail, conf, skip = evaluate_non_python_repair_budget(
            domain="lint",
            pre_set=pre,
            post_text=post_txt,
            edited_norm=["pkg/x.py"],
            parse_post=parse_ruff_error_signatures,
        )
        self.assertTrue(ok)
        self.assertEqual(conf, "high")
        self.assertEqual(skip, "")

    def test_build_npm_subset_when_configured(self):
        pre = parse_npm_build_signatures("npm ERR! code ELIFECYCLE\nnpm ERR! errno 1\n")
        post_txt = ""
        ok, _, _, skip = evaluate_non_python_repair_budget(
            domain="build",
            pre_set=pre,
            post_text=post_txt,
            edited_norm=["package.json"],
            parse_post=parse_npm_build_signatures,
        )
        self.assertTrue(ok)
        self.assertEqual(skip, "")

    def test_resolve_ts_uses_probe_subset(self):
        stderr = "src/a.ts(1,1): error TS2322: bad\nsrc/b.ts(2,2): error TS100: x\n"
        failed = [
            {"name": "TypeCheck", "kind": "typecheck", "status": "failed", "stderr": stderr},
        ]
        res = RepairSpecialistResult(
            ok=True,
            parse_ok=True,
            provider_ok=True,
            edits_to_apply=[{"path": "src/a.ts", "old_str": "x", "new_str": "y"}],
        )
        post_only_b = "src/b.ts(2,2): error TS100: x\n"
        with mock.patch(
            "apps.cli.runtime.repair_budget_nonpy._probe_tsc_output",
            return_value=post_only_b,
        ):
            g, detail, conf, skip = resolve_repair_operational_budget(
                cwd=".",
                structural_python_failure=False,
                applied=1,
                res=res,
                failed_checks=failed,
            )
        self.assertTrue(g)
        self.assertEqual(conf, "high")
        self.assertEqual(skip, "")

    def test_churn_gate_still_blocks_at_budget_manager(self):
        """Operational grant API remains subject to read churn (see test_autonomy)."""
        mgr = BudgetManager(200)
        for _ in range(5):
            mgr.consume("read_file", path="churn.py")
        add, skip = mgr.grant_extension_for_operational_progress(
            reason="repair_structured_progress",
            skip_if_read_churn=True,
        )
        self.assertEqual(add, 0)
        self.assertTrue(skip)

    @mock.patch("apps.cli.runtime.repair_budget_nonpy._probe_tsc_output_raw")
    def test_typecheck_empty_pre_skips_tsc_subprocess(self, m_raw):
        failed = [
            {
                "name": "TypeCheck",
                "kind": "typecheck",
                "status": "failed",
                "stderr": "generic failure with no file.ts(line) pattern",
            },
        ]
        res = RepairSpecialistResult(
            ok=True,
            parse_ok=True,
            provider_ok=True,
            edits_to_apply=[{"path": "a.ts", "old_str": "x", "new_str": "y"}],
        )
        class _S:
            def __init__(self) -> None:
                self.write_epoch = 3
                self.repair_probe_cache: dict = {}

        g, _, _, skip = resolve_repair_operational_budget(
            cwd=".",
            structural_python_failure=False,
            applied=1,
            res=res,
            failed_checks=failed,
            session=_S(),
        )
        self.assertFalse(g)
        self.assertEqual(skip, "no_structured_pre_errors")
        m_raw.assert_not_called()

    @mock.patch("apps.cli.runtime.repair_budget_nonpy._probe_tsc_output_raw", return_value="x")
    def test_tsc_probe_cached_within_same_write_epoch(self, m_raw):
        class _S:
            def __init__(self) -> None:
                self.write_epoch = 7
                self.repair_probe_cache: dict = {}

        s = _S()
        _probe_tsc_output(".", s)
        _probe_tsc_output(".", s)
        self.assertEqual(m_raw.call_count, 1)

    def test_resolve_catches_internal_errors(self):
        with mock.patch(
            "apps.cli.runtime.repair_budget_nonpy._resolve_repair_operational_budget_inner",
            side_effect=RuntimeError("boom"),
        ):
            g, _, _, skip = resolve_repair_operational_budget(
                cwd=".",
                structural_python_failure=False,
                applied=1,
                res=RepairSpecialistResult(
                    ok=True,
                    parse_ok=True,
                    provider_ok=True,
                    edits_to_apply=[{"path": "a.ts", "old_str": "a", "new_str": "b"}],
                ),
                failed_checks=[
                    {"name": "TC", "kind": "typecheck", "status": "failed", "stderr": "a.ts(1,1): error TS1: x"}
                ],
            )
        self.assertFalse(g)
        self.assertEqual(skip, "internal_budget_resolution_error")


class TestSessionEventCompaction(unittest.TestCase):
    def test_budget_skip_coalesces(self):
        class _S:
            def __init__(self) -> None:
                self.events: list = []

        s = _S()
        append_compacted_session_event(
            s,
            {"event": "budget_extension_skipped", "reason": "r", "detail": "d"},
        )
        append_compacted_session_event(
            s,
            {"event": "budget_extension_skipped", "reason": "r", "detail": "d"},
        )
        self.assertEqual(len(s.events), 1)
        self.assertEqual(s.events[0].get("repeat"), 2)

    @mock.patch("apps.cli.runtime.repair_budget_nonpy._probe_tsc_output_raw", return_value=None)
    @mock.patch("apps.cli.runtime.repair_budget_nonpy._diagnose_missing_executable", return_value=True)
    def test_tsc_probe_failure_emits_tool_unavailable_event(self, _diag, _raw):
        stderr = "src/a.ts(1,1): error TS2322: bad\n"
        failed = [{"name": "TypeCheck", "kind": "typecheck", "status": "failed", "stderr": stderr}]
        res = RepairSpecialistResult(
            ok=True,
            parse_ok=True,
            provider_ok=True,
            edits_to_apply=[{"path": "src/a.ts", "old_str": "x", "new_str": "y"}],
        )

        class _S:
            def __init__(self) -> None:
                self.write_epoch = 1
                self.repair_probe_cache: dict = {}
                self.events: list = []

        s = _S()
        resolve_repair_operational_budget(
            cwd=".",
            structural_python_failure=False,
            applied=1,
            res=res,
            failed_checks=failed,
            session=s,
        )
        evs = [e for e in s.events if e.get("event") == "runtime_tool_unavailable"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].get("probe"), "tsc")
        self.assertEqual(evs[0].get("impact"), "repair_operational_budget_probe_unavailable")

    def test_operator_runtime_digest_shape(self):
        class _S:
            def __init__(self) -> None:
                self.phase = "VERIFY"
                self.closure_reason = ""
                self.budget_exhausted_reason = ""
                self.last_repair_causal_digest = "abc"
                self.last_verify_failure_snapshot = {"structural_digest": "def"}
                self.budget_extension_events = [{"reason": "repair_structured_progress", "points": 12}]
                self.phase_transitions = [{"to": "REPAIR", "detail": "x"}]
                self.events = [
                    {
                        "event": "verify_failure_set_delta",
                        "delta": "failure_reduced",
                        "causal_digest_after": "cafeca",
                        "structural_digest_after": "dead",
                        "budget_extended": True,
                    }
                ]

        d = build_operator_runtime_digest(_S())
        self.assertEqual(d.get("active_causal_digest"), "abc")
        self.assertEqual(d.get("verify_failure_structural_digest"), "def")
        self.assertEqual(d.get("last_failure_set_delta_event", {}).get("delta"), "failure_reduced")
