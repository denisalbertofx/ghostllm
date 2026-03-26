"""
GhostRenderer — única implementación UI usada por CodexAssistant.

La interfaz pública de este módulo debe coincidir con todas las llamadas
``self.renderer.<method>(...)`` en ``assistant.py``. Cualquier kwargs nuevo
en el asistente debe aceptarse aquí (p. ej. ``append_tool_trace(..., auto_approved=)``).

Ver: docs/GHOST_RENDERER_ASSISTANT_CONTRACT.md
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.markup import escape
from rich.syntax import Syntax
from rich import box
from rich.table import Table


def _tool_ui_mode() -> str:
    return os.getenv("GHOST_TOOL_UI", "compact").strip().lower()


def _feature_flag_visible(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return bool(raw) and raw not in ("0", "false", "off", "no")


def _verification_normalize_row(check: Dict[str, Any]) -> Tuple[str, str, str, bool]:
    """
    (name, display_label, style_name, executed_failed).

    executed_failed: True solo si provenance=executed y el check falló de verdad.
    """
    name = str(check.get("name") or "?")
    st = str(check.get("status") or "").lower()
    prov = str(check.get("provenance") or "")
    executed = prov == "executed"

    if not executed:
        if st in ("passed", "success"):
            return name, "NOT RUN", "yellow", False
        if st in ("not_run", "skipped") or "not_run" in prov.lower():
            return name, "NOT RUN", "yellow", False
        if not st:
            return name, "NOT RUN", "yellow", False
        return name, st.upper().replace("_", " "), "yellow", False

    exit_code = check.get("exit_code")
    if exit_code is None:
        exit_code = 0 if st in ("passed", "success") else 1
    try:
        exit_code_i = int(exit_code)
    except (TypeError, ValueError):
        exit_code_i = 1

    if st in ("passed", "success") and exit_code_i == 0:
        return name, "PASSED", "green", False
    if st in ("passed", "success") and exit_code_i != 0:
        return name, "FAILED", "red", True
    if st in ("failed", "error"):
        return name, "FAILED", "red", True
    return name, st.upper().replace("_", " "), "red", True


class GhostRenderer:
    def __init__(self, console: Console):
        self.console = console
        self._tool_segment_active: bool = False
        self._tool_segment_buffer: List[Dict[str, Any]] = []

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
            self.console.print(
                Panel(
                    Syntax(diff_text, "diff", theme="monokai", line_numbers=True),
                    title=f"Δ {path}",
                    border_style="cyan",
                )
            )

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
            "ready": "[bold green]Ready[/bold green]",
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
            "ready": "[bold green]Ready[/bold green]",
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
            table.add_row(
                t["id"],
                t["title"][:50] + "..." if len(t["title"]) > 50 else t["title"],
                f"[{style}]{status.upper()}[/{style}]",
                t["updated_at"],
            )

        self.console.print("\n")
        self.console.print(table)
        self.console.print("[dim]Use '/resume <id>' to continue a specific task.[/dim]\n")

    def render_swarm_board(self, workers: List[Any]):
        """Display a professional dashboard for active swarm workers."""
        table = Table(
            title="[bold yellow]Ghost Swarm Board (v0)[/bold yellow]",
            box=box.DOUBLE,
            header_style="bold yellow",
        )
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
                w.worktree_path or "N/A",
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
        """
        Verification summary with provenance-aware labels.

        No muestra ALL SYSTEMS CLEAR si ningún check tuvo provenance=executed.
        """
        if results.get("status") == "skipped":
            return

        checks: List[Dict[str, Any]] = list(results.get("checks") or [])
        if not checks:
            return

        rows = [_verification_normalize_row(c) for c in checks]
        any_executed_failed = any(r[3] for r in rows)
        any_executed = any(str(c.get("provenance") or "") == "executed" for c in checks)

        steps_executed = results.get("steps_executed_count")
        if steps_executed is None:
            steps_executed = sum(1 for c in checks if str(c.get("provenance") or "") == "executed")

        global_failed = str(results.get("status") or "").lower() == "failed" or any_executed_failed

        table = Table(box=box.MINIMAL, show_header=True, header_style="bold magenta", padding=(0, 2))
        table.add_column("Integrity Check", style="cyan")
        table.add_column("Status", style="bold")

        for (_name, label, style, _xf) in rows:
            icon = "✓" if label == "PASSED" else "✘"
            table.add_row(_name, f"[{style}]{icon} {label}[/{style}]")

        self.console.print("\n")
        self.console.print(
            Panel(
                table,
                title="🛡️ [bold magenta]GHOST INTEGRITY[/bold magenta]",
                border_style="magenta",
                expand=False,
            )
        )

        if global_failed:
            self.console.print("[bold red]System status: FAILED[/bold red]")

        status_lower = str(results.get("status") or "").lower()
        if steps_executed == 0 and not any_executed and status_lower == "incomplete":
            self.console.print(
                "[bold yellow]No verification checks were actually executed[/bold yellow] "
                "(steps_executed_count=0; none ran on disk).\n"
            )

        show_stderr_for: List[Dict[str, Any]] = [
            c
            for c in checks
            if str(c.get("provenance") or "") == "executed"
            and str(c.get("status") or "").lower() not in ("passed", "success")
        ]

        if show_stderr_for:
            self.console.print(
                "[bold red]⚠ SYSTEM INTEGRITY ALERT:[/bold red] Some checks failed. Reviewing logs...\n"
            )
            for check in show_stderr_for:
                self.console.print(f"[red]>>> {check.get('name', '?')} Output:[/red]")
                self.console.print(
                    Panel(
                        check.get("stderr") or check.get("stdout") or "No output",
                        style="dim red",
                    )
                )

        can_all_systems_clear = (
            not global_failed
            and any_executed
            and not (steps_executed == 0 and not any_executed and status_lower == "incomplete")
        )
        if can_all_systems_clear:
            self.console.print(" [bold green]✓ ALL SYSTEMS CLEAR[/bold green]\n")

    def render_artifact_summary(self, artifact: Dict[str, Any]):
        """Render a full task outcome bundle."""
        self.console.print("\n" + "─" * 40)
        self.console.print("[bold magenta]👻 GHOST ARTIFACT BUNDLE[/bold magenta]")
        sid = artifact.get("session_id", "?")
        ts = artifact.get("timestamp", "")
        self.console.print(f"[dim]Session: {sid} | {ts}[/dim]\n")

        trace_md = (artifact.get("trace_detail_md") or "").strip()
        trace_short = (artifact.get("trace_summary_short") or "").strip()
        trace_path = (artifact.get("trace_path") or "").strip()
        if trace_md:
            self.console.print(
                Panel(trace_md, title="SESSION TRACE", border_style="dim", expand=False)
            )
        elif trace_short or trace_path:
            blob = trace_short or trace_path
            self.console.print(
                Panel(escape(str(blob)), title="SESSION TRACE", border_style="dim", expand=False)
            )

        spec_body = artifact.get("taskspec") or artifact.get("contract_spec")
        if isinstance(spec_body, dict) and spec_body:
            try:
                spec_txt = json.dumps(spec_body, indent=2, ensure_ascii=False)
            except (TypeError, ValueError):
                spec_txt = str(spec_body)
            self.console.print(Panel(spec_txt, title="CONTRACT SPEC", border_style="blue", expand=False))

        rp = artifact.get("repo_profile")
        if isinstance(rp, dict) and rp:
            try:
                rp_txt = json.dumps(rp, indent=2, ensure_ascii=False)
            except (TypeError, ValueError):
                rp_txt = str(rp)
            self.console.print(Panel(rp_txt, title="REPO PROFILE", border_style="green", expand=False))

        if artifact.get("plan"):
            self.console.print(Panel(artifact["plan"], title="🎯 PLAN", border_style="cyan"))
        if artifact.get("root_cause"):
            self.console.print(Panel(artifact["root_cause"], title="🔍 DIAGNOSTIC", border_style="yellow"))

        if artifact.get("diff_summary"):
            table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
            table.add_column("File", style="cyan")
            table.add_column("Change", style="dim")
            for item in artifact["diff_summary"]:
                table.add_row(item["file"], item["type"])
            self.console.print(Panel(table, title="🛠️ CHANGES", border_style="blue"))

        if artifact.get("next_action"):
            self.console.print(Panel(artifact["next_action"], title="🚀 NEXT ACTION", border_style="green"))

        self.console.print(f"[dim]Saved to: .ghost/artifacts/{sid}.md[/dim]")
        self.console.print("─" * 40 + "\n")

    # ─── CodexAssistant contract ─────────────────────────────────────────────

    def render_cli_startup_summary(
        self,
        command_mode: str = "dev",
        assistant_mode: str = "Chat",
        model: str = "",
        prep=None,
        cwd_same_as_repo: bool = True,
        role_models=None,
        *,
        llm_gateway_url: str = "",
        llm_gateway_reachable: Optional[bool] = None,
        llm_gateway_checked: str = "",
        provider_backend_label: str = "",
        **kwargs: Any,
    ):
        """Render startup banner showing active mode, model, gateway URL and feature flags."""
        _ = kwargs
        flags = []
        if prep and hasattr(prep, "active_feature_flags"):
            for k, v in (prep.active_feature_flags or {}).items():
                if _feature_flag_visible(v):
                    flags.append(k)
        flags_line = "  ".join(flags) if flags else "—"
        self.console.print(
            f"\n[bold magenta]👻 Ghost[/bold magenta] "
            f"[dim]{command_mode}[/dim]  "
            f"[cyan]{assistant_mode}[/cyan]  "
            f"[dim]model:[/dim] [white]{model}[/white]  "
            f"[dim]flags:[/dim] [dim]{flags_line}[/dim]\n"
        )
        if llm_gateway_url:
            if llm_gateway_reachable is True:
                gw_line = (
                    f"[dim]gateway:[/dim] [cyan]{escape(llm_gateway_url)}[/cyan] "
                    f"[bold green]reachable[/bold green]"
                )
                if llm_gateway_checked:
                    gw_line += f" [dim]({escape(llm_gateway_checked)})[/dim]"
            elif llm_gateway_reachable is False:
                gw_line = (
                    f"[dim]gateway:[/dim] [cyan]{escape(llm_gateway_url)}[/cyan] "
                    f"[bold red]unreachable[/bold red]"
                )
            else:
                gw_line = f"[dim]gateway:[/dim] [cyan]{escape(llm_gateway_url)}[/cyan] [dim](sin preflight)[/dim]"
            self.console.print(gw_line)
        if provider_backend_label:
            self.console.print(
                f"[dim]provider (transporte):[/dim] [white]{escape(provider_backend_label)}[/white]"
            )
        if role_models and isinstance(role_models, dict) and role_models:
            rm = ", ".join(f"{k}={v}" for k, v in list(role_models.items())[:6])
            self.console.print(f"[dim]role_models:[/dim] [dim]{escape(rm)}[/dim]")
        self.console.print("")

    def begin_tool_segment(self) -> None:
        """Start buffering tool trace lines for one model tool round."""
        self._tool_segment_active = True
        self._tool_segment_buffer = []

    def flush_tool_segment(self) -> None:
        """Flush buffered tool traces (panel or compact)."""
        self._tool_segment_active = False
        if not self._tool_segment_buffer:
            return
        mode = _tool_ui_mode()
        if mode == "panel":
            t = Table(
                title="Herramientas (lote del modelo)",
                box=box.SIMPLE,
                header_style="bold cyan",
            )
            t.add_column("Herramienta", style="cyan")
            t.add_column("Objetivo", style="white", overflow="fold")
            t.add_column("Estado", style="dim")
            for row in self._tool_segment_buffer:
                t.add_row(row["name"], row.get("detail") or "", row.get("estado") or "")
            self.console.print("\n")
            self.console.print(t)
            self.console.print("")
        else:
            for row in self._tool_segment_buffer:
                self.console.print(row.get("line") or "")
        self._tool_segment_buffer = []

    def append_tool_trace(
        self,
        name: str,
        detail: str,
        ok: Optional[bool] = None,
        *,
        auto_approved: Optional[bool] = None,
        **kwargs: Any,
    ) -> None:
        """
        Registra una herramienta.

        - Llamadas legacy: ``append_tool_trace(name, detail, False)`` → ``ok`` bool finalizado.
        - Pre-exec (assistant ``_execute_tool``): solo ``auto_approved=...`` → línea neutra.
        - ``**kwargs`` absorbe argumentos futuros sin romper el runtime.
        """
        _ = kwargs  # extensión futura / compat

        if ok is not None:
            icon = "[green]✓[/green]" if ok else "[red]✘[/red]"
            estado = "ok" if ok else "error"
            line = f" [dim]⚙ {escape(str(name))}[/dim] · {icon} {escape(str(detail))}"
        else:
            tag = (
                "[dim](auto-approved)[/dim]"
                if auto_approved
                else "[dim](approval required)[/dim]"
                if auto_approved is False
                else ""
            )
            estado = "auto" if auto_approved else "pending"
            line = f" [dim]⚙ {escape(str(name))}[/dim] · [cyan]⋯[/cyan] {escape(str(detail))} {tag}".strip()

        payload = {"name": name, "detail": detail, "estado": estado, "line": line}

        if self._tool_segment_active:
            self._tool_segment_buffer.append(payload)
        else:
            self.console.print(line)

    def render_budget_status(self, remaining: int, total: int):
        """Show autonomy budget bar (compact)."""
        if total <= 0:
            return
        pct = int((remaining / total) * 100)
        color = "green" if pct > 50 else "yellow" if pct > 20 else "red"
        self.console.print(
            f" [dim]Budget:[/dim] [{color}]{remaining}/{total}[/{color}]",
            end="\r",
        )

    def render_autonomy_checkpoint(self, checkpoint):
        """Display an autonomy / stagnation checkpoint."""
        reason = getattr(checkpoint, "reason", None) or str(checkpoint)
        self.console.print(
            f"\n[bold yellow]⚠ Autonomy checkpoint:[/bold yellow] [dim]{escape(str(reason))}[/dim]\n"
        )

    def render_bundled_approval_request(self, actions) -> bool:
        """Ask user to approve a bundle of write/shell actions. Returns True if approved."""
        self.console.print("\n[bold yellow]⚠ Approval required for:[/bold yellow]")
        for a in actions:
            self.console.print(f"  • [cyan]{a['name']}[/cyan] {a.get('detail', '')}")
        try:
            ans = self.console.input("[bold]Approve all? (y/n): [/bold]").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans == "y"

    def render_verify_batch_approval_request(self, shell_commands) -> bool:
        """Ask user to approve a batch of verification shell commands."""
        self.console.print("\n[bold yellow]⚠ Run verification commands?[/bold yellow]")
        for cmd in shell_commands:
            self.console.print(f"  [dim]$ {cmd}[/dim]")
        try:
            ans = self.console.input("[bold]Approve? (y/n): [/bold]").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans == "y"

    def render_merge_preview(self, bundle):
        """Display merge preview bundle."""
        self.console.print(
            f"\n[bold cyan]Merge Preview:[/bold cyan] risk=[yellow]{getattr(bundle, 'conflict_risk', 'unknown')}[/yellow]"
        )

    def render_merge_success(self, task_id: str, files):
        """Display successful merge confirmation."""
        self.console.print(
            f"\n[bold green]✓ Merge applied:[/bold green] {task_id} ({len(files or [])} file(s))"
        )

    def render_review_bundle(self, bundle):
        """Display review bundle."""
        self.console.print(f"\n[bold magenta]Review Bundle:[/bold magenta] {getattr(bundle, 'task_id', '?')}")

    def render_review_decision(self, task_id: str, decision: str):
        """Display review decision confirmation."""
        icon = "✓" if decision == "approved" else "✘"
        color = "green" if decision == "approved" else "red"
        self.console.print(f"\n[bold {color}]{icon} Review {decision}:[/bold {color}] {task_id}")

    def print_help(self):
        """Alias for render_help (used by assistant)."""
        self.render_help()
