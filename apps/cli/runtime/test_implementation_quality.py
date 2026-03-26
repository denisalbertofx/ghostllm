"""
Tests for implementation quality checks (GEP-10).
Ensures buggy Zod patterns are detected and do not pass as already_implemented.
"""
import unittest

try:
    from implementation_quality import (
        evaluate_implementation_quality,
        QUALITY_VALID,
        QUALITY_SUSPICIOUS,
        QUALITY_INCORRECT,
    )
    from outcome_engine import (
        determine_task_outcome,
        OUTCOME_ALREADY_IMPLEMENTED,
        OUTCOME_PARTIALLY_IMPLEMENTED,
    )
except ImportError:
    from apps.cli.runtime.implementation_quality import (
        evaluate_implementation_quality,
        QUALITY_VALID,
        QUALITY_SUSPICIOUS,
        QUALITY_INCORRECT,
    )
    from apps.cli.runtime.outcome_engine import (
        determine_task_outcome,
        OUTCOME_ALREADY_IMPLEMENTED,
        OUTCOME_PARTIALLY_IMPLEMENTED,
    )


class FakeSession:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def make_read_file_result(content: str) -> dict:
    import json
    return {"role": "tool", "name": "read_file", "content": json.dumps({"content": content})}


class TestImplementationQuality(unittest.TestCase):
    def test_zod_safeparse_raw_in_eq_incorrect(self):
        """Code validates status with safeParse but uses raw status in eq() - should be incorrect."""
        buggy_code = """
const status = searchParams.get("status");
if (status !== null) {
  const parseResult = issueStatusEnum.safeParse(status);
  if (!parseResult.success) {
    return NextResponse.json({ error: "Invalid" }, { status: 400 });
  }
}
const allIssues = await db.query.issues.findMany({
  where: status ? eq(issues.status, status) : undefined,
  orderBy: [desc(issues.createdAt)],
});
"""
        tool_history = [make_read_file_result(buggy_code)]
        session = FakeSession(diff_summary=[], task_intent="implementation")
        result = evaluate_implementation_quality(session, tool_history)
        self.assertIn(result.quality_status, (QUALITY_SUSPICIOUS, QUALITY_INCORRECT))
        self.assertFalse(result.quality_status == QUALITY_VALID)
        self.assertGreater(len(result.issues_found), 0)

    def test_zod_safeparse_with_parsed_data_valid(self):
        """Code uses parseResult.data correctly - should be valid."""
        correct_code = """
const status = searchParams.get("status");
let validatedStatus = null;
if (status !== null) {
  const parseResult = issueStatusEnum.safeParse(status);
  if (!parseResult.success) {
    return NextResponse.json({ error: "Invalid" }, { status: 400 });
  }
  validatedStatus = parseResult.data;
}
const allIssues = await db.query.issues.findMany({
  where: validatedStatus ? eq(issues.status, validatedStatus) : undefined,
});
"""
        tool_history = [make_read_file_result(correct_code)]
        session = FakeSession(diff_summary=[])
        result = evaluate_implementation_quality(session, tool_history)
        self.assertEqual(result.quality_status, QUALITY_VALID)

    def test_outcome_not_already_implemented_when_quality_fails(self):
        """Full outcome flow: buggy code should yield partially_implemented, not already_implemented."""
        buggy_code = """
const status = searchParams.get("status");
if (status !== null) {
  const parseResult = issueStatusEnum.safeParse(status);
  if (!parseResult.success) return NextResponse.json({ error: "Invalid" }, { status: 400 });
}
const allIssues = await db.query.issues.findMany({
  where: status ? eq(issues.status, status) : undefined,
});
"""
        session = FakeSession(
            task="Add status filter",
            task_type="direct_edit",
            task_intent="implementation",
            diff_summary=[],
        )
        messages = [
            {"role": "user", "content": "Add status filter"},
            make_read_file_result(buggy_code),
            {"role": "tool", "name": "ls", "content": '{"files": ["route.ts"]}'},
            {"role": "tool", "name": "run_shell", "content": '{"exit_code": 0}'},
        ]
        verification = {"status": "skipped", "checks": []}
        result = determine_task_outcome(session, messages, verification)
        self.assertNotEqual(result.outcome, OUTCOME_ALREADY_IMPLEMENTED)
        self.assertEqual(result.outcome, OUTCOME_PARTIALLY_IMPLEMENTED)


if __name__ == "__main__":
    unittest.main()
