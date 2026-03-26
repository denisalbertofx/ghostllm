"""StagnationDetector: no arrastrar historial entre tareas simuladas."""
import unittest

from apps.cli.runtime.autonomy import StagnationDetector


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

    def test_consecutive_reads_reset(self) -> None:
        d = StagnationDetector(max_idle_reads=4)
        for _ in range(4):
            d.add_action("read_file", "same.ts")
        self.assertTrue(d.check_stagnation()[0])
        d.reset()
        self.assertFalse(d.check_stagnation()[0])


if __name__ == "__main__":
    unittest.main()
