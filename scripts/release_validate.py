#!/usr/bin/env python3
"""
GhostLLM release-readiness validator.

Answers: "Is Ghost release-ready?" — runs targeted pytest modules, checks artifact shape,
and validates phase transitions on sample JSON (fixtures + optional dirs).

Usage:
  python scripts/release_validate.py
  python scripts/release_validate.py --json
  python scripts/release_validate.py --artifact-dir .ghost/artifacts --artifact-limit 15

Exit code: 0 = PASS, 1 = FAIL (CI-friendly).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "release_sample_artifacts"


@dataclass
class SectionResult:
    id: str
    name: str
    passed: bool
    detail: str = ""
    duration_s: float = 0.0


@dataclass
class ReleaseReport:
    status: str  # PASS | FAIL
    sections: List[SectionResult] = field(default_factory=list)
    summary_lines: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "sections": [
                {
                    "id": s.id,
                    "name": s.name,
                    "passed": s.passed,
                    "detail": s.detail,
                    "duration_s": round(s.duration_s, 3),
                }
                for s in self.sections
            ],
            "summary": self.summary_lines,
        }


def _run_pytest(targets: List[str], *, extra_args: Optional[List[str]] = None) -> Tuple[bool, str, float]:
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=no", "--no-header"]
    if extra_args:
        cmd.extend(extra_args)
    cmd.extend(targets)
    t0 = time.monotonic()
    p = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    dt = time.monotonic() - t0
    ok = p.returncode == 0
    tail = (p.stdout or "") + (p.stderr or "")
    tail = tail.strip()[-4000:] if tail else ""
    if not ok and not tail:
        tail = f"exit_code={p.returncode}"
    return ok, tail, dt


def _run_cli_command(args: List[str], *, timeout_s: float = 45.0) -> Tuple[bool, str, float]:
    cmd = [sys.executable, str(REPO_ROOT / "apps" / "cli" / "main.py"), *args]
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    t0 = time.monotonic()
    p = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        env=env,
    )
    dt = time.monotonic() - t0
    output = ((p.stdout or "") + (p.stderr or "")).strip()
    return p.returncode == 0, output[-4000:], dt


def _wait_for_http(url: str, *, timeout_s: float = 20.0) -> Tuple[bool, str]:
    deadline = time.monotonic() + timeout_s
    last_error = "timeout"
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=1.5)
            if r.status_code < 500:
                return True, f"HTTP {r.status_code}"
            last_error = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            last_error = str(e)
        time.sleep(0.4)
    return False, last_error


def _request_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout_s: float = 8.0,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    try:
        r = httpx.request(method, url, headers=headers, json=json_body, timeout=timeout_s)
    except httpx.HTTPError as e:
        return False, str(e), None
    try:
        payload = r.json()
    except ValueError:
        payload = None
    if r.status_code >= 400:
        body = payload if payload is not None else r.text
        return False, f"HTTP {r.status_code}: {body}", payload if isinstance(payload, dict) else None
    return True, f"HTTP {r.status_code}", payload if isinstance(payload, dict) else None


def _product_smoke_sections() -> List[SectionResult]:
    sections: List[SectionResult] = []
    base_url = os.environ.get("GHOST_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
    api_key = os.environ.get("GHOST_API_KEY", "ghost-dev-2026")
    auth_headers = {
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    started_by_gate = False

    ok, detail = _wait_for_http(f"{base_url}/health", timeout_s=2.0)
    if not ok:
        cmd_ok, cmd_detail, dt = _run_cli_command(["start"], timeout_s=30.0)
        sections.append(
            SectionResult(
                id="product_start",
                name="Product smoke: ghost start",
                passed=cmd_ok,
                detail=cmd_detail if not cmd_ok else "ghost start OK",
                duration_s=dt,
            )
        )
        if cmd_ok:
            started_by_gate = True
            ok, detail = _wait_for_http(f"{base_url}/health", timeout_s=20.0)
    else:
        sections.append(
            SectionResult(
                id="product_start",
                name="Product smoke: ghost start",
                passed=True,
                detail="server already reachable; start not required",
                duration_s=0.0,
            )
        )

    gateway_ok = ok
    gateway_lines: List[str] = []
    if gateway_ok:
        ok_h, detail_h, payload_h = _request_json("GET", f"{base_url}/health")
        gateway_ok &= ok_h and isinstance(payload_h, dict) and isinstance(payload_h.get("provider"), dict)
        gateway_lines.append("GET /health: OK" if ok_h else f"GET /health: FAIL — {detail_h}")

        ok_r, detail_r, payload_r = _request_json("GET", f"{base_url}/ready")
        gateway_ok &= ok_r and isinstance(payload_r, dict) and payload_r.get("ready") is True
        gateway_lines.append("GET /ready: OK" if ok_r else f"GET /ready: FAIL — {detail_r}")

        ok_m, detail_m, payload_m = _request_json("GET", f"{base_url}/v1/models")
        gateway_ok &= ok_m and isinstance(payload_m, dict) and payload_m.get("object") == "list"
        gateway_lines.append("GET /v1/models: OK" if ok_m else f"GET /v1/models: FAIL — {detail_m}")

        ok_ct, detail_ct, payload_ct = _request_json(
            "POST",
            f"{base_url}/v1/messages/count_tokens",
            headers=auth_headers,
            json_body={
                "model": "coder",
                "messages": [{"role": "user", "content": "smoke test"}],
            },
        )
        gateway_ok &= ok_ct and isinstance(payload_ct, dict) and isinstance(payload_ct.get("input_tokens"), int)
        gateway_lines.append(
            "POST /v1/messages/count_tokens: OK"
            if ok_ct
            else f"POST /v1/messages/count_tokens: FAIL — {detail_ct}"
        )
    else:
        gateway_lines.append(f"gateway unavailable after start attempt — {detail}")

    sections.append(
        SectionResult(
            id="product_gateway_surface",
            name="Product smoke: gateway health, readiness, models, Claude token surface",
            passed=gateway_ok,
            detail="\n".join(gateway_lines),
            duration_s=0.0,
        )
    )

    cli_checks = [
        ("product_doctor", "Product smoke: ghost doctor", ["doctor"]),
        ("product_dev_preflight", "Product smoke: ghost dev preflight", ["dev", "--preflight-only"]),
        ("product_plan_preflight", "Product smoke: ghost plan preflight", ["plan", "smoke release task", "--preflight-only"]),
        ("product_do_preflight", "Product smoke: ghost do preflight", ["do", "smoke release task", "--preflight-only"]),
        ("product_claude_preflight", "Product smoke: ghost claude preflight", ["claude", "--preflight-only"]),
    ]
    for sec_id, sec_name, cmd_args in cli_checks:
        cmd_ok, cmd_detail, dt = _run_cli_command(cmd_args, timeout_s=45.0)
        sections.append(
            SectionResult(
                id=sec_id,
                name=sec_name,
                passed=cmd_ok,
                detail=cmd_detail if not cmd_ok else "CLI path OK",
                duration_s=dt,
            )
        )

    if started_by_gate:
        stop_ok, stop_detail, dt = _run_cli_command(["stop"], timeout_s=20.0)
        sections.append(
            SectionResult(
                id="product_stop",
                name="Product smoke: ghost stop",
                passed=stop_ok,
                detail=stop_detail if not stop_ok else "ghost stop OK",
                duration_s=dt,
            )
        )

    return sections


def _canonical_artifact_dict() -> Dict[str, Any]:
    """Single in-memory artifact matching current export contract (metrics + health + phases)."""
    from apps.cli.runtime.artifacts import ArtifactSession
    from apps.cli.runtime.task_contract import Intent, ensure_task_contract_foundation

    s = ArtifactSession("release_validate_canonical", "canonical probe")
    ensure_task_contract_foundation(
        s,
        Intent(mode="Chat", task="probe", original_text="probe"),
    )
    for fr, to in (
        ("", "INTAKE"),
        ("INTAKE", "EXPLORE"),
        ("EXPLORE", "ACT"),
        ("ACT", "VERIFY"),
        ("VERIFY", "CLOSING"),
        ("CLOSING", "DONE"),
    ):
        s.record_phase_transition(to, detail="release_validate", from_phase=fr)
    s.phase = "DONE"
    s.task_outcome = "implemented"
    s.terminal_resolution_record = {
        "terminal_phase": "DONE",
        "outcome": "implemented",
        "loop_abort_reason": None,
        "resolution_sources": {"policy_status": "ok", "provider_status": "ok"},
    }
    s.verification = {"status": "success", "steps_executed_count": 1, "checks": []}
    return s.to_dict()


def _normalize_phase_rows(rows: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                **row,
                "from": str(row.get("from") or "").upper(),
                "to": str(row.get("to") or "").upper(),
            }
        )
    return out


def _validate_phase_transitions_on_dict(data: Dict[str, Any], label: str) -> None:
    from apps.cli.runtime.phase_invariants import (
        assert_phase_transition_chain_coherent,
        assert_repair_outgoing_edges_valid,
        phase_edges_from_transitions,
    )

    raw = data.get("phase_transitions")
    if raw is None:
        raise ValueError(f"{label}: missing phase_transitions (cannot validate graph)")
    if isinstance(raw, list) and len(raw) == 0:
        raise ValueError(f"{label}: empty phase_transitions")
    norm = _normalize_phase_rows(raw)
    assert_phase_transition_chain_coherent(norm)
    assert_repair_outgoing_edges_valid(phase_edges_from_transitions(norm))


def _assert_operational_metrics_block(data: Dict[str, Any], label: str) -> None:
    from apps.cli.runtime.session_metrics import OPERATIONAL_METRICS_DEFAULTS

    om = data.get("operational_metrics")
    if not isinstance(om, dict):
        raise ValueError(f"{label}: operational_metrics must be a dict")
    for k in OPERATIONAL_METRICS_DEFAULTS:
        if k not in om:
            raise ValueError(f"{label}: operational_metrics missing key {k!r}")


def _assert_runtime_health_block(data: Dict[str, Any], label: str) -> None:
    from apps.cli.runtime.runtime_health import RUNTIME_HEALTH_SCHEMA_VERSION

    rh = data.get("runtime_health")
    if not isinstance(rh, dict):
        raise ValueError(f"{label}: runtime_health must be a dict")
    for k in (
        "schema_version",
        "health_status",
        "verify_status",
        "repair_status",
        "exploration_status",
        "terminal_status",
        "main_risk_flags",
    ):
        if k not in rh:
            raise ValueError(f"{label}: runtime_health missing key {k!r}")
    if int(rh.get("schema_version") or 0) != int(RUNTIME_HEALTH_SCHEMA_VERSION):
        raise ValueError(
            f"{label}: runtime_health.schema_version mismatch "
            f"(got {rh.get('schema_version')}, want {RUNTIME_HEALTH_SCHEMA_VERSION})"
        )


def _iter_artifact_json_files(
    extra_dir: Optional[Path],
    *,
    limit: int,
) -> List[Path]:
    paths: List[Path] = []
    if DEFAULT_FIXTURE_DIR.is_dir():
        paths.extend(sorted(DEFAULT_FIXTURE_DIR.glob("*.json")))
    if extra_dir and extra_dir.is_dir():
        paths.extend(sorted(extra_dir.glob("*.json")))
    # de-dupe
    seen = set()
    uniq: List[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        uniq.append(p)
    return uniq[: max(1, limit)]


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser(description="GhostLLM release-readiness gate")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON summary to stdout (after human summary).",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print only JSON to stdout (for CI parsers). Implies structured output.",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=None,
        help="Extra directory of task *.json artifacts to scan (optional).",
    )
    parser.add_argument(
        "--artifact-limit",
        type=int,
        default=20,
        help="Max JSON files to scan (fixtures dir + --artifact-dir).",
    )
    parser.add_argument(
        "--skip-artifact-files",
        action="store_true",
        help="Only validate in-memory canonical artifact (no JSON files on disk).",
    )
    args = parser.parse_args()
    if args.json_only:
        args.json = True

    report = ReleaseReport(status="FAIL")

    # --- 1. Architecture invariants (excluding verification reuse class) ---
    ok, detail, dt = _run_pytest(
        ["tests/test_runtime_architecture_invariants.py"],
        extra_args=["-k", "not VerificationReuse"],
    )
    report.sections.append(
        SectionResult(
            id="invariants",
            name="Invariant tests (architecture / phases / terminal authority)",
            passed=ok,
            detail=detail if not ok else "pytest OK",
            duration_s=dt,
        )
    )

    # --- 2. Runtime regression pack ---
    ok, detail, dt = _run_pytest(["tests/test_ghost_v1_runtime_regression.py"])
    report.sections.append(
        SectionResult(
            id="runtime_regression",
            name="Runtime regression pack (Ghost v1)",
            passed=ok,
            detail=detail if not ok else "pytest OK",
            duration_s=dt,
        )
    )

    # --- 3: sample artifacts — phase graph ---
    phase_ok = True
    om_ok = True
    rh_ok = True
    phase_lines: List[str] = []
    om_lines: List[str] = []
    rh_lines: List[str] = []

    canon = _canonical_artifact_dict()
    try:
        _validate_phase_transitions_on_dict(canon, "canonical(session.to_dict)")
        phase_lines.append("canonical(session.to_dict): OK")
    except Exception as e:
        phase_ok = False
        phase_lines.append(f"canonical(session.to_dict): FAIL — {e}")

    try:
        _assert_operational_metrics_block(canon, "canonical(session.to_dict)")
        om_lines.append("canonical(session.to_dict): OK")
    except Exception as e:
        om_ok = False
        om_lines.append(f"canonical(session.to_dict): FAIL — {e}")

    try:
        _assert_runtime_health_block(canon, "canonical(session.to_dict)")
        rh_lines.append("canonical(session.to_dict): OK")
    except Exception as e:
        rh_ok = False
        rh_lines.append(f"canonical(session.to_dict): FAIL — {e}")

    if not args.skip_artifact_files:
        for path in _iter_artifact_json_files(args.artifact_dir, limit=args.artifact_limit):
            try:
                label = str(path.relative_to(REPO_ROOT))
            except ValueError:
                label = str(path)
            try:
                data = _load_json(path)
            except Exception as e:
                phase_ok = False
                om_ok = False
                rh_ok = False
                phase_lines.append(f"{label}: load FAIL — {e}")
                om_lines.append(f"{label}: load FAIL — {e}")
                rh_lines.append(f"{label}: load FAIL — {e}")
                continue
            if not isinstance(data, dict):
                phase_lines.append(f"{label}: skip (not an object)")
                om_lines.append(f"{label}: skip (not an object)")
                rh_lines.append(f"{label}: skip (not an object)")
                continue
            if "phase_transitions" not in data or not data.get("phase_transitions"):
                phase_lines.append(f"{label}: skip (no phase_transitions — legacy)")
            else:
                try:
                    _validate_phase_transitions_on_dict(data, label)
                    phase_lines.append(f"{label}: OK")
                except Exception as e:
                    phase_ok = False
                    phase_lines.append(f"{label}: FAIL — {e}")

            if "operational_metrics" not in data:
                om_lines.append(f"{label}: skip (no operational_metrics — legacy)")
            else:
                try:
                    _assert_operational_metrics_block(data, label)
                    om_lines.append(f"{label}: OK")
                except Exception as e:
                    om_ok = False
                    om_lines.append(f"{label}: FAIL — {e}")
            if "runtime_health" not in data:
                rh_lines.append(f"{label}: skip (no runtime_health — legacy)")
            else:
                try:
                    _assert_runtime_health_block(data, label)
                    rh_lines.append(f"{label}: OK")
                except Exception as e:
                    rh_ok = False
                    rh_lines.append(f"{label}: FAIL — {e}")

    report.sections.append(
        SectionResult(
            id="artifact_phase_graph",
            name="Sample artifacts: legal phase transitions",
            passed=phase_ok,
            detail="\n".join(phase_lines) if phase_lines else "no checks",
            duration_s=0.0,
        )
    )

    # --- 4. Verification reuse ---
    ok, detail, dt = _run_pytest(
        [
            "tests/test_runtime_architecture_invariants.py::TestVerificationReuseInvariants",
            "apps/cli/runtime/test_verification_coordinator.py",
        ]
    )
    report.sections.append(
        SectionResult(
            id="verification_reuse",
            name="Verification reuse tests",
            passed=ok,
            detail=detail if not ok else "pytest OK",
            duration_s=dt,
        )
    )

    # --- 5. Repair specialist ---
    ok, detail, dt = _run_pytest(["apps/cli/runtime/test_repair_specialist.py"])
    report.sections.append(
        SectionResult(
            id="repair_specialist",
            name="Repair specialist outcome tests",
            passed=ok,
            detail=detail if not ok else "pytest OK",
            duration_s=dt,
        )
    )

    # --- 6. TaskContract immutability ---
    ok, detail, dt = _run_pytest(["tests/test_task_contract_immutability.py"])
    report.sections.append(
        SectionResult(
            id="task_contract_immutability",
            name="TaskContract immutability tests",
            passed=ok,
            detail=detail if not ok else "pytest OK",
            duration_s=dt,
        )
    )

    # --- 7. Artifact forensic serialization ---
    ok, detail, dt = _run_pytest(["tests/test_artifact_forensic.py"])
    report.sections.append(
        SectionResult(
            id="artifact_forensic",
            name="Artifact forensic serialization tests",
            passed=ok,
            detail=detail if not ok else "pytest OK",
            duration_s=dt,
        )
    )

    report.sections.append(
        SectionResult(
            id="artifact_operational_metrics",
            name="Artifacts: operational_metrics block present and complete",
            passed=om_ok,
            detail="\n".join(om_lines) if om_lines else "no checks",
            duration_s=0.0,
        )
    )
    report.sections.append(
        SectionResult(
            id="artifact_runtime_health",
            name="Artifacts: runtime_health block present and complete",
            passed=rh_ok,
            detail="\n".join(rh_lines) if rh_lines else "no checks",
            duration_s=0.0,
        )
    )

    ok, detail, dt = _run_pytest(
        ["tests/test_session_operational_metrics.py", "tests/test_runtime_health.py"]
    )
    report.sections.append(
        SectionResult(
            id="metrics_health_engine_tests",
            name="Engine tests: operational metrics + runtime_health computation",
            passed=ok,
            detail=detail if not ok else "pytest OK",
            duration_s=dt,
        )
    )

    # --- 8. Claude bridge smoke (local stubbed provider) ---
    ok, detail, dt = _run_pytest(["tests/test_claude_bridge_smoke.py"])
    report.sections.append(
        SectionResult(
            id="claude_bridge_smoke",
            name="Server smoke: Anthropic bridge local translation path",
            passed=ok,
            detail=detail if not ok else "pytest OK",
            duration_s=dt,
        )
    )

    # --- 9. Product smoke gate ---
    report.sections.extend(_product_smoke_sections())

    failed = [s for s in report.sections if not s.passed]
    report.status = "PASS" if not failed else "FAIL"

    report.summary_lines = [
        f"GhostLLM release validation: {report.status}",
        f"Sections checked: {len(report.sections)}",
        f"Failed: {len(failed)}",
    ]
    if failed:
        report.summary_lines.append("Failing sections:")
        for s in failed:
            report.summary_lines.append(f"  - [{s.id}] {s.name}")
            if s.detail:
                for line in s.detail.split("\n")[:5]:
                    report.summary_lines.append(f"      {line[:500]}")

    if not args.json_only:
        print("\n".join(report.summary_lines))
        print()
        for s in report.sections:
            mark = "PASS" if s.passed else "FAIL"
            print(f"[{mark}] {s.name} ({s.id})")
            if s.duration_s > 0:
                print(f"       ({s.duration_s:.2f}s)")
            if not s.passed and s.detail:
                print(f"       {s.detail[:2000]}")

    if args.json:
        if not args.json_only:
            print()
        print(json.dumps(report.to_json(), indent=2, ensure_ascii=False))

    return 0 if report.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
