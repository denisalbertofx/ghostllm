"""ghost plan must not send literal 'planner' as API model (403 on server)."""
from __future__ import annotations

import os
import unittest
class TestPlanModelResolution(unittest.TestCase):
    def tearDown(self) -> None:
        for k in ("GHOST_PLANNER_MODEL",):
            os.environ.pop(k, None)

    def test_explicit_model_passthrough(self) -> None:
        from apps.cli.main import _resolve_plan_command_model

        # Normalized to upstream_id from repo configs/models.yaml
        self.assertEqual(_resolve_plan_command_model("kimi"), "moonshotai/kimi-k2.5")
        self.assertEqual(_resolve_plan_command_model("smart"), "openai/gpt-oss-120b")

    def test_planner_uses_env_upstream(self) -> None:
        from apps.cli.main import _resolve_plan_command_model

        os.environ["GHOST_PLANNER_MODEL"] = "openai/gpt-oss-120b"
        self.assertEqual(_resolve_plan_command_model("planner"), "openai/gpt-oss-120b")
        self.assertEqual(_resolve_plan_command_model("Planner"), "openai/gpt-oss-120b")

    def test_planner_fallback_kimi(self) -> None:
        from apps.cli.main import _resolve_plan_command_model

        self.assertEqual(_resolve_plan_command_model("planner"), "moonshotai/kimi-k2.5")

    def test_planner_short_upstream_id_normalized(self) -> None:
        from apps.cli.main import _resolve_plan_command_model

        os.environ["GHOST_PLANNER_MODEL"] = "kimi-k2.5"
        self.assertEqual(_resolve_plan_command_model("planner"), "moonshotai/kimi-k2.5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
