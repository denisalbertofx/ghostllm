"""TaskContract sealed after INTAKE: core immutable, narrow mutable buckets."""
import unittest
from unittest import mock

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.task_contract import (
    TaskContractDict,
    TaskContractMutationError,
    append_repair_run_record,
    append_verification_run_record,
    ensure_task_contract_foundation,
    seal_task_contract_after_intake,
)
from apps.cli.runtime.task_contract import Intent


class TestTaskContractImmutability(unittest.TestCase):
    def test_seal_freezes_core_top_level_setitem_blocked_logged(self):
        session = ArtifactSession("s1", "t")
        ensure_task_contract_foundation(session, Intent(mode="Chat", task="x", original_text="x"))
        tc = session.task_contract
        assert isinstance(tc, TaskContractDict)
        tc["spec"] = {"intent": "modification", "scope": ["api"]}
        tc["risk_level"] = "low"
        seal_task_contract_after_intake(session)

        with self.assertLogs("apps.cli.runtime.task_contract", level="WARNING") as cm:
            tc["risk_level"] = "high"
        self.assertTrue(any("mutation blocked" in r.getMessage() for r in cm.records))
        self.assertEqual(tc.get("risk_level"), "low")

    def test_runtime_buckets_remain_mutable_after_seal(self):
        session = ArtifactSession("s1", "t")
        ensure_task_contract_foundation(session, Intent(mode="Chat", task="x", original_text="x"))
        tc = session.task_contract
        tc["spec"] = {"intent": "review"}
        seal_task_contract_after_intake(session)
        append_verification_run_record(session, {"status": "passed"})
        append_repair_run_record(session, {"outcome": "nudge"})
        ra = tc["runtime_annotations"]
        ra["decision_plan"] = {"exploration_plan": {}, "execution_plan": {}}
        self.assertEqual(len(tc["verification_runs"]), 1)
        self.assertEqual(len(tc["repair_runs"]), 1)
        self.assertIn("decision_plan", ra)

    def test_strict_raises_on_core_mutation(self):
        session = ArtifactSession("s1", "t")
        ensure_task_contract_foundation(session, Intent(mode="Chat", task="x", original_text="x"))
        session.task_contract["spec"] = {}
        seal_task_contract_after_intake(session)
        with mock.patch.dict("os.environ", {"GHOST_TASK_CONTRACT_STRICT": "1"}):
            with self.assertRaises(TaskContractMutationError):
                session.task_contract["budget"] = {"max_tool_calls": 3}


if __name__ == "__main__":
    unittest.main()
