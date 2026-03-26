"""Micro-task detection (RO-2 / RO-3 style narrow prompts)."""
from __future__ import annotations

import unittest

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.micro_task_mode import (
    TASK_MODE_MICRO_TASK,
    TASK_MODE_STANDARD,
    detect_micro_task_kind,
    micro_task_behavior_matrix,
    micro_task_mode_enabled,
    micro_task_system_prompt_section,
    resolve_task_mode,
)


class TestMicroTaskMode(unittest.TestCase):
    def test_ro3_shallow_list_plan(self) -> None:
        t = (
            "/plan Lista los .py directamente dentro de apps/cli/runtime/adapters "
            "(solo primer nivel, sin subcarpetas). Devuelve solo la lista de nombres de archivo, una por línea."
        )
        k = detect_micro_task_kind(t, explore_act_blocked=True, contract_spec=None)
        self.assertEqual(k, "shallow_list")

    def test_ro3_blocked_when_readonly_exit_off(self) -> None:
        t = (
            "Lista los .py directamente dentro de apps/cli/runtime/adapters "
            "(solo primer nivel, sin subcarpetas)."
        )
        k = detect_micro_task_kind(
            t,
            explore_act_blocked=True,
            contract_spec={"readonly_early_exit": False},
        )
        self.assertIsNone(k)

    def test_broad_task_not_micro(self) -> None:
        t = "Redesign the entire codebase architecture and list every module under apps/"
        self.assertIsNone(
            detect_micro_task_kind(t, explore_act_blocked=False, contract_spec=None),
        )

    def test_narrow_factual(self) -> None:
        t = "¿Qué hace la función seal_task_contract_after_intake y en qué archivo está?"
        k = detect_micro_task_kind(t, explore_act_blocked=False, contract_spec=None)
        self.assertEqual(k, "narrow_factual")

    def test_ro1_style_not_shallow_listing(self) -> None:
        t = (
            "En menos de 12 líneas, lista los archivos bajo apps/cli/runtime "
            'que contengan "verification" en el nombre'
        )
        self.assertIsNone(detect_micro_task_kind(t, explore_act_blocked=True, contract_spec=None))

    def test_prompt_section_nonempty(self) -> None:
        self.assertIn("Micro-task", micro_task_system_prompt_section("shallow_list"))
        self.assertEqual(micro_task_system_prompt_section(""), "")

    def test_enabled_default(self) -> None:
        self.assertTrue(micro_task_mode_enabled())

    def test_resolve_task_mode(self) -> None:
        self.assertEqual(
            resolve_task_mode(micro_task_kind="shallow_list", mode_enabled=True),
            TASK_MODE_MICRO_TASK,
        )
        self.assertEqual(
            resolve_task_mode(micro_task_kind=None, mode_enabled=True),
            TASK_MODE_STANDARD,
        )
        self.assertEqual(
            resolve_task_mode(micro_task_kind="shallow_list", mode_enabled=False),
            TASK_MODE_STANDARD,
        )
        self.assertEqual(TASK_MODE_STANDARD, "standard")
        self.assertEqual(TASK_MODE_MICRO_TASK, "micro_task")

    def test_behavior_matrix_stable(self) -> None:
        rows = micro_task_behavior_matrix()
        self.assertGreaterEqual(len(rows), 4)
        self.assertTrue(any("Iteration cap" in r["aspect"] for r in rows))

    def test_artifact_json_exports_task_mode(self) -> None:
        s = ArtifactSession("sid", "hello")
        s.task_mode = TASK_MODE_MICRO_TASK
        s.micro_task_kind = "shallow_list"
        d = s.to_dict()
        self.assertEqual(d.get("task_mode"), "micro_task")
        self.assertEqual(d.get("micro_task_kind"), "shallow_list")

    def test_repo_overview(self) -> None:
        t = "/plan listame todo los archivos de esta app y dime el stack tecnologico cual es"
        k = detect_micro_task_kind(t, explore_act_blocked=True, contract_spec=None)
        self.assertEqual(k, "repo_overview")


if __name__ == "__main__":
    unittest.main(verbosity=2)
