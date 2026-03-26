"""ArtifactSession forensic fields: phase_transitions, promotions, terminal snapshot."""
import unittest

from apps.cli.runtime.artifacts import ArtifactSession


class TestArtifactForensic(unittest.TestCase):
    def test_phase_transition_records_from_and_iteration(self):
        s = ArtifactSession("s1", "task")
        s.record_phase_transition("intake", detail="start", from_phase="", iteration=None)
        s.record_phase_transition("explore", detail="post-intake", from_phase="intake", iteration=1)
        self.assertEqual(len(s.phase_transitions), 2)
        self.assertEqual(s.phase_transitions[1]["from"], "intake")
        self.assertEqual(s.phase_transitions[1]["to"], "explore")
        self.assertEqual(s.phase_transitions[1]["iteration"], 1)
        self.assertIn("ts", s.phase_transitions[1])
        d = s.to_dict()
        self.assertEqual(len(d["phase_transitions"]), 2)
        self.assertIn("terminal_resolution_record", d)

    def test_promotion_decision_structure(self):
        s = ArtifactSession("s1", "task")
        s.append_promotion_decision(
            kind="explore_to_act",
            reason_code="discovery_cap_reached",
            from_phase="explore",
            to_phase="act",
            iteration=2,
            extra={"when": "text_only"},
        )
        row = s.promotion_decisions[0]
        self.assertEqual(row["kind"], "explore_to_act")
        self.assertEqual(row["reason_code"], "discovery_cap_reached")
        self.assertEqual(row["iteration"], 2)
        self.assertIn("ts", row)


if __name__ == "__main__":
    unittest.main()
