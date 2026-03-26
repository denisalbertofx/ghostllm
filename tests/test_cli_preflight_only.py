from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root not in sys.path:
    sys.path.insert(0, root)
py_core = os.path.join(root, "packages", "py-core")
if py_core not in sys.path:
    sys.path.insert(0, py_core)

from apps.cli.main import Profile, _prepare_runtime_for_assistant, app


runner = CliRunner()


def test_dev_preflight_only_exits_before_runtime_loop() -> None:
    pf = SimpleNamespace(base_url="http://127.0.0.1:8000", ok=True)
    with patch("apps.cli.main.is_running", return_value=True), patch(
        "apps.cli.main._preflight_gateway_or_exit", return_value=pf
    ):
        result = runner.invoke(app, ["dev", "--preflight-only"])
    assert result.exit_code == 0, result.stdout
    assert "Ghost Dev preflight OK" in result.stdout


def test_plan_preflight_only_exits_before_runtime_loop() -> None:
    pf = SimpleNamespace(base_url="http://127.0.0.1:8000", ok=True)
    with patch("apps.cli.main.is_running", return_value=True), patch(
        "apps.cli.main._preflight_gateway_or_exit", return_value=pf
    ):
        result = runner.invoke(app, ["plan", "smoke task", "--preflight-only"])
    assert result.exit_code == 0, result.stdout
    assert "Ghost Plan preflight OK" in result.stdout


def test_claude_preflight_only_skips_external_claude_launch() -> None:
    pf = SimpleNamespace(ok=True)
    with patch("apps.cli.main.is_running", return_value=True), patch(
        "apps.cli.main._effective_gateway_url", return_value="http://127.0.0.1:8000"
    ), patch("apps.cli.main.probe_gateway", return_value=pf), patch(
        "apps.cli.main.require_provider_for_assistant"
    ), patch("apps.cli.main.subprocess.run") as run_mock:
        result = runner.invoke(app, ["claude", "--preflight-only"])
    assert result.exit_code == 0, result.stdout
    assert "Ghost Claude bridge preflight OK" in result.stdout
    run_mock.assert_not_called()


def test_prepare_runtime_for_assistant_applies_coder_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    for key in list(os.environ):
        if key.startswith("GHOST_") or key.startswith("_GHOST_"):
            monkeypatch.delenv(key, raising=False)
    prep = _prepare_runtime_for_assistant(Profile.coder, initial_task="fix doctor")
    assert os.environ["GHOST_ACTIVE_PROFILE"] == "dev"
    assert prep.operational_profile == "dev"
    assert prep.active_feature_flags["GHOST_USE_DECISION_PLANNER"] == "1"


def test_prepare_runtime_for_assistant_applies_architect_alias(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    for key in list(os.environ):
        if key.startswith("GHOST_") or key.startswith("_GHOST_"):
            monkeypatch.delenv(key, raising=False)
    prep = _prepare_runtime_for_assistant(Profile.architect, initial_task="plan architecture")
    assert os.environ["GHOST_ACTIVE_PROFILE"] == "safe"
    assert prep.operational_profile == "safe"
