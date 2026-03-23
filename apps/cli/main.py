import typer
import subprocess
import os
import sys
import time
import requests
from pathlib import Path
from enum import Enum
from typing import Optional

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(root_dir / "packages" / "py-core") not in sys.path:
    sys.path.insert(0, str(root_dir / "packages" / "py-core"))

from ghostllm_core.config import load_registry

app = typer.Typer(help="👻 Ghost Dev: Premium Native Programming Runtime", no_args_is_help=True)
PID_FILE = str(root_dir / "ghost.pid")
LOG_FILE = str(root_dir / "ghost.log")
SERVER_URL = "http://127.0.0.1:11434"

class Profile(str, Enum):
    fast = "fast"
    coder = "coder"
    architect = "architect"

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
    
    # Fallback: probe the port
    try:
        requests.get(SERVER_URL + "/v1/models", timeout=0.1)
        return True
    except:
        return False

def get_api_key():
    return os.getenv("GHOST_API_KEY", "ghost-dev-2026")

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
    
    # Wait for server to be ready
    for _ in range(10):
        try:
            requests.get(SERVER_URL + "/v1/models", timeout=1)
            break
        except:
            time.sleep(0.5)
            
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
    
    table = Table(title="🔍 Ghost Dev Health Check", show_header=False, border_style="magenta")
    
    # Daemon Status
    d_status = "[bold green]Running[/bold green]" if is_running() else "[bold red]Stopped[/bold red]"
    table.add_row("Daemon", d_status)
    
    # Server Connectivity
    try:
        resp = requests.get(SERVER_URL + "/v1/models", timeout=1)
        s_status = f"[bold green]Online[/bold green] ({resp.status_code})"
    except:
        s_status = "[bold red]Offline[/bold red]"
    table.add_row("API Server", s_status)
    
    # Environment
    table.add_row("Project", os.path.basename(os.getcwd()))
    table.add_row("Profile", "Platinum")
    table.add_row("Memory", "[blue].ghost_memory.db[/blue]")
    
    console.print(table)

@app.command()
def claude():
    """Launch Claude Code integrated with Ghost backend."""
    if not is_running(): start()
    typer.secho("🚀 Launching Claude Code with Ghost Backend...", fg=typer.colors.CYAN)
    os.environ["ANTHROPIC_BASE_URL"] = f"{SERVER_URL}/v1"
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
        profile: Profile = typer.Option(Profile.coder, "--profile", "-p", help="Operation profile")):
    """Launch the Ghost Dev main runtime."""
    if not is_running(): start()
    from apps.cli.assistant import CodexAssistant
    assistant = CodexAssistant(SERVER_URL, get_api_key(), model, mode="Chat", auto_approve=auto_approve, profile=profile.value)
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
def plan(task: str = typer.Argument(...), model: str = typer.Option("planner"), profile: Profile = Profile.architect):
    """Plan a task without changes."""
    if not is_running(): start()
    from apps.cli.assistant import CodexAssistant
    assistant = CodexAssistant(SERVER_URL, get_api_key(), model, mode="Plan", auto_approve=True, profile=profile.value)
    assistant.run(initial_task=task)

@app.command()
def do(task: str = typer.Argument(...), 
       model: str = typer.Option("kimi"), 
       auto_approve: bool = typer.Option(True, "-y"),
       profile: Profile = typer.Option(Profile.coder),
       role: str = typer.Option("main", help="The role for this session"),
       swarm_mode: bool = typer.Option(False, "--swarm-mode", help="Enable swarm worker mode")):
    """Execute a task with autonomous actions."""
    if not is_running(): start()
    from apps.cli.assistant import CodexAssistant
    assistant = CodexAssistant(
        SERVER_URL, 
        get_api_key(), 
        model, 
        mode="Execute", 
        auto_approve=auto_approve, 
        profile=profile.value,
        role=role,
        is_swarm_worker=swarm_mode
    )
    assistant.run(initial_task=task)

@app.command()
def edit(path: str = typer.Argument(...), auto_approve: bool = typer.Option(False, "-y")):
    """Edit a file."""
    if not is_running(): start()
    from apps.cli.assistant import CodexAssistant
    assistant = CodexAssistant(SERVER_URL, get_api_key(), "kimi", mode="Patch", auto_approve=auto_approve)
    assistant.run(initial_task=f"Edita el archivo: {path}")

@app.command()
def fix(auto_approve: bool = typer.Option(True, "-y")):
    """Find and fix project issues."""
    if not is_running(): start()
    from apps.cli.assistant import CodexAssistant
    assistant = CodexAssistant(SERVER_URL, get_api_key(), "kimi", mode="Fix", auto_approve=auto_approve)
    assistant.run(initial_task="Busca errores y corrígelos.")

if __name__ == "__main__":
    app()

if __name__ == "__main__":
    app()
