"""
Contrato CodexAssistant ↔ GhostRenderer.

Si ``assistant.py`` llama a ``self.renderer.foo(...)`` y ``GhostRenderer.foo`` no acepta
esos argumentos, el runtime cae en TypeError antes de EXPLORE/tools (p. ej. ghost dev/plan).

Este módulo fija la lista de métodos esperados y valida la firma crítica de
``append_tool_trace`` (legacy ``ok`` bool + kwargs ``auto_approved``).
"""
from __future__ import annotations

import inspect
import os
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.cli.ui import renderer as ghost_renderer_mod
from apps.cli.ui import ui_contract
from apps.cli.ui.renderer import GhostRenderer
from apps.cli.ui.theme import make_ghost_console

# Sincronizar con grep ``self.renderer.`` en apps/cli/assistant.py
_ASSISTANT_RENDERER_METHODS = (
    "render_cli_startup_summary",
    "render_verify_batch_approval_request",
    "begin_tool_segment",
    "flush_tool_segment",
    "append_tool_trace",
    "render_autonomy_checkpoint",
    "read_input",
    "render_task_list",
    "render_swarm_board",
    "render_merge_preview",
    "render_merge_success",
    "render_review_bundle",
    "render_review_decision",
    "print_help",
    "render_artifact_summary",
    "update_status",
    "render_bundled_approval_request",
    "render_budget_status",
    "render_verification_results",
    "render_diff",
    "session_status",
)


def test_ui_contract_stable_strings_for_assistant_flows() -> None:
    assert ui_contract.MSG_APPROVAL_SAME_BUNDLE.strip()
    assert ui_contract.MSG_ACTIVE_WORKSET_PLAIN_PREFIX.endswith(":")
    assert ui_contract.output_contains_review_panel(f"…{ui_contract.PANEL_TITLE_REVIEW}…")


def test_renderer_exposes_all_methods_used_by_assistant() -> None:
    for name in _ASSISTANT_RENDERER_METHODS:
        assert hasattr(GhostRenderer, name), f"GhostRenderer falta método: {name}"
        assert callable(getattr(GhostRenderer, name))


def test_append_tool_trace_accepts_ok_and_auto_approved() -> None:
    sig = inspect.signature(GhostRenderer.append_tool_trace)
    p = sig.parameters
    assert "name" in p and "detail" in p and "ok" in p and "auto_approved" in p
    assert p["auto_approved"].kind == inspect.Parameter.KEYWORD_ONLY
    assert p["kwargs"].kind == inspect.Parameter.VAR_KEYWORD


def test_session_status_and_update_status_live_status_kwargs() -> None:
    ss = inspect.signature(GhostRenderer.session_status)
    assert "rail_profile" in ss.parameters
    us = inspect.signature(GhostRenderer.update_status)
    assert "live_detail" in us.parameters
    assert "rail_profile" in us.parameters


def test_append_tool_trace_call_variants() -> None:
    buf = StringIO()
    r = GhostRenderer(make_ghost_console(file=buf, force_terminal=False))
    r.append_tool_trace("ls", ".", False)
    r.append_tool_trace("read_file", "README.md", True)
    r.append_tool_trace("grep", "needle", auto_approved=True)
    r.append_tool_trace("grep", "needle", auto_approved=False)
    r.append_tool_trace("run_shell", "npm test", None, auto_approved=True)
    r.begin_tool_segment()
    r.append_tool_trace("ls", "apps", auto_approved=False)
    r.flush_tool_segment()
    out = buf.getvalue()
    assert "ls" in out or "apps" in out


def test_artifact_interactive_compact_for_readonly_without_diff() -> None:
    assert ghost_renderer_mod._artifact_interactive_compact(
        {
            "taskspec": {"intent": "analysis", "change_expectation": "should_not_write"},
            "diff_summary": [],
        }
    )
    assert not ghost_renderer_mod._artifact_interactive_compact(
        {
            "taskspec": {"intent": "analysis", "change_expectation": "should_not_write"},
            "diff_summary": [{"file": "a.py", "type": "Edit"}],
        }
    )


def test_startup_summary_ignores_disabled_flags() -> None:
    buf = StringIO()
    renderer = GhostRenderer(make_ghost_console(file=buf, force_terminal=False))
    prep = SimpleNamespace(
        active_feature_flags={
            "GHOST_USE_TASKSPEC": "1",
            "GHOST_USE_DECISION_PLANNER": "0",
            "GHOST_PARALLEL_PIPELINE": "partial",
        }
    )
    with patch.dict(os.environ, {"GHOST_UI_VERBOSE": "1"}, clear=False):
        renderer.render_cli_startup_summary(
            command_mode="dev",
            assistant_mode="Chat",
            model="coder",
            prep=prep,
        )
    out = buf.getvalue()
    assert "GHOST_USE_TASKSPEC" in out
    assert "GHOST_PARALLEL_PIPELINE" in out
    assert "GHOST_USE_DECISION_PLANNER" not in out


def test_startup_summary_shows_phase_routing() -> None:
    buf = StringIO()
    renderer = GhostRenderer(make_ghost_console(file=buf, force_terminal=False))
    with patch.dict(os.environ, {"GHOST_UI_VERBOSE": "1"}, clear=False):
        renderer.render_cli_startup_summary(
            command_mode="dev",
            assistant_mode="Chat",
            model="coder",
            role_models={
                "explore": "qwen-explore",
                "act": "qwen-act",
                "verify": "llama-verify",
                "fallback": "llama-fallback",
            },
        )
    out = buf.getvalue()
    assert "routing:" in out
    assert "explore=qwen-explore" in out
    assert "verify=llama-verify" in out
