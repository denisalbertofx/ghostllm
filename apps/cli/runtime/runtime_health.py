"""
Computed runtime health summary for artifact review (release evaluation).

Derived deterministically from ``operational_metrics``, forensic fields, and terminal record.
Stable string enums and ``main_risk_flags`` codes — do not rename without migration note.

"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Set

from apps.cli.runtime.explore_promotion import REASON_READ_ONLY_STAGNATION_THRESHOLD
from apps.cli.runtime.outcome_engine import OUTCOME_BLOCKED, OUTCOME_NO_OP, OUTCOME_VERIFICATION_FAILED
from apps.cli.runtime.repair_specialist import REPAIR_OUTCOME_NO_EFFECTIVE_PATCH
from apps.cli.runtime.session_metrics import refresh_operational_metrics_snapshot
from apps.cli.runtime.session_phase import (
    LOOP_ABORT_POLICY,
    LOOP_ABORT_PROVIDER,
    LOOP_ABORT_STAGNATION,
)

RUNTIME_HEALTH_SCHEMA_VERSION = 1

# Thresholds (deterministic; document changes in release notes)
PROMPT_BLOAT_PROMPT_CHARS = 400_000
LONG_EXPLORE_PHASE_ENTRIES = 5
REPEATED_REPAIR_MIN_ATTEMPTS = 2
PROVIDER_RETRY_HEAVY_MIN = 3

ALLOWED_RISK_FLAGS = frozenset(
    {
        "verification_skipped",
        "repeated_repair_failures",
        "long_explore",
        "prompt_bloat",
        "reuse_in_finalize",
        "provider_retry_heavy",
        "no_effective_patch",
        "aborted_budget",
        "aborted_policy",
        "aborted_provider",
        "aborted_stagnation",
        # Exploration Engine v2
        "repo_mismatch",
        "explore_churn",
    }
)

_OUTCOME_FAILED_HEALTH = frozenset(
    {OUTCOME_VERIFICATION_FAILED, OUTCOME_NO_OP, OUTCOME_BLOCKED}
)


def _g(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _contract_skip_verify(session: Any) -> bool:
    tc = _g(session, "task_contract")
    if isinstance(tc, dict) and bool(tc.get("skip_verify")):
        return True
    return False


def _explore_transition_count(session: Any) -> int:
    n = 0
    for row in _g(session, "phase_transitions") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("to") or "") == "EXPLORE":
            n += 1
    return n


def _exploration_status(session: Any) -> str:
    for row in _g(session, "promotion_decisions") or []:
        if not isinstance(row, dict):
            continue
        if row.get("kind") != "explore_to_act":
            continue
        rc = str(row.get("reason_code") or "")
        if "stagnation" in rc or rc == REASON_READ_ONLY_STAGNATION_THRESHOLD:
            return "forced_promote"
    if _explore_transition_count(session) >= LONG_EXPLORE_PHASE_ENTRIES:
        return "prolonged"
    return "normal"


def _terminal_status(session: Any) -> str:
    rec = _g(session, "terminal_resolution_record")
    if isinstance(rec, dict):
        tp = str(rec.get("terminal_phase") or "").upper()
        if tp == "ABORTED":
            return "aborted"
        if tp == "DONE":
            return "done"
    ph = str(_g(session, "phase") or "").upper()
    if ph == "ABORTED":
        return "aborted"
    if ph == "DONE":
        return "done"
    return "aborted" if _outcome_failed(_g(session, "task_outcome")) else "done"


def _outcome_failed(outcome: Any) -> bool:
    return str(outcome or "") in _OUTCOME_FAILED_HEALTH


def _verify_status(session: Any, om: Mapping[str, Any]) -> str:
    if int(om.get("verification_reuse_count") or 0) > 0:
        return "reused"
    ver = _g(session, "verification")
    if not isinstance(ver, dict) or not ver:
        return "skipped"
    status = str(ver.get("status") or "")
    steps = int(ver.get("steps_executed_count") or 0)
    if status == "skipped" or (steps == 0 and status not in ("success", "failed")):
        return "skipped"
    if status == "failed":
        return "failed"
    if status == "success":
        return "passed"
    if status == "blocked":
        return "failed"
    return "skipped"


def _repair_status(session: Any, om: Mapping[str, Any]) -> str:
    attempts = int(om.get("repair_attempts") or _g(session, "repair_attempt_count") or 0)
    if attempts <= 0:
        return "not_needed"
    if int(om.get("repair_success_count") or 0) > 0:
        return "attempted_succeeded"
    return "attempted_failed"


def _collect_risk_flags(
    session: Any,
    om: Mapping[str, Any],
    exploration: str,
    *,
    terminal_status: str,
) -> List[str]:
    flags: Set[str] = set()
    _ver = _g(session, "verification")
    ver = _ver if isinstance(_ver, dict) else {}
    diff = _g(session, "diff_summary") or []
    has_diff = bool(diff)
    vstat = str(ver.get("status") or "")
    vsteps = int(ver.get("steps_executed_count") or 0)

    if _contract_skip_verify(session) or (has_diff and vstat == "skipped" and vsteps == 0):
        flags.add("verification_skipped")

    if int(om.get("verification_reuse_count") or 0) > 0:
        flags.add("reuse_in_finalize")

    if int(om.get("repair_attempts") or 0) >= REPEATED_REPAIR_MIN_ATTEMPTS and int(om.get("repair_success_count") or 0) == 0:
        flags.add("repeated_repair_failures")

    if exploration == "prolonged":
        flags.add("long_explore")

    if int(om.get("prompt_chars_sent") or 0) >= PROMPT_BLOAT_PROMPT_CHARS:
        flags.add("prompt_bloat")

    snap = _g(session, "provider_response_snapshot")
    if isinstance(snap, dict):
        retries = int(snap.get("retry_attempts") or snap.get("http_retries") or snap.get("retries") or 0)
        if retries >= PROVIDER_RETRY_HEAVY_MIN:
            flags.add("provider_retry_heavy")

    if str(_g(session, "repair_specialist_outcome") or "") == REPAIR_OUTCOME_NO_EFFECTIVE_PATCH:
        flags.add("no_effective_patch")

    if bool(_g(session, "repo_mismatch_detected")):
        flags.add("repo_mismatch")

    # Exploration churn: closed via explore_churn_abort event
    for ev in _g(session, "events") or []:
        if isinstance(ev, dict) and ev.get("event") == "explore_v2_churn_abort":
            flags.add("explore_churn")
            break

    far = str(om.get("final_aborted_reason") or "")
    rec = _g(session, "terminal_resolution_record")
    lar = ""
    if isinstance(rec, dict):
        lar = str(rec.get("loop_abort_reason") or "")
    abort_context = terminal_status == "aborted" or bool(far.strip())
    if abort_context:
        abort_sig = far or lar
        if "budget" in abort_sig.lower() or abort_sig == "budget_exhausted":
            flags.add("aborted_budget")
        if abort_sig in (LOOP_ABORT_POLICY, "policy_block", "terminal_stop") or "policy" in abort_sig.lower():
            flags.add("aborted_policy")
        if abort_sig in (LOOP_ABORT_PROVIDER, "provider_error"):
            flags.add("aborted_provider")
        if abort_sig in (LOOP_ABORT_STAGNATION, "stagnation"):
            flags.add("aborted_stagnation")
        if isinstance(rec, dict):
            raw = rec.get("resolution_sources")
            if isinstance(raw, dict):
                if raw.get("budget_exhausted"):
                    flags.add("aborted_budget")
                if str(raw.get("policy_status") or "") != "ok":
                    flags.add("aborted_policy")
                if str(raw.get("provider_status") or "") != "ok":
                    flags.add("aborted_provider")
                r0 = raw.get("loop_abort_reason_raw")
                if r0 == LOOP_ABORT_STAGNATION:
                    flags.add("aborted_stagnation")

    return sorted(flags & ALLOWED_RISK_FLAGS)


def _health_status(
    session: Any,
    terminal: str,
    verify: str,
    repair: str,
    exploration: str,
    flags: List[str],
) -> str:
    rec = _g(session, "terminal_resolution_record")
    tp = ""
    if isinstance(rec, dict):
        tp = str(rec.get("terminal_phase") or "")
    outcome = str(_g(session, "task_outcome") or "")
    has_diff = bool(_g(session, "diff_summary") or [])

    if terminal == "aborted" or tp.upper() == "ABORTED" or _outcome_failed(outcome):
        return "failed"

    if verify == "failed" or repair == "attempted_failed":
        return "degraded"

    if flags:
        return "degraded"

    if exploration != "normal":
        return "degraded"

    if verify == "reused":
        return "degraded"

    if verify == "skipped":
        if has_diff or _contract_skip_verify(session):
            return "degraded"
        return "healthy"

    if verify == "passed":
        return "healthy"

    return "degraded"


def compute_runtime_health(
    session: Any,
    *,
    operational_metrics: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build the ``runtime_health`` object for JSON export.

    If ``operational_metrics`` is omitted, refreshes from the session (same as artifact export).
    """
    om = operational_metrics
    if om is None:
        om = refresh_operational_metrics_snapshot(session)
    elif not isinstance(om, Mapping):
        om = refresh_operational_metrics_snapshot(session)
    else:
        om = dict(om)

    exploration = _exploration_status(session)
    terminal = _terminal_status(session)
    verify = _verify_status(session, om)
    repair = _repair_status(session, om)
    flags = _collect_risk_flags(session, om, exploration, terminal_status=terminal)
    health = _health_status(session, terminal, verify, repair, exploration, flags)

    return {
        "schema_version": RUNTIME_HEALTH_SCHEMA_VERSION,
        "health_status": health,
        "verify_status": verify,
        "repair_status": repair,
        "exploration_status": exploration,
        "terminal_status": terminal,
        "main_risk_flags": flags,
    }


def format_runtime_health_markdown(block: Mapping[str, Any]) -> str:
    flags = block.get("main_risk_flags") or []
    flags_s = ", ".join(f"`{f}`" for f in flags) if flags else "—"
    lines = [
        "## 🩺 Runtime health",
        "",
        f"- **health_status:** `{block.get('health_status', '')}`",
        f"- **terminal_status:** `{block.get('terminal_status', '')}`",
        f"- **verify_status:** `{block.get('verify_status', '')}`",
        f"- **repair_status:** `{block.get('repair_status', '')}`",
        f"- **exploration_status:** `{block.get('exploration_status', '')}`",
        f"- **main_risk_flags:** {flags_s}",
        "",
    ]
    return "\n".join(lines)
