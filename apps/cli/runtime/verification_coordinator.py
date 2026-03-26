"""
Single entry point for post-write verification. Wraps VerificationManager, adds coordinator
summary (passed, failed_checks, stderr, reparable), fingerprint-based reuse, and task_contract.skip_verify.

Callers in the assistant must use this module — not VerificationManager directly — so skip_verify,
contract spec, fingerprints, and coordinator fields stay consistent.

Extension guide: docs/RUNTIME_EXTENSION_BOUNDARIES.md
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from apps.cli.runtime.task_contract import append_verification_run_record, get_task_contract_spec
from apps.cli.runtime.verification import (
    CHECK_BLOCKED,
    CHECK_ERROR,
    CHECK_FAILED,
    VerificationManager,
    adjust_verification_plan_for_ux_level,
    choose_verification_plan,
    choose_verification_plan_from_contract_spec,
    filter_plan_to_available_checks,
)

logger = logging.getLogger(__name__)


def verification_diff_fingerprint(diff_summary: List[Dict[str, Any]]) -> str:
    """
    Strong fingerprint for verification reuse: per touched file, path + op metadata + content_sha256.

    Cheap: one SHA-256 over a canonical JSON of sorted rows (no disk reads). Rows missing
    content_sha256 produce an ``incomplete_`` fingerprint — reuse is disabled for those
    (see verification_fingerprint_eligible_for_reuse).
    """
    rows: List[List[str]] = []
    for d in diff_summary or []:
        if not isinstance(d, dict):
            continue
        fp = d.get("file")
        if not fp:
            continue
        norm = str(fp).replace("\\", "/")
        typ = str(d.get("type", ""))
        st = str(d.get("status", ""))
        sha = str(d.get("content_sha256") or "")
        rows.append([norm, typ, st, sha])
    if not rows:
        return hashlib.sha256(b"empty_diff").hexdigest()[:48]
    rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    payload = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    if any(not r[3] for r in rows):
        return "incomplete_" + hashlib.sha256(payload).hexdigest()[:40]
    return hashlib.sha256(payload).hexdigest()[:48]


def verification_fingerprint_eligible_for_reuse(fp: str) -> bool:
    """True only for content-strong fingerprints (every diff row had content_sha256)."""
    return bool(fp) and not str(fp).startswith("incomplete_")


def attach_coordinator_view(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Augment VerificationManager output with structured coordinator fields (artifact + repair)."""
    checks = list(raw.get("checks") or [])
    failed_checks = [
        dict(c) for c in checks if c.get("status") in (CHECK_FAILED, CHECK_ERROR, CHECK_BLOCKED)
    ]
    stderr: List[Dict[str, Any]] = []
    for c in checks:
        if c.get("status") in (CHECK_FAILED, CHECK_ERROR):
            text = c.get("stderr") or c.get("stdout") or c.get("output_summary") or ""
            stderr.append({"name": c.get("name"), "text": str(text)[:12000]})
    st = raw.get("status")
    passed = st == "success"
    reparable = any(
        c.get("name") in ("Build", "TypeCheck", "Lint")
        for c in failed_checks
        if isinstance(c, dict)
    )
    out = dict(raw)
    out["coordinator"] = {
        "passed": passed,
        "failed_checks": failed_checks,
        "stderr": stderr,
        "reparable": reparable,
    }
    out["verification_source"] = "VerificationCoordinator"
    return out


class VerificationCoordinator:
    """Decides plan (via VerificationManager), runs checks once, returns structured results."""

    def __init__(self, manager: VerificationManager) -> None:
        self._mgr = manager

    def planned_check_labels(
        self,
        *,
        task_type: str,
        scope: str,
        files_changed: Optional[List[Dict[str, Any]]],
        budget_remaining: Optional[int],
        contract_spec: Optional[Dict[str, Any]] = None,
        taskspec_dict: Optional[Dict[str, Any]] = None,
        repo_v2: Optional[Dict[str, Any]] = None,
        failed_check_names: Optional[List[str]] = None,
    ) -> List[str]:
        """Human-readable planned check names (same resolution as verify_change)."""
        spec: Optional[Dict[str, Any]] = (
            contract_spec if contract_spec is not None else taskspec_dict
        )
        all_checks = self._mgr.get_applicable_checks(
            task_type=task_type, scope=scope, files_changed=files_changed
        )
        if spec is not None:
            plan = choose_verification_plan_from_contract_spec(
                spec,
                task_type=task_type,
                scope=scope,
                files_changed=files_changed,
                budget_remaining=budget_remaining,
                failed_check_names=failed_check_names,
                repo_v2=repo_v2,
            )
        else:
            plan = choose_verification_plan(
                task_type=task_type,
                scope=scope,
                files_changed=files_changed,
                budget_remaining=budget_remaining,
                failed_check_names=failed_check_names,
                repo_v2=repo_v2,
            )
        plan = filter_plan_to_available_checks(plan, all_checks)
        plan = adjust_verification_plan_for_ux_level(
            plan, all_checks, failed_check_names=failed_check_names
        )
        if plan.check_names:
            return list(plan.check_names)
        return [c["name"] for c in all_checks[:5]]

    def try_reuse_stored_verification(
        self,
        session: Any,
        diff_summary: List[Dict[str, Any]],
        *,
        verification_completed: bool,
    ) -> Optional[Dict[str, Any]]:
        fp_new = verification_diff_fingerprint(diff_summary)
        fp_old = getattr(session, "verification_diff_fingerprint", "") or ""

        def _audit(reused: bool, reason: str, **extra: Any) -> None:
            if session is None or not hasattr(session, "verification_reuse_decisions"):
                return
            row: Dict[str, Any] = {
                "ts": datetime.now().isoformat(),
                "reused": reused,
                "reason": reason,
                "fp_new": fp_new,
                "fp_old": fp_old,
                "fp_new_eligible": verification_fingerprint_eligible_for_reuse(fp_new),
                "fp_old_eligible": verification_fingerprint_eligible_for_reuse(fp_old),
            }
            row.update(extra)
            session.verification_reuse_decisions.append(row)

        if not verification_completed:
            _audit(False, "prior_verification_not_completed")
            return None
        prev = getattr(session, "verification", None) or {}
        if int(prev.get("steps_executed_count") or 0) <= 0:
            _audit(False, "no_prior_executed_steps")
            return None
        if not verification_fingerprint_eligible_for_reuse(fp_old):
            _audit(False, "stored_fingerprint_ineligible")
            return None
        if not verification_fingerprint_eligible_for_reuse(fp_new):
            _audit(False, "current_diff_fingerprint_ineligible")
            return None
        if fp_new != fp_old:
            _audit(False, "fingerprint_mismatch", fp_match=False)
            return None
        out = attach_coordinator_view(dict(prev))
        _audit(True, "fingerprint_match", fp_match=True)
        logger.info("VerificationCoordinator: reusing stored verification (fingerprint match)")
        return out

    def run(
        self,
        *,
        diff_summary: List[Dict[str, Any]],
        task_contract: Optional[Dict[str, Any]],
        task_type: str,
        scope: str,
        budget_remaining: int,
        history: Optional[List[Dict[str, Any]]] = None,
        blocked_commands: Optional[List[tuple]] = None,
        skip_execution: bool = False,
        skip_reason: Optional[str] = None,
        repo_v2: Optional[Dict[str, Any]] = None,
        verify_batch_approval_fn: Optional[Callable[[List[str]], bool]] = None,
        session: Any = None,
        reuse_if_verified: bool = False,
        failed_check_names: Optional[List[str]] = None,
        verification_completed_flag: bool = False,
    ) -> Dict[str, Any]:
        """
        Run integrity checks or reuse last executed results if fingerprint matches diff_summary.
        task_contract: optional; spec drives verification_policy; skip_verify=True forces skip_execution.
        """
        spec: Optional[Dict[str, Any]] = None
        if isinstance(task_contract, dict):
            if bool(task_contract.get("skip_verify")):
                skip_execution = True
                skip_reason = skip_reason or "skip_verify"
            sp = task_contract.get("spec")
            if isinstance(sp, Mapping):
                spec = dict(sp)
        if spec is None and session is not None:
            ts = get_task_contract_spec(session)
            if isinstance(ts, Mapping):
                spec = dict(ts)

        if reuse_if_verified and session is not None:
            reused = self.try_reuse_stored_verification(
                session,
                diff_summary,
                verification_completed=verification_completed_flag,
            )
            if reused is not None:
                return reused

        raw = self._mgr.verify_change(
            task_type=task_type,
            history=history,
            blocked_commands=blocked_commands,
            skip_execution=skip_execution,
            skip_reason=skip_reason,
            scope=scope,
            files_changed=diff_summary,
            budget_remaining=budget_remaining,
            failed_check_names=failed_check_names,
            contract_spec=spec,
            repo_v2=repo_v2,
            verify_batch_approval_fn=verify_batch_approval_fn,
        )
        out = attach_coordinator_view(raw)
        if session is not None and int(out.get("steps_executed_count") or 0) > 0 and not skip_execution:
            session.verification_diff_fingerprint = verification_diff_fingerprint(diff_summary)
        if session is not None:
            try:
                append_verification_run_record(
                    session,
                    {
                        "at": datetime.now().isoformat(),
                        "status": out.get("status"),
                        "skip_execution": bool(skip_execution),
                        "skip_reason": skip_reason,
                        "steps_executed_count": int(out.get("steps_executed_count") or 0),
                    },
                )
            except Exception:
                pass
        return out


def coordinator_for_session(cwd: str, repo_vc: Optional[Dict[str, Any]] = None) -> VerificationCoordinator:
    m = VerificationManager(cwd, repo_verification_commands=repo_vc)
    return VerificationCoordinator(m)
