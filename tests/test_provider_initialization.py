"""
Regression tests for NVIDIA provider initialization failure handling.
Verifies: preflight check, recovery message, no partial task/artifact on provider-not-initialized.
"""
import sys
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, root)
sys.path.insert(0, os.path.join(root, "packages", "py-core"))


runner = CliRunner()


class TestProviderInitialization(unittest.TestCase):
    """Regression coverage for provider-not-initialized failure mode."""

    def test_check_provider_ready_returns_false_when_provider_not_initialized(self):
        """Preflight: check_provider_ready returns (False, detail) when /ready returns 503."""
        from apps.cli.main import check_provider_ready

        with patch("apps.cli.main.requests.get") as mock_get:
            mock_get.return_value.status_code = 503
            mock_get.return_value.text = ""
            mock_get.return_value.json.return_value = {
                "ready": False,
                "detail": "NVIDIA Provider not initialized",
            }

            ready, err = check_provider_ready()

            self.assertFalse(ready)
            self.assertIn("NVIDIA Provider not initialized", err)

    def test_check_provider_ready_returns_true_when_ready(self):
        """Preflight: check_provider_ready returns (True, '') when /ready returns 200 and ready: true."""
        from apps.cli.main import check_provider_ready

        with patch("apps.cli.main.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"ready": True}

            ready, err = check_provider_ready()

            self.assertTrue(ready)
            self.assertEqual(err, "")

    def test_no_stale_ready_missing_ready_key_returns_false(self):
        """When response lacks 'ready' or ready is not True, must never return ready (fixes doctor/dev mismatch)."""
        from apps.cli.main import check_provider_ready

        with patch("apps.cli.main.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {}  # No 'ready' key - old server

            ready, err = check_provider_ready()

            self.assertFalse(ready, "Must not default to ready when 'ready' key is missing")

    def test_doctor_dev_agreement_ready_not_possible_when_completions_would_fail(self):
        """Regression: doctor says Ready => first request must not fail with provider not initialized."""
        from apps.cli.main import check_provider_ready

        with patch("apps.cli.main.requests.get") as mock_get:
            mock_get.return_value.status_code = 503
            mock_get.return_value.text = ""
            mock_get.return_value.json.return_value = {
                "ready": False,
                "detail": "NVIDIA Provider not initialized",
            }

            ready, err = check_provider_ready()

            self.assertFalse(ready)
            self.assertIn("NVIDIA Provider not initialized", err)

    def test_require_provider_raises_system_exit_when_not_ready(self):
        """Preflight: require_provider_for_assistant raises SystemExit(1) when provider not ready."""
        from apps.cli.main import require_provider_for_assistant

        with patch("apps.cli.main.check_provider_ready", return_value=(False, "NVIDIA Provider not initialized")):
            with patch("rich.console.Console") as mock_console_cls:
                mock_console_cls.return_value.print = MagicMock()
                with self.assertRaises(SystemExit) as ctx:
                    require_provider_for_assistant()
                self.assertEqual(ctx.exception.code, 1)

    def test_doctor_reports_gateway_healthy_but_not_ready(self):
        """Doctor must distinguish /health OK from /ready not ready in the rendered status."""
        from apps.cli.main import app

        preflight = SimpleNamespace(ok=True, checked_url="http://127.0.0.1:8000/health", status_code=200)
        with patch("apps.cli.main.is_running", return_value=True), patch(
            "apps.cli.main._effective_gateway_url", return_value="http://127.0.0.1:8000"
        ), patch("apps.cli.main.probe_gateway", return_value=preflight), patch(
            "apps.cli.main.check_provider_ready",
            return_value=(False, "NVIDIA Provider not initialized"),
        ):
            result = runner.invoke(app, ["doctor"])

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("Gateway healthy but not ready", result.stdout)
        self.assertIn("/health OK", result.stdout)
        self.assertIn("/ready returned", result.stdout)

    def test_doctor_marks_daemon_running_when_gateway_reachable(self):
        from apps.cli.main import app

        preflight = SimpleNamespace(ok=True, checked_url="http://127.0.0.1:8000/health", status_code=200)
        with patch("apps.cli.main.is_running", return_value=False), patch(
            "apps.cli.main._effective_gateway_url", return_value="http://127.0.0.1:8000"
        ), patch("apps.cli.main.probe_gateway", return_value=preflight), patch(
            "apps.cli.main.check_provider_ready", return_value=(True, "")
        ), patch("apps.cli.main._registry_sync_status", return_value=(True, "In sync")):
            result = runner.invoke(app, ["doctor"])

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("Running", result.stdout)

    def test_doctor_reports_stale_model_registry(self):
        """Doctor should surface when the running daemon registry differs from local configs/models.yaml."""
        from apps.cli.main import app

        preflight = SimpleNamespace(ok=True, checked_url="http://127.0.0.1:8000/health", status_code=200)
        with patch("apps.cli.main.is_running", return_value=True), patch(
            "apps.cli.main._effective_gateway_url", return_value="http://127.0.0.1:8000"
        ), patch("apps.cli.main.probe_gateway", return_value=preflight), patch(
            "apps.cli.main.check_provider_ready",
            return_value=(True, ""),
        ), patch(
            "apps.cli.main._registry_sync_status",
            return_value=(False, "planner=qwen/qwen2.5-coder-32b-instruct"),
        ):
            result = runner.invoke(app, ["doctor"])

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("Model registry sync", result.stdout)
        self.assertIn("Stale", result.stdout)

    def test_assistant_detects_provider_not_initialized_and_sets_fatal_flag(self):
        """Assistant detects 500 with 'NVIDIA Provider not initialized' and sets _fatal_provider_error."""
        from apps.cli.assistant import CodexAssistant

        assistant = CodexAssistant("http://localhost:11434", "key", "coder")
        assistant.history = [{"role": "user", "content": "hello"}]
        with patch.object(assistant.console, "print"):  # Avoid Windows encoding issues with Unicode chars
            with patch("apps.cli.assistant.requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 500
                mock_resp.text = '{"detail":"NVIDIA Provider not initialized"}'
                mock_resp.json.return_value = {"detail": "NVIDIA Provider not initialized"}
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=None)
                mock_resp.iter_lines.return_value = []
                mock_post.return_value = mock_resp

                result = assistant._execute_stream_request(parent_status=None)

                self.assertTrue(getattr(assistant, "_fatal_provider_error", False))
                self.assertEqual(result, {})

    def test_assistant_does_not_retry_on_provider_not_initialized(self):
        """Provider-not-initialized is not retryable; _stream_completion returns immediately."""
        from apps.cli.assistant import CodexAssistant

        assistant = CodexAssistant("http://localhost:11434", "key", "coder")
        assistant.history = [{"role": "user", "content": "hello"}]

        call_count = 0

        def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.status_code = 500
            r.text = '{"detail":"NVIDIA Provider not initialized"}'
            r.json.return_value = {"detail": "NVIDIA Provider not initialized"}
            r.__enter__ = MagicMock(return_value=r)
            r.__exit__ = MagicMock(return_value=None)
            r.iter_lines.return_value = []
            return r

        with patch.object(assistant.console, "print"):
            with patch("apps.cli.assistant.requests.post", side_effect=mock_post):
                result = assistant._stream_completion(parent_status=None)

        self.assertEqual(result, {})
        self.assertEqual(call_count, 1, "Should not retry on provider-not-initialized")


if __name__ == "__main__":
    unittest.main(verbosity=2)
