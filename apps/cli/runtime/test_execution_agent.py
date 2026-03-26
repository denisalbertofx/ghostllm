"""Execution Agent v1 — modes, context bundle, model selection, advisory behavior."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.execution_agent import (
    DEFAULT_CODE_WRITER_MODEL,
    DEFAULT_GENERAL_FALLBACK_MODEL,
    ENV_EXECUTION_AGENT_LIVE,
    ENV_EXECUTION_FALLBACK_MODEL,
    ENV_EXECUTION_MODEL,
    ENV_GENERAL_FALLBACK_MODEL,
    ENV_REPAIR_MODEL,
    ENV_USE_EXECUTION_AGENT,
    ExecutionAgentInput,
    build_execution_context_bundle,
    build_execution_prompt,
    clear_execution_agent_fields,
    execution_agent_writes_enabled,
    parse_execution_response_to_proposals,
    resolve_execution_mode,
    resolve_model_role_map,
    run_execution_agent,
    run_execution_agent_for_session,
    select_execution_model,
)
from apps.cli.runtime.model_benchmark import BenchmarkResult, benchmark_model_call
from apps.cli.runtime.nim_provider import ProviderResponse, ProviderTiming, ProviderUsage


class TestExecutionAgent(unittest.TestCase):
    def test_no_op_should_not_write(self) -> None:
        inp = ExecutionAgentInput(
            taskspec={"change_expectation": "should_not_write", "intent": "modification", "scope": ["api"]},
            repo_profile_v2={},
            decision_plan={},
        )
        out = run_execution_agent(inp)
        self.assertEqual(out.execution_mode, "no_op")
        self.assertEqual(out.selected_targets, [])
        self.assertEqual(out.proposed_edits, [])

    def test_no_op_review_intent(self) -> None:
        inp = ExecutionAgentInput(
            taskspec={
                "change_expectation": "may_write",
                "intent": "review",
                "scope": ["api"],
                "target_files": ["a.ts"],
            },
            repo_profile_v2={},
            decision_plan={},
        )
        out = run_execution_agent(inp)
        self.assertEqual(out.execution_mode, "no_op")

    def test_single_file_edit_one_target_low_risk(self) -> None:
        inp = ExecutionAgentInput(
            taskspec={
                "change_expectation": "may_write",
                "intent": "modification",
                "scope": ["api"],
                "target_files": ["app/api/x/route.ts"],
            },
            repo_profile_v2={},
            decision_plan={"execution_plan": {"risk": {"level": "low"}, "estimated_complexity": "low"}},
        )
        out = run_execution_agent(inp)
        self.assertEqual(out.execution_mode, "single_file_edit")
        self.assertIn("app/api/x/route.ts", out.selected_targets)

    def test_multi_file_edit(self) -> None:
        inp = ExecutionAgentInput(
            taskspec={
                "change_expectation": "may_write",
                "intent": "implementation",
                "scope": ["api"],
                "target_files": ["src/workers/a.ts", "src/workers/b.ts", "src/workers/c.ts"],
            },
            repo_profile_v2={},
            decision_plan={"execution_plan": {"risk": {"level": "low"}, "estimated_complexity": "low"}},
        )
        out = run_execution_agent(inp)
        self.assertEqual(out.execution_mode, "multi_file_edit")
        self.assertGreaterEqual(len(out.selected_targets), 2)

    def test_staged_edit_medium_risk(self) -> None:
        inp = ExecutionAgentInput(
            taskspec={
                "change_expectation": "may_write",
                "intent": "implementation",
                "scope": ["api"],
                "target_files": ["app/api/x/route.ts"],
            },
            repo_profile_v2={},
            decision_plan={"execution_plan": {"risk": {"level": "medium"}, "estimated_complexity": "medium"}},
        )
        out = run_execution_agent(inp)
        self.assertEqual(out.execution_mode, "staged_edit")

    def test_staged_cross_layers(self) -> None:
        mode, _ = resolve_execution_mode(
            {"change_expectation": "may_write", "intent": "implementation"},
            {"execution_plan": {"risk": {"level": "low"}}},
            ["app/api/x/route.ts", "lib/validations.ts"],
            {"validation_files": ["lib/validations.ts"], "api_routes": [{"file": "app/api/x/route.ts"}]},
        )
        self.assertEqual(mode, "staged_edit")

    def test_forbidden_ui_excluded_from_bundle(self) -> None:
        inp = ExecutionAgentInput(
            taskspec={
                "change_expectation": "may_write",
                "intent": "implementation",
                "scope": ["ui"],
                "forbidden_layers": ["ui"],
                "target_files": ["src/app/page.tsx", "app/api/x/route.ts"],
            },
            repo_profile_v2={"entrypoints": {"ui_roots": ["src/app"]}},
            decision_plan={},
            retrieval_result={
                "top_files": [{"path": "src/app/page.tsx", "score": 200}],
                "hits": [
                    {
                        "file_path": "src/app/page.tsx",
                        "start_line": 1,
                        "end_line": 5,
                        "preview": "x",
                        "score": 200,
                    }
                ],
            },
        )
        bundle = build_execution_context_bundle(inp)
        paths = {t.path for t in bundle.targets}
        self.assertNotIn("src/app/page.tsx", paths)
        chunk_files = {c.file_path for c in bundle.chunks}
        self.assertNotIn("src/app/page.tsx", chunk_files)

    def test_retrieval_chunks_in_bundle(self) -> None:
        inp = ExecutionAgentInput(
            taskspec={"change_expectation": "may_write", "intent": "implementation", "scope": ["api"]},
            repo_profile_v2={},
            decision_plan={},
            retrieval_result={
                "hits": [
                    {
                        "file_path": "app/api/z/route.ts",
                        "start_line": 2,
                        "end_line": 9,
                        "preview": "export function GET",
                        "score": 50,
                    }
                ]
            },
        )
        bundle = build_execution_context_bundle(inp)
        self.assertTrue(any(c.file_path.endswith("route.ts") for c in bundle.chunks))

    def test_model_default_devstral(self) -> None:
        with mock.patch.dict(os.environ, {ENV_EXECUTION_MODEL: ""}):
            sel = select_execution_model({}, {}, {}, target_file_count=1)
        self.assertEqual(sel.model_id, DEFAULT_CODE_WRITER_MODEL)

    def test_fallback_qwen_multi_file_low_risk(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                ENV_EXECUTION_FALLBACK_MODEL: "qwen2.5-coder-32b-instruct",
                ENV_EXECUTION_MODEL: "devstral-2-123b-instruct-2512",
            },
        ):
            sel = select_execution_model(
                {"intent": "implementation"},
                {"execution_plan": {"risk": {"level": "low"}, "estimated_complexity": "low"}},
                {},
                target_file_count=4,
            )
        self.assertEqual(sel.model_id, "qwen2.5-coder-32b-instruct")

    def test_multi_file_default_fallback_is_deepseek(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                ENV_EXECUTION_FALLBACK_MODEL: "",
                ENV_GENERAL_FALLBACK_MODEL: "",
                ENV_EXECUTION_MODEL: "devstral-2-123b-instruct-2512",
            },
            clear=False,
        ):
            sel = select_execution_model(
                {"intent": "implementation"},
                {"execution_plan": {"risk": {"level": "low"}, "estimated_complexity": "low"}},
                {},
                target_file_count=4,
            )
        self.assertEqual(sel.model_id, DEFAULT_GENERAL_FALLBACK_MODEL)

    def test_repair_prefers_glm_when_configured(self) -> None:
        with mock.patch.dict(os.environ, {ENV_REPAIR_MODEL: "glm-5"}):
            sel = select_execution_model(
                {"intent": "bugfix"},
                {},
                {"repair_attempt_count": 2},
                target_file_count=1,
            )
        self.assertEqual(sel.model_id, "glm-5")

    def test_advisory_proposals_no_writes_path(self) -> None:
        with mock.patch.dict(os.environ, {"GHOST_EXECUTION_AGENT_WRITES": "0"}):
            self.assertFalse(execution_agent_writes_enabled())
        inp = ExecutionAgentInput(
            taskspec={
                "change_expectation": "may_write",
                "intent": "modification",
                "scope": ["api"],
                "target_files": ["a.ts"],
            },
            repo_profile_v2={},
            decision_plan={"execution_plan": {"risk": {"level": "low"}}},
        )
        out = run_execution_agent(inp)
        self.assertTrue(out.proposed_edits)
        self.assertTrue(out.provenance.advisory_only)

    def test_artifact_fields_after_apply(self) -> None:
        inp = ExecutionAgentInput(
            taskspec={
                "change_expectation": "may_write",
                "intent": "modification",
                "target_files": ["x.ts"],
                "scope": ["api"],
            },
            repo_profile_v2={},
            decision_plan={"execution_plan": {"risk": {"level": "low"}}},
        )
        out = run_execution_agent(inp)
        s = ArtifactSession("s", "t")
        from apps.cli.runtime.execution_agent import apply_execution_agent_to_session

        apply_execution_agent_to_session(s, out)
        d = s.to_dict()
        self.assertTrue(d["execution_agent_used"])
        self.assertIn("selected_execution_model", d)
        self.assertIn("proposed_edits", d)
        md = s.to_markdown()
        self.assertIn("Execution Agent", md)
        self.assertIn("Proposed edits", md)

    def test_feature_flag_off_clears_session(self) -> None:
        from apps.cli.runtime.task_contract import Intent, ensure_task_contract_foundation

        s = ArtifactSession("s", "t")
        ensure_task_contract_foundation(s, Intent(mode="Chat", task="t", original_text="t"))
        s.runtime_contract_source = "taskspec"
        s.task_contract["spec"] = {
            "intent": "modification",
            "scope": ["api"],
            "change_expectation": "may_write",
        }
        s.repo_profile = {"profile_v2": {}}
        s.execution_agent_used = True
        s.selected_execution_model = "x"
        with mock.patch.dict(os.environ, {ENV_USE_EXECUTION_AGENT: "0"}):
            run_execution_agent_for_session(s)
        self.assertFalse(s.execution_agent_used)
        self.assertEqual(s.selected_execution_model, "")

    def test_build_execution_prompt_readable_not_json_dump(self) -> None:
        bundle = build_execution_context_bundle(
            ExecutionAgentInput(
                taskspec={
                    "intent": "modification",
                    "scope": ["api"],
                    "change_expectation": "may_write",
                    "forbidden_layers": [],
                    "verification_policy": {"required": True, "typecheck": "tsc"},
                },
                repo_profile_v2={},
                decision_plan={"execution_plan": {"guardrails": {"max_diff_lines": 100}}},
                retrieval_result={
                    "hits": [
                        {
                            "file_path": "a.ts",
                            "start_line": 1,
                            "end_line": 3,
                            "preview": "code",
                        }
                    ]
                },
            )
        )
        text = build_execution_prompt(
            bundle,
            {
                "intent": "modification",
                "scope": ["api"],
                "change_expectation": "may_write",
                "forbidden_layers": [],
                "verification_policy": {"required": True, "typecheck": "tsc"},
            },
            {"execution_plan": {"guardrails": {"max_diff_lines": 100}}},
            {},
        )
        self.assertIn("EXECUTION AGENT", text)
        self.assertIn("a.ts:1-3", text)
        self.assertNotIn('{"taskspec"', text)

    def test_parse_valid_json_proposals(self) -> None:
        raw = (
            '[{"file_path":"x.ts","action_type":"edit","summary":"s",'
            '"diff_or_patch":"//d","confidence":0.9}]'
        )
        props, ok = parse_execution_response_to_proposals(raw, [])
        self.assertTrue(ok)
        self.assertEqual(props[0].file_path, "x.ts")
        self.assertTrue(props[0].parsed_from_model)

    def test_parse_json_fence(self) -> None:
        raw = '```json\n[{"file_path":"a.ts","action_type":"edit","summary":"x","diff_or_patch":"","confidence":1}]\n```'
        props, ok = parse_execution_response_to_proposals(raw, [])
        self.assertTrue(ok)
        self.assertEqual(props[0].file_path, "a.ts")

    def test_parse_fail_preserves_raw_carryall(self) -> None:
        props, ok = parse_execution_response_to_proposals("not json {{{", ["z.ts"])
        self.assertFalse(ok)
        self.assertTrue(props)
        self.assertIn("not json", props[0].diff_or_patch or "")

    def test_resolve_model_role_map(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "GHOST_EXECUTION_MODEL": "dev-x",
                "GHOST_PLANNER_MODEL": "plan-x",
                "GHOST_REPAIR_MODEL": "rep-x",
                ENV_GENERAL_FALLBACK_MODEL: "ds-x",
                ENV_EXECUTION_FALLBACK_MODEL: "",
            },
            clear=False,
        ):
            m = resolve_model_role_map()
        self.assertEqual(m["execution"], "dev-x")
        self.assertEqual(m["planner"], "plan-x")
        self.assertEqual(m["repair"], "rep-x")
        self.assertEqual(m["general_fallback"], "ds-x")
        self.assertEqual(m["execution_fallback"], "ds-x")
        # Sin NIM configurado, el backend declarado no debe ser la cadena "null".
        self.assertEqual(m["provider_backend"], "openai_compatible")

    def test_live_generation_mocked_nim(self) -> None:
        fake = ProviderResponse(
            model="m",
            raw_text='[{"file_path":"w.ts","action_type":"edit","summary":"a","diff_or_patch":"//","confidence":1}]',
            ok=True,
            timing=ProviderTiming(latency_ms=12.5),
            usage=ProviderUsage(),
        )
        inp = ExecutionAgentInput(
            taskspec={
                "change_expectation": "may_write",
                "intent": "modification",
                "scope": ["api"],
                "target_files": ["w.ts"],
            },
            repo_profile_v2={},
            decision_plan={"execution_plan": {"risk": {"level": "low"}}},
        )
        env = {
            ENV_USE_EXECUTION_AGENT: "1",
            "GHOST_USE_NVIDIA_NIM": "1",
            "GHOST_NIM_BASE_URL": "https://nim.test",
            "GHOST_NIM_API_KEY": "k",
            ENV_EXECUTION_AGENT_LIVE: "1",
            "GHOST_EXECUTION_AGENT_WRITES": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("apps.cli.runtime.execution_agent.nim_chat_complete", return_value=fake):
                out = run_execution_agent(inp)
        self.assertTrue(out.live_generation_used)
        self.assertTrue(out.provider_ok)
        self.assertTrue(out.proposal_parsed_ok)
        self.assertEqual(out.provider_latency_ms, 12.5)
        self.assertTrue(out.live_raw_proposal)

    def test_live_provider_failure_keeps_deterministic_proposals(self) -> None:
        fake = ProviderResponse(
            model="m",
            ok=False,
            error_message="rate_limited",
            timing=ProviderTiming(latency_ms=5.0),
        )
        inp = ExecutionAgentInput(
            taskspec={
                "change_expectation": "may_write",
                "intent": "modification",
                "scope": ["api"],
                "target_files": ["w.ts"],
            },
            repo_profile_v2={},
            decision_plan={"execution_plan": {"risk": {"level": "low"}}},
        )
        env = {
            ENV_USE_EXECUTION_AGENT: "1",
            "GHOST_USE_NVIDIA_NIM": "1",
            "GHOST_NIM_BASE_URL": "https://nim.test",
            "GHOST_NIM_API_KEY": "k",
            ENV_EXECUTION_AGENT_LIVE: "1",
            "GHOST_EXECUTION_AGENT_WRITES": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("apps.cli.runtime.execution_agent.nim_chat_complete", return_value=fake):
                out = run_execution_agent(inp)
        self.assertFalse(out.provider_ok)
        self.assertTrue(out.proposed_edits)
        self.assertEqual(out.proposed_edits[0].file_path, "w.ts")

    def test_artifact_shows_provider_metadata(self) -> None:
        inp = ExecutionAgentInput(
            taskspec={
                "change_expectation": "may_write",
                "intent": "modification",
                "target_files": ["x.ts"],
                "scope": ["api"],
            },
            repo_profile_v2={},
            decision_plan={"execution_plan": {"risk": {"level": "low"}}},
        )
        out = run_execution_agent(inp)
        out.live_generation_used = True
        out.provider_ok = True
        out.provider_latency_ms = 99.0
        out.proposal_parsed_ok = True
        s = ArtifactSession("s", "t")
        from apps.cli.runtime.execution_agent import apply_execution_agent_to_session

        apply_execution_agent_to_session(s, out)
        d = s.to_dict()
        self.assertTrue(d["live_generation_used"])
        self.assertTrue(d["provider_ok"])
        self.assertEqual(d["provider_latency_ms"], 99.0)
        self.assertTrue(d["proposal_parsed"])
        self.assertIn("provider_backend", d)

    def test_benchmark_result_structure(self) -> None:
        br = BenchmarkResult(
            role="execution",
            model="m",
            success=True,
            latency_ms=10.0,
            chars_out=5,
            parsed_ok=True,
        )
        self.assertEqual(br.model_dump()["role"], "execution")

    def test_benchmark_model_call_without_nim(self) -> None:
        with mock.patch.dict(os.environ, {"GHOST_NIM_BASE_URL": "", "GHOST_NIM_API_KEY": ""}, clear=False):
            r = benchmark_model_call("execution", "any", "ping")
        self.assertFalse(r.success)
        self.assertIn("nim", r.error_message.lower())

    def test_clear_execution_agent_new_fields(self) -> None:
        s = ArtifactSession("s", "t")
        s.live_generation_used = True
        s.provider_ok = False
        s.execution_raw_proposal = "x"
        s.general_fallback_model = "x"
        s.execution_fallback_model = "y"
        clear_execution_agent_fields(s)
        self.assertFalse(s.live_generation_used)
        self.assertTrue(s.provider_ok)
        self.assertEqual(s.execution_raw_proposal, "")
        self.assertEqual(s.general_fallback_model, "")
        self.assertEqual(s.execution_fallback_model, "")


if __name__ == "__main__":
    unittest.main()
