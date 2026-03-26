import typer
import subprocess
import os
import sys
import time
import requests
from pathlib import Path
from enum import Enum
from typing import Optional, Union

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(root_dir / "packages" / "py-core") not in sys.path:
    sys.path.insert(0, str(root_dir / "packages" / "py-core"))

from ghostllm_core.config import resolve_gateway_model_id

from apps.cli.runtime.gateway_endpoint import (
    GatewayPreflightResult,
    format_preflight_failure_console,
    probe_gateway,
    resolve_cli_gateway_url,
)
from apps.cli.runtime.ghost_profile import apply_operational_profile_to_environment
from apps.cli.runtime.runtime_env import (
    enrich_prep_after_operational_profile,
    prepare_runtime,
)

app = typer.Typer(help="👻 Ghost Dev: Premium Native Programming Runtime", no_args_is_help=True)
PID_FILE = str(root_dir / "ghost.pid")
LOG_FILE = str(root_dir / "ghost.log")
REGISTRY_PATH = str(root_dir / "configs" / "models.yaml")


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


def check_provider_ready(base_url: Optional[str] = None) -> tuple[bool, str]:
    base = (base_url or _effective_gateway_url()).rstrip("/")
    try:
        response = requests.get(f"{base}/ready", timeout=1.5)
    except requests.RequestException as e:
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
        requested = os.environ.get("GHOST_PLANNER_MODEL", "").strip() or "kimi"
    return resolve_gateway_model_id(requested, REGISTRY_PATH)


def _preflight_gateway_or_exit() -> GatewayPreflightResult:
    """Antes del loop del asistente: endpoint vivo o salida con diagnóstico (timeout corto)."""
    from rich.console import Console

    base = _effective_gateway_url()
    result = probe_gateway(base, api_key=get_api_key())
    if not result.ok:
        Console(file=sys.stderr).print(format_preflight_failure_console(result))
        raise typer.Exit(code=2)
    require_provider_for_assistant(base)
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
    if os.path.exists(PID_FILE):
        with open(PID_FILE, "r") as f:
            try: return int(f.read().strip())
            except: return None
    return None

def is_running():
    pid = get_pid()
    if pid:
        try:
            if sys.platform == "win32":
                # Check if process exists on windows
                res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
                if str(pid) in res.stdout: return True
            else:
                os.kill(pid, 0)
                return True
        except: pass
    
    # Fallback: probe configured gateway (short timeout)
    try:
        base = _effective_gateway_url()
        requests.get(base + "/health", timeout=0.35)
        return True
    except Exception:
        return False


@app.command()
def start():
    """Start the GhostLLM daemon."""
    if is_running():
        typer.secho("✓ GhostLLM is already running", fg=typer.colors.GREEN)
        return
    
    file_path = root_dir / "apps" / "server" / "main.py"
    # Use a log file for stderr to catch startup errors
    with open(LOG_FILE, "a") as log:
        process = subprocess.Popen([sys.executable, str(file_path)], 
                                 stdout=subprocess.DEVNULL, 
                                 stderr=log,
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0)
    
    with open(PID_FILE, "w") as f:
        f.write(str(process.pid))
    
    # Wait for server to be ready (health es más barato que /v1/models)
    base = _effective_gateway_url()
    for _ in range(40):
        try:
            requests.get(base + "/health", timeout=0.6)
            break
        except Exception:
            time.sleep(0.4)
    else:
        typer.secho(
            f"⚠ Daemon iniciado pero no responde en {base}/health — revisa {LOG_FILE}",
            fg=typer.colors.YELLOW,
        )

    typer.secho("👻 Ghost Dev Daemon started", fg=typer.colors.MAGENTA, bold=True)

@app.command()
def serve():
    """Alias for 'start'."""
    start()

@app.command()
def stop():
    """Stop the GhostLLM daemon."""
    pid = get_pid()
    if pid:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
            if os.path.exists(PID_FILE): os.remove(PID_FILE)
            typer.secho("Ghost Dev stopped", fg=typer.colors.RED)
        except:
            typer.secho("Error stopping Ghost", fg=typer.colors.RED)
    else:
        typer.secho("Ghost is not running", fg=typer.colors.YELLOW)

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

    # Daemon Status
    d_status = "[bold green]Running[/bold green]" if is_running() else "[bold red]Stopped[/bold red]"
    table.add_row("Daemon", d_status)

    pf = probe_gateway(gw, api_key=get_api_key(), timeout_sec=1.5)
    if pf.ok:
        s_status = f"[bold green]Reachable[/bold green] [dim]({pf.checked_url} → HTTP {pf.status_code})[/dim]"
    else:
        s_status = f"[bold red]Unreachable[/bold red] [dim]{pf.error[:120]}[/dim]"
    table.add_row("Model endpoint", s_status)
    ready, ready_err = check_provider_ready(gw)
    r_status = "[bold green]Ready[/bold green]" if ready else f"[bold red]Not ready[/bold red] [dim]{ready_err[:120]}[/dim]"
    table.add_row("Provider ready", r_status)

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
    if not is_running():
        start()
    gw = _effective_gateway_url()
    pf = probe_gateway(gw, api_key=get_api_key())
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
def dev(model: str = typer.Option("kimi", help="Model to use"), 
        auto_approve: bool = typer.Option(False, "--auto-approve", "-y"),
        profile: Profile = typer.Option(Profile.coder, "--profile", "-p", help="Operation profile"),
        preflight_only: bool = typer.Option(
            False,
            "--preflight-only",
            help="Validate the Ghost Dev command path without entering the runtime loop.",
        )):
    """Launch the Ghost Dev main runtime."""
    if not is_running():
        start()
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
def codex(model: str = typer.Option("kimi"), auto_approve: bool = False, profile: Profile = Profile.coder):
    """Alias for 'dev'."""
    dev(model=model, auto_approve=auto_approve, profile=profile)

@app.command()
def chat(model: str = typer.Option("kimi"), auto_approve: bool = False, profile: Profile = Profile.coder):
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
    if not is_running():
        start()
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
    assistant.run(initial_task=task)

@app.command()
def do(task: str = typer.Argument(...), 
       model: str = typer.Option("kimi"), 
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
    if not is_running():
        start()
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
    assistant.run(initial_task=task)

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
    if not is_running():
        start()
    pf = _preflight_gateway_or_exit()
    if _handle_preflight_only("Ghost Patch preflight OK", preflight_only):
        return
    initial_task = f"Edita el archivo: {path}"
    runtime_prep = _prepare_runtime_for_assistant(Profile.coder, initial_task=initial_task)
    from apps.cli.assistant import CodexAssistant
    assistant = CodexAssistant(
        pf.base_url,
        get_api_key(),
        "kimi",
        mode="Patch",
        auto_approve=auto_approve,
        runtime_prep=runtime_prep,
        gateway_preflight=pf,
    )
    assistant.run(initial_task=initial_task)

@app.command()
def fix(
    auto_approve: bool = typer.Option(True, "-y"),
    preflight_only: bool = typer.Option(
        False,
        "--preflight-only",
        help="Validate the fix command path without entering the runtime loop.",
    ),
):
    """Find and fix project issues."""
    if not is_running():
        start()
    pf = _preflight_gateway_or_exit()
    if _handle_preflight_only("Ghost Fix preflight OK", preflight_only):
        return
    initial_task = "Busca errores y corrígelos."
    runtime_prep = _prepare_runtime_for_assistant(Profile.coder, initial_task=initial_task)
    from apps.cli.assistant import CodexAssistant
    assistant = CodexAssistant(
        pf.base_url,
        get_api_key(),
        "kimi",
        mode="Fix",
        auto_approve=auto_approve,
        runtime_prep=runtime_prep,
        gateway_preflight=pf,
    )
    assistant.run(initial_task="Busca errores y corrígelos.")


if __name__ == "__main__":
    app()
