import os
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.markup import escape
from rich.syntax import Syntax
from rich import box
from rich.table import Table
from typing import Dict, Any, List

class GhostRenderer:
    def __init__(self, console: Console):
        self.console = console

    def print_header(self, product_name: str, mode: str, profile: str, model: str):
        """Display the professional product header."""
        header_text = Text()
        header_text.append(" GHOST | ", style="bold magenta")
        header_text.append(f"{product_name}", style="bold white")
        header_text.append(f" {' ' * 20} Mode: ", style="dim")
        header_text.append(f"{mode}", style="bold cyan")
        header_text.append(" | Profile: ", style="dim")
        header_text.append(f"{profile}", style="bold green")
        header_text.append(f"\n Model: ", style="dim")
        header_text.append(f"{model}", style="white")
        header_text.append(" " * 27 + " Tools: ", style="dim")
        header_text.append("Ready", style="bold green")
        header_text.append(" | Context: ", style="dim")
        header_text.append("Live", style="bold blue")

        self.console.print(Panel(header_text, box=box.ROUNDED, padding=(0, 1)))

    def print_tool_trace(self, name: str, target: str):
        """Log a tool execution in a professional, dimmed style."""
        self.console.print(f" [dim]⚙ tool execution: {name} {target}[/dim]")

    def print_success(self, message: str):
        self.console.print(f" [bold green]✓[/bold green] {message}")

    def print_error(self, message: str):
        self.console.print(f" [bold red]✘[/bold red] {message}")

    def render_diff(self, path: str, diff_text: str):
        """Display code changes in a syntax-highlighted panel."""
        if diff_text:
            self.console.print(Panel(
                Syntax(diff_text, "diff", theme="monokai", line_numbers=True), 
                title=f"Δ {path}", 
                border_style="cyan"
            ))

    def render_local_inspection(self, stats: Dict[str, Any]):
        """Special UI for structural local inspection results."""
        if "error" in stats:
            self.print_error(f"Local inspection failed: {stats['error']}")
            return

        res = f"\n[bold green]✓ RESULTADO DE INSPECCIÓN LOCAL EXACTO:[/bold green]\n"
        res += f"  1) Primera línea exacta: [cyan]`{escape(stats['first_line'])}`[/cyan]\n"
        res += f"  2) Total de líneas: [bold blue]{stats['total_lines']}[/bold blue]\n"
        res += f"  3) Línea exacta encontrada: [cyan]`{escape(stats['found_line'])}`[/cyan] (Índice: {stats['found_index']})\n"
        res += f"  4) Versión detectada: [yellow]{stats['version_line']}[/yellow]\n"
        res += f"\n[dim]Nota: Datos extraídos mediante el Extractor Local (By-passing LLM).[/dim]\n"
        self.console.print(res)

    def status_context(self, initial_state: str = "thinking"):
        """Context manager for professional status management."""
        messages = {
            "thinking": "[bold magenta]Thinking...[/bold magenta]",
            "waiting": "[bold magenta]Waiting for model...[/bold magenta]",
            "tool_exec": "[bold cyan]Executing tool...[/bold cyan]",
            "tool_result": "[bold blue]Processing result...[/bold blue]",
            "building": "[bold green]Building response...[/bold green]",
            "ready": "[bold green]Ready[/bold green]"
        }
        initial_msg = messages.get(initial_state, initial_state)
        return self.console.status(initial_msg, spinner="dots9")

    def update_status(self, status_obj, state: str):
        """Update the message of an active status spinner."""
        messages = {
            "thinking": "[bold magenta]Thinking...[/bold magenta]",
            "waiting": "[bold magenta]Waiting for model...[/bold magenta]",
            "tool_exec": "[bold cyan]Executing tool...[/bold cyan]",
            "tool_result": "[bold blue]Processing result...[/bold blue]",
            "building": "[bold green]Building response...[/bold green]",
            "ready": "[bold green]Ready[/bold green]"
        }
        msg = messages.get(state, state)
        status_obj.update(msg)

    def render_mode_change(self, new_mode: str):
        """Display a banner for mode transitions."""
        self.console.print(f"\n[bold green]✓ Modo cambiado a: {new_mode}[/bold green]")

    def render_help(self):
        """Display the professional help menu."""
        help_table = Table(title="👻 Ghost Dev Commands", box=box.SIMPLE, header_style="bold magenta")
        help_table.add_column("Command", style="cyan")
        help_table.add_column("Description", style="white")
        
        help_table.add_row("/plan <task>", "Architectural analysis & strategy (Read-only)")
        help_table.add_row("/do <task>", "Autonomous implementation & execution")
        help_table.add_row("/edit <file>", "Direct surgical patching mode")
        help_table.add_row("/mode <mode>", "Switch between: Chat, Plan, Execute, Review, Fix")
        help_table.add_row("/debug [on|off]", "Toggle diagnostic trace output")
        help_table.add_row("/clear", "Reset session conversation history")
        help_table.add_row("/help", "Show this mastery menu")
        
        self.console.print("\n")
        self.console.print(help_table)
        self.console.print("[dim]Use 'exit' or 'quit' to shutdown the runtime safely.[/dim]\n")
    def render_task_list(self, tasks: List[Dict[str, Any]]):
        """Display a table of persistent tasks."""
        if not tasks:
            self.print_error("No persistent tasks found.")
            return

        table = Table(title="👻 Ghost Persistent Tasks", box=box.SIMPLE, header_style="bold magenta")
        table.add_column("Task ID", style="cyan")
        table.add_column("Title", style="white")
        table.add_column("Status", style="bold")
        table.add_column("Updated At", style="dim")

        for t in tasks:
            status = t["status"]
            style = "green" if status == "done" else "red" if status == "failed" else "blue"
            table.add_row(t["id"], t["title"][:50] + "..." if len(t["title"]) > 50 else t["title"], f"[{style}]{status.upper()}[/{style}]", t["updated_at"])

        self.console.print("\n")
        self.console.print(table)
        self.console.print("[dim]Use '/resume <id>' to continue a specific task.[/dim]\n")

    def render_swarm_board(self, workers: List[Any]):
        """Display a professional dashboard for active swarm workers."""
        table = Table(title="[bold yellow]Ghost Swarm Board (v0)[/bold yellow]", box=box.DOUBLE, header_style="bold yellow")
        table.add_column("Worker ID", style="cyan")
        table.add_column("Role", style="magenta")
        table.add_column("Task ID", style="dim")
        table.add_column("Status", style="bold")
        table.add_column("Worktree Path", style="dim", overflow="fold")

        for w in workers:
            status = getattr(w, "status", "unknown")
            style = "green" if status == "active" else "red" if status == "terminated" else "white"
            icon = "●" if status == "active" else "○"
            table.add_row(
                w.worker_id,
                w.role.upper(),
                w.task_id,
                f"[{style}]{icon} {status}[/{style}]",
                w.worktree_path or "N/A"
            )
        
        if not workers:
            self.console.print("\n[dim]No active workers in swarm.[/dim]\n")
        else:
            self.console.print("\n")
            self.console.print(table)
            self.console.print("\n")

    def read_input(self) -> str:
        """Read user input with a professional prompt."""
        try:
            return self.console.input("[bold magenta]👤 denis:[/bold magenta] ").strip()
        except (KeyboardInterrupt, EOFError):
            return "exit"

    def render_verification_results(self, results: Dict[str, Any]):
        """Special UI for verification summary (Build, Lint, Tests)."""
        if results.get("status") == "skipped":
            # Very quiet skip
            return

        table = Table(box=box.MINIMAL, show_header=True, header_style="bold magenta", padding=(0, 2))
        table.add_column("Integrity Check", style="cyan")
        table.add_column("Status", style="bold")
        
        has_failures = False
        for check in results.get("checks", []):
            status = check["status"]
            style = "green" if status == "success" else "red"
            icon = "✓" if status == "success" else "✘"
            table.add_row(check["name"], f"[{style}]{icon} {status.upper()}[/{style}]")
            if status != "success": has_failures = True
        
        self.console.print("\n")
        self.console.print(Panel(table, title="🛡️ [bold magenta]GHOST INTEGRITY[/bold magenta]", border_style="magenta", expand=False))
        
        if has_failures:
            self.console.print("[bold red]⚠ SYSTEM INTEGRITY ALERT:[/bold red] Some checks failed. Reviewing logs...\n")
            for check in results.get("checks", []):
                if check["status"] != "success":
                    self.console.print(f"[red]>>> {check['name']} Output:[/red]")
                    self.console.print(Panel(check.get('stderr') or check.get('stdout') or "No output", style="dim red"))
        else:
            self.console.print(" [bold green]✓ ALL SYSTEMS CLEAR[/bold green]\n")

    def render_artifact_summary(self, artifact: Dict[str, Any]):
        """Render a full task outcome bundle."""
        self.console.print("\n" + "─" * 40)
        self.console.print("[bold magenta]👻 GHOST ARTIFACT BUNDLE[/bold magenta]")
        self.console.print(f"[dim]Session: {artifact['session_id']} | {artifact['timestamp']}[/dim]\n")
        
        # 1. Plan & Root Cause
        if artifact.get("plan"):
            self.console.print(Panel(artifact["plan"], title="🎯 PLAN", border_style="cyan"))
        if artifact.get("root_cause"):
            self.console.print(Panel(artifact["root_cause"], title="🔍 DIAGNOSTIC", border_style="yellow"))
            
        # 2. Diff Summary
        if artifact.get("diff_summary"):
            table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
            table.add_column("File", style="cyan")
            table.add_column("Change", style="dim")
            for item in artifact["diff_summary"]:
                table.add_row(item["file"], item["type"])
            self.console.print(Panel(table, title="🛠️ CHANGES", border_style="blue"))

        # 3. Next Action
        if artifact.get("next_action"):
            self.console.print(Panel(artifact["next_action"], title="🚀 NEXT ACTION", border_style="green"))
            
        self.console.print(f"[dim]Saved to: .ghost/artifacts/{artifact['session_id']}.md[/dim]")
        self.console.print("─" * 40 + "\n")
