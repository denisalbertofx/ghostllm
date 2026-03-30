"""Editorial enforcement for read-only /plan and analysis replies."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from apps.cli.runtime.analysis_output_policy import (
    dedupe_grounding_prose,
    finalize_readonly_editorial_reply,
    soften_tier_language,
    strip_trailing_implementation_ctas,
)


class TestStripImplementationCtas(unittest.TestCase):
    def test_strips_spanish_fix_cta_paragraph(self) -> None:
        body = (
            "Hipótesis: posible condición de carrera en el handler.\n\n"
            "Evidencia: se vio un patrón similar en logs (no reproducido aquí).\n\n"
            "¿Desea que proceda con las correcciones?"
        )
        out = strip_trailing_implementation_ctas(body)
        self.assertNotIn("¿Desea", out)
        self.assertNotIn("correcciones", out.lower())
        self.assertIn("Hipótesis", out)

    def test_keeps_read_suggestion_without_fix_touch(self) -> None:
        body = "Revise `apps/cli/main.py` con read_file para confirmar la firma.\n\n¿Quiere que priorice esa ruta?"
        out = strip_trailing_implementation_ctas(body)
        self.assertIn("read_file", out)


class TestTierLanguage(unittest.TestCase):
    def test_suspected_softens_confirmado(self) -> None:
        t = "Bug confirmado en el parser; impacto crítico."
        out = soften_tier_language(t, "suspected")
        self.assertNotIn("Bug confirmado", out)
        self.assertNotIn("impacto crítico", out.lower())


class TestGroundingDedupe(unittest.TestCase):
    def test_single_primary_note(self) -> None:
        note = "No cito el fragmento porque no quedó respaldado literalmente por una lectura válida en esta sesión."
        blob = f"A.\n\n{note}\n\nB.\n\n{note}\n"
        out = dedupe_grounding_prose(blob)
        self.assertEqual(out.count(note), 1)


class TestFinalizeReadonlyEditorial(unittest.TestCase):
    def test_plan_unverified_prepends_discipline_line_when_grounding_lost(self) -> None:
        gr = SimpleNamespace(fenced_replaced=1, inline_replaced=0)
        raw = "Tabla:\n| a | b |\n| - | - |\n| 1 | 2 |\n| 3 | 4 |\n\n¿Puedo aplicar el fix ahora?"
        out = finalize_readonly_editorial_reply(raw, tier="unverified", grounding=gr, cli_mode="Plan")
        self.assertIn("Modo /plan", out)
        self.assertNotIn("¿Puedo aplicar", out)
        self.assertNotIn("fix ahora", out.lower())

    def test_confirmed_tier_no_plan_banner(self) -> None:
        gr = SimpleNamespace(fenced_replaced=1, inline_replaced=0)
        out = finalize_readonly_editorial_reply("OK.", tier="confirmed", grounding=gr, cli_mode="Plan")
        self.assertNotIn("Modo /plan", out)


if __name__ == "__main__":
    unittest.main()
