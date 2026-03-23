import os
import sys
import json
import re
import time
import subprocess
import difflib
from typing import List, Dict, Any, Optional
import requests
from dataclasses import asdict
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.status import Status
from rich.markup import escape
from rich.table import Table
from rich import box

from .runtime.intent_router import IntentRouter, Intent
from .runtime.policy_gate import PolicyGate
from .runtime.verification import VerificationManager
from .runtime.artifacts import ArtifactManager
from .runtime.hooks import HookManager, HookEvents
from .runtime.tasks import TaskManager
from .runtime.swarm import SwarmManager
from .indexer import RepoIndexer
from .ui.renderer import GhostRenderer
from ghostllm_core.memory import MemoryStore

class CodexAssistant:
    SYSTEM_PROMPT = """
# GHOST CORE | Integrity First
You are Ghost, the high-performance evolution of the AXI framework.
Your primary goal is to safely and efficiently maintain project integrity.

# Build-Fix Safe Mode
1. **INTEGRITY FIRST**: Before any repair or multi-file edit, you MUST verify the current state.
2. **SURGICAL EDITS**: Avoid rewriting entire files. Use `edit_file` whenever possible to minimize risk.
3. **POLICY BOUNDARIES**: You already have a Policy Gate active. If a tool call is safe and within intent, proceed.
4. **WINDOWS NATIVE**: Always use PowerShell-compatible commands.
5. **ERROR CLASSIFICATION**: Before fixing, classify the error as (SYNTAX_ERROR, DEPENDENCY_MISSING, LOGIC_BUG, ASSET_MISSING).

# Mode Boundaries
- **Plan**: Research only. No `write_file`, `edit_file`, or `run_shell`.
- **Code/Fix/Debug**: Active execution permitted.

# Tool Call Format
Use JSON within <tool_call> tags. Example:
<tool_call>{{"name": "read_file", "arguments": {{"path": "main.py"}}}}</tool_call>

# Automatic Integrity Check
- Ghost performs a verification cycle (Build/Lint/Tests) automatically at the end of every task.
- Do NOT run manual verification tools unless you specifically need to debug a verification failure.
"""

    NATIVE_TOOLS = [
        {"name": "ls", "description": "List files in a directory", "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}},
        {"name": "read_file", "description": "Read file content", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "write_file", "description": "Write entire file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
        {"name": "edit_file", "description": "Surgical string replacement", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}}, "required": ["path", "old_str", "new_str"]}},
        {"name": "run_shell", "description": "Execute PowerShell command", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "delete_file", "description": "Delete a file with mandatory evidence", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["path", "evidence"]}},
        {"name": "summarize_repo", "description": "Get high-level project summary", "parameters": {"type": "object", "properties": {}}}
    ]

    def __init__(self, server_url: str, api_key: str, model: str, mode: str = "Chat", auto_approve: bool = False, profile: str = "coder", project_name: str = "GhostLLM", role: str = "main", is_swarm_worker: bool = False):
        self.role = role
        self.is_swarm_worker = is_swarm_worker
        
        # Override mode based on role
        if role == "architect": mode = "Plan"
        elif role == "reviewer": mode = "Review"
        
        self.server_url = server_url
        self.api_key = api_key
        self.model = model
        self.mode = mode
        self.auto_approve = auto_approve
        self.profile = profile
        self.project_name = project_name
        self.history = []
        self.session_id = f"ghost_{int(time.time())}"
        self.cwd = os.getcwd()
        self.memory = MemoryStore()
        
        # UI & Runtime
        self.console = Console()
        self.renderer = GhostRenderer(self.console)
        self.intent_router = IntentRouter()
        self.policy_gate = PolicyGate(self.console, self.auto_approve)
        self.verification_manager = VerificationManager(self.cwd)
        self.artifact_manager = ArtifactManager(self.cwd)
        self.task_manager = TaskManager(self.cwd)
        self.swarm_manager = SwarmManager(self.cwd)
        self.indexer = RepoIndexer(self.cwd)
        self.hook_manager = HookManager()
        self._register_default_hooks()
        
        self.current_intent = Intent(mode="Chat", task="", is_slash_command=False, task_type="ask")
        self.stop_loop = False

    def run(self, initial_task: Optional[str] = None):
        if initial_task:
            self._process_input(initial_task)
        
        while not self.stop_loop:
            try:
                text = self.renderer.read_input()
                if not text or text.lower() in ["exit", "quit", "q"]: 
                    break
                self._process_input(text)
            except KeyboardInterrupt:
                break
        
        self.console.print("\n[dim]Ghost session terminated. Stay secure.[/dim]")

    def _process_input(self, text: str):
        # 1. Handle Internal Task Commands
        if text.startswith("/tasks"):
            tasks = self.task_manager.list_tasks()
            self.renderer.render_task_list(tasks)
            return

        if text.startswith("/board"):
            workers = self.swarm_manager.list_workers()
            self.renderer.render_swarm_board(workers)
            return

        if text.startswith("/swarm"):
            self.console.print("[bold yellow]⚠ Ghost Swarm v0 Initialized.[/bold yellow]")
            self.console.print("[dim]Worktree management and coordination layer active.[/dim]")
            return

        if text.startswith("/worker spawn"):
            try:
                parts = text.split(" ")
                role = parts[2]
                task_id = "unknown"
                if "--task" in parts:
                    idx = parts.index("--task")
                    task_id = parts[idx+1]
                
                worker = self.swarm_manager.spawn_worker(role, task_id)
                self.console.print(f"[bold green]✓ Worker Spawned:[/bold green] {worker.worker_id} ({worker.role})")
                return
            except Exception as e:
                self.console.print(f"[bold red]✘ Spawn failed:[/bold red] {e}")
                return

        is_resumed = False
        if text.startswith("/resume "):
            task_id = text.split(" ")[1].strip()
            task = self.task_manager.resume_task(task_id)
            if task:
                self.console.print(f"[bold green]✓ Resuming Task:[/bold green] {task.task_id} - {task.title}")
                is_resumed = True
            else:
                self.console.print(f"[bold red]✘ Task not found:[/bold red] {task_id}")
                return

        self.current_intent = self.intent_router.route(text)
        intent = self.current_intent
        
        # Start Persistent Task (if not resumed)
        if not self.task_manager.current_task:
            task = self.task_manager.create_task(text, intent.mode, owner="denis")
        else:
            task = self.task_manager.current_task
            if is_resumed:
                task.title = f"{task.title} (Resumed: {text})"
        
        # Start Artifact Session (Linked to Task) & Trigger TaskStart Hook
        self._session_failed = False
        self.artifact_manager.start_session(text, task_id=task.task_id)
        self.artifact_manager.current_session.plan = intent.task or text
        self.hook_manager.trigger(HookEvents.TASK_START, task=text, intent=asdict(intent))
        
        # Update Task State: created -> planned (Only if not resuming)
        if not is_resumed:
            self.task_manager.update_status("planned")
        
        if intent.is_slash_command:
            if self._handle_intent(intent): return
        
        # 2. Check for Rapid Analysis (No LLM path)
        if intent.mode == "Extraer":
            target_path = intent.task
            from ghostllm_core.extractors.local_extractor import LocalExtractor
            le = LocalExtractor(self.cwd)
            with self.console.status("[bold cyan]Quick Extraction...") as status:
                stats = le.extract(target_path) if target_path else le.extract_raw(text)
                
                self.renderer.render_local_inspection(stats)
                summary = f"Local Analysis: {target_path or 'Pasted Block'} - Robust Stats extracted."
                self.artifact_manager.current_session.root_cause = summary
                
                # Persistence & Finalization (Non-terminating)
                self.artifact_manager.persist()
                self.renderer.render_artifact_summary(self.artifact_manager.current_session.to_dict())
                
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": summary})
                return

        # 3. Main Chat Loop
        self.history.append({"role": "user", "content": text})
        self.memory.add_message(self.session_id, "user", text)
        self._chat_loop()

    def _handle_intent(self, intent: Intent) -> bool:
        if intent.task_type == "plan":
            self.console.print("[cyan]Ghost Designer Mode Active. Formulating plan...[/cyan]")
            return False # Continue to LLM for planning
        elif intent.task_type == "ask" and intent.task == "help":
            self.renderer.print_help()
            return True
        return False

    def _chat_loop(self):
        max_iterations = int(os.getenv("GHOST_MAX_ITERATIONS", 15))
        iterations = 0
        
        # Update Task State: planned -> executing
        self.task_manager.update_status("executing")
        
        while iterations < max_iterations:
            iterations += 1
            with self.console.status("[bold cyan]Ghost thinking...") as status:
                msg = self._stream_completion(status)
                if not msg: break
                
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls")
                
                if tool_calls:
                    self.renderer.update_status(status, "tool_exec")
                    for tc in tool_calls:
                        tc_name = tc.get("function", {}).get("name")
                        tc_args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                        tc_id = tc.get("id")
                        
                        tool_result = self._execute_tool({"name": tc_name, "arguments": tc_args})
                        res_content = json.dumps(tool_result)
                        self.history.append({"role": "tool", "tool_call_id": tc_id, "name": tc_name, "content": res_content})
                        self.memory.add_message(self.session_id, "tool", res_content)
                
                # FALLBACK LEGACY XML PATH (Isolated)
                else:
                    legacy_calls = self._extract_tool_calls(content)
                    if legacy_calls:
                        self.renderer.update_status(status, "tool_exec")
                        for call in legacy_calls:
                            tool_result = self._execute_tool(call)
                            res_content = f"TOOL_RESULT: {json.dumps(tool_result)}"
                            self.history.append({"role": "user", "content": res_content})
                            self.memory.add_message(self.session_id, "user", res_content)
                    else:
                        # Pure text response
                        if status: status.stop()
                        if hasattr(self, "current_intent") and self.current_intent.mode != "Chat":
                            self.console.print(f"\n[dim]💡 Siguiente Mejor Acción: [bold]ghost review[/bold][/dim]")
                        break
            
            if iterations >= max_iterations:
                self.console.print(f"\n[bold yellow]⚠ Límite de autonomía alcanzado ({max_iterations} it.)[/bold yellow]")
                self.console.print(f"[dim]Usa 'continua' para darle más pasos o 'ghost review' para terminar.[/dim]")
                break

        # Finalize and Persist Artifact for EVERY task session
        if self.artifact_manager.current_session:
            # Bug 2: Accurate detection via diff_summary
            made_changes = bool(self.artifact_manager.current_session.diff_summary)
            if made_changes:
                # Update Task State: executing -> verifying
                self.task_manager.update_status("verifying")
                
                self.hook_manager.trigger(HookEvents.PRE_VERIFICATION)
                v_res = self.verification_manager.verify_change(history=self.history)
                self.hook_manager.trigger(HookEvents.POST_VERIFICATION, results=v_res)
                self.renderer.render_verification_results(v_res)
                self.artifact_manager.current_session.verification = v_res
            else:
                self.artifact_manager.current_session.verification = {"status": "skipped", "checks": []}
                
            self.artifact_manager.current_session.next_action = "Review task outcome. Use 'continua' if needed."
            
            # Mutual Exclusion: only trigger success if no failure occurred
            if not getattr(self, "_session_failed", False):
                self.task_manager.update_status("done")
                self.hook_manager.trigger(HookEvents.TASK_COMPLETE, changes=made_changes)
            else:
                self.task_manager.update_status("failed")
            
            self.task_manager.add_artifact(self.artifact_manager.current_session.session_id)
            self.artifact_manager.persist()
            self.renderer.render_artifact_summary(self.artifact_manager.current_session.to_dict())

    def _extract_tool_calls(self, text: str) -> List[Dict[str, Any]]:
        matches = re.findall(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)
        calls = []
        for m in matches:
            try:
                calls.append(json.loads(m.strip()))
            except Exception as e:
                try:
                    inner_match = re.search(r"\{.*\}", m, re.DOTALL)
                    if inner_match: calls.append(json.loads(inner_match.group(0)))
                except: pass
        return calls

    def _stream_completion(self, parent_status=None) -> Dict[str, Any]:
        """Wrapper for _execute_stream_requests with retry logic."""
        for attempt in range(2):
            try:
                return self._execute_stream_request(parent_status)
            except (requests.exceptions.RequestException, Exception) as e:
                err_str = str(e).lower()
                retryable_msgs = ["429", "prematurely", "timeout", "broken pipe", "connection reset"]
                is_retryable = any(m in err_str for m in retryable_msgs) or "ChunkedEncodingError" in str(type(e))
                
                if attempt == 0 and is_retryable:
                    self.console.print(f"[yellow]⚠ Stream interrupted ({type(e).__name__}), retrying ({attempt+1}/2)...[/yellow]")
                    time.sleep(2)
                    continue
                self.console.print(f"[bold red]✘ System Error:[/bold red] {e}")
                if parent_status: parent_status.stop()
                return {}
        return {}

    def _execute_stream_request(self, parent_status=None) -> Dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        tree = self.indexer.get_project_tree(max_depth=1)
        git = self._get_git_info()
        branch_info = f"{git['branch']} ({git['dirty']})"
        
        max_iter = int(os.getenv("GHOST_MAX_ITERATIONS", 15))
        auth_status = "ACTIVE (High-Trust)" if self.auto_approve else "DISABLED (Manual Confirmation Required)"
        full_system = self.SYSTEM_PROMPT.format(
            mode=self.mode if hasattr(self, "mode") else "Chat", 
            project=self.project_name if hasattr(self, "project_name") else "GhostLLM", 
            cwd=self.cwd, 
            tree=tree, 
            profile=self.profile if hasattr(self, "profile") else "coder", 
            branch_info=branch_info,
            auto_approve=auth_status,
            max_iter=max_iter
        )
        
        latest_user_content = next((msg["content"] for msg in reversed(self.history) if msg["role"] == "user"), "")
        use_stream = self._should_use_stream(latest_user_content)
        
        active_messages = [{"role": "system", "content": full_system}] + self.history
            
        payload = {
            "model": self.model, "messages": active_messages, "stream": use_stream, 
            "temperature": 0.1, "tools": self.NATIVE_TOOLS, "tool_choice": "auto"
        }
        
        response_text = ""
        tool_calls_buffer = {}
        status = parent_status
        self.console.print(f"\n[bold magenta]GHOST {getattr(self, 'project_name', 'GhostLLM')}[/bold magenta]")
        
        if not use_stream:
            r = requests.post(f"{self.server_url}/v1/chat/completions", headers=headers, json=payload, timeout=(15, 300))
            if r.status_code != 200:
                err_msg = r.text
                try: err_msg = r.json().get("error", {}).get("message", r.text)
                except: pass
                if r.status_code == 429: raise Exception(f"429: {err_msg}")
                self.console.print(f"[bold red]✘ API Error ({r.status_code}):[/bold red] {err_msg}")
                return {}
            data = r.json()
            choice = data.get("choices", [{}])[0]
            msg_data = choice.get("message", {})
            response_text = msg_data.get("content") or ""
            if response_text: self.console.print(f"{escape(response_text)}")
            
            tcs_raw = msg_data.get("tool_calls", [])
            for idx, tc in enumerate(tcs_raw):
                tool_calls_buffer[idx] = {"id": tc.get("id"), "function": {"name": tc.get("function", {}).get("name", ""), "arguments": tc.get("function", {}).get("arguments", "")}, "type": "function"}
        else:
            with requests.post(f"{self.server_url}/v1/chat/completions", headers=headers, json=payload, stream=True, timeout=(15, 300)) as r:
                if r.status_code != 200:
                    err_msg = r.text
                    try: err_msg = r.json().get("error", {}).get("message", r.text)
                    except: pass
                    if r.status_code == 429: raise Exception(f"429: {err_msg}")
                    self.console.print(f"[bold red]✘ API Error ({r.status_code}):[/bold red] {err_msg}")
                    return {}
                in_xml_tool_call = False
                try:
                    for line in r.iter_lines():
                        if not line: continue
                        line_str = line.decode("utf-8").strip()
                        if line_str.startswith("data: "):
                            data_content = line_str[6:].strip()
                            if data_content == "[DONE]": break
                            try:
                                data = json.loads(data_content)
                                if "error" in data:
                                    if "429" in data["error"] or "retry" in data["error"].lower():
                                        raise Exception(f"429: {data['error']}")
                                    self.console.print(f"[bold red]✘ Stream Error:[/bold red] {data['error']}")
                                    break
                                choice = data["choices"][0]
                                delta = choice.get("delta", {})
                                
                                content = delta.get("content", "")
                                if content:
                                    if status:
                                        self.renderer.update_status(status, "building")
                                        status.stop() 
                                        status = None
                                    response_text += content
                                    if "<tool_call>" in content: in_xml_tool_call = True
                                    if not in_xml_tool_call: self.console.print(escape(content), end="")
                                    # Bug 6: Removed redundant line
                                    if "</tool_call>" in content: in_xml_tool_call = False
                                
                                tcs = delta.get("tool_calls", [])
                                for tc in tcs:
                                    if status: self.renderer.update_status(status, "tool_exec")
                                    idx = tc.get("index", 0)
                                    if idx not in tool_calls_buffer: tool_calls_buffer[idx] = {"id": tc.get("id"), "function": {"name": "", "arguments": ""}, "type": "function"}
                                    if tc.get("id"): tool_calls_buffer[idx]["id"] = tc.get("id")
                                    fn = tc.get("function", {})
                                    if fn.get("name"): tool_calls_buffer[idx]["function"]["name"] += fn.get("name")
                                    if fn.get("arguments"): tool_calls_buffer[idx]["function"]["arguments"] += fn.get("arguments")
                            except Exception as e:
                                if "429" in str(e): raise
                                pass
                except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError) as e:
                    raise Exception(f"Stream interrupted prematurely: {e}")
            print("\n")
        
        final_tool_calls = [tool_calls_buffer[i] for i in sorted(tool_calls_buffer.keys())]
        safe_content = response_text if response_text else ("" if final_tool_calls else "...")
        msg = {"role": "assistant", "content": safe_content, "tool_calls": final_tool_calls if final_tool_calls else None}
        
        self.history.append(msg)
        self.memory.add_message(self.session_id, "assistant", response_text or "")
        return msg

    def _execute_tool(self, call: Dict[str, Any]) -> Dict[str, Any]:
        """Defensive Tool Execution Engine."""
        name, args = call.get("name"), call.get("arguments", {})
        target = args.get("path") or args.get("command") or ""
        self.renderer.print_tool_trace(name, target)
        
        # Trigger PreToolUse Hook
        self.hook_manager.trigger(HookEvents.PRE_TOOL_USE, name=name, arguments=args)
        
        metadata = {
            "task_type": self.current_intent.task_type if hasattr(self, "current_intent") else "ask",
            "mode": getattr(self, "mode", "Chat"),
            "role": self.role,
            "is_swarm_worker": self.is_swarm_worker
        }
        
        try:
            result = self._inner_execute_tool(name, args, metadata)
            # Trigger PostToolUse Hook
            self.hook_manager.trigger(HookEvents.POST_TOOL_USE, name=name, result=result)
            return result
        except Exception as e:
            self._session_failed = True
            self.hook_manager.trigger(HookEvents.TASK_FAILED, error=str(e))
            return {"error": str(e)}

    def _inner_execute_tool(self, name: str, args: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path")
        if name == "ls":
            path = args.get("path", ".")
            if not self.policy_gate.check_permission("List Directory", path, getattr(self, "mode", "Chat"), metadata):
                return {"error": "Access denied by policy."}
            full_path = os.path.join(self.cwd, path)
            if not os.path.exists(full_path): return {"error": f"Path not found: {path}"}
            return {"files": os.listdir(full_path)}
            
        elif name == "read_file":
            if not path: return {"error": "Missing 'path' argument."}
            if not self.policy_gate.check_permission("Read File", path, getattr(self, "mode", "Chat"), metadata):
                return {"error": "Access denied by policy."}
            full_path = os.path.join(self.cwd, path)
            if not os.path.exists(full_path): return {"error": f"Path not found: {path}"}
            if os.path.isdir(full_path): return {"error": f"'{path}' is a directory. Use 'ls' instead."}
            with open(full_path, "r", encoding="utf-8-sig", newline="") as f: content = f.read()
            return {"content": content}
            
        elif name == "write_file":
            content = args.get("content")
            if not path or content is None: return {"error": "Missing 'path' or 'content' arguments."}
            if not self.policy_gate.check_permission("Write File", path, getattr(self, "mode", "Chat"), metadata):
                return {"error": "Action denied by user or policy."}
            full_path = os.path.join(self.cwd, path)
            old_content = ""
            if os.path.exists(full_path):
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f: old_content = f.read()
            
            target_dir = os.path.dirname(full_path)
            if target_dir: os.makedirs(target_dir, exist_ok=True)
            
            with open(full_path, "w", encoding="utf-8") as f: f.write(content)
            self.renderer.render_diff(path, "".join(difflib.unified_diff(old_content.splitlines(keepends=True), content.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}")))
            self.artifact_manager.add_diff(path, "Write File")
            return {"status": "success", "bytes": len(content)}
            
        elif name == "edit_file":
            old_str, new_str = args.get("old_str"), args.get("new_str")
            if not all([path, old_str is not None, new_str is not None]): return {"error": "Missing required arguments."}
            if not self.policy_gate.check_permission("Patch File", path, getattr(self, "mode", "Chat"), metadata):
                return {"error": "Action denied by user or policy."}
            full_path = os.path.join(self.cwd, path)
            if not os.path.exists(full_path): return {"error": f"File not found: {path}"}
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f: content = f.read()
            if old_str not in content: return {"error": "Target string (old_str) not found."}
            new_content = content.replace(old_str, new_str, 1)
            with open(full_path, "w", encoding="utf-8") as f: f.write(new_content)
            self.renderer.render_diff(path, "".join(difflib.unified_diff(content.splitlines(keepends=True), new_content.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}")))
            self.artifact_manager.add_diff(path, "Edit File")
            return {"status": "patched"}
            
        elif name == "run_shell":
            cmd = args.get("command")
            if not cmd: return {"error": "Missing 'command' argument."}
            if not self.policy_gate.check_permission("Execute Shell", cmd, getattr(self, "mode", "Chat"), metadata):
                return {"error": "Action denied by user or policy."}
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=os.environ.copy(), cwd=self.cwd)
            return {"stdout": res.stdout, "stderr": res.stderr, "exit_code": res.returncode}
            
        elif name == "delete_file":
            evidence = args.get("evidence", "")
            if not path: return {"error": "Missing 'path' argument."}
            metadata["evidence"] = evidence
            if not self.policy_gate.check_permission("Delete File", path, getattr(self, "mode", "Chat"), metadata):
                return {"error": "Action denied by policy."}
            full_path = os.path.join(self.cwd, path)
            if not os.path.exists(full_path): return {"error": f"File not found: {path}"}
            if self.auto_approve or self.policy_gate.confirm_action("Delete File", f"Evidence: {evidence}"):
                os.remove(full_path)
                self.artifact_manager.add_diff(path, "Delete File")
                return {"status": "success", "file": path}
            return {"status": "aborted"}
            
        elif name == "summarize_repo":
            return {"summary": self.indexer.get_project_summary()}
            
        return {"error": f"Tool '{name}' not found."}

    def _audit_hook(self, event_name: str, **kwargs):
        """Standard audit log implementation for artifacts."""
        if self.artifact_manager.current_session:
            details = ""
            if event_name == HookEvents.TASK_START:
                mode = kwargs.get("intent", {}).get("mode", "Unknown")
                details = f"Mode: {mode} | Task: {kwargs.get('task')}"
            elif event_name == HookEvents.PRE_TOOL_USE:
                details = f"Initializing tool: {kwargs.get('name')}"
            elif event_name == HookEvents.POST_TOOL_USE:
                res = kwargs.get("result", {})
                status = "success" if "error" not in res else "failed"
                details = f"Completed: {kwargs.get('name')} ({status})"
            elif event_name == HookEvents.PRE_VERIFICATION:
                details = "Starting integrity verification cycle"
            elif event_name == HookEvents.POST_VERIFICATION:
                res = kwargs.get("results", {})
                status = res.get("status", "unknown").upper()
                details = f"Verification finished with status: {status}"
            elif event_name == HookEvents.TASK_COMPLETE:
                made_changes = "Changes detected" if kwargs.get("changes") else "No changes made"
                details = f"Session finalized successfully. {made_changes}."
            elif event_name == HookEvents.TASK_FAILED:
                details = f"Critical Error: {kwargs.get('error')}"
            
            entry = {
                "event": event_name,
                "timestamp": time.time(),
                "details": details
            }
            self.artifact_manager.current_session.events.append(entry)

    def _register_default_hooks(self):
        """Registers system-level hooks for observability."""
        # Register audit_log for key events
        for event in [HookEvents.TASK_START, HookEvents.PRE_TOOL_USE, HookEvents.POST_TOOL_USE, HookEvents.PRE_VERIFICATION, HookEvents.POST_VERIFICATION, HookEvents.TASK_COMPLETE, HookEvents.TASK_FAILED]:
            self.hook_manager.register(event, lambda e=event, **kw: self._audit_hook(e, **kw))

    def _get_git_info(self) -> Dict[str, str]:
        try:
            branch = subprocess.check_output("git rev-parse --abbrev-ref HEAD", shell=True, text=True, stderr=subprocess.DEVNULL).strip()
            dirty = "dirty" if subprocess.run("git diff --quiet", shell=True, stderr=subprocess.DEVNULL).returncode != 0 else "clean"
            return {"branch": branch, "dirty": dirty}
        except:
            return {"branch": "unknown", "dirty": "unknown"}

    def _should_use_stream(self, text: str) -> bool:
        # Bug 4: More robust intent-aware policy
        if not text: return True
        
        # Disable stream for Plan mode if input is large to ensure atomic analysis
        if self.current_intent.mode == "Plan" and len(text) > 2000:
            return False
            
        # Always use stream for Chat and simple turns
        if self.current_intent.mode == "Chat":
            return True
            
        return len(text) < 5000
