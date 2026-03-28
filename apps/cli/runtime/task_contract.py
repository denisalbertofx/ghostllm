"""
Single runtime TaskContract: intent (routing), policies, planner outputs, and spec body.
Created during INTAKE; core fields are sealed (immutable) after intake completes.
Operational spec via task_contract["spec"] (get_task_contract_spec); no session.taskspec mirror.

Mutable after seal (only): runtime_annotations, verification_runs, repair_runs.

Extension boundaries (what not to mutate after seal, adapters): docs/RUNTIME_EXTENSION_BOUNDARIES.md
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from datetime import datetime
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CONTRACT_VERSION = 1

# Legacy JSON/session value: operational ``spec`` came from the spec engine (``task_contract["spec"]``).
RUNTIME_CONTRACT_SOURCE_SPEC_ENGINE_LEGACY = "taskspec"

READ_ONLY_TOOL_NAMES = frozenset({"ls", "read_file", "summarize_repo"})
FULL_TOOL_NAMES = frozenset({"ls", "read_file", "write_file", "edit_file", "delete_file", "run_shell", "summarize_repo"})

SLASH_MODES: Dict[str, str] = {
    "/plan": "Plan",
    "/do": "Execute",
    "/edit": "Patch",
    "/fix": "Fix",
    "/review": "Review",
    "/chat": "Chat",
    "/mode": "ModeChange",
    "/debug": "DebugToggle",
    "/clear": "ClearHistory",
    "/help": "Help",
}


@dataclass
class Intent:
    """CLI routing intent (slash / heuristics). Frozen at INTAKE — do not recompute mid-session."""

    mode: str
    task: str
    task_type: str = "ask"
    scaffold_type: str = "none"
    is_slash_command: bool = False
    is_exact_command: bool = False
    requires_tools: bool = False
    requires_shell: bool = False
    requires_file_scope: bool = False
    original_text: str = ""
    target_folder: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def _has_bootstrap_scaffold_keywords(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    bootstrap_keywords = (
        "desde cero",
        "from scratch",
        "nuevo proyecto",
        "proyecto nuevo",
        "crea un proyecto",
        "create a project",
        "bootstrap",
        "clean start",
        "scaffold",
    )
    return any(k in t for k in bootstrap_keywords)


def _looks_like_scaffold_continuation(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    continuation_markers = (
        "continua este proyecto",
        "continúa este proyecto",
        "continua el proyecto",
        "continúa el proyecto",
        "continue this project",
        "finish this project",
        "terminalo",
        "termínalo",
    )
    scaffold_markers = (
        "cli",
        "aplicacion",
        "aplicación",
        "app",
        "proyecto",
        "python",
        "sqlite",
        "pytest",
        "readme",
    )
    return any(marker in t for marker in continuation_markers) and any(
        marker in t for marker in scaffold_markers
    )


def _enrich_intent(intent: Intent) -> None:
    task_lower = intent.task.lower()
    bootstrap_keywords = ["desde cero", "nuevo", "inicializa", "re-inicializa", "bootstrap", "clean start"]
    if any(k in intent.task.lower() for k in bootstrap_keywords):
        intent.scaffold_type = "bootstrap"
        intent.task_type = "scaffold"
    elif _looks_like_scaffold_continuation(intent.task):
        intent.scaffold_type = "extend"
        intent.task_type = "scaffold"
    elif intent.task_type == "scaffold":
        intent.scaffold_type = "extend"
        folder_match = re.search(r"(?:en|carpeta|directorio)\s+(\.?[\w\-/]+)", intent.task)
        if folder_match:
            tf = folder_match.group(1).strip(".")
            intent.target_folder = tf if tf else None
    extend_words = ["añade", "agrega", "crea la pagina", "crea el componente", "en el proyecto", "extiende", "en este repo"]
    if any(w in task_lower for w in extend_words):
        intent.scaffold_type = "extend"
    shell_keywords = ["ejecuta", "run", "npm", "git", "bash", "shell", "build", "test"]
    if any(w in task_lower for w in shell_keywords):
        intent.requires_shell = True
        intent.requires_tools = True
    file_keywords = ["archivo", "file", "en ", ".py", ".ts", ".js", ".tsx", ".json", "clase", "funcion"]
    if any(w in task_lower for w in file_keywords):
        intent.requires_file_scope = True
        intent.requires_tools = True
    if intent.task_type in ["code", "debug", "fix", "review", "research"]:
        intent.requires_tools = True


def route_intake_intent(text: str) -> Intent:
    """Single entry for user → Intent at INTAKE (replaces ad-hoc IntentRouter.route)."""
    text = text.strip()
    if not text:
        return Intent(mode="Chat", task="", original_text=text)
    if text.startswith("/"):
        parts = text.split(" ", 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        if cmd in SLASH_MODES:
            mode = SLASH_MODES[cmd]
            is_exact = False
            task_str = args
            for p in ("ejecuta exactamente:", "corre exactamente:", "shell exacto:"):
                if args.lower().startswith(p):
                    is_exact = True
                    task_str = args[len(p) :].strip()
                    break
            intent = Intent(
                mode=mode,
                task=task_str,
                is_slash_command=True,
                is_exact_command=is_exact,
                original_text=text,
            )
            _enrich_intent(intent)
            return intent
        return Intent(mode="Invalid", task=cmd, is_slash_command=True, original_text=text)
    text_lower = text.lower()
    intent = Intent(mode="Chat", task=text, original_text=text)
    if any(w in text_lower for w in ["arregla", "fix", "corrige", "repara"]):
        intent.task_type = "fix"
        intent.requires_tools = True
    elif any(w in text_lower for w in ["error", "fallo", "falla", "no funciona", "broken", "bug", "crash"]):
        intent.task_type = "debug"
        intent.requires_tools = True
        intent.requires_file_scope = True
    elif any(w in text_lower for w in ["diseña", "arquitectura", "pantalla", "interfaz", "ui", "ux", "como construyo", "crea un diseño"]):
        intent.task_type = "architect"
    elif any(w in text_lower for w in ["escribe", "codifica", "implementa", "programa", "write a", "create a function"]):
        intent.task_type = "code"
        intent.requires_tools = True
    elif any(w in text_lower for w in ["revisa", "review", "chequea", "mira si"]):
        intent.task_type = "review"
        intent.requires_tools = True
        intent.requires_file_scope = True
    elif any(w in text_lower for w in ["busca", "encuentra", "donde esta", "investiga", "research"]):
        intent.task_type = "research"
        intent.requires_tools = True
    elif any(w in text_lower for w in ["inicializa", "crea un proyecto", "proyecto nuevo", "bootstrap"]):
        intent.task_type = "scaffold"
        intent.requires_tools = True
    analysis_triggers = ["total de líneas", "conteo", "qué dice la línea", "última línea", "primeras n líneas"]
    if any(t in text_lower for t in analysis_triggers):
        intent.mode = "Analyze"
        intent.task_type = "research"
        intent.requires_tools = False
    _enrich_intent(intent)
    return intent


def infer_work_task_type(text: str) -> str:
    """TaskManager taxonomy from natural language (unchanged heuristics)."""
    t = text.lower()
    if _has_bootstrap_scaffold_keywords(t):
        return "scaffold"
    if _looks_like_scaffold_continuation(t):
        return "scaffold"
    if any(w in t for w in ["dependencia", "dependency", "package", "install", "npm", "pip", "uv", "requirements.txt"]):
        return "dependency_change"
    if any(w in t for w in ["config", "entorno", "setup", ".env", "toml", "yaml", "yml", "json"]):
        if not any(w in t for w in ["package.json", "tsconfig.json"]):
            return "config_change"
    if any(w in t for w in ["compila", "build", "pipeline", "ci", "github actions", "deploy"]):
        return "build_fix"
    if any(w in t for w in ["tipos", "types", "tsc", "typescript", "interface", "typing"]):
        return "type_fix"
    if any(w in t for w in ["refactor", "limpia", "clean", "organiza", "mejorar"]):
        return "refactor"
    if any(w in t for w in ["arregla", "bug", "fix", "error", "fallo", "repara", "broken"]):
        return "bug_fix"
    if any(w in t for w in ["readme", "documentación", "docs", "comento", "comentario", "comment"]):
        return "docs_change"
    return "direct_edit"


def compute_allowed_tool_names(intent_dict: Dict[str, Any], spec: Optional[Dict[str, Any]]) -> List[str]:
    if spec and spec.get("change_expectation") == "should_not_write":
        return sorted(READ_ONLY_TOOL_NAMES)
    mode = str(intent_dict.get("mode") or "Chat")
    if mode == "Plan":
        return sorted(READ_ONLY_TOOL_NAMES)
    return sorted(FULL_TOOL_NAMES)


# --- Sealed TaskContract (post-INTAKE immutability) ---------------------------------

TASK_CONTRACT_MUTABLE_TOP_LEVEL_KEYS = frozenset(
    {"runtime_annotations", "verification_runs", "repair_runs"}
)


class TaskContractMutationError(RuntimeError):
    """Raised when GHOST_TASK_CONTRACT_STRICT=1 and a core contract mutation is attempted."""


def _task_contract_strict_mutations() -> bool:
    return os.getenv("GHOST_TASK_CONTRACT_STRICT", "").strip().lower() in ("1", "true", "yes", "on")


def _log_core_contract_mutation(op: str, key: str) -> None:
    logger.warning(
        "task_contract core mutation blocked after INTAKE (%s key=%r); "
        "use runtime_annotations, verification_runs, or repair_runs only",
        op,
        key,
    )


def deep_freeze_contract_value(obj: Any) -> Any:
    """Recursive read-only snapshot (MappingProxyType / tuple) for sealed contract values."""
    if isinstance(obj, dict):
        return MappingProxyType({k: deep_freeze_contract_value(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return tuple(deep_freeze_contract_value(x) for x in obj)
    return obj


def _contract_raw_setitem(tc: Any, key: str, value: Any) -> None:
    if isinstance(tc, TaskContractDict):
        dict.__setitem__(tc, key, value)
    else:
        tc[key] = value


def _migrate_legacy_decision_plan(tc: Any) -> None:
    """Inline legacy top-level decision_plan into runtime_annotations."""
    if not isinstance(tc, dict):
        return
    ra = tc.get("runtime_annotations")
    if not isinstance(ra, dict):
        ra = {"decision_plan": {}}
        _contract_raw_setitem(tc, "runtime_annotations", ra)
    leg = tc.get("decision_plan")
    if isinstance(leg, dict) and leg:
        if not ra.get("decision_plan"):
            ra["decision_plan"] = dict(leg)
    if "decision_plan" in tc:
        if isinstance(tc, TaskContractDict):
            dict.__delitem__(tc, "decision_plan")
        else:
            del tc["decision_plan"]


class TaskContractDict(dict):
    """
    Task contract mapping: core keys become read-only at top level after seal_after_intake().
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "_contract_sealed", False)

    @property
    def contract_sealed(self) -> bool:
        return bool(getattr(self, "_contract_sealed", False))

    def _raw_setitem(self, key: str, value: Any) -> None:
        dict.__setitem__(self, key, value)

    def seal_after_intake(self) -> None:
        if self.contract_sealed:
            return
        _migrate_legacy_decision_plan(self)
        for key in list(self.keys()):
            if key in TASK_CONTRACT_MUTABLE_TOP_LEVEL_KEYS:
                continue
            frozen = deep_freeze_contract_value(self[key])
            dict.__setitem__(self, key, frozen)
        object.__setattr__(self, "_contract_sealed", True)

    def __setitem__(self, key: str, value: Any) -> None:
        if self.contract_sealed and key not in TASK_CONTRACT_MUTABLE_TOP_LEVEL_KEYS:
            _log_core_contract_mutation("setitem", key)
            if _task_contract_strict_mutations():
                raise TaskContractMutationError(f"task_contract setitem forbidden for {key!r} after INTAKE")
            return
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        if self.contract_sealed and key not in TASK_CONTRACT_MUTABLE_TOP_LEVEL_KEYS:
            _log_core_contract_mutation("delitem", key)
            if _task_contract_strict_mutations():
                raise TaskContractMutationError(f"task_contract delitem forbidden for {key!r} after INTAKE")
            return
        super().__delitem__(key)

    def clear(self) -> None:  # type: ignore[override]
        if self.contract_sealed:
            _log_core_contract_mutation("clear", "*")
            if _task_contract_strict_mutations():
                raise TaskContractMutationError("task_contract clear forbidden after INTAKE")
            return
        super().clear()

    def pop(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        key = args[0] if args else None
        if self.contract_sealed and key is not None and key not in TASK_CONTRACT_MUTABLE_TOP_LEVEL_KEYS:
            _log_core_contract_mutation("pop", str(key))
            if _task_contract_strict_mutations():
                raise TaskContractMutationError(f"task_contract pop forbidden for {key!r} after INTAKE")
            return None
        return super().pop(*args, **kwargs)

    def popitem(self) -> Any:  # type: ignore[override]
        if self.contract_sealed:
            _log_core_contract_mutation("popitem", "*")
            if _task_contract_strict_mutations():
                raise TaskContractMutationError("task_contract popitem forbidden after INTAKE")
            return None, None
        return super().popitem()

    def setdefault(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        if key in self:
            return self[key]
        if self.contract_sealed and key not in TASK_CONTRACT_MUTABLE_TOP_LEVEL_KEYS:
            _log_core_contract_mutation("setdefault", key)
            if _task_contract_strict_mutations():
                raise TaskContractMutationError(f"task_contract setdefault forbidden for {key!r} after INTAKE")
            return default
        super().__setitem__(key, default)
        return default

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        if self.contract_sealed:
            incoming: Dict[str, Any] = {}
            if args and isinstance(args[0], Mapping):
                incoming.update(dict(args[0]))
            elif args and isinstance(args[0], dict):
                incoming.update(args[0])
            incoming.update(kwargs)
            bad = [k for k in incoming if k not in TASK_CONTRACT_MUTABLE_TOP_LEVEL_KEYS]
            if bad:
                for k in bad:
                    _log_core_contract_mutation("update", k)
                if _task_contract_strict_mutations():
                    raise TaskContractMutationError(
                        f"task_contract update forbidden for {bad!r} after INTAKE"
                    )
            good = {k: v for k, v in incoming.items() if k in TASK_CONTRACT_MUTABLE_TOP_LEVEL_KEYS}
            if good:
                super().update(good)
            return
        super().update(*args, **kwargs)


def seal_task_contract_after_intake(session: Any) -> None:
    """Freeze core TaskContract fields; call once at end of INTAKE."""
    tc = getattr(session, "task_contract", None)
    if isinstance(tc, TaskContractDict):
        tc.seal_after_intake()
    elif isinstance(tc, dict) and not isinstance(tc, TaskContractDict):
        _migrate_legacy_decision_plan(tc)


def append_verification_run_record(session: Any, record: Dict[str, Any]) -> None:
    """Append one verification run summary (allowed mutation after seal)."""
    tc = getattr(session, "task_contract", None)
    if not isinstance(tc, dict):
        return
    runs = tc.get("verification_runs")
    if not isinstance(runs, list):
        runs = []
        tc["verification_runs"] = runs
    runs.append(dict(record))


def task_contract_to_jsonable(obj: Any) -> Any:
    """Recursively convert MappingProxyType / tuples to JSON-serializable dict/list."""
    if isinstance(obj, MappingProxyType):
        return {k: task_contract_to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, dict):
        return {k: task_contract_to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return [task_contract_to_jsonable(x) for x in obj]
    if isinstance(obj, list):
        return [task_contract_to_jsonable(x) for x in obj]
    return obj


def append_budget_extension_record(session: Any, record: Dict[str, Any]) -> None:
    """Append compact budget extension audit (operational grants, not verify-only)."""
    row = dict(record)
    row.setdefault("ts", datetime.now().isoformat())
    ev = getattr(session, "budget_extension_events", None)
    if not isinstance(ev, list):
        ev = []
        session.budget_extension_events = ev
    ev.append(row)
    if len(ev) > 32:
        del ev[:-32]
    events = getattr(session, "events", None)
    if isinstance(events, list):
        bev: Dict[str, Any] = {
            "event": "budget_extension",
            "points": row.get("points", 0),
            "reason": row.get("reason", ""),
            "detail": (row.get("detail") or "")[:240],
            "skipped": row.get("skipped") or "",
        }
        if row.get("confidence"):
            bev["confidence"] = str(row.get("confidence"))[:32]
        events.append(bev)


def append_repair_run_record(session: Any, record: Dict[str, Any]) -> None:
    """Append one repair attempt summary (allowed mutation after seal)."""
    tc = getattr(session, "task_contract", None)
    if not isinstance(tc, dict):
        return
    runs = tc.get("repair_runs")
    if not isinstance(runs, list):
        runs = []
        tc["repair_runs"] = runs
    runs.append(dict(record))


def get_task_contract_spec(session: Any) -> Optional[Mapping[str, Any]]:
    """TaskSpec-shaped body from task_contract['spec'] only (no session mirrors)."""
    tc = getattr(session, "task_contract", None)
    if not isinstance(tc, dict):
        return None
    sp = tc.get("spec")
    return sp if isinstance(sp, Mapping) else None


def contract_has_operational_spec(session: Any) -> bool:
    """True when task_contract['spec'] exists and is non-empty (engine or legacy adapter)."""
    ts = get_task_contract_spec(session)
    return isinstance(ts, Mapping) and bool(ts)


def get_task_contract_decision_plan(session: Any) -> Dict[str, Any]:
    """Planner output from runtime_annotations['decision_plan'] (legacy top-level key supported)."""
    tc = getattr(session, "task_contract", None)
    if not isinstance(tc, dict):
        return {}
    ra = tc.get("runtime_annotations")
    if isinstance(ra, dict):
        dp = ra.get("decision_plan")
        if isinstance(dp, dict) and dp:
            return dict(dp)
    legacy = tc.get("decision_plan")
    if isinstance(legacy, dict) and legacy:
        return dict(legacy)
    return {}


def get_task_contract(session: Any) -> Optional[Dict[str, Any]]:
    tc = getattr(session, "task_contract", None)
    return tc if isinstance(tc, dict) else None


def ensure_task_contract_foundation(session: Any, intent: Intent) -> None:
    """Call at INTAKE start after route_intake_intent — single contract shell."""
    intent_d = asdict(intent)
    session.task_contract = TaskContractDict(
        {
            "version": CONTRACT_VERSION,
            "intent": intent_d,
            "allowed_tools": compute_allowed_tool_names(intent_d, None),
            "allowed_paths": [],
            "budget": {},
            "skip_verify": False,
            "verification_policy": {},
            "repair_policy": {},
            "blast_radius": "",
            "risk_level": "",
            "spec": None,
            "exploration_plan": {},
            "runtime_contract_source": "pending",
            "runtime_annotations": {"decision_plan": {}},
            "verification_runs": [],
            "repair_runs": [],
        }
    )
    sync_contract_mirrors_to_session(session)


def sync_contract_mirrors_to_session(session: Any) -> None:
    """Sync planner/session flags from task_contract.

    Does not populate ``session.taskspec`` (use ``get_task_contract_spec``).
    ``ArtifactSession.decision_plan`` is a deprecated read-only property over the
    contract, not a separate writable mirror.
    """
    tc = getattr(session, "task_contract", None)
    if not isinstance(tc, dict):
        return
    dp_dict = get_task_contract_decision_plan(session)
    session.planner_used = bool(dp_dict)
    session.planner_risk_level = str(tc.get("risk_level") or "")
    src = tc.get("runtime_contract_source")
    if isinstance(src, str) and src and src != "pending":
        session.runtime_contract_source = src


def update_task_contract_after_spec_apply(session: Any, spec_dict: Dict[str, Any]) -> None:
    """After spec-engine merge: policies + allowed paths/tools live on ``task_contract``."""
    tc = getattr(session, "task_contract", None)
    if not isinstance(tc, dict):
        ensure_task_contract_foundation(session, Intent(mode="Chat", task="", original_text=""))
        tc = session.task_contract
    assert isinstance(tc, dict)
    tc["spec"] = spec_dict
    tc["verification_policy"] = dict(spec_dict.get("verification_policy") or {})
    tc["repair_policy"] = dict(spec_dict.get("repair_policy") or {})
    tc["budget"] = dict(spec_dict.get("budget_policy") or {})
    tc["allowed_paths"] = list(spec_dict.get("target_files") or [])
    intent_d = tc.get("intent") if isinstance(tc.get("intent"), Mapping) else {}
    intent_plain = dict(intent_d) if isinstance(intent_d, Mapping) else {}
    tc["allowed_tools"] = compute_allowed_tool_names(intent_plain, spec_dict)
    tc["runtime_contract_source"] = RUNTIME_CONTRACT_SOURCE_SPEC_ENGINE_LEGACY
    if isinstance(session.exploration_plan, dict):
        tc["exploration_plan"] = dict(session.exploration_plan)
    sync_contract_mirrors_to_session(session)


# Deprecated name (pre–TaskContract-first wording).
update_task_contract_after_taskspec_apply = update_task_contract_after_spec_apply


def update_task_contract_after_legacy_fill(session: Any, spec_dict: Dict[str, Any]) -> None:
    tc = getattr(session, "task_contract", None)
    if not isinstance(tc, dict):
        ensure_task_contract_foundation(session, Intent(mode="Chat", task="", original_text=""))
        tc = session.task_contract
    assert isinstance(tc, dict)
    tc["spec"] = spec_dict
    tc["verification_policy"] = dict(spec_dict.get("verification_policy") or {})
    tc["repair_policy"] = dict(spec_dict.get("repair_policy") or {})
    tc["budget"] = dict(spec_dict.get("budget_policy") or {})
    tc["allowed_paths"] = list(spec_dict.get("target_files") or [])
    intent_d = tc.get("intent") if isinstance(tc.get("intent"), Mapping) else {}
    intent_plain = dict(intent_d) if isinstance(intent_d, Mapping) else {}
    tc["allowed_tools"] = compute_allowed_tool_names(intent_plain, spec_dict)
    tc["runtime_contract_source"] = "legacy"
    ra = tc.get("runtime_annotations")
    if isinstance(ra, dict):
        ra["decision_plan"] = {}
    else:
        tc["runtime_annotations"] = {"decision_plan": {}}
    tc["blast_radius"] = ""
    tc["risk_level"] = ""
    session.planner_used = False
    session.planner_version = ""
    session.planner_advisory_mode = True
    session.planner_risk_level = ""
    session.planner_estimated_complexity = ""
    session.planner_provenance = {}
    sync_contract_mirrors_to_session(session)
