"""RepoProfile v2 semantic analyzer — unit + integration with temp fixtures."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from apps.cli.runtime.repo_profile import RepoProfile, scan_repo_profile
from apps.cli.runtime.repo_profile_v2 import (
    PROFILE_VERSION,
    RepoProfileV2,
    build_repo_profile,
    repo_profile_to_prompt_block,
    repo_profile_to_summary_text,
    verification_command_command,
    verification_command_cwd,
)
from apps.cli.runtime.taskspec_adapter import repo_profile_to_dict
from apps.cli.runtime.verification import VerificationManager


class TestRepoProfileV2NextDrizzle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_next_drizzle_fixture(self, use_pnpm: bool = False):
        pkg = {
            "name": "issue-tracker-fixture",
            "scripts": {
                "build": "next build",
                "lint": "next lint",
                "typecheck": "tsc --noEmit",
                "test": "vitest run",
            },
            "dependencies": {
                "drizzle-orm": "^0.29.0",
                "better-sqlite3": "^9.0.0",
                "next": "^14.0.0",
            },
        }
        (self.root / "package.json").write_text(json.dumps(pkg, indent=2), encoding="utf-8")
        if use_pnpm:
            (self.root / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
        else:
            (self.root / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")

        (self.root / "tsconfig.json").write_text(
            '{"compilerOptions": {"strict": true, "noEmit": true}}', encoding="utf-8"
        )
        (self.root / "next.config.js").write_text("module.exports = {};\n", encoding="utf-8")
        (self.root / "drizzle.config.ts").write_text(
            "export default { schema: './db/schema.ts', dialect: 'sqlite' };\n",
            encoding="utf-8",
        )
        (self.root / "db").mkdir(parents=True)
        (self.root / "db" / "schema.ts").write_text(
            'import { sqliteTable, text } from "drizzle-orm/sqlite-core";\n'
            'export const issues = sqliteTable("issues", { id: text("id") });\n',
            encoding="utf-8",
        )
        (self.root / "lib").mkdir(parents=True)
        (self.root / "lib" / "validations.ts").write_text(
            'import { z } from "zod";\nexport const IssueSchema = z.object({ title: z.string() });\n',
            encoding="utf-8",
        )
        (self.root / "app" / "api" / "issues").mkdir(parents=True)
        (self.root / "app" / "api" / "issues" / "route.ts").write_text(
            "export async function GET() { return Response.json([]); }\n"
            "export async function POST() { return new Response(); }\n",
            encoding="utf-8",
        )

    def test_next_ts_drizzle_sqlite_stack(self):
        self._write_next_drizzle_fixture()
        p = build_repo_profile(self.root, cache=False, force_refresh=True)
        self.assertEqual(p.version, PROFILE_VERSION)
        self.assertIn("nextjs", p.stack.framework)
        self.assertIn("typescript", p.stack.language)
        self.assertIn("drizzle", p.stack.orm)
        self.assertIn("sqlite", p.stack.database)
        self.assertGreaterEqual(p.confidence, 0.8)
        self.assertIn("package.json", p.key_files)
        self.assertIn("tsconfig.json", p.key_files)
        self.assertTrue(any("drizzle.config" in f for f in p.key_files))
        self.assertIn("db/schema.ts", p.db_schema_files)
        self.assertIn("lib/validations.ts", p.validation_files)

    def test_api_routes_get_post(self):
        self._write_next_drizzle_fixture()
        p = build_repo_profile(self.root, cache=False)
        issues_routes = [r for r in p.api_routes if "issues" in (r.path or "")]
        self.assertTrue(issues_routes)
        methods = {r.method for r in issues_routes if r.kind == "app_router"}
        self.assertIn("GET", methods)
        self.assertIn("POST", methods)
        path = next(r.path for r in issues_routes if r.path)
        self.assertIn("api/issues", path.replace("//", "/"))

    def test_verification_commands_npm(self):
        self._write_next_drizzle_fixture(use_pnpm=False)
        p = build_repo_profile(self.root, cache=False)
        self.assertIn("npm", " ".join(p.stack.package_manager).lower())
        self.assertIsNotNone(p.verification_commands.typecheck)
        self.assertIn("typecheck", verification_command_command(p.verification_commands.typecheck) or "")
        self.assertIsNotNone(p.verification_commands.build)
        self.assertIsNotNone(p.verification_commands.lint)
        self.assertEqual(verification_command_cwd(p.verification_commands.build), ".")

    def test_verification_commands_pnpm(self):
        self._write_next_drizzle_fixture(use_pnpm=True)
        p = build_repo_profile(self.root, cache=False)
        self.assertIn("pnpm", p.stack.package_manager)
        self.assertIn("pnpm", verification_command_command(p.verification_commands.typecheck) or "")

    def test_verification_manager_uses_profile_commands(self):
        self._write_next_drizzle_fixture()
        p = build_repo_profile(self.root, cache=False)
        m = VerificationManager(
            str(self.root),
            repo_verification_commands={
                "typecheck": p.verification_commands.typecheck,
                "build": p.verification_commands.build,
                "lint": p.verification_commands.lint,
                "tests": p.verification_commands.tests,
            },
        )
        checks = {c["name"]: c for c in m.get_applicable_checks(files_changed=[{"file": "app/api/issues/route.ts"}])}
        self.assertIn("TypeCheck", checks)
        self.assertEqual(
            checks["TypeCheck"]["command"],
            verification_command_command(p.verification_commands.typecheck),
        )

    def test_repo_profile_to_dict_session_shape(self):
        self._write_next_drizzle_fixture()
        p = build_repo_profile(self.root, cache=False)
        rp = RepoProfile(
            root=self.root,
            stack="nextjs",
            layers_detected=list(p.layers_detected),
            has_package_json=True,
            important_folders=list(p.important_folders),
            v2=p,
        )
        d = repo_profile_to_dict(rp)
        self.assertEqual(d["stack"], "nextjs")
        self.assertIn("profile_v2", d)
        self.assertIn("verification_commands", d)

    def test_summary_and_prompt_block(self):
        self._write_next_drizzle_fixture()
        p = build_repo_profile(self.root, cache=False)
        s = repo_profile_to_summary_text(p)
        self.assertIn("nextjs", s.lower())
        b = repo_profile_to_prompt_block(p)
        self.assertIn("REPO PROFILE v2", b)
        self.assertIn("verification_commands", b)

    def test_cache_reuse(self):
        self._write_next_drizzle_fixture()
        ghost = self.root / ".ghost" / "cache"
        if ghost.exists():
            shutil.rmtree(ghost)
        a = build_repo_profile(self.root, cache=True, force_refresh=False)
        self.assertFalse(a.cache.used)
        b = build_repo_profile(self.root, cache=True, force_refresh=False)
        self.assertTrue(b.cache.used)
        self.assertEqual(b.cache.cache_key, a.cache.cache_key)

    def test_force_refresh_bypasses_cache(self):
        self._write_next_drizzle_fixture()
        build_repo_profile(self.root, cache=True)
        c = build_repo_profile(self.root, cache=True, force_refresh=True)
        self.assertFalse(c.cache.used)


class TestRepoProfileV2AgentsMd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_agents_md_commands_take_priority(self):
        pkg = {"name": "x", "scripts": {"typecheck": "tsc --noEmit", "build": "webpack"}}
        (self.root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        (self.root / "tsconfig.json").write_text("{}", encoding="utf-8")
        (self.root / "AGENTS.md").write_text(
            "## Verify\n"
            "typecheck: `npm run custom-tc`\n"
            "build: `npm run custom-build`\n",
            encoding="utf-8",
        )
        p = build_repo_profile(self.root, cache=False)
        self.assertIn("agents_md", p.verification_commands.source)
        self.assertIn("custom-tc", verification_command_command(p.verification_commands.typecheck) or "")
        self.assertIn("custom-build", verification_command_command(p.verification_commands.build) or "")


class TestRepoProfileV2PlainNode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_node_only_package_json(self):
        pkg = {"name": "plain", "scripts": {"build": "echo ok", "lint": "echo lint"}}
        (self.root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        p = build_repo_profile(self.root, cache=False)
        self.assertEqual(p.stack.runtime, "node")
        self.assertIn("none", p.stack.framework)
        self.assertIsNotNone(p.verification_commands.build)


class TestRepoProfileV2Python(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_python_pytest_signals(self):
        (self.root / "requirements.txt").write_text("pytest>=7\n", encoding="utf-8")
        (self.root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text("def test_a(): assert 1\n", encoding="utf-8")
        p = build_repo_profile(self.root, cache=False)
        self.assertEqual(p.stack.runtime, "python")
        self.assertIn("python", p.stack.language)
        self.assertIn("pytest", p.stack.test_runner)


class TestRepoProfileV2PolyglotMonorepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detects_python_plus_nested_next_workspace(self):
        (self.root / "pyproject.toml").write_text(
            "[project]\n"
            "name='ghostllm'\n"
            "dependencies=['fastapi>=0.115.0','typer>=0.12.0','sqlmodel>=0.0.22','pytest>=8.0.0']\n"
            "\n[tool.uv]\nmanaged=true\n",
            encoding="utf-8",
        )
        (self.root / "apps" / "server").mkdir(parents=True)
        (self.root / "apps" / "server" / "main.py").write_text("from fastapi import FastAPI\n", encoding="utf-8")
        (self.root / "apps" / "server" / "database.py").write_text("from sqlmodel import SQLModel\n", encoding="utf-8")
        (self.root / "apps" / "cli").mkdir(parents=True)
        (self.root / "apps" / "cli" / "main.py").write_text("import typer\n", encoding="utf-8")
        (self.root / "apps" / "web" / "src" / "app").mkdir(parents=True)
        (self.root / "apps" / "web" / "package.json").write_text(
            json.dumps(
                {
                    "name": "web",
                    "scripts": {"build": "next build", "lint": "next lint"},
                    "dependencies": {"next": "14.2.35", "react": "^18"},
                    "devDependencies": {"typescript": "^5", "tailwindcss": "^3.4.1"},
                }
            ),
            encoding="utf-8",
        )
        (self.root / "apps" / "web" / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
        (self.root / "apps" / "web" / "next.config.mjs").write_text("export default {};\n", encoding="utf-8")
        (self.root / "apps" / "web" / "tsconfig.json").write_text("{}", encoding="utf-8")
        (self.root / "apps" / "web" / "src" / "app" / "page.tsx").write_text("export default function Page(){return null}\n", encoding="utf-8")

        p = build_repo_profile(self.root, cache=False, force_refresh=True)
        self.assertEqual(p.version, PROFILE_VERSION)
        self.assertEqual(p.stack.runtime, "polyglot")
        self.assertIn("python", p.stack.language)
        self.assertIn("typescript", p.stack.language)
        self.assertIn("fastapi", p.stack.framework)
        self.assertIn("nextjs", p.stack.framework)
        self.assertIn("uv", p.stack.package_manager)
        self.assertIn("npm", p.stack.package_manager)
        self.assertIn("api", p.layers_detected)
        self.assertIn("ui", p.layers_detected)
        self.assertIn("apps/web/src/app", p.entrypoints.ui_roots)
        self.assertIn("apps/server", p.entrypoints.api_roots)
        self.assertEqual(verification_command_cwd(p.verification_commands.build), "apps/web")


class TestScanRepoProfileCompat(unittest.TestCase):
    def test_scan_returns_v2_attached(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"name":"x"}', encoding="utf-8")
            rp = scan_repo_profile(root, cache=False)
            self.assertIsNotNone(rp.v2)
            self.assertIsInstance(rp.v2, RepoProfileV2)


if __name__ == "__main__":
    unittest.main()
