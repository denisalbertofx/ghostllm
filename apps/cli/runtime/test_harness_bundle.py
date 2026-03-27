"""Harness bundle: AGENTS tension, tree, plan formatting, diff truncation."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from apps.cli.ui import ui_contract
from apps.cli.runtime.harness_bundle import (
    build_agents_project_prompt_block,
    build_change_proposal_packet,
    format_review_packet_as_pr_body,
    build_workset_tree_lines,
    detect_agents_user_tension,
    format_operator_execution_plan,
    hydrate_agents_on_session,
    normalize_change_kind,
    readonly_analysis_truthfulness_prompt_section,
    runtime_mode_label,
    truncate_unified_diff,
    verification_plan_summary_from_spec,
)


class _Sess:
    def __init__(self) -> None:
        self.agents_md_path = ""
        self.agents_md_digest = ""
        self.agents_md_sha12 = ""
        self.agents_user_tension_note = ""


class TestHarnessBundle(unittest.TestCase):
    def test_readonly_truthfulness_section_mentions_snippets_and_suspected(self) -> None:
        s = readonly_analysis_truthfulness_prompt_section()
        self.assertIn("read_file", s)
        self.assertIn("suspected", s.lower())
        self.assertIn("verbatim", s.lower())
        self.assertIn("implementation CTAs", s)

    def test_normalize_change_kind(self) -> None:
        self.assertEqual(normalize_change_kind("Write File"), "new")
        self.assertEqual(normalize_change_kind("Edit File"), "modified")

    def test_workset_tree_groups(self) -> None:
        diff = [
            {"file": "apps/cli/a.py", "type": "Edit File"},
            {"file": "apps/server/b.py", "type": "Write File"},
        ]
        lines = build_workset_tree_lines(diff, None)
        self.assertTrue(any("apps" in ln for ln in lines))
        self.assertTrue(any("+ apps/server" in ln or "apps/server" in ln for ln in lines))

    def test_workset_tree_ascii_prefix(self) -> None:
        diff = [{"file": "pkg/x.py", "type": "Edit File"}]
        lines = build_workset_tree_lines(diff, None, ascii_safe=True)
        self.assertTrue(any(ln.startswith("> ") for ln in lines))

    def test_format_plan_merges_decision_steps(self) -> None:
        text = format_operator_execution_plan(
            "Step A",
            {"steps": ["read target", "patch", "verify"]},
        )
        self.assertIn("Step A", text)
        self.assertIn("read target", text)

    def test_verification_plan_summary_readonly(self) -> None:
        s = verification_plan_summary_from_spec({"verification_policy": {"required": False}})
        self.assertIn("no requerida", s.lower())

    def test_truncate_unified_diff(self) -> None:
        many = "\n".join(f"--- line {i}" for i in range(100))
        out, cut = truncate_unified_diff(many, max_lines=10)
        self.assertTrue(cut)
        self.assertLess(len(out), len(many))

    def test_runtime_mode_label(self) -> None:
        self.assertEqual(runtime_mode_label("analysis", "should_not_write", 0), "analysis")
        self.assertEqual(runtime_mode_label("bugfix", "must_write", 0), "bugfix")
        self.assertEqual(runtime_mode_label("implementation", "must_write", 2), "repair")

    def test_agents_tension_ui(self) -> None:
        ag = "Do not touch UI when working on API-only tasks."
        note = detect_agents_user_tension("Please change the login button in the UI", ag)
        self.assertIn("UI", note)

    def test_hydrate_and_prompt_block(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "AGENTS.md").write_text("# Rules\n\nAlways run tests.\n", encoding="utf-8")
            s = _Sess()
            hydrate_agents_on_session(s, str(root), "skip all tests forever")
            self.assertTrue(s.agents_md_path)
            self.assertIn("Always run", s.agents_md_digest)
            blk = build_agents_project_prompt_block(s)
            self.assertIn("PROJECT AGENTS.md", blk)
            self.assertIn("tests", blk.lower())

    def test_build_change_proposal_packet_shape(self) -> None:
        class S:
            session_id = "s"
            task_id = "t"
            phase = "DONE"
            task_outcome = "implemented"
            closure_reason_summary_es = ""
            closure_reason = ""
            next_action = "ship"
            diff_summary = [{"file": "z.py"}]
            verification = {"status": "passed", "steps_executed_count": 1}
            verification_scope = ""

        pkt = build_change_proposal_packet(
            S(), plan_relpath="a.md", handoff_relpath="b.json", envelope_relpath="c.json"
        )
        self.assertEqual(pkt.get("kind"), "ghost_change_proposal")
        self.assertEqual(pkt.get("format_version"), 2)
        self.assertIn("z.py", pkt.get("files_touched") or [])
        self.assertEqual(pkt.get("links", {}).get("plan"), "a.md")
        self.assertIn("open_risks", pkt)
        self.assertIsInstance(pkt.get("what_changed"), list)
        self.assertTrue(pkt.get("ready_for_review"))
        self.assertEqual(pkt.get("review_readiness"), "ready")
        body = format_review_packet_as_pr_body(pkt)
        self.assertIn(ui_contract.HARNESS_PR_H2_SUMMARY, body)
        self.assertIn(ui_contract.HARNESS_PR_H2_WHAT_CHANGED, body)
        self.assertIn(ui_contract.HARNESS_PR_H2_FILES, body)
        self.assertIn(ui_contract.HARNESS_PR_H2_VERIFICATION, body)
        self.assertIn(ui_contract.HARNESS_PR_H2_REVIEWER, body)
        self.assertIn(ui_contract.HARNESS_PR_H2_LINKS_HANDOFF, body)
        self.assertIn(ui_contract.HARNESS_PR_H2_NEXT_OPERATOR, body)
        self.assertIn("z.py", body)

    def test_build_change_proposal_not_ready_when_verify_not_on_disk(self) -> None:
        class S:
            session_id = "s"
            task_id = "t"
            phase = "DONE"
            task_outcome = "implemented"
            closure_reason_summary_es = ""
            closure_reason = ""
            next_action = "run verify"
            diff_summary = [{"file": "z.py", "type": "Edit File"}]
            verification = {
                "status": "passed",
                "steps_executed_count": 0,
                "checks": [{"name": "Tests", "status": "passed", "provenance": "inferred_not_allowed"}],
            }
            verification_scope = ""
            risks: list = []

        pkt = build_change_proposal_packet(S(), plan_relpath="a.md", handoff_relpath="b.json")
        self.assertFalse(pkt.get("ready_for_review"))
        self.assertEqual(pkt.get("review_readiness"), "verify_not_executed")
        body = format_review_packet_as_pr_body(pkt)
        self.assertIn("pendiente", body.lower())
        self.assertNotIn(ui_contract.HARNESS_PR_H2_OPEN_RISKS, body)

    def test_build_change_proposal_ready_when_check_executed_even_if_steps_zero(self) -> None:
        class S:
            session_id = "s"
            task_id = "t"
            phase = "DONE"
            task_outcome = "implemented"
            closure_reason_summary_es = ""
            closure_reason = ""
            next_action = ""
            diff_summary = [{"file": "z.py"}]
            verification = {
                "status": "passed",
                "steps_executed_count": 0,
                "checks": [{"name": "Lint", "status": "passed", "provenance": "executed"}],
            }
            verification_scope = ""
            risks: list = []

        pkt = build_change_proposal_packet(S(), plan_relpath="a.md", handoff_relpath="b.json")
        self.assertTrue(pkt.get("ready_for_review"))
        self.assertEqual(pkt.get("review_readiness"), "ready")


if __name__ == "__main__":
    unittest.main()
