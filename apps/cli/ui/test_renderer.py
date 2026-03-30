"""Tests for renderer verification provenance - no default PASSED."""
import os
import unittest
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from apps.cli.ui import ui_contract
from apps.cli.ui.renderer import (
    GhostRenderer,
    format_live_phase_rail,
    format_phase_rail,
    format_tool_live_hint_from_prepared,
    summarize_test_runner_output,
)
from apps.cli.ui.slash_menu import (
    slash_menu_apply_selection,
    slash_menu_examples,
    slash_menu_group_label,
    slash_menu_quick_actions,
    slash_menu_start_suggestions,
    slash_menu_state,
)
from apps.cli.ui.theme import make_ghost_console
from apps.cli.ui.visual_layout import build_ghost_visual_layout, format_live_phase_rail


class TestPhaseRailAndChips(unittest.TestCase):
    def test_slash_menu_root_shows_core_commands(self):
        state = slash_menu_state("/")
        cmds = [it.command for it in state.items]
        self.assertTrue(state.active)
        self.assertIn("/plan", cmds)
        self.assertIn("/do", cmds)
        self.assertIn("/fix", cmds)

    def test_slash_menu_filters_fix(self):
        state = slash_menu_state("/fi")
        self.assertTrue(state.active)
        self.assertTrue(state.items)
        self.assertEqual(state.items[0].command, "/fix")

    def test_slash_menu_apply_selection_adds_space_for_argument_commands(self):
        state = slash_menu_state("/pl")
        updated = slash_menu_apply_selection("/pl", state.items[0])
        self.assertEqual(updated, "/plan ")

    def test_slash_menu_quick_actions_contract(self):
        self.assertEqual(slash_menu_quick_actions(), ["/plan", "/do", "/fix", "/doctor"])

    def test_slash_menu_group_and_examples_contract(self):
        state = slash_menu_state("/")
        self.assertEqual(slash_menu_group_label(state.items[0]), "EXPLORAR")
        examples = slash_menu_examples(state.items, limit=2)
        self.assertTrue(examples)
        self.assertIn("/plan", examples[0])

    def test_slash_menu_start_suggestions_contract(self):
        rows = slash_menu_start_suggestions()
        self.assertEqual(rows[0][0], "/plan")
        self.assertEqual(rows[1][0], "/do")

    def test_slash_menu_recent_commands_are_promoted(self):
        state = slash_menu_state("/", recent_commands=["/fix", "/do"])
        cmds = [it.command for it in state.items[:2]]
        cats = [it.category for it in state.items[:2]]
        self.assertEqual(cmds, ["/fix", "/do"])
        self.assertEqual(cats, ["reciente", "reciente"])

    def test_slash_menu_recommended_commands_rank_first(self):
        state = slash_menu_state("/", recommended_commands=["/review", "/fix"])
        cmds = [it.command for it in state.items[:2]]
        cats = [it.category for it in state.items[:2]]
        self.assertEqual(cmds, ["/review", "/fix"])
        self.assertEqual(cats, ["recomendado", "recomendado"])

    def test_phase_rail_marks_verify_bucket(self):
        r = format_phase_rail("VERIFY")
        self.assertIn(ui_contract.PHASE_RAIL_LABEL_VERIFY, r)
        self.assertIn(ui_contract.PHASE_RAIL_LABEL_GATHER, r)
        self.assertTrue(ui_contract.live_rail_shows_current_marker(r))
        self.assertTrue(
            ui_contract.live_rail_has_step_labels(
                r,
                ui_contract.PHASE_RAIL_LABEL_GATHER,
                ui_contract.PHASE_RAIL_LABEL_PLAN,
                ui_contract.PHASE_RAIL_LABEL_ACT,
                ui_contract.PHASE_RAIL_LABEL_VERIFY,
            )
        )

    def test_live_rail_plan_profile_three_steps(self):
        r = format_live_phase_rail("EXPLORE", ui_contract.LIVE_RAIL_PROFILE_READONLY)
        self.assertIn(ui_contract.PHASE_RAIL_LABEL_GATHER, r)
        self.assertIn(ui_contract.PHASE_RAIL_LABEL_REVIEW, r)
        self.assertNotIn(ui_contract.PHASE_RAIL_LABEL_ACT, r)
        r_act = format_live_phase_rail("ACT", ui_contract.LIVE_RAIL_PROFILE_READONLY)
        self.assertIn(ui_contract.PHASE_RAIL_LABEL_REVIEW, r_act)
        self.assertTrue(ui_contract.live_rail_shows_current_marker(r_act))

    def test_format_tool_live_hint_read_file(self):
        h = format_tool_live_hint_from_prepared(
            [{"name": "read_file", "args": {"path": "apps/server/main.py"}}]
        )
        self.assertIn(ui_contract.LIVE_ACTION_READING, h)
        self.assertIn("main.py", h)

    def test_update_status_skips_duplicate_spinner_message(self):
        class _St:
            def __init__(self) -> None:
                self.n = 0

            def update(self, _msg: str) -> None:
                self.n += 1

        buf = StringIO()
        r = GhostRenderer(make_ghost_console(file=buf, force_terminal=False))
        st = _St()
        r.update_status(st, "thinking", phase="EXPLORE")
        r.update_status(st, "thinking", phase="EXPLORE")
        self.assertEqual(st.n, 1)
        r.update_status(st, "building", phase="EXPLORE")
        self.assertEqual(st.n, 2)

    def test_tool_chip_shows_duration(self):
        buf = StringIO()
        r = GhostRenderer(make_ghost_console(file=buf, force_terminal=False))
        r.append_tool_trace("read_file", "x.py", True, duration_ms=42.0)
        out = buf.getvalue()
        self.assertIn("42ms", out)
        self.assertIn("read", out)


class TestArtifactSummaryFindingsTier(unittest.TestCase):
    """Cierre read-only: revisión unificada (operador) vs paneles detallados (verbose)."""

    def setUp(self):
        self.output = StringIO()
        self.console = make_ghost_console(file=self.output, force_terminal=False)
        self.renderer = GhostRenderer(self.console)

    def test_operator_mode_review_panel_findings_first(self):
        art = {
            "session_id": "x",
            "timestamp": "",
            "task": "/plan bug scan",
            "harness_mode_label": "analysis",
            "task_outcome": "read_only",
            "closure_operator_view": {"primary_headline_es": "Inspección completada"},
            "diff_summary": [],
            "taskspec": {"intent": "analysis", "change_expectation": "should_not_write"},
            "findings_evidence_tier": "unverified",
            "analysis_grounding_digest": "fenced_snippet_ungrounded:1",
            "evidence_lines": ["read_file devolvió 120 líneas de foo.py"],
        }
        with patch.dict(os.environ, {"GHOST_UI_VERBOSE": "0"}, clear=False):
            self.renderer.render_artifact_summary(art)
        out = self.output.getvalue()
        self.assertTrue(ui_contract.output_contains_review_panel(out))
        self.assertIn(ui_contract.TIER_SUBSTRING_SIN_VERIFICAR, out)
        self.assertIn("foo.py", out)
        self.assertNotIn(ui_contract.DEBUG_GROUNDING_PREFIX, out)
        self.assertNotIn(ui_contract.PANEL_TITLE_CONTRACT_SPEC, out)

    def test_verbose_mode_shows_grounding_and_evidence_panels(self):
        art = {
            "session_id": "x",
            "timestamp": "",
            "task": "/plan bug scan",
            "harness_mode_label": "analysis",
            "task_outcome": "read_only",
            "closure_operator_view": {},
            "diff_summary": [],
            "taskspec": {"intent": "analysis", "change_expectation": "should_not_write"},
            "findings_evidence_tier": "unverified",
            "analysis_grounding_digest": "fenced_snippet_ungrounded:1",
            "evidence_lines": ["Findings tier: unverified — inspección superficial.", "tool: ls"],
        }
        with patch.dict(os.environ, {"GHOST_UI_VERBOSE": "1"}, clear=False):
            self.renderer.render_artifact_summary(art)
        out = self.output.getvalue()
        self.assertIn(ui_contract.DEBUG_GROUNDING_PREFIX, out)
        self.assertIn("fenced_snippet_ungrounded", out)
        self.assertIn(ui_contract.PANEL_TITLE_EVIDENCIA_HERRAMIENTAS, out)

    def test_operator_artifact_footer_uses_compact_marker(self):
        art = {
            "session_id": "x",
            "timestamp": "",
            "task": "t",
            "harness_mode_label": "implementation",
            "task_outcome": "implemented",
            "closure_operator_view": {"primary_headline_es": "OK"},
            "diff_summary": [{"file": "a.py", "type": "Edit File"}],
            "taskspec": {"intent": "implementation", "change_expectation": "may_write"},
            "trace_path": ".ghost/traces/x.json",
            "artifacts_json_path": ".ghost/artifacts/x.json",
        }
        with patch.dict(os.environ, {"GHOST_UI_VERBOSE": "0"}, clear=False):
            self.renderer.render_artifact_summary(art)
        out = self.output.getvalue()
        self.assertTrue(ui_contract.output_contains_artifact_footer_line(out))

    def test_readonly_artifact_summary_can_skip_review_panel_once(self):
        art = {
            "session_id": "x",
            "timestamp": "",
            "task": "/plan bug scan",
            "harness_mode_label": "analysis",
            "task_outcome": "read_only",
            "closure_operator_view": {"primary_headline_es": "Hallazgos preliminares."},
            "findings_evidence_tier": "unverified",
            "evidence_lines": ["Verification: no checks executed"],
            "review_packet_path": ".ghost/review_packets/x.json",
            "artifact_path": ".ghost/artifacts/x.md",
            "plan_path": ".ghost/plans/x.md",
        }
        self.renderer._suppress_next_readonly_review_panel = True
        with patch.dict(os.environ, {"GHOST_UI_VERBOSE": "0"}, clear=False):
            self.renderer.render_artifact_summary(art)
        out = self.output.getvalue()
        self.assertFalse(ui_contract.output_contains_review_panel(out))
        self.assertTrue(ui_contract.output_contains_artifact_footer_line(out))

    def test_startup_summary_shows_quick_actions_hint(self):
        with patch.object(
            self.renderer,
            "_workspace_context",
            return_value=("GhostLLM · branch main · files 847 · last session 2h ago", "GhostLLM"),
        ):
            self.renderer.render_cli_startup_summary(
                command_mode="dev",
                assistant_mode="Chat",
                model="coder",
                prep=None,
                llm_gateway_url="http://127.0.0.1:8000",
                llm_gateway_reachable=True,
                provider_backend_label="openai_compatible",
                response_mode_label="streaming",
                auto_approve=False,
                approval_mode_label="approval-first",
            )
        out = self.output.getvalue()
        normalized = out.replace("\n", " ")
        self.assertIn("mode", out.lower())
        self.assertIn("next:", out.lower())
        self.assertIn("/plan", out)
        self.assertIn("quick", out.lower())
        self.assertIn("escribe `/`", out)
        self.assertIn("workspace:", out.lower())
        self.assertIn("runtime:", out.lower())
        self.assertIn("permissions:", out.lower())
        self.assertIn("approval mode:", out.lower())
        self.assertIn("approval-first", out.lower())
        self.assertIn("streaming", out.lower())
        self.assertIn("branch main", out)
        self.assertIn("last", normalized)
        self.assertIn("session 2h ago", normalized)
        self.assertNotIn("C:\\repo", out)
        self.assertNotIn("Chat │ coder │ dev", out)

    def test_renderer_history_navigation_helper(self):
        self.renderer._record_input_history("/plan bugs")
        self.renderer._record_input_history("/do fix startup")
        idx, text = self.renderer._move_history(None, -1)
        self.assertEqual(text, "/do fix startup")
        idx, text = self.renderer._move_history(idx, -1)
        self.assertEqual(text, "/plan bugs")
        idx, text = self.renderer._move_history(idx, 1)
        self.assertEqual(text, "/do fix startup")

    def test_renderer_recent_slash_dedupes_and_keeps_latest_first(self):
        self.renderer._record_recent_slash("/plan")
        self.renderer._record_recent_slash("/do")
        self.renderer._record_recent_slash("/plan")
        self.assertEqual(list(self.renderer._slash_recent), ["/plan", "/do"])

    def test_renderer_recommended_actions_follow_phase(self):
        self.renderer._live_rail_profile = ui_contract.LIVE_RAIL_PROFILE_IMPLEMENT
        self.renderer._last_status_phase = "VERIFY"
        self.assertEqual(self.renderer._recommended_actions(), ["/fix", "/review", "/do"])

    def test_prompt_toolbar_mentions_multiline_and_recommended(self):
        self.renderer._live_rail_profile = ui_contract.LIVE_RAIL_PROFILE_READONLY
        self.renderer._last_status_phase = "EXPLORE"
        bar = self.renderer._prompt_toolkit_bottom_toolbar()
        self.assertIn("plan mode", bar)
        self.assertIn("/plan", bar)
        self.assertIn("F2", bar)
        self.assertIn("read", bar)

    def test_windows_slash_menu_disabled_without_vt_markers(self):
        renderer = GhostRenderer(make_ghost_console(file=StringIO(), force_terminal=False))
        renderer.console = SimpleNamespace(is_terminal=True, legacy_windows=False)

        class _TTY:
            def isatty(self) -> bool:
                return True

        with patch("apps.cli.ui.renderer.os.name", "nt"), patch(
            "apps.cli.ui.renderer.sys.stdout",
            _TTY(),
        ), patch.dict(
            os.environ,
            {"GHOST_INPUT_MENU": "auto"},
            clear=False,
        ):
            for key in ("WT_SESSION", "ANSICON", "ConEmuANSI", "TERM_PROGRAM", "TERM"):
                os.environ.pop(key, None)
            self.assertFalse(renderer._supports_windows_slash_menu())

    def test_windows_slash_menu_enabled_with_windows_terminal_marker(self):
        renderer = GhostRenderer(make_ghost_console(file=StringIO(), force_terminal=False))
        renderer.console = SimpleNamespace(is_terminal=True, legacy_windows=False)

        class _TTY:
            def isatty(self) -> bool:
                return True

        with patch("apps.cli.ui.renderer.os.name", "nt"), patch(
            "apps.cli.ui.renderer.sys.stdout",
            _TTY(),
        ), patch.dict(
            os.environ,
            {"GHOST_INPUT_MENU": "auto", "WT_SESSION": "1"},
            clear=False,
        ):
            self.assertTrue(renderer._supports_windows_slash_menu())

    def test_render_swarm_board_hides_absolute_worktree_path(self):
        class _Worker:
            worker_id = "worker_1234"
            role = "coder"
            task_id = "task_abc"
            status = "active"
            worktree_path = "C:\\Users\\denis\\OneDrive\\Escritorio\\GhostLLM\\.ghost\\worktrees\\worker_1234"

        self.renderer.render_swarm_board([_Worker()])
        out = self.output.getvalue()
        self.assertIn("SWARM", out)
        self.assertIn("worker_1234", out)
        self.assertNotIn("C:\\Users\\denis\\OneDrive\\Escritorio\\GhostLLM", out)

    def test_display_phase_for_rail_distinguishes_gather_and_plan(self):
        self.renderer._live_rail_profile = ui_contract.LIVE_RAIL_PROFILE_IMPLEMENT
        self.assertEqual(self.renderer._display_phase_for_rail("EXPLORE", "tool_exec"), "GATHER")
        self.assertEqual(self.renderer._display_phase_for_rail("EXPLORE", "thinking"), "PLAN")

    def test_readonly_plan_response_uses_plan_mode_panel_and_next(self):
        self.renderer.render_readonly_plan_response(
            """
## Plan de migracion

JWT migration is feasible. No breaking changes expected.

1. Install jsonwebtoken
2. Create JWTService.ts
3. Update auth middleware
""",
            tier="suspected",
            evidence_count=8,
            next_command="/do migrate auth to jwt",
        )
        out = self.output.getvalue()
        self.assertIn(ui_contract.PANEL_TITLE_PLAN_MODE, out)
        self.assertIn("JWT migration is feasible", out)
        self.assertIn("Install jsonwebtoken", out)
        self.assertIn("/do migrate auth to jwt", out)
        self.assertIn("execute", out)
        self.assertNotIn("execute next:", out)

    def test_readonly_plan_response_filters_meta_explore_summary(self):
        self.renderer.render_readonly_plan_response(
            """
Voy a analizar el código buscando bugs críticos. Empezaré buscando patrones problemáticos comunes y comentarios de TODO.
""",
            tier="unverified",
            evidence_count=1,
            next_command="/do encuentra los bugs mas importantes",
        )
        out = self.output.getvalue()
        self.assertNotIn("Voy a analizar el código", out)
        self.assertIn("Hay contexto para orientar el siguiente paso", out)

    def test_readonly_plan_response_prefers_structured_sections(self):
        self.renderer.render_readonly_plan_response(
            """
Conclusion: JWT migration is feasible without breaking current auth flows.
Findings:
1. Current middleware is still session-based.
2. Token validation is concentrated in AuthService.ts.
Steps:
1. Add JWTService.ts
2. Update auth middleware
Evidence:
apps/server/auth/middleware.py
apps/server/auth/security.py
Next: /do migrate auth to jwt
""",
            tier="suspected",
            evidence_count=2,
            next_command="/do migrate auth to jwt",
        )
        out = self.output.getvalue()
        self.assertIn("findings", out)
        self.assertIn("Current middleware is still session-based", out)
        self.assertIn("JWTService.ts", out)
        self.assertIn("middleware.py", out)

    def test_readonly_plan_response_filters_intermediate_audit_lines(self):
        self.renderer.render_readonly_plan_response(
            """
Conclusion: Identificando posibles problemas en apps/cli/main.py
Steps:
1. apps/cli/main.py:
2. Validar todas las entradas de usuario antes de pasarlas a funciones internas.
3. Asegurar que las rutas de archivo sean verificadas para prevenir la inyección de rutas.
""",
            tier="suspected",
            evidence_count=3,
            next_command="/do encuentra los bugs mas importantes",
        )
        out = self.output.getvalue()
        self.assertNotIn("Identificando posibles problemas", out)
        self.assertNotIn("apps/cli/main.py:", out)
        self.assertIn("Validar todas las entradas de usuario", out)

    def test_compact_tool_segment_aggregates_read_batches(self):
        with patch.dict(os.environ, {"GHOST_TOOL_UI": "compact"}, clear=False):
            self.renderer.begin_tool_segment()
            self.renderer.append_tool_trace("read_file", "apps/server/main.py", True, duration_ms=18)
            self.renderer.append_tool_trace("read_file", "apps/cli/main.py", True, duration_ms=21)
            self.renderer.append_tool_trace("search_code", "validateToken", True, duration_ms=11)
            self.renderer.flush_tool_segment()
        out = self.output.getvalue()
        self.assertIn("reads", out)
        self.assertIn("lote de lectura", out)
        self.assertNotIn("apps/server/main.py", out)

    def test_implementation_closure_ready_for_review_banner(self):
        art = {
            "session_id": "x",
            "timestamp": "",
            "task": "t",
            "harness_mode_label": "implementation",
            "task_outcome": "implemented",
            "closure_operator_view": {"primary_headline_es": "Listo"},
            "diff_summary": [{"file": "a.py", "type": "Edit File"}],
            "taskspec": {"intent": "implementation", "change_expectation": "may_write"},
            "review_ready_for_review": True,
            "review_readiness_detail_es": "Cambios registrados y comprobaciones ejecutadas en disco.",
            "review_packet_path": ".ghost/review_packets/x.json",
            "trace_path": ".ghost/traces/x.json",
        }
        with patch.dict(os.environ, {"GHOST_UI_VERBOSE": "0"}, clear=False):
            self.renderer.render_artifact_summary(art)
        out = self.output.getvalue()
        self.assertIn(ui_contract.CLOSURE_REVIEW_STATUS_READY, out)
        self.assertIn(ui_contract.CLOSURE_DIFF_REVIEW_CAPTION, out)
        rp_p = ".ghost/review_packets/x.json"
        tr_p = ".ghost/traces/x.json"
        self.assertIn(rp_p, out)
        self.assertIn(tr_p, out)
        self.assertLess(out.find(rp_p), out.find(tr_p))

    def test_implementation_closure_pending_review_when_not_ready(self):
        art = {
            "session_id": "x",
            "timestamp": "",
            "task": "t",
            "harness_mode_label": "implementation",
            "task_outcome": "implemented",
            "closure_operator_view": {"primary_headline_es": "Casi"},
            "diff_summary": [{"file": "a.py", "type": "Edit File"}],
            "taskspec": {"intent": "implementation", "change_expectation": "may_write"},
            "review_ready_for_review": False,
            "review_readiness_detail_es": "Verify incompleto.",
        }
        with patch.dict(os.environ, {"GHOST_UI_VERBOSE": "0"}, clear=False):
            self.renderer.render_artifact_summary(art)
        out = self.output.getvalue()
        self.assertIn(ui_contract.CLOSURE_REVIEW_STATUS_PENDING, out)
        self.assertIn("Verify incompleto", out)


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
        self.assertNotIn(ui_contract.VERIFY_MSG_ALL_SYSTEMS_CLEAR, out)

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
        self.assertIn(ui_contract.VERIFY_CHECK_DISPLAY_NOT_RUN, out)
        self.assertNotIn(ui_contract.VERIFY_MSG_ALL_SYSTEMS_CLEAR, out)
        self.assertTrue(ui_contract.output_contains_verify_brand_or_rule(out))

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
        self.assertIn(ui_contract.VERIFY_MSG_NO_CHECKS_ON_DISK, out)
        self.assertNotIn(ui_contract.VERIFY_MSG_ALL_SYSTEMS_CLEAR, out)

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
        self.assertIn(ui_contract.RESULTADO_LINE_FAILED, out)
        self.assertIn("Build", out)
        self.assertIn("TypeCheck", out)
        self.assertNotIn(ui_contract.VERIFY_MSG_ALL_SYSTEMS_CLEAR, out)

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
        self.assertIn(ui_contract.VERIFY_CHECK_DISPLAY_NOT_RUN, out)
        self.assertIn(ui_contract.VERIFY_MSG_CHECKS_PASSED, out)

    def test_render_failed_check_uses_pytest_suite_summary(self):
        stderr = (
            "pytest session starts\n"
            "tests/unit/test_x.py::test_a PASSED [0.01s]\n"
            "=========== 1 passed in 1.2s ===========\n"
        )
        verification = {
            "status": "failed",
            "steps_executed_count": 1,
            "checks": [
                {
                    "name": "Tests",
                    "status": "failed",
                    "provenance": "executed",
                    "exit_code": 1,
                    "stderr": stderr,
                },
            ],
        }
        self.renderer.render_verification_results(verification)
        out = self.output.getvalue()
        self.assertIn("test_x.py::test_a", out)
        self.assertIn(ui_contract.VERIFY_PYTEST_SUMMARY_HEADER_PREFIX, out)

    def test_tool_segment_flushes_single_panel(self):
        """GHOST_TOOL_UI=panel: tabla con título de lote."""
        with patch.dict(os.environ, {"GHOST_TOOL_UI": "panel"}, clear=False):
            self.renderer.begin_tool_segment()
            self.renderer.append_tool_trace("ls", "app", auto_approved=False)
            self.renderer.append_tool_trace("read_file", "README.md", auto_approved=True)
            self.renderer.flush_tool_segment()
        out = self.output.getvalue()
        self.assertIn(ui_contract.TOOL_TABLE_TITLE_MARKER, out)
        self.assertIn("ls", out)
        self.assertIn("read", out)
        self.assertIn("README.md", out)

    def test_tool_segment_compact_default_one_line(self):
        """Por defecto: una sola línea por lote, sin panel."""
        with patch.dict(os.environ, {"GHOST_TOOL_UI": "compact"}, clear=False):
            self.renderer.begin_tool_segment()
            self.renderer.append_tool_trace("read_file", "package.json", auto_approved=False)
            self.renderer.flush_tool_segment()
        out = self.output.getvalue()
        self.assertNotIn(ui_contract.LEGACY_TOOLS_LOTE_PHRASE, out)
        self.assertIn("read", out)
        self.assertIn("package.json", out)
        self.assertNotIn("Ghost · operaciones", out)

    def test_tool_segment_compact_quiet_for_successful_read_only_batch(self):
        with patch.dict(os.environ, {"GHOST_TOOL_UI": "compact", "GHOST_UI_VERBOSE": "0"}, clear=False):
            self.renderer.begin_tool_segment()
            self.renderer.append_tool_trace("read_file", "a.py", True, duration_ms=8)
            self.renderer.append_tool_trace("read_file", "b.py", True, duration_ms=9)
            self.renderer.flush_tool_segment()
        out = self.output.getvalue()
        self.assertEqual(out.strip(), "")

    def test_tool_segment_compact_reuses_same_section_without_double_spacing(self):
        with patch.dict(os.environ, {"GHOST_TOOL_UI": "compact"}, clear=False):
            self.renderer.begin_tool_segment()
            self.renderer.append_tool_trace("read_file", "a.py", auto_approved=False)
            self.renderer.flush_tool_segment()
            self.renderer.begin_tool_segment()
            self.renderer.append_tool_trace("read_file", "b.py", auto_approved=False)
            self.renderer.flush_tool_segment()
        out = self.output.getvalue()
        self.assertEqual(out.count(ui_contract.RULE_LABEL_HERRAMIENTAS), 1)
        self.assertGreaterEqual(out.count("últimas"), 2)
        self.assertNotIn("\n\n\n", out)

    def test_summarize_pytest_extracts_nodes_and_duration(self):
        blob = (
            "pytest collected 2 items\n\n"
            "tests/unit/test_x.py::test_a PASSED [0.01s]\n"
            "tests/unit/test_x.py::test_b FAILED [0.02s]\n"
            "=========== 1 failed, 1 passed in 2.5s ===========\n"
        )
        s = summarize_test_runner_output(blob)
        self.assertIsNotNone(s)
        self.assertIn(ui_contract.VERIFY_PYTEST_SUMMARY_HEADER_EXTRACTO, s)
        self.assertIn("test_x.py::test_a", s)
        self.assertIn("PASSED", s)
        self.assertIn("FAILED", s)

    def test_summarize_random_blob_returns_none(self):
        self.assertIsNone(summarize_test_runner_output("hello world no tests here"))


class TestVisualLayoutAdaptive(unittest.TestCase):
    def test_compact_density_reduces_review_cap_vs_comfortable(self) -> None:
        c = build_ghost_visual_layout(width=100, verbose=False, density="compact")
        h = build_ghost_visual_layout(width=100, verbose=False, density="comfortable")
        self.assertLess(c.review_md_lines_normal, h.review_md_lines_normal)

    def test_ascii_rail_uses_asterisk_not_bullet(self) -> None:
        ly = build_ghost_visual_layout(width=100, verbose=False, ascii_ui=True, density="normal")
        r = format_live_phase_rail("ACT", ui_contract.LIVE_RAIL_PROFILE_IMPLEMENT, layout=ly)
        self.assertIn("*", r)
        self.assertNotIn("●", r)

    def test_narrow_console_merges_status_message_single_line(self) -> None:
        buf = StringIO()
        console = make_ghost_console(file=buf, width=48, force_terminal=False)
        renderer = GhostRenderer(console)
        with patch.dict(os.environ, {"GHOST_UI_VERBOSE": "0"}, clear=False):
            msg = renderer._status_full_message("ACT", "thinking")
        self.assertIn("·", msg)
        self.assertEqual(msg.count("\n"), 0)


if __name__ == "__main__":
    unittest.main()
