"""Autonomy runtime helpers."""
import unittest

from apps.cli.runtime.autonomy import BudgetManager, StagnationDetector, stagnation_tool_detail


class TestStagnationDetector(unittest.TestCase):
    def test_reset_clears_stagnation_for_new_task(self) -> None:
        d = StagnationDetector(max_same_action_same_target=3)
        for _ in range(3):
            d.add_action("edit_file", "app/x.ts")
        self.assertTrue(d.check_stagnation()[0])
        d.reset()
        self.assertFalse(d.check_stagnation()[0])
        d.add_action("edit_file", "app/x.ts")
        self.assertFalse(d.check_stagnation()[0])


class TestBudgetManager(unittest.TestCase):
    def test_reserved_write_tool_calls_allow_first_patch_after_read_budget(self) -> None:
        mgr = BudgetManager()
        mgr.reset_contract_spec_tool_limits(
            {"max_tool_calls": 3, "max_shell_calls": 0, "reserved_write_tool_calls": 2}
        )
        ok, _ = mgr.contract_spec_allows_tool("edit_file")
        self.assertTrue(ok)
        mgr.record_contract_spec_tool_invocation("edit_file")
        for _ in range(2):
            ok, _ = mgr.contract_spec_allows_tool("read_file")
            self.assertTrue(ok)
            mgr.record_contract_spec_tool_invocation("read_file")
        ok, reason = mgr.contract_spec_allows_tool("edit_file")
        self.assertTrue(ok, reason)
        mgr.record_contract_spec_tool_invocation("edit_file")
        ok, reason = mgr.contract_spec_allows_tool("write_file")
        self.assertTrue(ok, reason)
        mgr.record_contract_spec_tool_invocation("write_file")
        ok, reason = mgr.contract_spec_allows_tool("edit_file")
        self.assertFalse(ok)
        self.assertIn("max_tool_calls", reason)

    def test_soft_read_only_extension_keeps_long_debug_session_alive(self) -> None:
        mgr = BudgetManager()
        mgr.reset_contract_spec_tool_limits(
            {
                "max_tool_calls": 2,
                "max_shell_calls": 0,
                "reserved_write_tool_calls": 0,
                "soft_read_only_tool_calls": 2,
            }
        )
        for _ in range(2):
            ok, _ = mgr.contract_spec_allows_tool("read_file")
            self.assertTrue(ok)
            mgr.record_contract_spec_tool_invocation("read_file")
        ok, reason = mgr.contract_spec_allows_tool("read_file")
        self.assertTrue(ok, reason)
        mgr.record_contract_spec_tool_invocation("read_file")
        ok, reason = mgr.contract_spec_allows_tool("summarize_repo")
        self.assertTrue(ok, reason)
        mgr.record_contract_spec_tool_invocation("summarize_repo")
        ok, reason = mgr.contract_spec_allows_tool("read_file")
        self.assertFalse(ok)
        self.assertIn("max_tool_calls", reason)

    def test_consecutive_reads_reset(self) -> None:
        d = StagnationDetector(max_idle_reads=4)
        for _ in range(4):
            d.add_action("read_file", "same.ts")
        self.assertTrue(d.check_stagnation()[0])
        d.reset()
        self.assertFalse(d.check_stagnation()[0])

    def test_failed_edit_same_path_triggers_stagnation(self) -> None:
        d = StagnationDetector(max_failed_edits_same_target=2)
        d.add_failed_edit_attempt("pkg/foo.py")
        self.assertFalse(d.check_stagnation()[0])
        d.add_failed_edit_attempt("pkg/foo.py")
        self.assertTrue(d.check_stagnation()[0])

    def test_reset_after_verified_progress_clears_history(self) -> None:
        d = StagnationDetector(max_same_action_same_target=2)
        d.add_action("read_file", "a.py")
        d.add_action("read_file", "a.py")
        self.assertTrue(d.check_stagnation()[0])
        d.reset_after_verified_progress()
        self.assertFalse(d.check_stagnation()[0])

    def test_read_file_distinct_windows_not_threepeat_loop(self) -> None:
        d = StagnationDetector(max_same_action_same_target=3)
        for sl, ml in ((1, 20), (40, 15), (100, 10)):
            d.add_action(
                "read_file",
                stagnation_tool_detail("read_file", {"path": "pkg/x.py", "start_line": sl, "max_lines": ml}),
            )
        self.assertFalse(d.check_stagnation()[0])

    def test_read_file_same_window_still_triggers_loop(self) -> None:
        d = StagnationDetector(max_same_action_same_target=3)
        det = stagnation_tool_detail("read_file", {"path": "pkg/x.py", "start_line": 5, "max_lines": 10})
        for _ in range(3):
            d.add_action("read_file", det)
        self.assertTrue(d.check_stagnation()[0])


class TestBudgetManagerAdaptive(unittest.TestCase):
    def test_reset_session_budget_state_restores_initial_total(self) -> None:
        mgr = BudgetManager(80)
        mgr.consume("run_shell")
        mgr.total_budget = 200
        mgr.reset_session_budget_state()
        self.assertEqual(mgr.used_budget, 0)
        self.assertEqual(mgr.total_budget, 80)

    def test_grant_extension_respects_cap(self) -> None:
        mgr = BudgetManager(40)
        added = mgr.grant_extension_for_verified_progress(steps_executed=2)
        self.assertGreater(added, 0)
        self.assertLessEqual(mgr.total_budget, mgr._adaptive_cap)

    def test_operational_progress_grant_skipped_on_read_churn(self) -> None:
        mgr = BudgetManager(200)
        for _ in range(5):
            mgr.consume("read_file", path="churn.py")
        add, skip = mgr.grant_extension_for_operational_progress(
            reason="test", skip_if_read_churn=True
        )
        self.assertEqual(add, 0)
        self.assertIn("churn", skip.lower())

    def test_operational_progress_grant_when_no_churn(self) -> None:
        mgr = BudgetManager(200)
        add, skip = mgr.grant_extension_for_operational_progress(
            reason="test", skip_if_read_churn=True
        )
        self.assertGreater(add, 0)
        self.assertEqual(skip, "")

    def test_read_churn_multiplier_after_many_reads_same_path(self) -> None:
        mgr = BudgetManager(500)
        c1 = mgr.consume("read_file", path="same.py")
        c2 = mgr.consume("read_file", path="same.py")
        c3 = mgr.consume("read_file", path="same.py")
        c4 = mgr.consume("read_file", path="same.py")
        self.assertEqual(c1, c2)
        self.assertGreater(c4, c1)

    def test_successful_write_clears_read_churn(self) -> None:
        mgr = BudgetManager(500)
        for _ in range(3):
            mgr.consume("read_file", path="x.py")
        mgr.note_successful_write_path("x.py")
        c_after = mgr.consume("read_file", path="x.py")
        self.assertEqual(c_after, mgr.get_cost("read_file"))


if __name__ == "__main__":
    unittest.main()
