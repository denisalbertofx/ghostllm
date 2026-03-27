"""Tests for outcome_engine verification truthfulness and tool normalization."""
import unittest
from dataclasses import dataclass
from typing import Any, Dict, Optional

import json

from apps.cli.runtime.outcome_engine import (
    determine_task_outcome,
    _has_final_nl_response,
    _build_verification_failed_next_action,
    _collect_paths_from_failed_checks,
    _get_normalized_tool_errors,
    _is_recovered_tool_failure,
    OUTCOME_BLOCKED,
    OUTCOME_IMPLEMENTED,
    OUTCOME_ALREADY_IMPLEMENTED,
    OUTCOME_NO_OP,
    OUTCOME_PARTIALLY_IMPLEMENTED,
    OUTCOME_READ_ONLY,
    OUTCOME_VERIFICATION_FAILED,
)


@dataclass
class MockSession:
    diff_summary: list
    task: str = ""
    task_type: str = "direct_edit"
    task_intent: str = ""
    repair_attempt_count: int = 0
    repair_summary: str = ""
    task_contract: Optional[Dict[str, Any]] = None
    runtime_contract_source: str = ""
    write_epoch: int = 0
    last_integrity_ok_write_epoch: int = -1

    def __post_init__(self) -> None:
        if self.task_contract is None:
            self.task_contract = {"spec": {}}


class TestEditMismatchNotMissingFiles(unittest.TestCase):
    """edit_file old_str miss must not be classified as missing_files / blocked."""

    def test_old_str_not_found_yields_partial_not_blocked(self):
        session = MockSession(
            diff_summary=[],
            task_contract={"spec": {"intent": "implementation"}},
            runtime_contract_source="taskspec",
        )
        messages = [
            {
                "role": "tool",
                "name": "edit_file",
                "content": '{"error": "Target string (old_str) not found."}',
            },
        ]
        result = determine_task_outcome(session, messages, {})
        self.assertEqual(result.outcome, OUTCOME_PARTIALLY_IMPLEMENTED)
        self.assertNotIn("missing_files", result.summary.lower())

    def test_path_not_found_still_blocked(self):
        session = MockSession(
            diff_summary=[],
            task_contract={"spec": {"intent": "implementation"}},
            runtime_contract_source="taskspec",
        )
        messages = [
            {"role": "tool", "name": "read_file", "content": '{"error": "Path not found: missing.ts"}'},
        ]
        result = determine_task_outcome(session, messages, {})
        self.assertEqual(result.outcome, OUTCOME_BLOCKED)
        self.assertIn("missing_files", result.summary)


class TestRecoveredToolFailure(unittest.TestCase):
    """Edit failed + write succeeded must not surface as unresolved error."""

    def test_recovered_edit_failure_not_surfaced(self):
        diff_summary = [{"file": "foo.ts", "type": "Write File"}]
        tool_history = [
            {"role": "tool", "name": "edit_file", "content": '{"error": "Target string (old_str) not found."}'},
            {"role": "tool", "name": "write_file", "content": '{"status": "success", "bytes": 100}'},
        ]
        errors, penalty = _get_normalized_tool_errors(tool_history, diff_summary)
        self.assertEqual(errors, [], "Recovered edit failure should not be surfaced")
        self.assertEqual(penalty, 0)

    def test_unrecovered_tool_error_surfaced(self):
        diff_summary = []
        tool_history = [
            {"role": "tool", "name": "write_file", "content": '{"error": "Permission denied"}'},
        ]
        errors, penalty = _get_normalized_tool_errors(tool_history, diff_summary)
        self.assertGreater(len(errors), 0)
        self.assertGreater(penalty, 0)

    def test_already_implemented_fast_path_shell_block_not_surfaced(self):
        diff_summary = []
        tool_history = [
            {
                "role": "tool",
                "name": "run_shell",
                "content": '{"error": "Already implemented fast-path: nearby tests already cover this; answer without manual run_shell verification."}',
            },
        ]
        errors, penalty = _get_normalized_tool_errors(tool_history, diff_summary)
        self.assertEqual(errors, [])
        self.assertEqual(penalty, 0)

    def test_is_recovered_pattern(self):
        self.assertTrue(_is_recovered_tool_failure("Target string (old_str) not found.", [{}]))
        self.assertTrue(_is_recovered_tool_failure("old_str not found in file", [{}]))
        self.assertFalse(_is_recovered_tool_failure("File not found", [{}]))
        self.assertFalse(_is_recovered_tool_failure("Tool 'xyz' not found", [{}]))


class TestVerificationNextActionPaths(unittest.TestCase):
    """Next action must cite compiler paths, not only the last touched file from diff."""

    def test_collect_paths_multi_check(self):
        checks = [
            {"name": "Build", "status": "failed", "stderr": "./db/schema.ts:2:1\nCan't resolve 'drizzle-zod'"},
            {"name": "TypeCheck", "status": "failed", "stderr": "app/issues//page.tsx(385,19): error TS2322:"},
        ]
        paths = _collect_paths_from_failed_checks(checks, limit=8)
        self.assertIn("db/schema.ts", paths)
        self.assertTrue(any("page.tsx" in p for p in paths))

    def test_next_action_not_only_agents_md(self):
        checks = [
            {
                "name": "Build",
                "status": "failed",
                "stderr": "Turbopack\n./db/schema.ts:2:1\nCan't resolve 'drizzle-zod'",
            },
        ]
        diff = [{"file": "AGENTS.md", "type": "Write File"}]
        na = _build_verification_failed_next_action(checks, diff, "", 0, "failed")
        self.assertIn("db/schema", na)
        self.assertNotIn("still failing in AGENTS.md.", na)


class TestVerificationTruthfulness(unittest.TestCase):
    """Blocked verification must not show success; no verification => no fake badges."""

    def test_blocked_verification_not_implemented(self):
        session = MockSession(
            diff_summary=[{"file": "x.ts", "type": "Edit File"}],
            task_type="direct_edit",
        )
        messages = [
            {"role": "tool", "name": "write_file", "content": '{"status": "success"}'},
        ]
        verification = {
            "status": "blocked",
            "checks": [
                {"name": "TypeCheck", "status": "blocked", "reason": "POLICY VIOLATION: Unix-ism"},
            ],
        }
        result = determine_task_outcome(session, messages, verification)
        self.assertEqual(result.outcome, OUTCOME_VERIFICATION_FAILED)
        # Blocked verification must NOT yield implemented

    def test_no_verification_run_no_fake_success(self):
        session = MockSession(
            diff_summary=[{"file": "x.ts", "type": "Write File"}],
            task_type="direct_edit",
        )
        messages = [
            {"role": "tool", "name": "write_file", "content": '{"status": "success"}'},
        ]
        verification = {"status": "skipped", "checks": []}
        result = determine_task_outcome(session, messages, verification)
        self.assertEqual(result.outcome, OUTCOME_IMPLEMENTED)
        self.assertIn("skipped", result.summary.lower())

    def test_incomplete_verification_no_fake_passed(self):
        """Stream interruption (no manual_notes) -> verification incomplete -> verification_failed."""
        session = MockSession(
            diff_summary=[{"file": "x.ts", "type": "Write File"}],
            task_type="direct_edit",
        )
        messages = [
            {"role": "tool", "name": "write_file", "content": '{"status": "success"}'},
        ]
        verification = {
            "status": "incomplete",
            "steps_executed_count": 0,
            "checks": [
                {"name": "Build", "status": "not_run", "provenance": "inferred_not_allowed"},
                {"name": "TypeCheck", "status": "not_run", "provenance": "inferred_not_allowed"},
                {"name": "Lint", "status": "not_run", "provenance": "inferred_not_allowed"},
            ],
        }
        result = determine_task_outcome(session, messages, verification)
        self.assertEqual(result.outcome, OUTCOME_VERIFICATION_FAILED)
        self.assertIn("incomplete", result.summary.lower())

    def test_manual_only_verification_implemented(self):
        """Manual-only (manual_verification_notes set) -> implemented, not verification_failed."""
        session = MockSession(
            diff_summary=[{"file": "x.ts", "type": "Write File"}],
            task_type="direct_edit",
        )
        messages = [
            {"role": "tool", "name": "write_file", "content": '{"status": "success"}'},
        ]
        verification = {
            "status": "incomplete",
            "steps_executed_count": 0,
            "manual_verification_notes": "Model reported manual inspection only; no structured verification checks executed.",
            "checks": [
                {"name": "Build", "status": "not_run", "provenance": "inferred_not_allowed"},
                {"name": "TypeCheck", "status": "not_run", "provenance": "inferred_not_allowed"},
                {"name": "Lint", "status": "not_run", "provenance": "inferred_not_allowed"},
            ],
        }
        result = determine_task_outcome(session, messages, verification)
        self.assertEqual(result.outcome, OUTCOME_IMPLEMENTED)
        self.assertIn("Manual inspection performed", result.summary)
        self.assertNotIn("Verification: passed", result.evidence_lines)

    def test_one_blocked_two_not_run_no_success(self):
        """One blocked + two not_run must not yield verification passed."""
        session = MockSession(
            diff_summary=[{"file": "x.ts", "type": "Write File"}],
            task_type="direct_edit",
        )
        messages = [{"role": "tool", "name": "write_file", "content": '{"status": "success"}'}]
        verification = {
            "status": "blocked",
            "steps_executed_count": 1,
            "checks": [
                {"name": "TypeCheck", "status": "blocked", "provenance": "blocked_by_policy"},
                {"name": "Build", "status": "not_run", "provenance": "not_run"},
                {"name": "Lint", "status": "not_run", "provenance": "not_run"},
            ],
        }
        result = determine_task_outcome(session, messages, verification)
        self.assertEqual(result.outcome, OUTCOME_VERIFICATION_FAILED)

    def test_final_nl_response_prevents_no_op(self):
        """Final natural-language response + no tools + no writes => not no_op, artifact must not say skipped."""
        session = MockSession(diff_summary=[], task_type="direct_edit")
        messages = [
            {"role": "user", "content": "Is X already implemented?"},
            {"role": "assistant", "content": "Yes, the feature is already implemented. I checked the codebase and found..."},
        ]
        verification = {"status": "skipped", "checks": []}
        result = determine_task_outcome(session, messages, verification)
        self.assertNotEqual(result.outcome, OUTCOME_NO_OP)
        self.assertNotIn("No execution or answer produced", result.summary)

    def test_has_final_nl_response_detection(self):
        """_has_final_nl_response returns True when last assistant has non-empty content."""
        self.assertTrue(_has_final_nl_response([
            {"role": "user", "content": "?"},
            {"role": "assistant", "content": "Yes, it is already done."},
        ]))
        self.assertFalse(_has_final_nl_response([
            {"role": "user", "content": "?"},
            {"role": "assistant", "content": ""},
        ]))
        self.assertFalse(_has_final_nl_response([
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "?"},
        ]))

    def test_missing_verification_fail_closed(self):
        """Missing/empty verification object -> fail closed, no fake passed."""
        session = MockSession(
            diff_summary=[{"file": "x.ts", "type": "Write File"}],
            task_type="direct_edit",
        )
        messages = [{"role": "tool", "name": "write_file", "content": '{"status": "success"}'}]
        for verification in (None, {}, {"status": "", "checks": []}):
            result = determine_task_outcome(session, messages, verification)
            self.assertNotIn("Verification: passed", result.evidence_lines)

    def test_explicit_passed_typecheck_only(self):
        """TypeCheck passed, Build/Lint not_run -> steps_executed_count=1, can show implemented."""
        session = MockSession(
            diff_summary=[{"file": "x.ts", "type": "Edit File"}],
            task_type="direct_edit",
        )
        messages = [
            {"role": "tool", "name": "write_file", "content": '{"status": "success"}'},
        ]
        verification = {
            "status": "success",
            "steps_executed_count": 1,
            "checks": [
                {"name": "TypeCheck", "status": "passed", "provenance": "executed", "exit_code": 0},
                {"name": "Build", "status": "not_run", "provenance": "not_run"},
                {"name": "Lint", "status": "not_run", "provenance": "not_run"},
            ],
        }
        result = determine_task_outcome(session, messages, verification)
        self.assertEqual(result.outcome, OUTCOME_IMPLEMENTED)
        self.assertIn("Verification: passed", result.evidence_lines)

    def test_success_verification_stale_after_subsequent_writes_not_implemented(self):
        """Edits after last integrity checkpoint: do not close as implemented on old pytest snapshot."""
        session = MockSession(
            diff_summary=[{"file": "x.py", "type": "Edit File"}],
            task_type="direct_edit",
            write_epoch=3,
            last_integrity_ok_write_epoch=1,
        )
        messages = [{"role": "tool", "name": "write_file", "content": '{"status": "success"}'}]
        verification = {
            "status": "success",
            "steps_executed_count": 1,
            "checks": [
                {
                    "name": "Python Tests (explicit, targeted)",
                    "status": "passed",
                    "provenance": "executed",
                    "exit_code": 0,
                },
            ],
        }
        result = determine_task_outcome(session, messages, verification)
        self.assertEqual(result.outcome, OUTCOME_VERIFICATION_FAILED)
        self.assertNotIn("Verification: passed", result.evidence_lines)
        self.assertTrue(any("stale" in (line or "").lower() for line in result.evidence_lines))

    def test_already_implemented_manual_only_diagnostic(self):
        """already_implemented + steps_executed=0 => diagnostic mentions manual inspection"""
        session = MockSession(
            diff_summary=[],
            task_type="direct_edit",
            task_intent="implementation",
        )
        # Need 4+ reads + exploration for "Matching implementation" (score >= 10)
        messages = [
            {"role": "tool", "name": "summarize_repo", "content": '{"summary": "Next.js app"}'},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "function": {"name": "read_file", "arguments": '{"path": "app/api/issues/route.ts"}'}},
                {"id": "tc2", "function": {"name": "read_file", "arguments": '{"path": "lib/validations.ts"}'}},
                {"id": "tc3", "function": {"name": "read_file", "arguments": '{"path": "db/schema.ts"}'}},
                {"id": "tc4", "function": {"name": "read_file", "arguments": '{"path": "app/api/other.ts"}'}},
            ]},
            {"role": "tool", "tool_call_id": "tc1", "name": "read_file", "content": '{"content": "// route handler"}'},
            {"role": "tool", "tool_call_id": "tc2", "name": "read_file", "content": '{"content": "const x = 1"}'},
            {"role": "tool", "tool_call_id": "tc3", "name": "read_file", "content": '{"content": "export default"}'},
            {"role": "tool", "tool_call_id": "tc4", "name": "read_file", "content": '{"content": "function handler"}'},
        ]
        verification = {
            "status": "incomplete",
            "steps_executed_count": 0,
            "checks": [{"name": "Build", "status": "not_run", "provenance": "inferred_not_allowed"}],
        }
        result = determine_task_outcome(session, messages, verification)
        self.assertEqual(result.outcome, OUTCOME_ALREADY_IMPLEMENTED)
        self.assertIn("Manual inspection", result.summary)
        self.assertIn("no structured verification", result.summary.lower())

    def test_already_implemented_cites_file_and_pattern(self):
        """already_implemented with read evidence => diagnostic cites file and pattern"""
        session = MockSession(
            diff_summary=[],
            task_type="direct_edit",
            task_intent="implementation",
        )
        messages = [
            {"role": "tool", "name": "summarize_repo", "content": '{"summary": "Next.js"}'},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "function": {"name": "read_file", "arguments": '{"path": "app/api/issues/route.ts"}'}},
                {"id": "tc2", "function": {"name": "read_file", "arguments": '{"path": "lib/validations.ts"}'}},
                {"id": "tc3", "function": {"name": "read_file", "arguments": '{"path": "db/schema.ts"}'}},
                {"id": "tc4", "function": {"name": "read_file", "arguments": '{"path": "other.ts"}'}},
            ]},
            {"role": "tool", "tool_call_id": "tc1", "name": "read_file", "content": '{"content": "const x = statusParam.trim().toLowerCase(); issueStatusEnum.safeParse(x)"}'},
            {"role": "tool", "tool_call_id": "tc2", "name": "read_file", "content": '{"content": "// lib"}'},
            {"role": "tool", "tool_call_id": "tc3", "name": "read_file", "content": '{"content": "// schema"}'},
            {"role": "tool", "tool_call_id": "tc4", "name": "read_file", "content": '{"content": "// other"}'},
        ]
        verification = {
            "status": "incomplete",
            "steps_executed_count": 0,
            "checks": [],
        }
        result = determine_task_outcome(session, messages, verification)
        self.assertEqual(result.outcome, OUTCOME_ALREADY_IMPLEMENTED)
        self.assertIn("app/api/issues/route.ts", result.summary)
        self.assertIn("trim().toLowerCase()", result.summary)

    def test_implementation_no_writes_not_read_only(self):
        """implementation + may_write + no changes + exploration => already_implemented, NOT read_only"""
        session = MockSession(
            diff_summary=[],
            task_type="direct_edit",
            task_intent="implementation",
        )
        # 3 reads + ls + summarize (matches user scenario: Evidence 3 pts)
        messages = [
            {"role": "tool", "name": "summarize_repo", "content": '{"summary": "Next.js"}'},
            {"role": "tool", "name": "ls", "content": '{"files": ["route.ts"]}'},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "function": {"name": "read_file", "arguments": '{"path": "app/api/issues/route.ts"}'}},
                {"id": "tc2", "function": {"name": "read_file", "arguments": '{"path": "lib/validations.ts"}'}},
                {"id": "tc3", "function": {"name": "read_file", "arguments": '{"path": "db/schema.ts"}'}},
            ]},
            {"role": "tool", "tool_call_id": "tc1", "name": "read_file", "content": '{"content": "trim().toLowerCase()"}'},
            {"role": "tool", "tool_call_id": "tc2", "name": "read_file", "content": '{"content": "enum"}'},
            {"role": "tool", "tool_call_id": "tc3", "name": "read_file", "content": '{"content": "schema"}'},
        ]
        verification = {"status": "incomplete", "steps_executed_count": 0, "checks": []}
        result = determine_task_outcome(session, messages, verification)
        self.assertEqual(result.outcome, OUTCOME_ALREADY_IMPLEMENTED)
        self.assertNotEqual(result.outcome, "read_only")
        self.assertIn("app/api/issues/route.ts", result.summary)

    def test_already_implemented_verification_passed_diagnostic(self):
        """already_implemented + verification passed => diagnostic mentions passed checks"""
        session = MockSession(
            diff_summary=[],
            task_type="direct_edit",
            task_intent="implementation",
        )
        # 4+ reads + run_shell success + verification passed => strong evidence
        messages = [
            {"role": "tool", "name": "read_file", "content": '{"content": "valid code"}'},
            {"role": "tool", "name": "read_file", "content": '{"content": "a"}'},
            {"role": "tool", "name": "read_file", "content": '{"content": "b"}'},
            {"role": "tool", "name": "read_file", "content": '{"content": "c"}'},
            {"role": "tool", "name": "run_shell", "content": '{"exit_code": 0}'},
        ]
        verification = {
            "status": "success",
            "steps_executed_count": 2,
            "checks": [
                {"name": "TypeCheck", "status": "passed", "provenance": "executed", "exit_code": 0},
                {"name": "Build", "status": "passed", "provenance": "executed", "exit_code": 0},
            ],
        }
        result = determine_task_outcome(session, messages, verification)
        self.assertEqual(result.outcome, OUTCOME_ALREADY_IMPLEMENTED)
        self.assertIn("Structured verification passed", result.summary)
        self.assertIn("TypeCheck", result.summary)
        self.assertNotIn("Manual inspection only", result.summary)
        self.assertIn("No changes needed", result.recommended_next_action)

    def test_next_action_includes_failed_check_and_repair_context(self):
        """Next action for verification_failed includes specific check, cause, and repair context."""
        checks = [
            {"name": "TypeCheck", "status": "failed", "stderr": "error TS2322: Type 'string' is not assignable to type 'number'"},
            {"name": "Build", "status": "not_run"},
            {"name": "Lint", "status": "passed", "provenance": "executed"},
        ]
        diff_summary = [{"file": "app/api/issues/route.ts", "type": "Edit File"}]
        repair_summary = "Switched where conditions to and(...conditions)"
        repair_count = 1
        rec = _build_verification_failed_next_action(
            checks=checks,
            diff_summary=diff_summary,
            repair_summary=repair_summary,
            repair_count=repair_count,
            v_status="failed",
        )
        self.assertIn("TypeCheck", rec)
        self.assertIn("app/api/issues/route.ts", rec)
        self.assertIn("and(...conditions)", rec)
        self.assertIn("Re-run", rec)
        self.assertIn("error TS2322", rec)
        self.assertNotIn("Drizzle", rec)


class TestAnalysisReadOnlyOutcome(unittest.TestCase):
    def test_analysis_outcome_summary_is_findings_first_not_no_changes(self):
        session = MockSession(
            diff_summary=[],
            task_contract={"spec": {"intent": "analysis"}},
            runtime_contract_source="taskspec",
        )
        messages = [{"role": "tool", "name": "read_file", "content": json.dumps({"content": "x" * 20})}]
        result = determine_task_outcome(session, messages, {"status": "skipped", "checks": []})
        self.assertEqual(result.outcome, OUTCOME_READ_ONLY)
        self.assertIn("Inspección", result.summary)
        self.assertNotIn("no changes needed", result.summary.lower())
        self.assertEqual(result.findings_evidence_tier, "unverified")
        self.assertLess(result.confidence, 0.55)
        self.assertTrue(any("unverified" in (e or "").lower() for e in (result.evidence_lines or [])))

    def test_analysis_tier_confirmed_when_verify_executed(self):
        session = MockSession(
            diff_summary=[],
            task_contract={"spec": {"intent": "analysis"}},
            runtime_contract_source="taskspec",
        )
        messages = [{"role": "tool", "name": "read_file", "content": json.dumps({"content": "a"})}]
        verif = {
            "status": "success",
            "steps_executed_count": 1,
            "checks": [{"name": "Lint", "provenance": "executed", "status": "passed", "exit_code": 0}],
        }
        result = determine_task_outcome(session, messages, verif)
        self.assertEqual(result.findings_evidence_tier, "confirmed")
        self.assertGreaterEqual(result.confidence, 0.7)

    def test_modification_should_not_write_gets_findings_tier_not_already_implemented(self):
        """Read-only contract must not collapse to already_implemented after exploration."""
        session = MockSession(
            diff_summary=[],
            task_contract={
                "spec": {
                    "intent": "modification",
                    "change_expectation": "should_not_write",
                }
            },
            task_intent="modification",
            runtime_contract_source="taskspec",
        )
        messages = [{"role": "tool", "name": "read_file", "content": json.dumps({"content": "x" * 20})}]
        result = determine_task_outcome(session, messages, {"status": "skipped", "checks": []})
        self.assertEqual(result.outcome, OUTCOME_READ_ONLY)
        self.assertEqual(result.findings_evidence_tier, "unverified")


if __name__ == "__main__":
    unittest.main()
