import typer
import subprocess
import os
import sys
import time
import httpx
import yaml
from pathlib import Path
from enum import Enum
from typing import Optional, Union
from urllib.parse import urlparse

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(root_dir / "packages" / "py-core") not in sys.path:
    sys.path.insert(0, str(root_dir / "packages" / "py-core"))

from ghostllm_core.config import (
    ConfigValidationError,
    load_config,
    load_registry,
    resolve_gateway_model_id,
    resolve_config_path,
)
from ghostllm_core.memory import MemoryStore

from apps.cli.runtime.gateway_endpoint import (
    GatewayPreflightResult,
    format_preflight_failure_console,
    probe_gateway,
    resolve_cli_gateway_url,
)
from apps.cli.runtime.http_client import get as http_get
from apps.cli.runtime.filesystem_intents import revert_filesystem_intent, summarize_filesystem_intent
from apps.cli.runtime.ghost_profile import apply_operational_profile_to_environment
from apps.cli.runtime.runtime_env import (
    enrich_prep_after_operational_profile,
    prepare_runtime,
)

app = typer.Typer(help="👻 Ghost Dev: Premium Native Programming Runtime", no_args_is_help=True)
PID_FILE = str(root_dir / "ghost.pid")
LOG_FILE = str(root_dir / "ghost.log")
REGISTRY_PATH = str(root_dir / "configs" / "models.yaml")
DEFAULT_CONFIG_PATH = root_dir / "configs" / "default.yaml"
LOCAL_CONFIG_PATH = root_dir / "configs" / "default.local.yaml"


def _ensure_utf8_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_ensure_utf8_stdio()


def _effective_gateway_url() -> str:
    return resolve_cli_gateway_url()


def get_api_key():
    return os.getenv("GHOST_API_KEY", "ghost-dev-2026")


def _active_config_path() -> Path:
    return Path(resolve_config_path(str(DEFAULT_CONFIG_PATH)))


def _load_raw_config_template() -> dict:
    config_path = _active_config_path()
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_local_config(data: dict) -> None:
    with open(LOCAL_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def _memory_store_for_cwd(cwd: Optional[str] = None) -> MemoryStore:
    base = os.path.abspath(cwd or os.getcwd())
    return MemoryStore(os.path.join(base, "ghost_memory.db"))


def _option_was_explicit(flag: str) -> bool:
    return any(str(arg).strip().startswith(flag) for arg in sys.argv[1:])


def _has_provider_credentials_configured() -> bool:
    env_key = (
        os.getenv("GHOST_NVIDIA_API_KEY", "").strip()
        or os.getenv("NVIDIA_API_KEY", "").strip()
    )
    if env_key:
        return True
    try:
        upstream = dict(_load_raw_config_template().get("upstream") or {})
    except Exception:
        return False
    config_key = str(upstream.get("nvidia_api_key") or "").strip()
    return bool(config_key and "set-me" not in config_key.lower())


def _recover_pending_filesystem_intents(cwd: Optional[str] = None) -> None:
    store = _memory_store_for_cwd(cwd)
    pending = store.list_pending_filesystem_intents()
    if not pending:
        return
    intent = pending[0]
    summary = summarize_filesystem_intent(intent)
    decision = os.getenv("GHOST_PENDING_INTENT_DECISION", "").strip().lower()
    if decision not in {"continue", "revert"}:
        if sys.stdin.isatty():
            decision = (
                typer.prompt(
                    f"Encontré una operación incompleta ({summary}). Escribe 'continue' o 'revert'",
                    default="continue",
                )
                .strip()
                .lower()
            )
        else:
            decision = "continue"

    if decision == "revert":
        ok, detail = revert_filesystem_intent(os.path.abspath(cwd or os.getcwd()), intent)
        if ok:
            store.update_filesystem_intent_status(intent["intent_id"], "reverted")
            typer.secho(f"Recovered pending filesystem intent: {detail}", fg=typer.colors.YELLOW)
            return
        typer.secho(f"Could not revert pending filesystem intent: {detail}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    store.update_filesystem_intent_status(intent["intent_id"], "continued")
    typer.secho(f"Continuing after pending filesystem intent: {summary}", fg=typer.colors.YELLOW)


def _ensure_gateway_process() -> GatewayPreflightResult:
    base = _effective_gateway_url()
    result = probe_gateway(base, api_key=get_api_key())
    if result.ok:
        return result
    if not _has_provider_credentials_configured():
        return result
    typer.secho(
        "Ghost gateway is not reachable; starting the background gateway automatically...",
        fg=typer.colors.YELLOW,
    )
    _start_daemon_process(quiet=True)
    return probe_gateway(base, api_key=get_api_key())


def _load_local_registry_alias_map() -> dict[str, str]:
    try:
        registry = load_registry(REGISTRY_PATH)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for row in registry.models:
        name = str(row.name or "").strip()
        upstream = str(row.upstream_id or "").strip()
        if name and upstream:
            out[name] = upstream
    return out


def _fetch_remote_registry_alias_map(base_url: Optional[str] = None) -> tuple[dict[str, str], str]:
    base = (base_url or _effective_gateway_url()).rstrip("/")
    try:
        response = http_get(f"{base}/v1/models", timeout=1.5)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as e:
        return {}, str(e)
    except ValueError as e:
        return {}, f"invalid registry payload: {e}"
    out: dict[str, str] = {}
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("id") or "").strip()
        upstream = str(row.get("upstream_id") or "").strip()
        if name and upstream:
            out[name] = upstream
    return out, ""


def _registry_drift_summary(local_map: dict[str, str], remote_map: dict[str, str]) -> str:
    diffs: list[str] = []
    for name, local_upstream in sorted(local_map.items()):
        remote_upstream = remote_map.get(name, "")
        if not remote_upstream:
            diffs.append(f"{name} missing on daemon")
            continue
        if remote_upstream != local_upstream:
            diffs.append(f"{name}={remote_upstream}")
    if not diffs:
        return ""
    preview = ", ".join(diffs[:3])
    if len(diffs) > 3:
        preview += f" (+{len(diffs) - 3} more)"
    return preview


def _configured_repo_remote() -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def check_provider_ready(base_url: Optional[str] = None) -> tuple[bool, str]:
    base = (base_url or _effective_gateway_url()).rstrip("/")
    try:
        response = http_get(f"{base}/ready", timeout=1.5)
    except httpx.HTTPError as e:
        return False, str(e)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    ready = response.status_code == 200 and payload.get("ready") is True
    if ready:
        return True, ""
    detail = payload.get("detail") or response.text or f"HTTP {response.status_code}"
    return False, str(detail)


def require_provider_for_assistant(base_url: Optional[str] = None) -> None:
    ready, err = check_provider_ready(base_url)
    if ready:
        return
    from rich.console import Console

    Console(file=sys.stderr).print(f"[bold red]Provider not ready[/bold red]\n[dim]{err}[/dim]")
    raise SystemExit(1)


def _resolve_plan_command_model(model: str) -> str:
    requested = (model or "").strip()
    if requested.lower() == "planner":
        requested = os.environ.get("GHOST_PLANNER_MODEL", "").strip() or "planner"
    return resolve_gateway_model_id(requested, REGISTRY_PATH)


def _preflight_gateway_or_exit() -> GatewayPreflightResult:
    """Antes del loop del asistente: endpoint vivo o salida con diagnóstico (timeout corto)."""
    from rich.console import Console

    base = _effective_gateway_url()
    result = _ensure_gateway_process()
    if not result.ok:
        if not _has_provider_credentials_configured():
            Console(file=sys.stderr).print(
                "[bold yellow]Ghost is installed but not initialized[/bold yellow]\n"
                "[dim]Run `ghost init` to configure your NVIDIA API key and start the gateway.[/dim]\n"
            )
        Console(file=sys.stderr).print(format_preflight_failure_console(result))
        raise typer.Exit(code=2)
    require_provider_for_assistant(base)
    sync_ok, sync_detail = _refresh_daemon_registry_if_needed(base)
    if not sync_ok:
        Console(file=sys.stderr).print(
            f"[bold red]Daemon model registry is stale[/bold red]\n[dim]{sync_detail}[/dim]"
        )
        raise typer.Exit(code=2)
    return result


def _handle_preflight_only(message: str, enabled: bool) -> bool:
    if not enabled:
        return False
    typer.secho(message, fg=typer.colors.GREEN)
    return True

class Profile(str, Enum):
    fast = "fast"
    coder = "coder"
    architect = "architect"


_OPERATIONAL_PROFILE_ALIASES = {
    "fast": "fast",
    "coder": "dev",
    "architect": "safe",
    "dev": "dev",
    "safe": "safe",
    "simple": "simple",
    "turbo": "turbo",
}


def _resolve_operational_profile_id(profile: Union[Profile, str, None]) -> str:
    raw = getattr(profile, "value", profile) or "coder"
    return _OPERATIONAL_PROFILE_ALIASES.get(str(raw).strip().lower(), "dev")


def _prepare_runtime_for_assistant(
    profile: Union[Profile, str, None],
    *,
    initial_task: str = "",
):
    """
    Snapshot runtime state after autoload and then refresh it with the active
    operational profile so startup banner + trace reflect the real runtime.
    """
    cwd = os.path.abspath(os.getcwd())
    prep = prepare_runtime(cwd, initial_task=initial_task)
    apply_operational_profile_to_environment(
        _resolve_operational_profile_id(profile),
        cwd,
        root_dir,
    )
    enrich_prep_after_operational_profile(prep)
    return prep

def get_pid():
    pid_from_file = None
    if os.path.exists(PID_FILE):
        with open(PID_FILE, "r") as f:
            try:
                pid_from_file = int(f.read().strip())
            except Exception:
                pid_from_file = None
    if pid_from_file is not None and not _pid_exists(pid_from_file):
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        pid_from_file = None
    listener_pid = _find_gateway_listener_pid()
    if pid_from_file is not None and _pid_exists(pid_from_file):
        if listener_pid is None or listener_pid == pid_from_file:
            return pid_from_file
    if listener_pid is not None:
        if listener_pid != pid_from_file:
            try:
                with open(PID_FILE, "w") as f:
                    f.write(str(listener_pid))
            except Exception:
                pass
        return listener_pid
    return pid_from_file


def _pid_exists(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=False,
            )
            return str(pid) in res.stdout
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _gateway_host_port(base_url: Optional[str] = None) -> tuple[str, int]:
    parsed = urlparse((base_url or _effective_gateway_url()).rstrip("/"))
    host = parsed.hostname or "127.0.0.1"
    if parsed.port:
        return host, parsed.port
    if parsed.scheme == "https":
        return host, 443
    return host, 80


def _find_gateway_listener_pid(base_url: Optional[str] = None) -> Optional[int]:
    _host, port = _gateway_host_port(base_url)
    try:
        if sys.platform == "win32":
            res = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                check=False,
            )
            needle = f":{port}"
            for line in res.stdout.splitlines():
                parts = line.split()
                if len(parts) < 5:
                    continue
                local_addr = parts[1]
                state = parts[3].upper()
                pid_raw = parts[4]
                if state != "LISTENING":
                    continue
                if not local_addr.endswith(needle):
                    continue
                if pid_raw.isdigit():
                    return int(pid_raw)
            return None
        for cmd in (["lsof", "-ti", f"tcp:{port}"], ["ss", "-ltnp"]):
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if cmd[0] == "lsof":
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        return int(line)
                continue
            for line in res.stdout.splitlines():
                if f":{port}" not in line or "LISTEN" not in line.upper():
                    continue
                pid_chunk = line.rsplit("pid=", 1)[-1].split(",", 1)[0].strip()
                if pid_chunk.isdigit():
                    return int(pid_chunk)
    except Exception:
        return None
    return None

def is_running():
    listener_pid = _find_gateway_listener_pid()
    if listener_pid and _pid_exists(listener_pid):
        return True

    # Fallback: probe configured gateway (short timeout)
    try:
        base = _effective_gateway_url()
        http_get(base + "/health", timeout=1.5)
        return True
    except Exception:
        return False


def _stop_daemon_process(*, quiet: bool = False) -> bool:
    pid = get_pid()
    if not pid:
        if not quiet:
            typer.secho("Ghost is not running", fg=typer.colors.YELLOW)
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            import signal

            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                os.kill(pid, signal.SIGTERM)
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        listener_pid = _find_gateway_listener_pid()
        if listener_pid == pid:
            for _ in range(20):
                time.sleep(0.2)
                if _find_gateway_listener_pid() != pid:
                    break
        if not quiet:
            typer.secho("Ghost Dev stopped", fg=typer.colors.RED)
        return True
    except Exception:
        if not quiet:
            typer.secho("Error stopping Ghost", fg=typer.colors.RED)
        return False


def _start_daemon_process(*, quiet: bool = False) -> bool:
    if is_running():
        if not quiet:
            typer.secho("✓ GhostLLM is already running", fg=typer.colors.GREEN)
        return True

    file_path = root_dir / "apps" / "server" / "main.py"
    with open(LOG_FILE, "a") as log:
        popen_kwargs = {
            "args": [sys.executable, str(file_path)],
            "stdout": subprocess.DEVNULL,
            "stderr": log,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(**popen_kwargs)

    with open(PID_FILE, "w") as f:
        f.write(str(process.pid))

    base = _effective_gateway_url()
    for _ in range(40):
        try:
            http_get(base + "/health", timeout=0.6)
            break
        except Exception:
            time.sleep(0.4)
    else:
        if not quiet:
            typer.secho(
                f"Daemon iniciado pero no responde en {base}/health — revisa {LOG_FILE}",
                fg=typer.colors.YELLOW,
            )

    if not quiet:
        typer.secho("Ghost Dev Daemon started", fg=typer.colors.MAGENTA, bold=True)
    listener_pid = _find_gateway_listener_pid(base)
    if listener_pid is not None:
        with open(PID_FILE, "w") as f:
            f.write(str(listener_pid))
    return True


def _registry_sync_status(base_url: Optional[str] = None) -> tuple[bool, str]:
    local_map = _load_local_registry_alias_map()
    if not local_map:
        return True, "Local registry unavailable"
    remote_map, err = _fetch_remote_registry_alias_map(base_url)
    if err:
        return False, err
    drift = _registry_drift_summary(local_map, remote_map)
    if drift:
        return False, drift
    return True, "In sync"


def _refresh_daemon_registry_if_needed(base_url: Optional[str] = None) -> tuple[bool, str]:
    base = (base_url or _effective_gateway_url()).rstrip("/")
    synced, detail = _registry_sync_status(base)
    if synced:
        return True, detail
    if not get_pid():
        return False, f"Daemon model registry is stale: {detail}"
    typer.secho(
        "Local model registry changed; refreshing Ghost daemon before entering the runtime...",
        fg=typer.colors.YELLOW,
    )
    _stop_daemon_process(quiet=True)
    time.sleep(0.6)
    _start_daemon_process(quiet=True)
    post = probe_gateway(base, api_key=get_api_key(), timeout_sec=2.0)
    if not post.ok:
        return False, f"Daemon refresh failed: {post.error or post.details}"
    ready, ready_err = check_provider_ready(base)
    if not ready:
        return False, f"Daemon refreshed but provider is not ready: {ready_err}"
    synced_after, detail_after = _registry_sync_status(base)
    if not synced_after:
        return False, f"Daemon registry still stale after refresh: {detail_after}"
    return True, "Daemon refreshed to current registry"


@app.command()
def init(
    nvidia_api_key: str = typer.Option(
        "",
        "--nvidia-api-key",
        help="NVIDIA NIM API key. If omitted, Ghost will prompt for it.",
    ),
    base_url: str = typer.Option(
        "https://integrate.api.nvidia.com/v1",
        "--base-url",
        help="Upstream base URL for the NVIDIA-compatible provider.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Local Ghost gateway host."),
    port: int = typer.Option(8000, "--port", help="Local Ghost gateway port."),
    force: bool = typer.Option(False, "--force", help="Overwrite configs/default.local.yaml."),
):
    """Create a local, non-versioned Ghost configuration."""
    if LOCAL_CONFIG_PATH.exists() and not force:
        typer.secho(
            f"Local config already exists at {LOCAL_CONFIG_PATH}. Use --force to overwrite it.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    api_key = (nvidia_api_key or os.getenv("GHOST_NVIDIA_API_KEY") or os.getenv("NVIDIA_API_KEY") or "").strip()
    if sys.stdin.isatty() and not api_key:
        api_key = typer.prompt("1/3 NVIDIA NIM API key", hide_input=True).strip()
    resolved_base_url = base_url.strip() or "https://integrate.api.nvidia.com/v1"
    if sys.stdin.isatty() and not _option_was_explicit("--base-url"):
        resolved_base_url = typer.prompt(
            "2/3 Upstream base URL",
            default=resolved_base_url,
        ).strip()
    resolved_port = int(port)
    if sys.stdin.isatty() and not _option_was_explicit("--port"):
        resolved_port = int(
            typer.prompt(
                "3/3 Local gateway port",
                default=str(port),
            ).strip()
        )
    if not api_key:
        typer.secho("A valid NVIDIA API key is required.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    data = _load_raw_config_template()
    server = dict(data.get("server") or {})
    upstream = dict(data.get("upstream") or {})
    monitoring = dict(data.get("monitoring") or {})
    models = data.get("models") or {}

    server.update({"host": host, "port": resolved_port, "api_key": "ghost-local-..."})
    upstream.update({"base_url": resolved_base_url, "nvidia_api_key": api_key})
    monitoring.setdefault("log_format", "json")
    monitoring.setdefault("log_level", "INFO")
    monitoring.setdefault("prometheus_port", 9090)

    payload = {
        "server": server,
        "upstream": upstream,
        "monitoring": monitoring,
        "models": models,
    }
    _write_local_config(payload)

    typer.secho(f"Ghost local config written to {LOCAL_CONFIG_PATH}", fg=typer.colors.GREEN)
    if is_running():
        _stop_daemon_process(quiet=True)
        time.sleep(0.4)
    _start_daemon_process(quiet=True)
    typer.echo("Ghost is initialized and the gateway has been started.")
    doctor()


@app.command()
def start():
    """Start the GhostLLM daemon."""
    _start_daemon_process()

@app.command()
def serve():
    """Alias for 'start'."""
    start()

@app.command()
def update():
    """Pull the latest repo changes and refresh Python dependencies."""
    repo_remote = _configured_repo_remote()
    if not repo_remote:
        typer.secho(
            "No git remote is configured for this Ghost installation. Configure origin before using update.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    if is_running():
        typer.secho("Ghost is running. Stop it before updating.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    typer.echo("Updating GhostLLM repository...")
    subprocess.run(["git", "pull", "--ff-only"], cwd=root_dir, check=True)
    typer.echo("Syncing Python dependencies...")
    subprocess.run(["uv", "sync"], cwd=root_dir, check=True)
    typer.secho("GhostLLM updated successfully.", fg=typer.colors.GREEN)

@app.command()
def stop():
    """Stop the GhostLLM daemon."""
    _stop_daemon_process()

@app.command()
def logs(n: int = 20):
    """View the last N lines of Ghost logs."""
    log_path = Path(LOG_FILE)
    if log_path.exists():
        with open(log_path, "r") as f:
            lines = f.readlines()
            for line in lines[-n:]:
                print(line.strip())
    else:
        typer.echo("No logs found.")

@app.command()
def status():
    """Check Ghost daemon status."""
    doctor()

@app.command()
def doctor():
    """System health check."""
    from rich.console import Console
    from rich.table import Table
    console = Console()
    
    table = Table(title="Ghost Dev Health Check", show_header=False, border_style="magenta")

    gw = _effective_gateway_url()
    table.add_row("Gateway URL (efectiva)", gw)
    table.add_row("Provider (CLI transport)", "[cyan]openai_compatible[/cyan]")

    try:
        load_config(str(_active_config_path()))
        config_status = "[bold green]Valid[/bold green]"
    except ConfigValidationError as exc:
        config_status = f"[bold red]Invalid[/bold red] [dim]{str(exc)[:120]}[/dim]"

    pf = probe_gateway(gw, api_key=get_api_key(), timeout_sec=1.5)
    daemon_running = is_running() or pf.ok
    d_status = "[bold green]Running[/bold green]" if daemon_running else "[bold red]Stopped[/bold red]"
    table.add_row("Daemon", d_status)
    if pf.ok:
        s_status = f"[bold green]Reachable[/bold green] [dim]({pf.checked_url} → HTTP {pf.status_code})[/dim]"
    else:
        s_status = f"[bold red]Unreachable[/bold red] [dim]{pf.error[:120]}[/dim]"
    table.add_row("Model endpoint", s_status)
    ready, ready_err = check_provider_ready(gw)
    if ready:
        r_status = "[bold green]Ready[/bold green]"
    elif pf.ok:
        # Gateway responde /health pero no está listo (no /ready)
        r_status = (
            "[bold yellow]Gateway healthy but not ready[/bold yellow] "
            f"[dim](/health OK but /ready returned: {ready_err[:100]})[/dim]"
        )
    else:
        r_status = f"[bold red]Not ready[/bold red] [dim]{ready_err[:120]}[/dim]"
    table.add_row("Provider ready", r_status)
    sync_ok, sync_detail = _registry_sync_status(gw)
    if sync_ok:
        sync_status = "[bold green]In sync[/bold green]"
    else:
        sync_status = f"[bold yellow]Stale[/bold yellow] [dim]{sync_detail[:120]}[/dim]"
    table.add_row("Model registry sync", sync_status)
    table.add_row("Config", config_status)

    # Environment
    table.add_row("Project", os.path.basename(os.getcwd()))
    table.add_row("Profile", "Platinum")
    table.add_row("Memory", "[blue].ghost_memory.db[/blue]")
    
    console.print(table)

@app.command()
def claude(
    preflight_only: bool = typer.Option(
        False,
        "--preflight-only",
        help="Validate the Claude bridge path without launching Claude Code.",
    )
):
    """Launch Claude Code integrated with Ghost backend."""
    _recover_pending_filesystem_intents()
    gw = _effective_gateway_url()
    pf = _ensure_gateway_process()
    if not pf.ok:
        from rich.console import Console

        Console(file=sys.stderr).print(format_preflight_failure_console(pf))
        raise typer.Exit(code=2)
    require_provider_for_assistant(gw)
    if _handle_preflight_only("Ghost Claude bridge preflight OK", preflight_only):
        return
    typer.secho("🚀 Launching Claude Code with Ghost Backend...", fg=typer.colors.CYAN)
    os.environ["ANTHROPIC_BASE_URL"] = f"{gw.rstrip('/')}/v1"
    os.environ["ANTHROPIC_API_KEY"] = get_api_key()
    
    try:
        # Check if claude is installed
        subprocess.run("claude --version", shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run("claude", shell=True)
    except:
        typer.secho("✘ Error: 'claude' (Claude Code) no está instalado.", fg=typer.colors.RED)
        typer.echo("Instálalo con: npm install -g @anthropic-ai/claude-code")

@app.command()
def dev(model: str = typer.Option("coder", help="Model to use"), 
        auto_approve: bool = typer.Option(False, "--auto-approve", "-y"),
        profile: Profile = typer.Option(Profile.coder, "--profile", "-p", help="Operation profile"),
        preflight_only: bool = typer.Option(
            False,
            "--preflight-only",
            help="Validate the Ghost Dev command path without entering the runtime loop.",
        )):
    """Launch the Ghost Dev main runtime."""
    _recover_pending_filesystem_intents()
    pf = _preflight_gateway_or_exit()
    if _handle_preflight_only("Ghost Dev preflight OK", preflight_only):
        return
    runtime_prep = _prepare_runtime_for_assistant(profile)
    from apps.cli.assistant import CodexAssistant
    assistant = CodexAssistant(
        pf.base_url,
        get_api_key(),
        model,
        mode="Chat",
        auto_approve=auto_approve,
        profile=profile.value,
        runtime_prep=runtime_prep,
        gateway_preflight=pf,
    )
    assistant.run()

@app.command()
def codex(model: str = typer.Option("coder"), auto_approve: bool = False, profile: Profile = Profile.coder):
    """Alias for 'dev'."""
    dev(model=model, auto_approve=auto_approve, profile=profile)

@app.command()
def chat(model: str = typer.Option("coder"), auto_approve: bool = False, profile: Profile = Profile.coder):
    """Alias for 'dev'."""
    dev(model=model, auto_approve=auto_approve, profile=profile)

@app.command()
def plan(
    task: str = typer.Argument(...),
    model: str = typer.Option("planner"),
    profile: Profile = Profile.architect,
    preflight_only: bool = typer.Option(
        False,
        "--preflight-only",
        help="Validate the planning command path without entering the runtime loop.",
    ),
):
    """Plan a task without changes."""
    _recover_pending_filesystem_intents()
    pf = _preflight_gateway_or_exit()
    if _handle_preflight_only("Ghost Plan preflight OK", preflight_only):
        return
    runtime_prep = _prepare_runtime_for_assistant(profile, initial_task=task)
    from apps.cli.assistant import CodexAssistant
    assistant = CodexAssistant(
        pf.base_url,
        get_api_key(),
        _resolve_plan_command_model(model),
        mode="Plan",
        auto_approve=True,
        profile=profile.value,
        runtime_prep=runtime_prep,
        gateway_preflight=pf,
    )
    assistant.run(initial_task=task, keep_open=False)

@app.command()
def do(task: str = typer.Argument(...), 
       model: str = typer.Option("coder"), 
       auto_approve: bool = typer.Option(True, "-y"),
       profile: Profile = typer.Option(Profile.coder),
       role: str = typer.Option("main", help="The role for this session"),
       swarm_mode: bool = typer.Option(False, "--swarm-mode", help="Enable swarm worker mode"),
       preflight_only: bool = typer.Option(
           False,
           "--preflight-only",
           help="Validate the execute command path without entering the runtime loop.",
       )):
    """Execute a task with autonomous actions."""
    _recover_pending_filesystem_intents()
    pf = _preflight_gateway_or_exit()
    if _handle_preflight_only("Ghost Execute preflight OK", preflight_only):
        return
    runtime_prep = _prepare_runtime_for_assistant(profile, initial_task=task)
    from apps.cli.assistant import CodexAssistant
    assistant = CodexAssistant(
        pf.base_url,
        get_api_key(),
        model,
        mode="Execute",
        auto_approve=auto_approve,
        profile=profile.value,
        role=role,
        is_swarm_worker=swarm_mode,
        runtime_prep=runtime_prep,
        gateway_preflight=pf,
    )
    assistant.run(initial_task=task, keep_open=False)

@app.command()
def edit(
    path: str = typer.Argument(...),
    auto_approve: bool = typer.Option(False, "-y"),
    preflight_only: bool = typer.Option(
        False,
        "--preflight-only",
        help="Validate the patch command path without entering the runtime loop.",
    ),
):
    """Edit a file."""
    _recover_pending_filesystem_intents()
    pf = _preflight_gateway_or_exit()
    if _handle_preflight_only("Ghost Patch preflight OK", preflight_only):
        return
    initial_task = f"Edita el archivo: {path}"
    runtime_prep = _prepare_runtime_for_assistant(Profile.coder, initial_task=initial_task)
    from apps.cli.assistant import CodexAssistant
    assistant = CodexAssistant(
        pf.base_url,
        get_api_key(),
        "coder",
        mode="Patch",
        auto_approve=auto_approve,
        runtime_prep=runtime_prep,
        gateway_preflight=pf,
    )
    assistant.run(initial_task=initial_task, keep_open=False)

@app.command()
def fix(
    task: str | None = typer.Argument(None, help="Specific issue to fix in plain language."),
    auto_approve: bool = typer.Option(True, "-y"),
    preflight_only: bool = typer.Option(
        False,
        "--preflight-only",
        help="Validate the fix command path without entering the runtime loop.",
    ),
):
    """Find and fix project issues."""
    _recover_pending_filesystem_intents()
    pf = _preflight_gateway_or_exit()
    if _handle_preflight_only("Ghost Fix preflight OK", preflight_only):
        return
    initial_task = (task or "").strip() or "Busca errores y corrígelos."
    runtime_prep = _prepare_runtime_for_assistant(Profile.coder, initial_task=initial_task)
    from apps.cli.assistant import CodexAssistant
    assistant = CodexAssistant(
        pf.base_url,
        get_api_key(),
        "coder",
        mode="Fix",
        auto_approve=auto_approve,
        runtime_prep=runtime_prep,
        gateway_preflight=pf,
    )
    assistant.run(initial_task=initial_task, keep_open=False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
