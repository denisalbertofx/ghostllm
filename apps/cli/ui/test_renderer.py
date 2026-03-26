"""Tests for renderer verification provenance - no default PASSED."""
import os
import unittest
from io import StringIO
from unittest.mock import patch

from apps.cli.ui.renderer import GhostRenderer
from apps.cli.ui.theme import make_ghost_console


class TestRendererVerificationProvenance(unittest.TestCase):
    """Renderer must not show PASSED when checks lack provenance=executed."""

    def setUp(self):
        self.output = StringIO()
        self.console = make_ghost_console(file=self.output, force_terminal=False)
        self.renderer = GhostRenderer(self.console)

    def test_empty_checks_returns_early(self):
        """Renderer fed empty checks -> returns early, no panel."""
        self.renderer.render_verification_results({"status": "success", "checks": []})
        out = self.output.getvalue()
        self.assertNotIn("PASSED", out)
        self.assertNotIn("ALL SYSTEMS CLEAR", out)

    def test_checks_without_provenance_not_passed(self):
        """Checks with status=passed but no provenance=executed -> NOT RUN, no ALL SYSTEMS CLEAR."""
        verification = {
            "status": "success",
            "steps_executed_count": 0,
            "checks": [
                {"name": "Build", "status": "passed"},  # no provenance
                {"name": "TypeCheck", "status": "passed"},  # no provenance
                {"name": "Lint", "status": "passed"},  # no provenance
            ],
        }
        self.renderer.render_verification_results(verification)
        out = self.output.getvalue()
        self.assertIn("NOT RUN", out)
        self.assertNotIn("ALL SYSTEMS CLEAR", out)

    def test_steps_executed_zero_no_success_banner(self):
        """steps_executed_count=0, all not_run -> No verification checks were actually executed."""
        verification = {
            "status": "incomplete",
            "steps_executed_count": 0,
            "checks": [
                {"name": "Build", "status": "not_run", "provenance": "inferred_not_allowed"},
                {"name": "TypeCheck", "status": "not_run", "provenance": "inferred_not_allowed"},
            ],
        }
        self.renderer.render_verification_results(verification)
        out = self.output.getvalue()
        self.assertIn("No verification checks were actually executed", out)
        self.assertNotIn("ALL SYSTEMS CLEAR", out)

    def test_mixed_passed_failed_shows_system_status_failed(self):
        """Lint PASSED + Build/TypeCheck FAILED -> global status clearly FAILED."""
        verification = {
            "status": "failed",
            "steps_executed_count": 3,
            "checks": [
                {"name": "Lint", "status": "passed", "provenance": "executed", "exit_code": 0},
                {"name": "Build", "status": "failed", "provenance": "executed", "exit_code": 1, "stderr": "compile error"},
                {"name": "TypeCheck", "status": "failed", "provenance": "executed", "exit_code": 1, "stderr": "TS error"},
            ],
        }
        self.renderer.render_verification_results(verification)
        out = self.output.getvalue()
        self.assertIn("System status: FAILED", out)
        self.assertIn("Build", out)
        self.assertIn("TypeCheck", out)
        self.assertNotIn("ALL SYSTEMS CLEAR", out)

    def test_only_typecheck_executed_build_lint_not_run(self):
        """Only TypeCheck executed+passed => Build and Lint must render as NOT RUN."""
        verification = {
            "status": "success",
            "steps_executed_count": 1,
            "checks": [
                {"name": "TypeCheck", "status": "passed", "provenance": "executed", "exit_code": 0},
                {"name": "Build", "status": "not_run", "provenance": "not_run"},
                {"name": "Lint", "status": "not_run", "provenance": "not_run"},
            ],
        }
        self.renderer.render_verification_results(verification)
        out = self.output.getvalue()
        self.assertIn("TypeCheck", out)
        self.assertIn("PASSED", out)
        self.assertIn("Build", out)
        self.assertIn("Lint", out)
        self.assertIn("NOT RUN", out)
        self.assertIn("ALL SYSTEMS CLEAR", out)

    def test_tool_segment_flushes_single_panel(self):
        """GHOST_TOOL_UI=panel: tabla con título de lote."""
        with patch.dict(os.environ, {"GHOST_TOOL_UI": "panel"}, clear=False):
            self.renderer.begin_tool_segment()
            self.renderer.append_tool_trace("ls", "app", auto_approved=False)
            self.renderer.append_tool_trace("read_file", "README.md", auto_approved=True)
            self.renderer.flush_tool_segment()
        out = self.output.getvalue()
        self.assertIn("Herramientas (lote del modelo)", out)
        self.assertIn("ls", out)
        self.assertIn("read_file", out)
        self.assertIn("README.md", out)

    def test_tool_segment_compact_default_one_line(self):
        """Por defecto: una sola línea por lote, sin panel."""
        with patch.dict(os.environ, {"GHOST_TOOL_UI": "compact"}, clear=False):
            self.renderer.begin_tool_segment()
            self.renderer.append_tool_trace("read_file", "package.json", auto_approved=False)
            self.renderer.flush_tool_segment()
        out = self.output.getvalue()
        self.assertNotIn("Herramientas (lote del modelo)", out)
        self.assertIn("read_file", out)
        self.assertIn("package.json", out)


if __name__ == "__main__":
    unittest.main()
