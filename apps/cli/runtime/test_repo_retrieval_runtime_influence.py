"""Sprint 2 — retrieval confidence, merged read order, TaskSpec-safe filtering, prompt/artifact fields."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.repo_retrieval import (
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
    build_retrieval_highlight_chunks,
    build_retrieval_index,
    build_retrieval_prompt_block,
    derive_likely_edit_targets,
    merge_exploration_and_retrieval,
    resolve_retrieval_mode,
    retrieval_confidence_score,
    retrieval_influences_runtime_enabled,
    retrieval_is_actionable,
    retrieval_result_to_prompt_block,
    retrieve_code,
    run_retrieval_for_session,
)
from apps.cli.runtime.task_contract import Intent, ensure_task_contract_foundation
from apps.cli.runtime.taskspec_adapter import build_contract_prompt_blocks


def _artifact_with_taskspec_spec(spec: dict) -> ArtifactSession:
    s = ArtifactSession("s", "t")
    ensure_task_contract_foundation(s, Intent(mode="Chat", task="t", original_text="t"))
    s.task_contract["spec"] = spec
    s.runtime_contract_source = "taskspec"
    return s


def _mini_repo(root: Path) -> None:
    (root / "app" / "api" / "issues").mkdir(parents=True)
    (root / "app" / "api" / "issues" / "route.ts").write_text(
        """
export async function GET() {
  return Response.json([]);
}
export async function POST(req: Request) {
  return Response.json({ ok: true });
}
""",
        encoding="utf-8",
    )
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "validations.ts").write_text(
        'import { z } from "zod";\nexport const issueSchema = z.object({ status: z.enum(["open"]) });\n',
        encoding="utf-8",
    )
    (root / "db").mkdir(parents=True)
    (root / "db" / "schema.ts").write_text(
        "import { pgTable } from 'drizzle-orm/pg-core';\nexport const issues = pgTable('issues', {});\n",
        encoding="utf-8",
    )


class TestRepoRetrievalRuntimeInfluence(unittest.TestCase):
    def test_actionable_merge_inserts_retrieval_before_exploration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mini_repo(root)
            res = build_retrieval_index(root, {}, force_refresh=True)
            dq = RetrievalQuery(
                text="GET issues API handler",
                layer_boost_api=True,
                max_hits=14,
            )
            rr = retrieve_code(dq, res.index)
            self.assertTrue(retrieval_is_actionable(rr, {}, {}, dq))
            ep = {"ranked_files": ["lib/validations.ts", "db/schema.ts"], "ranked_dirs": [], "evidence_lines": []}
            merged_with = merge_exploration_and_retrieval(
                {}, ep, None, {}, rr, dq, include_retrieval_tier=True
            )
            merged_without = merge_exploration_and_retrieval(
                {}, ep, None, {}, rr, dq, include_retrieval_tier=False
            )
            paths_with = [m["path"] for m in merged_with]
            paths_without = [m["path"] for m in merged_without]
            self.assertIn("app/api/issues/route.ts", paths_with)
            route_idx = paths_with.index("app/api/issues/route.ts")
            lib_idx = paths_with.index("lib/validations.ts")
            self.assertLess(route_idx, lib_idx)
            self.assertNotIn("app/api/issues/route.ts", paths_without)

    def test_weak_retrieval_omits_tier_matches_planner_exploration_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mini_repo(root)
            res = build_retrieval_index(root, {}, force_refresh=True)
            dq = RetrievalQuery(text="zzzznonexistenttoken", max_hits=14)
            rr = retrieve_code(dq, res.index)
            self.assertLess(len(rr.hits), 2)
            self.assertFalse(retrieval_is_actionable(rr, {}, {}, dq))
            ep = {"ranked_files": ["db/schema.ts", "lib/validations.ts"], "ranked_dirs": [], "evidence_lines": []}
            m = merge_exploration_and_retrieval({}, ep, None, {}, rr, dq, include_retrieval_tier=False)
            paths = [x["path"] for x in m]
            self.assertEqual(paths[:2], ["db/schema.ts", "lib/validations.ts"])

    def test_forbidden_ui_roots_excluded_from_merge_even_if_high_score(self):
        rr = RetrievalResult(
            hits=[
                RetrievalHit(
                    file_path="src/app/page.tsx",
                    chunk_id="c1",
                    score=200.0,
                    reasons=["lexical"],
                    start_line=1,
                    end_line=5,
                ),
                RetrievalHit(
                    file_path="app/api/x/route.ts",
                    chunk_id="c2",
                    score=50.0,
                    reasons=["api_layer"],
                    start_line=1,
                    end_line=10,
                ),
            ],
            top_files=[("src/app/page.tsx", 200.0), ("app/api/x/route.ts", 50.0)],
        )
        ts = {"forbidden_layers": ["ui"], "target_files": []}
        rp = {"entrypoints": {"ui_roots": ["src/app"]}}
        dq = RetrievalQuery(text="page", forbidden_roots=[], max_hits=8)
        m = merge_exploration_and_retrieval(ts, {}, None, rp, rr, dq, include_retrieval_tier=True)
        paths = [x["path"] for x in m]
        self.assertNotIn("src/app/page.tsx", paths)
        self.assertIn("app/api/x/route.ts", paths)

    def test_target_files_stay_first_in_merge(self):
        rr = RetrievalResult(
            hits=[
                RetrievalHit(file_path="z/other.ts", chunk_id="a", score=99.0, reasons=[], start_line=1, end_line=2),
                RetrievalHit(file_path="z/b.ts", chunk_id="b", score=88.0, reasons=[], start_line=1, end_line=2),
            ],
            top_files=[("z/other.ts", 99.0)],
        )
        ts = {"target_files": ["db/schema.ts"], "forbidden_layers": []}
        dq = RetrievalQuery(text="x", max_hits=8)
        m = merge_exploration_and_retrieval(ts, {"ranked_files": ["z/other.ts"]}, None, {}, rr, dq, include_retrieval_tier=True)
        self.assertEqual(m[0]["path"], "db/schema.ts")
        self.assertEqual(m[0]["source"], "contract_spec_target")

    def test_likely_edit_targets_when_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mini_repo(root)
            res = build_retrieval_index(root, {}, force_refresh=True)
            dq = RetrievalQuery(text="issues GET API", layer_boost_api=True, max_hits=14)
            rr = retrieve_code(dq, res.index)
            actionable = retrieval_is_actionable(rr, {}, {}, dq)
            self.assertTrue(actionable)
            paths, reasons = derive_likely_edit_targets(rr, dq, {}, {}, actionable=True)
            self.assertTrue(paths)
            self.assertEqual(len(paths), len(reasons))
            self.assertTrue(any("route" in p for p in paths))

    def test_mode_advisory_when_influence_flag_off(self):
        with mock.patch.dict(os.environ, {"GHOST_USE_RETRIEVAL": "1", "GHOST_RETRIEVAL_INFLUENCES_RUNTIME": "0"}):
            self.assertFalse(retrieval_influences_runtime_enabled())
            self.assertEqual(resolve_retrieval_mode(True), "advisory")

    def test_mode_influencing_when_both_flags_on(self):
        with mock.patch.dict(os.environ, {"GHOST_USE_RETRIEVAL": "1", "GHOST_RETRIEVAL_INFLUENCES_RUNTIME": "1"}):
            self.assertTrue(retrieval_influences_runtime_enabled())
            self.assertEqual(resolve_retrieval_mode(True), "influencing")

    def test_mode_fallback_when_not_actionable(self):
        self.assertEqual(resolve_retrieval_mode(False), "fallback")

    def test_artifact_dict_contains_influence_fields(self):
        s = ArtifactSession("s", "t")
        s.retrieval_enabled = True
        s.retrieval_mode = "influencing"
        s.retrieval_actionable = True
        s.retrieval_confidence = 0.71
        s.retrieval_runtime_influenced = True
        s.likely_edit_targets = ["app/api/route.ts"]
        s.likely_edit_target_reasons = ["top hit"]
        s.merged_candidate_order = [{"path": "app/api/route.ts", "source": "retrieval", "reason": "x"}]
        s.retrieval_highlight_chunks = [{"file_path": "a.ts", "start_line": 1, "end_line": 3, "reason": "r"}]
        d = s.to_dict()
        for k in (
            "retrieval_mode",
            "retrieval_actionable",
            "retrieval_confidence",
            "retrieval_runtime_influenced",
            "likely_edit_targets",
            "likely_edit_target_reasons",
            "merged_candidate_order",
            "retrieval_highlight_chunks",
        ):
            self.assertIn(k, d)

    def test_merge_is_deterministic(self):
        rr = RetrievalResult(
            hits=[
                RetrievalHit(file_path="a.ts", chunk_id="1", score=10.0, reasons=[], start_line=1, end_line=2),
                RetrievalHit(file_path="b.ts", chunk_id="2", score=9.0, reasons=[], start_line=1, end_line=2),
            ],
            top_files=[("a.ts", 10.0), ("b.ts", 9.0)],
        )
        dq = RetrievalQuery(text="t", max_hits=8)
        ep = {"ranked_files": ["c.ts"], "ranked_dirs": [], "evidence_lines": []}
        m1 = merge_exploration_and_retrieval({}, ep, None, {}, rr, dq, include_retrieval_tier=True)
        m2 = merge_exploration_and_retrieval({}, ep, None, {}, rr, dq, include_retrieval_tier=True)
        self.assertEqual(m1, m2)

    def test_prompt_block_includes_chunk_line_ranges(self):
        rr = RetrievalResult(
            hits=[
                RetrievalHit(
                    file_path="app/api/issues/route.ts",
                    chunk_id="h1",
                    score=80.0,
                    reasons=["api_layer"],
                    start_line=10,
                    end_line=42,
                )
            ],
            top_files=[("app/api/issues/route.ts", 80.0)],
        )
        hc = build_retrieval_highlight_chunks(rr)
        block = retrieval_result_to_prompt_block(
            rr,
            "q",
            mode="influencing",
            confidence=0.8,
            actionable=True,
            runtime_influenced=True,
            highlight_chunks=hc,
        )
        self.assertIn("app/api/issues/route.ts:10-42", block)
        self.assertIn("RETRIEVAL-GUIDED CONTEXT", block)

    def test_build_contract_uses_merged_exploration_when_influencing(self):
        s = _artifact_with_taskspec_spec(
            {"intent": "modification", "scope": ["api"], "forbidden_layers": [], "target_files": []}
        )
        s.retrieval_enabled = True
        s.retrieval_mode = "influencing"
        s.merged_candidate_order = [
            {"path": "app/api/z.ts", "source": "retrieval", "reason": "r"},
            {"path": "lib/v.ts", "source": "exploration", "reason": "e"},
        ]
        s.exploration_plan = {
            "ranked_files": ["lib/v.ts"],
            "ranked_dirs": ["app"],
            "evidence_lines": ["test evidence"],
            "ui_exploration_blocked": False,
        }
        _, _, exp = build_contract_prompt_blocks(s)
        self.assertIn("retrieval-guided merge", exp)
        self.assertIn("app/api/z.ts", exp)
        self.assertLess(exp.index("app/api/z.ts"), exp.index("lib/v.ts"))

    def test_advisory_mode_keeps_classic_exploration_block(self):
        s = _artifact_with_taskspec_spec(
            {"intent": "modification", "scope": ["api"], "forbidden_layers": [], "target_files": []}
        )
        s.retrieval_enabled = True
        s.retrieval_mode = "advisory"
        s.merged_candidate_order = [{"path": "only/in/artifact", "source": "retrieval", "reason": ""}]
        s.exploration_plan = {
            "ranked_files": ["lib/a.ts"],
            "ranked_dirs": [],
            "evidence_lines": [],
            "ui_exploration_blocked": False,
        }
        _, _, exp = build_contract_prompt_blocks(s)
        self.assertIn("RepoProfile v2", exp)
        self.assertNotIn("retrieval-guided merge", exp)
        self.assertNotIn("only/in/artifact", exp)

    def test_run_retrieval_flag_off_clears_influence_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mini_repo(root)
            session = _artifact_with_taskspec_spec(
                {
                    "intent": "implementation",
                    "scope": ["api"],
                    "target_files": [],
                    "forbidden_layers": [],
                }
            )
            session.repo_profile = {"profile_v2": {}}
            session.exploration_plan = {"ranked_files": [], "ranked_dirs": [], "evidence_lines": []}
            with mock.patch.dict(os.environ, {"GHOST_USE_RETRIEVAL": "0"}):
                run_retrieval_for_session(root, session, "GET issues")
            self.assertFalse(session.retrieval_enabled)
            self.assertEqual(session.retrieval_mode, "")
            self.assertEqual(session.merged_candidate_order, [])

    def test_retrieval_confidence_score_bounded(self):
        rr = RetrievalResult(hits=[])
        self.assertEqual(retrieval_confidence_score(rr), 0.0)
        rr2 = RetrievalResult(
            hits=[
                RetrievalHit(file_path="a.ts", chunk_id="1", score=100.0, reasons=["target_file"], start_line=1, end_line=2),
                RetrievalHit(file_path="b.ts", chunk_id="2", score=90.0, reasons=["symbol:x"], start_line=1, end_line=2),
            ]
        )
        c = retrieval_confidence_score(rr2)
        self.assertGreater(c, 0.4)
        self.assertLessEqual(c, 1.0)

    def test_build_retrieval_prompt_block_from_session(self):
        s = ArtifactSession("s", "t")
        s.retrieval_enabled = True
        s.retrieval_mode = "advisory"
        s.retrieval_actionable = True
        s.retrieval_confidence = 0.55
        s.retrieval_runtime_influenced = False
        s.retrieval_query = {"text": "hello"}
        s.retrieval_result = RetrievalResult(
            hits=[
                RetrievalHit(file_path="f.ts", chunk_id="c", score=40.0, reasons=["lexical"], start_line=2, end_line=8)
            ],
            top_files=[("f.ts", 40.0)],
            query_ms=1.0,
            chunks_scored=3,
        ).model_dump(mode="json")
        s.likely_edit_targets = ["f.ts"]
        s.likely_edit_target_reasons = ["score"]
        s.retrieval_highlight_chunks = build_retrieval_highlight_chunks(
            RetrievalResult.model_validate(s.retrieval_result)
        )
        blk = build_retrieval_prompt_block(s)
        self.assertIn("REPO RETRIEVAL", blk)
        self.assertIn("f.ts:2-8", blk)


if __name__ == "__main__":
    unittest.main()
