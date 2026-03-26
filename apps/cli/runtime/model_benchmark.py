"""
Deterministic micro-benchmarks for NIM model roles (no repo writes).
"""
from __future__ import annotations

import os
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from apps.cli.runtime.execution_agent import (
    LIVE_JSON_SYSTEM,
    CandidateEditTarget,
    ExecutionContextBundle,
    build_execution_prompt,
    build_planner_prompt,
    build_repair_prompt,
    parse_execution_response_to_proposals,
)
from apps.cli.runtime.nim_provider import ProviderRequest, nim_chat_complete

BENCHMARK_PROMPT_PLANNER = "List 3 bullet steps to triage a failing API route. Max 40 words."

BENCHMARK_PROMPT_CODE = """Return ONLY valid JSON: one object with keys file_path, action_type, summary, diff_or_patch, confidence.
Example file_path: demo.ts action_type: edit summary: noop diff_or_patch: // noop confidence: 0.5"""

BENCHMARK_PROMPT_REPAIR = "One sentence: what to check first when tests fail after a schema change?"


class BenchmarkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    model: str
    success: bool
    latency_ms: float = 0.0
    chars_out: int = 0
    parsed_ok: bool = False
    error_message: str = ""


def _nim_client():
    base = os.environ.get("GHOST_NIM_BASE_URL", "").strip()
    key = os.environ.get("GHOST_NIM_API_KEY", "").strip()
    return base, key


def benchmark_model_call(
    role: str,
    model_name: str,
    prompt: str,
    *,
    system: str = "",
    n: int = 1,
    session: Optional[Any] = None,
) -> BenchmarkResult:
    base, key = _nim_client()
    if not base or not key:
        return BenchmarkResult(
            role=role,
            model=model_name,
            success=False,
            error_message="nim_not_configured",
        )
    last = BenchmarkResult(role=role, model=model_name, success=False, error_message="no_runs")
    for _ in range(max(1, n)):
        req = ProviderRequest(model=model_name, system=system or "You are a benchmark target.", user=prompt)
        resp = nim_chat_complete(base, key, req, session=session)
        parsed = False
        if resp.ok and role == "execution":
            props, p_ok = parse_execution_response_to_proposals(resp.raw_text, ["demo.ts"])
            parsed = p_ok and bool(props)
        last = BenchmarkResult(
            role=role,
            model=model_name,
            success=resp.ok,
            latency_ms=resp.timing.latency_ms,
            chars_out=len(resp.raw_text) if resp.ok else 0,
            parsed_ok=parsed if role == "execution" else resp.ok,
            error_message=resp.error_message if not resp.ok else "",
        )
    return last


def compare_models_for_role(
    role: str,
    model_names: List[str],
    prompt: str,
    *,
    system: str = "",
) -> List[BenchmarkResult]:
    return [benchmark_model_call(role, m, prompt, system=system) for m in model_names]


def benchmark_planner_scenario(model_name: str) -> BenchmarkResult:
    ts = {"intent": "modification", "scope": ["api"], "acceptance_criteria": ["fix handler"]}
    dp = {"exploration_plan": {"ranked_candidates": []}, "execution_plan": {"execution_strategy": "direct_edit"}}
    p = build_planner_prompt(ts, dp)
    return benchmark_model_call("planner", model_name, p[:2000])


def benchmark_code_scenario(model_name: str) -> BenchmarkResult:
    bundle = ExecutionContextBundle(
        targets=[CandidateEditTarget(path="src/a.ts", source="taskspec", confidence=1.0, reasons=["t"])],
        chunks=[],
    )
    ts = {"intent": "modification", "scope": ["api"], "change_expectation": "may_write", "forbidden_layers": []}
    p = build_execution_prompt(bundle, ts, {}, {})
    return benchmark_model_call("execution", model_name, BENCHMARK_PROMPT_CODE + "\n\n" + p[:1500], system=LIVE_JSON_SYSTEM)


def benchmark_repair_scenario(model_name: str) -> BenchmarkResult:
    p = build_repair_prompt(
        {"intent": "bugfix", "scope": ["data"]},
        {"repair_attempt_count": 1},
        last_failure="typecheck failed",
    )
    return benchmark_model_call("repair", model_name, p[:2000])


def append_benchmark_to_session(session: Any, result: BenchmarkResult) -> None:
    if not hasattr(session, "benchmark_results"):
        session.benchmark_results = []
    session.benchmark_results.append(result.model_dump(mode="json"))
