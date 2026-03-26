"""
Architecture invariant suite: session phase machine, terminal authority, verification reuse,
TaskContract sealing, and artifact coherence.

Fails on regressions such as literal DONE/ABORTED in the assistant loop, REPAIR → DONE,
or skipping VERIFY when diffs exist without skip_verify.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.phase_invariants import (
    assert_diff_requires_verify_phase_visited,
    assert_phase_history_matches_transitions,
    assert_phase_transition_chain_coherent,
    assert_repair_outgoing_edges_valid,
    assert_terminal_record_matches_session_phase,
    assistant_source_must_not_apply_literal_terminal_phases,
    cli_production_code_task_outcome_assignments,
    load_assistant_source,
    phase_edges_from_transitions,
    simulate_phase_path,
)
from apps.cli.runtime.session_phase import LOOP_ABORT_MAX_ITERATIONS, SessionPhase
from apps.cli.runtime.task_contract import (
    TaskContractDict,
    TaskContractMutationError,
    ensure_task_contract_foundation,
    seal_task_contract_after_intake,
)
from apps.cli.runtime.task_contract import Intent
from apps.cli.runtime.outcome_engine import OUTCOME_READ_ONLY
from apps.cli.runtime.terminal_resolution import resolve_terminal_result, terminal_phase_from_signals
from apps.cli.runtime.verification_coordinator import (
    VerificationCoordinator,
    verification_diff_fingerprint,
    verification_fingerprint_eligible_for_reuse,
)
from apps.cli.runtime.verification import VerificationManager


REPO_ROOT = Path(__file__).resolve().parents[1]


class TestAssistantTerminalAuthority(unittest.TestCase):
    def test_no_literal_done_aborted_in_apply_session_phase(self) -> None:
        src = load_assistant_source(REPO_ROOT)
        assistant_source_must_not_apply_literal_terminal_phases(src)

    def test_terminal_resolution_record_assigned_once_in_postamble(self) -> None:
        src = load_assistant_source(REPO_ROOT)
        assigns = len(re.findall(r"terminal_resolution_record\s*=", src))
        self.assertEqual(
            assigns,
            1,
            "terminal_resolution_record should be set in a single authoritative block (postamble)",
        )

    def test_task_outcome_set_only_via_terminal_resolution_helper(self) -> None:
        src = load_assistant_source(REPO_ROOT)
        # Allow exactly one assignment in _apply_terminal_resolution
        self.assertEqual(
            src.count("session.task_outcome = tr.outcome"),
            1,
            "task_outcome must only be set from TerminalResolution in _apply_terminal_resolution",
        )

    def test_cli_production_code_only_one_task_outcome_assignment(self) -> None:
        hits = cli_production_code_task_outcome_assignments(REPO_ROOT)
        self.assertEqual(
            len(hits),
            1,
            f"unexpected task_outcome writers in apps/cli: {hits!r}",
        )
        p0 = hits[0][0].resolve()
        self.assertEqual(p0.name, "assistant.py", p0)
        self.assertIn("cli", p0.parts, p0)


class TestResolveTerminalResultBlackBox(unittest.TestCase):
    def test_resolve_yields_only_done_or_aborted_phase(self) -> None:
        sess = ArtifactSession("s", "task")
        sess.diff_summary = [{"file": "a.py", "type": "edit"}]
        sess.verification = {"status": "success", "checks": [], "steps_executed_count": 1}
        tr = resolve_terminal_result(
            sess,
            sess.verification,
            history=[],
            budget_status="ok",
            provider_status="ok",
            policy_status="ok",
            session_failed=False,
            loop_abort_reason=None,
        )
        self.assertIn(tr.phase, (SessionPhase.DONE, SessionPhase.ABORTED))

    def test_terminal_phase_from_signals_never_closing(self) -> None:
        ph = terminal_phase_from_signals(
            budget_exhausted=False,
            session_failed=False,
            outcome="implemented",
            loop_abort_reason=None,
        )
        self.assertNotEqual(ph, SessionPhase.CLOSING)

    def test_resolve_max_iterations_read_only_with_final_answer_is_done(self) -> None:
        sess = ArtifactSession("s", "task")
        sess.diff_summary = []
        sess.task_intent = "analysis"
        hist = [
            {"role": "user", "content": "list files in foo"},
            {"role": "assistant", "content": "Here is the list:\n- a.py"},
        ]
        tr = resolve_terminal_result(
            sess,
            {"status": "skipped", "checks": []},
            history=hist,
            budget_status="ok",
            provider_status="ok",
            policy_status="ok",
            session_failed=False,
            loop_abort_reason=LOOP_ABORT_MAX_ITERATIONS,
        )
        self.assertEqual(tr.outcome, OUTCOME_READ_ONLY)
        self.assertEqual(tr.phase, SessionPhase.DONE)


class TestRepairPhaseEdges(unittest.TestCase):
    def test_valid_repair_paths(self) -> None:
        rows = simulate_phase_path(
            [
                ("", "INTAKE"),
                ("INTAKE", "EXPLORE"),
                ("EXPLORE", "ACT"),
                ("ACT", "VERIFY"),
                ("VERIFY", "REPAIR"),
                ("REPAIR", "ACT"),
                ("ACT", "VERIFY"),
                ("VERIFY", "CLOSING"),
            ]
        )
        assert_repair_outgoing_edges_valid(phase_edges_from_transitions(rows))

    def test_repair_to_done_forbidden(self) -> None:
        rows = simulate_phase_path([("VERIFY", "REPAIR"), ("REPAIR", "DONE")])
        with self.assertRaises(AssertionError):
            assert_repair_outgoing_edges_valid(phase_edges_from_transitions(rows))

    def test_repair_to_verify_forbidden(self) -> None:
        rows = simulate_phase_path([("VERIFY", "REPAIR"), ("REPAIR", "VERIFY")])
        with self.assertRaises(AssertionError):
            assert_repair_outgoing_edges_valid(phase_edges_from_transitions(rows))


class TestActVerifyObligation(unittest.TestCase):
    def test_diff_without_skip_requires_verify_visited(self) -> None:
        tc = TaskContractDict({"skip_verify": False, "version": 1})
        rows = simulate_phase_path([("ACT", "CLOSING")])
        with self.assertRaises(AssertionError):
            assert_diff_requires_verify_phase_visited(
                diff_summary=[{"file": "x"}],
                phase_transitions=rows,
                task_contract=tc,
            )

    def test_skip_verify_allows_no_verify_phase(self) -> None:
        tc = TaskContractDict({"skip_verify": True, "version": 1})
        rows = simulate_phase_path([("ACT", "CLOSING")])
        assert_diff_requires_verify_phase_visited(
            diff_summary=[{"file": "x"}],
            phase_transitions=rows,
            task_contract=tc,
        )


class TestVerificationReuseInvariants(unittest.TestCase):
    def setUp(self) -> None:
        self.coord = VerificationCoordinator(VerificationManager(str(REPO_ROOT)))

    def test_reuse_only_when_fingerprints_eligible_and_equal(self) -> None:
        diff = [
            {
                "file": "src/a.ts",
                "type": "write",
                "status": "changed",
                "content_sha256": "abc" * 20,
            }
        ]
        fp = verification_diff_fingerprint(diff)
        self.assertTrue(verification_fingerprint_eligible_for_reuse(fp))

        class S:
            verification_diff_fingerprint = fp
            verification = {"status": "success", "checks": [], "steps_executed_count": 2}
            verification_reuse_decisions: list = []

        s = S()
        out = self.coord.try_reuse_stored_verification(s, list(diff), verification_completed=True)
        self.assertIsNotNone(out)

    def test_reuse_rejected_when_new_fingerprint_incomplete(self) -> None:
        good = [
            {
                "file": "a.ts",
                "type": "write",
                "status": "x",
                "content_sha256": "x" * 64,
            }
        ]
        bad = [{"file": "a.ts", "type": "write"}]
        fp_store = verification_diff_fingerprint(good)

        class S:
            verification_diff_fingerprint = fp_store
            verification = {"status": "success", "checks": [], "steps_executed_count": 1}
            verification_reuse_decisions: list = []

        s = S()
        self.assertIsNone(
            self.coord.try_reuse_stored_verification(s, bad, verification_completed=True)
        )


class TestTaskContractSealInvariant(unittest.TestCase):
    def test_core_mutation_strict_after_seal(self) -> None:
        session = ArtifactSession("s", "t")
        ensure_task_contract_foundation(session, Intent(mode="Chat", task="x", original_text="x"))
        tc = session.task_contract
        assert isinstance(tc, TaskContractDict)
        tc["spec"] = {"intent": "fix"}
        seal_task_contract_after_intake(session)
        with mock.patch.dict("os.environ", {"GHOST_TASK_CONTRACT_STRICT": "1"}):
            with self.assertRaises(TaskContractMutationError):
                tc["risk_level"] = "high"


class TestArtifactPhaseCoherence(unittest.TestCase):
    def test_history_and_transitions_aligned(self) -> None:
        s = ArtifactSession("s", "t")
        s.record_phase_transition("INTAKE", detail="a", from_phase="")
        s.record_phase_transition("EXPLORE", detail="b", from_phase="INTAKE")
        assert_phase_history_matches_transitions(s)
        assert_phase_transition_chain_coherent(s.phase_transitions)

    def test_terminal_record_matches_phase(self) -> None:
        s = ArtifactSession("s", "t")
        s.phase = "DONE"
        s.terminal_resolution_record = {"terminal_phase": "DONE", "outcome": "implemented"}
        assert_terminal_record_matches_session_phase(s)

    def test_terminal_record_mismatch_raises(self) -> None:
        s = ArtifactSession("s", "t")
        s.phase = "DONE"
        s.terminal_resolution_record = {"terminal_phase": "ABORTED"}
        with self.assertRaises(AssertionError):
            assert_terminal_record_matches_session_phase(s)


if __name__ == "__main__":
    unittest.main()
