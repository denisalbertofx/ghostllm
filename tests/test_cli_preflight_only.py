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


def test_fix_accepts_task_argument_and_runs_with_it() -> None:
    pf = SimpleNamespace(base_url="http://127.0.0.1:8000", ok=True)
    runtime_prep = SimpleNamespace(active_feature_flags={}, operational_profile="dev")
    with patch("apps.cli.main.is_running", return_value=True), patch(
        "apps.cli.main._preflight_gateway_or_exit", return_value=pf
    ), patch(
        "apps.cli.main._prepare_runtime_for_assistant", return_value=runtime_prep
    ) as prep_mock, patch("apps.cli.main.get_api_key", return_value="test-key"), patch(
        "apps.cli.assistant.CodexAssistant"
    ) as assistant_cls:
        result = runner.invoke(
            app,
            [
                "fix",
                "corrige los tests inestables y verifica otra vez",
            ],
        )
        assert result.exit_code == 0, result.stdout
        prep_mock.assert_called_once_with(
            Profile.coder,
            initial_task="corrige los tests inestables y verifica otra vez",
        )
        assistant_cls.return_value.run.assert_called_once_with(
            initial_task="corrige los tests inestables y verifica otra vez",
            keep_open=False,
        )


def test_do_runs_as_one_shot_command() -> None:
    pf = SimpleNamespace(base_url="http://127.0.0.1:8000", ok=True)
    runtime_prep = SimpleNamespace(active_feature_flags={}, operational_profile="dev")
    with patch("apps.cli.main.is_running", return_value=True), patch(
        "apps.cli.main._preflight_gateway_or_exit", return_value=pf
    ), patch(
        "apps.cli.main._prepare_runtime_for_assistant", return_value=runtime_prep
    ), patch("apps.cli.main.get_api_key", return_value="test-key"), patch(
        "apps.cli.assistant.CodexAssistant"
    ) as assistant_cls:
        result = runner.invoke(
            app,
            [
                "do",
                "crea el endpoint faltante y verifica al final",
            ],
        )
        assert result.exit_code == 0, result.stdout
        assistant_cls.return_value.run.assert_called_once_with(
            initial_task="crea el endpoint faltante y verifica al final",
            keep_open=False,
        )


def test_preflight_gateway_refreshes_stale_registry() -> None:
    from apps.cli.main import _preflight_gateway_or_exit

    pf = SimpleNamespace(base_url="http://127.0.0.1:8000", ok=True)
    with patch("apps.cli.main.probe_gateway", return_value=pf), patch(
        "apps.cli.main.require_provider_for_assistant"
    ), patch(
        "apps.cli.main._refresh_daemon_registry_if_needed", return_value=(True, "Daemon refreshed to current registry")
    ) as refresh_mock:
        result = _preflight_gateway_or_exit()

    assert result is pf
    refresh_mock.assert_called_once_with("http://127.0.0.1:8000")


def test_preflight_gateway_exits_when_registry_stays_stale() -> None:
    from apps.cli.main import _preflight_gateway_or_exit

    pf = SimpleNamespace(base_url="http://127.0.0.1:8000", ok=True)
    with patch("apps.cli.main.probe_gateway", return_value=pf), patch(
        "apps.cli.main.require_provider_for_assistant"
    ), patch(
        "apps.cli.main._refresh_daemon_registry_if_needed",
        return_value=(False, "planner=qwen/qwen2.5-coder-32b-instruct"),
    ):
        try:
            _preflight_gateway_or_exit()
            assert False, "expected typer.Exit"
        except Exception as exc:
            assert type(exc).__name__ == "Exit"
            assert getattr(exc, "exit_code", 2) == 2


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


def test_get_pid_prefers_listener_when_pid_file_is_stale(monkeypatch, tmp_path) -> None:
    from apps.cli import main as cli_main

    pid_file = tmp_path / "ghost.pid"
    pid_file.write_text("11800", encoding="utf-8")
    monkeypatch.setattr(cli_main, "PID_FILE", str(pid_file))

    with patch("apps.cli.main._pid_exists", return_value=False), patch(
        "apps.cli.main._find_gateway_listener_pid", return_value=33504
    ):
        assert cli_main.get_pid() == 33504


def test_get_pid_clears_stale_pid_file_without_listener(monkeypatch, tmp_path) -> None:
    from apps.cli import main as cli_main

    pid_file = tmp_path / "ghost.pid"
    pid_file.write_text("11800", encoding="utf-8")
    monkeypatch.setattr(cli_main, "PID_FILE", str(pid_file))

    with patch("apps.cli.main._pid_exists", return_value=False), patch(
        "apps.cli.main._find_gateway_listener_pid", return_value=None
    ):
        assert cli_main.get_pid() is None
    assert not pid_file.exists()


def test_start_daemon_process_detaches_on_unix(monkeypatch, tmp_path) -> None:
    from apps.cli import main as cli_main

    log_file = tmp_path / "ghost.log"
    pid_file = tmp_path / "ghost.pid"
    monkeypatch.setattr(cli_main, "LOG_FILE", str(log_file))
    monkeypatch.setattr(cli_main, "PID_FILE", str(pid_file))
    monkeypatch.setattr(cli_main, "root_dir", tmp_path)
    monkeypatch.setattr(cli_main.sys, "platform", "linux")

    process = SimpleNamespace(pid=4321)
    with patch("apps.cli.main.is_running", return_value=False), patch(
        "apps.cli.main.subprocess.Popen", return_value=process
    ) as popen_mock, patch(
        "apps.cli.main.requests.get", return_value=SimpleNamespace(status_code=200)
    ), patch("apps.cli.main._find_gateway_listener_pid", return_value=4321):
        assert cli_main._start_daemon_process(quiet=True) is True

    kwargs = popen_mock.call_args.kwargs
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is cli_main.subprocess.DEVNULL
    assert pid_file.read_text(encoding="utf-8").strip() == "4321"


def test_stop_daemon_process_uses_process_group_on_unix(monkeypatch, tmp_path) -> None:
    from apps.cli import main as cli_main

    pid_file = tmp_path / "ghost.pid"
    pid_file.write_text("4321", encoding="utf-8")
    monkeypatch.setattr(cli_main, "PID_FILE", str(pid_file))
    monkeypatch.setattr(cli_main.sys, "platform", "linux")

    with patch("apps.cli.main.get_pid", return_value=4321), patch(
        "apps.cli.main.os.getpgid", return_value=4321, create=True
    ), patch("apps.cli.main.os.killpg", create=True) as killpg_mock, patch(
        "apps.cli.main._find_gateway_listener_pid", side_effect=[4321, None]
    ):
        assert cli_main._stop_daemon_process(quiet=True) is True

    killpg_mock.assert_called_once()
    assert not pid_file.exists()


def test_is_running_requires_listener_or_health() -> None:
    from apps.cli import main as cli_main

    with patch("apps.cli.main._find_gateway_listener_pid", return_value=None), patch(
        "apps.cli.main.requests.get", side_effect=Exception("down")
    ):
        assert cli_main.is_running() is False


def test_prepare_runtime_for_assistant_applies_architect_alias(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    for key in list(os.environ):
        if key.startswith("GHOST_") or key.startswith("_GHOST_"):
            monkeypatch.delenv(key, raising=False)
    prep = _prepare_runtime_for_assistant(Profile.architect, initial_task="plan architecture")
    assert os.environ["GHOST_ACTIVE_PROFILE"] == "safe"
    assert prep.operational_profile == "safe"
    assert os.environ["GHOST_PLANNER_MODEL"] == "qwen/qwen3-coder-480b-a35b-instruct"


def test_doctor_reports_health_but_not_ready() -> None:
    """Verifica que doctor reporte claramente cuando /health responde pero /ready no."""
    from types import SimpleNamespace

    # Gateway responde /health (ok=True) pero /ready falla (ready=False)
    pf_ok = SimpleNamespace(ok=True, checked_url="http://127.0.0.1:8000/health", status_code=200)

    with patch("apps.cli.main.is_running", return_value=True), patch(
        "apps.cli.main.probe_gateway", return_value=pf_ok
    ), patch("apps.cli.main.check_provider_ready", return_value=(False, "provider initializing")), patch(
        "apps.cli.main._effective_gateway_url", return_value="http://127.0.0.1:8000"
    ), patch("apps.cli.main.get_api_key", return_value="test-key"):
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.stdout
        # Debe mostrar que el gateway es alcanzable
        assert "Reachable" in result.stdout or "reachable" in result.stdout.lower()
        # Debe reportar el estado "healthy but not ready"
        assert "healthy but not ready" in result.stdout.lower() or "Gateway healthy but not ready" in result.stdout
