"""Repo retrieval Sprint 1 — index, chunking, symbols, ranking, cache, advisory."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.repo_retrieval import (
    CodeChunk,
    ImportEdge,
    NullEmbeddingProvider,
    RepoRetrievalIndex,
    RetrievalQuery,
    RetrievalResult,
    SymbolRecord,
    build_retrieval_index,
    build_retrieval_query_from_contract_spec,
    extract_imports,
    extract_symbols_ts_py,
    is_retrieval_enabled,
    process_file,
    retrieve_code,
    retrieval_result_to_prompt_block,
    run_retrieval_for_session,
    clear_retrieval_session_fields,
)


class TestRepoRetrieval(unittest.TestCase):
    def _write_mini_repo(self, root: Path) -> None:
        (root / "app" / "api" / "issues").mkdir(parents=True)
        (root / "app" / "api" / "issues" / "route.ts").write_text(
            """
export async function GET() {
  return Response.json([]);
}
export async function POST(req: Request) {
  const body = await req.json();
  return Response.json({ ok: true });
}
""",
            encoding="utf-8",
        )
        (root / "lib").mkdir(parents=True)
        (root / "lib" / "validations.ts").write_text(
            """
import { z } from "zod";
export const issueSchema = z.object({ status: z.enum(["open", "closed"]) });
""",
            encoding="utf-8",
        )
        (root / "db").mkdir(parents=True)
        (root / "db" / "schema.ts").write_text(
            "import { pgTable } from 'drizzle-orm/pg-core';\nexport const issues = pgTable('issues', {});\n",
            encoding="utf-8",
        )

    def test_index_builds_small_ts_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_repo(root)
            res = build_retrieval_index(root, {}, force_refresh=True)
            self.assertGreater(res.stats.file_count, 0)
            self.assertGreater(res.stats.chunk_count, 0)
            self.assertTrue((root / ".ghost" / "cache" / "repo_retrieval_index.json").is_file())

    def test_chunking_bounded_line_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_repo(root)
            p = root / "app" / "api" / "issues" / "route.ts"
            chunks, syms, im, err = process_file(p, root)
            self.assertIsNone(err)
            self.assertTrue(chunks)
            for c in chunks:
                self.assertGreaterEqual(c.end_line, c.start_line)
                self.assertLessEqual(c.end_line - c.start_line, 500)

    def test_ignore_ghost_worktrees_from_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_repo(root)
            (root / ".ghost" / "worktrees" / "w1" / "apps" / "web").mkdir(parents=True)
            (root / ".ghost" / "worktrees" / "w1" / "apps" / "web" / "page.tsx").write_text(
                "export default function W(){return null}",
                encoding="utf-8",
            )

            res = build_retrieval_index(root, {}, force_refresh=True)
            indexed_paths = set(res.index.file_fingerprints.keys())
            self.assertTrue(indexed_paths)
            self.assertFalse(any(p.startswith(".ghost/") for p in indexed_paths))

    def test_symbol_extraction_functions_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_repo(root)
            p = root / "app" / "api" / "issues" / "route.ts"
            chunks, syms, _, err = process_file(p, root)
            self.assertIsNone(err)
            names = {s.name for s in syms if s.kind == "function"}
            self.assertIn("GET", names)
            self.assertIn("POST", names)

    def test_import_extraction(self):
        text = 'import { z } from "zod";\nconst x = require("fs");\n'
        edges = extract_imports(text, "lib/x.ts", "typescript")
        targets = {e.target_ref for e in edges}
        self.assertIn("zod", targets)

    def test_lexical_retrieval_api_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_repo(root)
            res = build_retrieval_index(root, {}, force_refresh=True)
            q = RetrievalQuery(text="GET issues API route", max_hits=8, layer_boost_api=True)
            rr = retrieve_code(q, res.index, NullEmbeddingProvider())
            files = [h.file_path for h in rr.hits]
            self.assertTrue(any("route.ts" in f for f in files))

    def test_forbidden_ui_roots_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "app").mkdir(parents=True)
            (root / "src" / "app" / "page.tsx").write_text("export default function Page(){return null}", encoding="utf-8")
            self._write_mini_repo(root)
            res = build_retrieval_index(root, {}, force_refresh=True)
            q = RetrievalQuery(
                text="page component",
                forbidden_roots=["src/app"],
                max_hits=20,
            )
            rr = retrieve_code(q, res.index, NullEmbeddingProvider())
            for h in rr.hits:
                self.assertNotIn("src/app", h.file_path.replace("\\", "/"))

    def test_planner_preferred_boost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_repo(root)
            res = build_retrieval_index(root, {}, force_refresh=True)
            q1 = RetrievalQuery(text="validation", max_hits=5)
            q2 = RetrievalQuery(text="validation", preferred_files=["lib/validations.ts"], max_hits=5)
            r1 = retrieve_code(q1, res.index, NullEmbeddingProvider())
            r2 = retrieve_code(q2, res.index, NullEmbeddingProvider())
            top1 = r1.top_files[0][0] if r1.top_files else ""
            top2 = r2.top_files[0][0] if r2.top_files else ""
            self.assertIn("validations", top2)
            if r1.top_files and r2.top_files:
                self.assertGreaterEqual(r2.top_files[0][1], r1.top_files[0][1] - 0.1)

    def test_target_files_outrank(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_repo(root)
            res = build_retrieval_index(root, {}, force_refresh=True)
            q = RetrievalQuery(
                text="schema",
                target_files=["db/schema.ts"],
                preferred_files=["lib/validations.ts"],
                max_hits=6,
            )
            rr = retrieve_code(q, res.index, NullEmbeddingProvider())
            self.assertTrue(rr.top_files)
            self.assertIn("schema.ts", rr.top_files[0][0])

    def test_validation_schema_boost_api_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_repo(root)
            res = build_retrieval_index(root, {}, force_refresh=True)
            q = RetrievalQuery(text="drizzle table issues", layer_boost_data=True, max_hits=8)
            rr = retrieve_code(q, res.index, NullEmbeddingProvider())
            self.assertTrue(any("schema" in h.file_path for h in rr.hits))

    def test_cache_incremental_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_repo(root)
            r1 = build_retrieval_index(root, {}, force_refresh=True)
            r2 = build_retrieval_index(root, {}, force_refresh=False)
            self.assertEqual(r1.stats.chunk_count, r2.stats.chunk_count)
            self.assertEqual(r2.cache_info.files_updated, 0)

    def test_embeddings_disabled_useful_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_repo(root)
            res = build_retrieval_index(root, {}, force_refresh=True)
            with mock.patch.dict(os.environ, {"GHOST_USE_EMBEDDINGS": "0"}):
                rr = retrieve_code(
                    RetrievalQuery(text="zod object", max_hits=5),
                    res.index,
                    NullEmbeddingProvider(),
                )
            self.assertFalse(rr.embeddings_used)
            self.assertTrue(rr.hits or rr.top_files)

    def test_prompt_block_readable(self):
        rr = RetrievalResult(
            hits=[],
            top_files=[("a.ts", 99.0)],
            top_symbols=[("foo", "a.ts")],
            embeddings_used=False,
            query_ms=12.0,
            chunks_scored=10,
        )
        block = retrieval_result_to_prompt_block(rr, "test query")
        self.assertIn("[REPO RETRIEVAL", block)
        self.assertIn("a.ts", block)
        self.assertNotIn("RetrievalResult(", block)

    def test_advisory_flag_off_no_retrieval(self):
        from apps.cli.runtime.task_contract import Intent, ensure_task_contract_foundation

        session = ArtifactSession("s", "t")
        ensure_task_contract_foundation(session, Intent(mode="Chat", task="t", original_text="t"))
        session.runtime_contract_source = "taskspec"
        session.task_contract["spec"] = {"intent": "modification", "scope": ["api"]}
        session.repo_profile = {"profile_v2": {}}
        with mock.patch.dict(os.environ, {"GHOST_USE_RETRIEVAL": "0"}):
            with tempfile.TemporaryDirectory() as tmp:
                run_retrieval_for_session(Path(tmp), session, "hello")
        self.assertFalse(session.retrieval_enabled)

    def test_artifact_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_repo(root)
            from apps.cli.runtime.task_contract import Intent, ensure_task_contract_foundation

            session = ArtifactSession("s", "t")
            ensure_task_contract_foundation(session, Intent(mode="Chat", task="t", original_text="t"))
            session.runtime_contract_source = "taskspec"
            session.task_contract["spec"] = {
                "intent": "implementation",
                "scope": ["api"],
                "target_files": [],
            }
            session.repo_profile = {"profile_v2": {}}
            with mock.patch.dict(os.environ, {"GHOST_USE_RETRIEVAL": "1"}):
                run_retrieval_for_session(root, session, "issues API")
            self.assertTrue(session.retrieval_enabled)
            d = session.to_dict()
            self.assertIn("retrieval_top_files", d)
            self.assertTrue(d["retrieval_result"])

    def test_build_query_from_taskspec(self):
        ts = {"intent": "modification", "scope": ["api", "data"], "target_files": ["app/x.ts"]}
        rp = {
            "api_routes": [{"file": "app/api/route.ts", "path": "/api"}],
            "validation_files": ["lib/v.ts"],
            "db_schema_files": ["db/schema.ts"],
            "layers_detected": ["api", "data"],
            "entrypoints": {"ui_roots": ["src/app"]},
        }
        q = build_retrieval_query_from_contract_spec(ts, rp, user_text="fix schema")
        self.assertIn("app/x.ts", [t.lower() for t in q.target_files])
        self.assertTrue(q.layer_boost_api)
        self.assertTrue(q.layer_boost_data)

    def test_clear_retrieval_fields(self):
        s = ArtifactSession("s", "t")
        s.retrieval_enabled = True
        s.retrieval_result = {"x": 1}
        s.retrieval_mode = "influencing"
        s.merged_candidate_order = [{"path": "a.ts"}]
        s.likely_edit_targets = ["a.ts"]
        clear_retrieval_session_fields(s)
        self.assertFalse(s.retrieval_enabled)
        self.assertEqual(s.retrieval_result, {})
        self.assertEqual(s.retrieval_mode, "")
        self.assertEqual(s.merged_candidate_order, [])
        self.assertEqual(s.likely_edit_targets, [])


if __name__ == "__main__":
    unittest.main()
