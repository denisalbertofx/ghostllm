import os
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock

from apps.cli.assistant import (
    CodexAssistant,
    _build_failed_manual_verify_check,
    _build_local_package_install_command,
    _collect_changed_local_package_install_files,
    _decode_subprocess_output,
    _edit_file_error_is_recoverable,
    _effective_tool_task_type,
    _extract_missing_command_token,
    _extract_missing_node_package_token,
    _extract_shell_command_cwd,
    _extract_local_package_install_packages,
    _filter_workspace_listing_entries,
    _greenfield_scaffold_verify_readiness,
    _infer_missing_local_node_package_from_failed_checks,
    _intent_implies_greenfield_write,
    _is_internal_workspace_metadata_path,
    _is_local_package_install_command,
    _local_package_install_already_satisfied,
    _local_package_manifest_relpaths,
    _manual_shell_check_kind_and_name,
    _parse_tool_arguments_payload,
    _repair_apply_tool_for_edit,
    _repair_focus_paths_from_failed_checks,
    _read_utf8_text_for_tool,
    _suggest_edit_file_required_arg_recovery,
    _snapshot_local_package_install_state,
    _should_allow_followup_repair_attempt,
    _should_grant_structured_repair_tailroom,
    _should_bridge_manual_verify_failure_to_repair,
    _should_retry_must_write_no_diff,
    _should_skip_manual_only_verify,
    _should_start_greenfield_in_act,
    _task_looks_dependency_only_change,
    _task_explicitly_requires_structured_verify,
    _verification_input_files,
    _verification_has_executed_checks,
)
from apps.cli.runtime.task_contract import route_intake_intent


class TestAssistantGreenfieldHelpers(unittest.TestCase):
    def test_filter_workspace_listing_entries_hides_internal_metadata_at_root(self) -> None:
        items = [".ghost", ".git", "ghost_memory.db", "README.md", "src", ".gitignore"]
        filtered = _filter_workspace_listing_entries(".", items)
        self.assertEqual(filtered, ["README.md", "src", ".gitignore"])

    def test_internal_workspace_metadata_path_detection(self) -> None:
        self.assertTrue(_is_internal_workspace_metadata_path(".ghost/plans/foo.md"))
        self.assertTrue(_is_internal_workspace_metadata_path("ghost_memory.db"))
        self.assertFalse(_is_internal_workspace_metadata_path(".gitignore"))
        self.assertFalse(_is_internal_workspace_metadata_path("src/main.py"))

    def test_should_start_greenfield_in_act_for_repo_shell_bootstrap(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            intent = route_intake_intent("/do crea desde cero una CLI de tareas en Python con SQLite")
            self.assertTrue(_should_start_greenfield_in_act(tmp, intent))

    def test_should_start_greenfield_in_act_for_partial_bootstrap_continuation(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as fh:
                fh.write("# demo\n")
            with open(os.path.join(tmp, "task_manager.py"), "w", encoding="utf-8") as fh:
                fh.write("print('hi')\n")
            intent = SimpleNamespace(task_type="scaffold", scaffold_type="extend", task="continua este proyecto")
            self.assertTrue(_should_start_greenfield_in_act(tmp, intent))

    def test_should_start_greenfield_in_act_for_seeded_backend_build(self) -> None:
        with TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, ".git"))
            with open(os.path.join(tmp, "pyproject.toml"), "w", encoding="utf-8") as fh:
                fh.write("[project]\nname='demo'\n")
            with open(os.path.join(tmp, "main.py"), "w", encoding="utf-8") as fh:
                fh.write("def app():\n    return None\n")
            intent = route_intake_intent(
                "/do crea el backend de una app de notas con FastAPI, SQLite, autenticacion y tests"
            )
            self.assertTrue(
                _should_start_greenfield_in_act(
                    tmp,
                    intent,
                    {"change_expectation": "must_write"},
                )
            )

    def test_intent_implies_greenfield_write_for_execute_must_write(self) -> None:
        intent = route_intake_intent(
            "/do crea el backend de una app de notas con FastAPI, SQLite, autenticacion y tests"
        )
        self.assertTrue(
            _intent_implies_greenfield_write(intent, {"change_expectation": "must_write"})
        )

    def test_read_utf8_text_for_tool_returns_error_for_binary_file(self) -> None:
        with TemporaryDirectory() as tmp:
            fp = os.path.join(tmp, "ghost_memory.db")
            with open(fp, "wb") as fh:
                fh.write(b"\x00\x8a\xff\x10sqlite")
            content, err = _read_utf8_text_for_tool(fp)
            self.assertIsNone(content)
            self.assertIn("not UTF-8 text", err or "")

    def test_decode_subprocess_output_handles_utf8_bytes(self) -> None:
        self.assertEqual(_decode_subprocess_output("ok \u2713".encode("utf-8")), "ok \u2713")

    def test_decode_subprocess_output_replaces_undecodable_bytes(self) -> None:
        decoded = _decode_subprocess_output(b"\x8fabc")
        self.assertIsInstance(decoded, str)
        self.assertIn("abc", decoded)

    def test_edit_file_error_is_recoverable_detects_missing_required_arguments(self) -> None:
        self.assertTrue(_edit_file_error_is_recoverable("Missing required arguments."))

    def test_suggest_edit_file_required_arg_recovery_reads_from_start(self) -> None:
        recovery = _suggest_edit_file_required_arg_recovery("notes-app/__tests__/notes.test.tsx", "a\nb\nc\n")
        self.assertEqual(
            recovery,
            {
                "path": "notes-app/__tests__/notes.test.tsx",
                "start_line": 1,
                "max_lines": 3,
            },
        )

    def test_effective_tool_task_type_promotes_fix_mode(self) -> None:
        self.assertEqual(_effective_tool_task_type("Fix", "ask"), "fix")
        self.assertEqual(_effective_tool_task_type("Fix", "direct_edit"), "fix")

    def test_effective_tool_task_type_promotes_execute_bootstrap_to_scaffold(self) -> None:
        self.assertEqual(
            _effective_tool_task_type("Execute", "ask", bootstrap_scaffold=True),
            "scaffold",
        )

    def test_local_package_install_command_detection(self) -> None:
        self.assertTrue(_is_local_package_install_command("cd notes-app && npm install"))
        self.assertTrue(_is_local_package_install_command("pnpm install"))
        self.assertFalse(_is_local_package_install_command("npm install -g create-next-app"))

    def test_extract_local_package_install_packages(self) -> None:
        self.assertEqual(
            _extract_local_package_install_packages('Set-Location "notes-app"; npm install typescript --save-dev'),
            ["typescript"],
        )
        self.assertEqual(
            _extract_local_package_install_packages("npm install @types/node react"),
            ["@types/node", "react"],
        )

    def test_local_package_manifest_relpaths_scope_to_project_dir(self) -> None:
        self.assertEqual(
            _local_package_manifest_relpaths("notes-app"),
            [
                "notes-app/package.json",
                "notes-app/package-lock.json",
                "notes-app/pnpm-lock.yaml",
                "notes-app/yarn.lock",
            ],
        )

    def test_snapshot_and_collect_changed_local_package_install_files(self) -> None:
        with TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "notes-app"), exist_ok=True)
            package_json = os.path.join(tmp, "notes-app", "package.json")
            with open(package_json, "w", encoding="utf-8") as fh:
                fh.write('{"name":"notes-app","dependencies":{}}\n')
            before = _snapshot_local_package_install_state(tmp, "notes-app")
            with open(package_json, "w", encoding="utf-8") as fh:
                fh.write('{"name":"notes-app","dependencies":{"better-sqlite3":"latest"}}\n')
            lock_path = os.path.join(tmp, "notes-app", "package-lock.json")
            with open(lock_path, "w", encoding="utf-8") as fh:
                fh.write('{"lockfileVersion":3}\n')
            changed = _collect_changed_local_package_install_files(tmp, "notes-app", before)
            self.assertIn("notes-app/package.json", changed)
            self.assertIn("notes-app/package-lock.json", changed)

    def test_local_package_install_already_satisfied_detects_declared_and_installed_dependency(self) -> None:
        with TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "notes-app", "node_modules", "typescript"), exist_ok=True)
            package_json = os.path.join(tmp, "notes-app", "package.json")
            os.makedirs(os.path.dirname(package_json), exist_ok=True)
            with open(package_json, "w", encoding="utf-8") as fh:
                fh.write('{"name":"notes-app","devDependencies":{"typescript":"latest"}}\n')
            self.assertTrue(
                _local_package_install_already_satisfied(
                    tmp,
                    'Set-Location "notes-app"; npm install typescript --save-dev',
                    project_cwd="notes-app",
                )
            )

    def test_local_package_install_already_satisfied_ignores_missing_node_modules_package(self) -> None:
        with TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "notes-app"), exist_ok=True)
            package_json = os.path.join(tmp, "notes-app", "package.json")
            with open(package_json, "w", encoding="utf-8") as fh:
                fh.write('{"name":"notes-app","devDependencies":{"typescript":"latest"}}\n')
            self.assertFalse(
                _local_package_install_already_satisfied(
                    tmp,
                    "npm install typescript --save-dev",
                    project_cwd="notes-app",
                )
            )

    def test_repair_apply_tool_for_edit_uses_write_file_for_missing_target(self) -> None:
        with TemporaryDirectory() as tmp:
            tool, args = _repair_apply_tool_for_edit(
                tmp,
                {"path": "notes-app/types/better-sqlite3.d.ts", "old_str": "", "new_str": "declare module 'better-sqlite3';\n"},
            )
            self.assertEqual(tool, "write_file")
            self.assertEqual(args["path"], "notes-app/types/better-sqlite3.d.ts")
            self.assertIn("declare module", args["content"])

    def test_repair_apply_tool_for_edit_keeps_edit_file_for_existing_target(self) -> None:
        with TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "notes-app", "lib"), exist_ok=True)
            db_path = os.path.join(tmp, "notes-app", "lib", "db.ts")
            with open(db_path, "w", encoding="utf-8") as fh:
                fh.write("import Database from 'better-sqlite3';\n")
            tool, args = _repair_apply_tool_for_edit(
                tmp,
                {"path": "notes-app/lib/db.ts", "old_str": "import Database from 'better-sqlite3';", "new_str": "import Database from 'better-sqlite3';"},
            )
            self.assertEqual(tool, "edit_file")
            self.assertEqual(args["path"], "notes-app/lib/db.ts")
            self.assertIn("old_str", args)

    def test_inner_execute_tool_edit_file_missing_args_returns_recovery(self) -> None:
        assistant = CodexAssistant.__new__(CodexAssistant)
        with TemporaryDirectory() as tmp:
            assistant.cwd = tmp
            assistant._pending_edit_recovery = None
            rel_path = "notes-app/__tests__/notes.test.tsx"
            abs_path = os.path.join(tmp, "notes-app", "__tests__", "notes.test.tsx")
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as fh:
                fh.write("line1\nline2\n")
            result = assistant._inner_execute_tool("edit_file", {"path": rel_path}, {}, is_authorized=True)
            self.assertEqual(result["error"], "Missing required arguments.")
            self.assertEqual(result["recovery"]["next_tool"], "read_file")
            self.assertEqual(result["recovery"]["arguments"]["path"], rel_path)
            self.assertEqual(
                assistant._pending_edit_recovery["arguments"]["path"],
                rel_path,
            )

    def test_dependency_only_task_detection(self) -> None:
        self.assertTrue(_task_looks_dependency_only_change("instala better-sqlite3 y termina"))
        self.assertFalse(
            _task_looks_dependency_only_change(
                "instala better-sqlite3 y convierte la app en una API CRUD con SQLite"
            )
        )

    def test_repair_focus_paths_prioritize_failed_check_paths(self) -> None:
        failed_checks = [
            {
                "status": "failed",
                "name": "TypeCheck",
                "stdout": (
                    "tsconfig.json(3,15): error TS5107: Option 'target=ES5' is deprecated\n"
                    "pages/api/notes.ts(2,22): error TS2307: Cannot find module '../../types'\n"
                ),
            }
        ]
        focus = _repair_focus_paths_from_failed_checks(failed_checks)
        self.assertIn("tsconfig.json", focus)
        self.assertIn("pages/api/notes.ts", focus)

    def test_parse_tool_arguments_payload_sanitizes_trailing_markup_in_path(self) -> None:
        args = _parse_tool_arguments_payload(
            '{"path":"notes-app/types.ts\\n</definition>","start_line":1,"max_lines":40}'
        )
        self.assertEqual(args["path"], "notes-app/types.ts")

    def test_retry_must_write_no_diff_forces_one_more_tool_turn(self) -> None:
        self.assertTrue(
            _should_retry_must_write_no_diff(
                change_expectation="must_write",
                has_diff=False,
                already_implemented=False,
                iterations=4,
                max_iter=14,
                budget_exhausted=False,
                nudge_count=0,
                nudge_cap=1,
            )
        )

    def test_retry_must_write_no_diff_stops_when_cap_reached(self) -> None:
        self.assertFalse(
            _should_retry_must_write_no_diff(
                change_expectation="must_write",
                has_diff=False,
                already_implemented=False,
                iterations=4,
                max_iter=14,
                budget_exhausted=False,
                nudge_count=1,
                nudge_cap=1,
            )
        )

    def test_should_grant_structured_repair_tailroom_near_iteration_cap(self) -> None:
        self.assertTrue(_should_grant_structured_repair_tailroom(14, 15))
        self.assertFalse(_should_grant_structured_repair_tailroom(11, 15))

    def test_manual_shell_check_kind_and_name_prefers_typecheck(self) -> None:
        kind, name = _manual_shell_check_kind_and_name("cd notes-app && npx tsc --noEmit")
        self.assertEqual(kind, "typecheck")
        self.assertEqual(name, "TypeCheck")

    def test_extract_shell_command_cwd_supports_cd_and_set_location(self) -> None:
        self.assertEqual(_extract_shell_command_cwd('cd notes-app && npm run build'), "notes-app")
        self.assertEqual(_extract_shell_command_cwd('Set-Location "notes-app"; npm run build'), "notes-app")
        self.assertEqual(_extract_shell_command_cwd("npm run build"), ".")

    def test_build_failed_manual_verify_check_keeps_cwd_and_failure(self) -> None:
        check = _build_failed_manual_verify_check(
            "cd notes-app && npm run build",
            {"stdout": "build failed", "stderr": "tsconfig.json(3,15): error TS5107", "exit_code": 1},
        )
        assert check is not None
        self.assertEqual(check["name"], "Build")
        self.assertEqual(check["kind"], "build")
        self.assertEqual(check["cwd"], "notes-app")
        self.assertEqual(check["status"], "failed")

    def test_bridge_manual_verify_failure_to_repair_requires_must_write(self) -> None:
        failed = [{"name": "Build", "status": "failed"}]
        self.assertTrue(
            _should_bridge_manual_verify_failure_to_repair(
                change_expectation="must_write",
                has_diff=False,
                failed_checks=failed,
                iterations=5,
                max_iter=14,
                budget_exhausted=False,
                bridge_count=0,
                bridge_cap=1,
            )
        )
        self.assertFalse(
            _should_bridge_manual_verify_failure_to_repair(
                change_expectation="should_not_write",
                has_diff=False,
                failed_checks=failed,
                iterations=5,
                max_iter=14,
                budget_exhausted=False,
                bridge_count=0,
                bridge_cap=1,
            )
        )

    def test_filter_diff_summary_for_repair_drops_single_unrelated_diff(self) -> None:
        assistant = CodexAssistant.__new__(CodexAssistant)
        assistant._repair_focus_files = lambda _sess, _failed: [
            "notes-app/tsconfig.json",
            "notes-app/components/NoteEditor.tsx",
        ]
        filtered = assistant._filter_diff_summary_for_repair(
            SimpleNamespace(),
            [{"name": "Build", "status": "failed"}],
            [{"file": "notes-app/pages/index.tsx", "type": "Edit File"}],
        )
        self.assertEqual(filtered, [])

    def test_followup_repair_attempt_allowed_when_focus_files_remain_unedited(self) -> None:
        self.assertTrue(
            _should_allow_followup_repair_attempt(
                repair_count=1,
                max_repair=1,
                focus_files=["notes-app/tsconfig.json", "notes-app/components/NoteEditor.tsx"],
                diff_summary=[{"file": "notes-app/components/NoteEditor.tsx", "type": "Edit File"}],
                bonus_cap=1,
            )
        )
        self.assertFalse(
            _should_allow_followup_repair_attempt(
                repair_count=1,
                max_repair=1,
                focus_files=["notes-app/components/NoteEditor.tsx"],
                diff_summary=[{"file": "notes-app/components/NoteEditor.tsx", "type": "Edit File"}],
                bonus_cap=1,
            )
        )

    def test_manual_only_verify_skip_is_disabled_when_runtime_can_resolve_checks(self) -> None:
        self.assertFalse(
            _should_skip_manual_only_verify(
                has_run_shell=False,
                contract_spec={"verification_policy": {"required": True}},
                planned_check_labels=["Build @ notes-app: npm run build"],
            )
        )

    def test_manual_only_verify_skip_stays_when_no_shell_and_no_checks(self) -> None:
        self.assertTrue(
            _should_skip_manual_only_verify(
                has_run_shell=False,
                contract_spec={"verification_policy": {"required": False}},
                planned_check_labels=[],
            )
        )

    def test_greenfield_verify_readiness_requires_requested_tests_and_readme(self) -> None:
        ready, reasons = _greenfield_scaffold_verify_readiness(
            [{"file": "task_manager.py", "type": "Write File"}],
            "crea desde cero una CLI en Python con pytest y README",
            {"verification_policy": {"required": True, "tests": True}},
        )
        self.assertFalse(ready)
        self.assertIn("missing_tests", reasons)
        self.assertIn("missing_readme", reasons)
        self.assertIn("single_file_only", reasons)

    def test_greenfield_verify_readiness_does_not_count_tests_init_as_test_suite(self) -> None:
        ready, reasons = _greenfield_scaffold_verify_readiness(
            [
                {"file": "pyproject.toml", "type": "Write File"},
                {"file": "src/main.py", "type": "Write File"},
                {"file": "tests/__init__.py", "type": "Write File"},
            ],
            "crea desde cero una API FastAPI con pytest y pyproject compatible con uv",
            {"verification_policy": {"required": True, "tests": True}},
        )
        self.assertFalse(ready)
        self.assertIn("missing_tests", reasons)

    def test_greenfield_verify_readiness_requires_requested_project_config(self) -> None:
        ready, reasons = _greenfield_scaffold_verify_readiness(
            [
                {"file": "src/main.py", "type": "Write File"},
                {"file": "tests/test_main.py", "type": "Write File"},
            ],
            "crea desde cero una API FastAPI con pytest y pyproject compatible con uv",
            {"verification_policy": {"required": True, "tests": True}},
        )
        self.assertFalse(ready)
        self.assertIn("missing_project_config", reasons)

    def test_greenfield_web_verify_readiness_requires_app_entry(self) -> None:
        ready, reasons = _greenfield_scaffold_verify_readiness(
            [
                {"file": "package.json", "type": "Write File"},
                {"file": "README.md", "type": "Write File"},
            ],
            "crea desde cero una app web de notas con Next.js y TypeScript. README claro y build verificable.",
            {"verification_policy": {"required": True, "build": True, "typecheck": True}},
        )
        self.assertFalse(ready)
        self.assertIn("missing_source", reasons)
        self.assertIn("missing_app_entry", reasons)

    def test_greenfield_verify_readiness_passes_when_structure_is_present(self) -> None:
        ready, reasons = _greenfield_scaffold_verify_readiness(
            [
                {"file": "pyproject.toml", "type": "Write File"},
                {"file": "task_manager.py", "type": "Write File"},
                {"file": "README.md", "type": "Write File"},
                {"file": "tests/test_task_manager.py", "type": "Write File"},
            ],
            "crea desde cero una CLI en Python con pytest, README y pyproject compatible con uv",
            {"verification_policy": {"required": True, "tests": True}},
        )
        self.assertTrue(ready)
        self.assertEqual(reasons, [])

    def test_greenfield_web_verify_readiness_passes_with_app_entry(self) -> None:
        ready, reasons = _greenfield_scaffold_verify_readiness(
            [
                {"file": "package.json", "type": "Write File"},
                {"file": "src/app/page.tsx", "type": "Write File"},
                {"file": "README.md", "type": "Write File"},
            ],
            "crea desde cero una app web de notas con Next.js y TypeScript. README claro y build verificable.",
            {"verification_policy": {"required": True, "build": True, "typecheck": True}},
        )
        self.assertTrue(ready)
        self.assertEqual(reasons, [])

    def test_task_explicitly_requires_structured_verify(self) -> None:
        self.assertTrue(
            _task_explicitly_requires_structured_verify(
                "completa la app y verifica con npm run build y npx tsc --noEmit",
                {"verification_policy": {"required": True}},
            )
        )
        self.assertFalse(
            _task_explicitly_requires_structured_verify(
                "completa la app",
                {"verification_policy": {"required": True}},
            )
        )

    def test_verification_input_files_uses_active_workset_when_diff_is_empty(self) -> None:
        files = _verification_input_files(
            [],
            {
                "candidate_files": ["notes-app/pages/index.tsx", "notes-app/pages/api/notes.ts"],
                "read_files": ["notes-app/lib/db.ts"],
            },
            {"verification_policy": {"required": True}, "target_files": ["notes-app/pages/index.tsx"]},
            "completa la app y verifica con npm run build, npx tsc --noEmit y los tests",
        )
        self.assertEqual(
            [item["file"] for item in files],
            [
                "notes-app/pages/index.tsx",
                "notes-app/pages/api/notes.ts",
                "notes-app/lib/db.ts",
            ],
        )

    def test_verification_has_executed_checks_detects_step_count(self) -> None:
        self.assertTrue(
            _verification_has_executed_checks(
                {"status": "success", "steps_executed_count": 2, "checks": []}
            )
        )

    def test_verification_has_executed_checks_detects_executed_check_without_step_count(self) -> None:
        self.assertTrue(
            _verification_has_executed_checks(
                {
                    "status": "success",
                    "steps_executed_count": 0,
                    "checks": [
                        {"name": "Build", "status": "passed", "provenance": "executed"}
                    ],
                }
            )
        )
        self.assertFalse(
            _verification_has_executed_checks(
                {
                    "status": "skipped",
                    "steps_executed_count": 0,
                    "checks": [
                        {"name": "Build", "status": "not_run", "provenance": "not_run"}
                    ],
                }
            )
        )

    def test_extract_missing_command_token_handles_windows_not_recognized(self) -> None:
        self.assertEqual(
            _extract_missing_command_token('"jest" no se reconoce como un comando interno o externo'),
            "jest",
        )
        self.assertEqual(
            _extract_missing_command_token("'vitest' is not recognized as an internal or external command"),
            "vitest",
        )

    def test_infer_missing_local_node_package_from_failed_checks_detects_missing_jest(self) -> None:
        with TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "notes-app"), exist_ok=True)
            package_json = os.path.join(tmp, "notes-app", "package.json")
            with open(package_json, "w", encoding="utf-8") as fh:
                fh.write('{"name":"notes-app","devDependencies":{"jest":"^29.0.0"}}\n')
            inferred = _infer_missing_local_node_package_from_failed_checks(
                tmp,
                [
                    {
                        "name": "Tests",
                        "status": "failed",
                        "cwd": "notes-app",
                        "stderr": '"jest" no se reconoce como un comando interno o externo',
                    }
                ],
            )
            self.assertEqual(inferred["package"], "jest")
            self.assertEqual(inferred["cwd"], "notes-app")
            self.assertEqual(inferred["declared"], "1")

    def test_extract_missing_node_package_token_detects_missing_preset(self) -> None:
        self.assertEqual(
            _extract_missing_node_package_token("Preset ts-jest not found."),
            "ts-jest",
        )

    def test_infer_missing_local_node_package_from_failed_checks_detects_missing_ts_jest(self) -> None:
        with TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "notes-app"), exist_ok=True)
            inferred = _infer_missing_local_node_package_from_failed_checks(
                tmp,
                [
                    {
                        "name": "Tests",
                        "status": "failed",
                        "cwd": "notes-app",
                        "stderr": "Preset ts-jest not found.",
                    }
                ],
            )
            self.assertEqual(inferred["package"], "ts-jest")
            self.assertEqual(inferred["cwd"], "notes-app")
            self.assertEqual(inferred["declared"], "")

    def test_build_local_package_install_command_prefers_plain_install_when_declared(self) -> None:
        with TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "notes-app"), exist_ok=True)
            with open(os.path.join(tmp, "notes-app", "package-lock.json"), "w", encoding="utf-8") as fh:
                fh.write("{}\n")
            self.assertEqual(
                _build_local_package_install_command(tmp, "notes-app", "jest", declared=True),
                'cd /d "notes-app" && npm install',
            )
            self.assertEqual(
                _build_local_package_install_command(tmp, "notes-app", "jest", declared=False),
                'cd /d "notes-app" && npm install --save-dev jest',
            )

    def test_auto_install_missing_local_node_package_requires_success_exit_code(self) -> None:
        assistant = CodexAssistant.__new__(CodexAssistant)
        assistant.cwd = "C:\\repo"
        assistant.console = SimpleNamespace(print=MagicMock())
        assistant.artifact_manager = SimpleNamespace(current_session=None)
        assistant._auto_install_missing_node_tool_count = 0
        assistant._inner_execute_tool = MagicMock(
            return_value={"stdout": "", "stderr": '"jest" no se reconoce como un comando interno o externo', "exit_code": 1}
        )
        assistant._record_phase_promotion = MagicMock()
        assistant._apply_session_phase = MagicMock()
        failed_checks = [
            {
                "name": "Tests",
                "status": "failed",
                "cwd": "notes-app",
                "stderr": '"jest" no se reconoce como un comando interno o externo',
            }
        ]

        with TemporaryDirectory() as tmp:
            assistant.cwd = tmp
            os.makedirs(os.path.join(tmp, "notes-app"), exist_ok=True)
            with open(os.path.join(tmp, "notes-app", "package.json"), "w", encoding="utf-8") as fh:
                fh.write('{"name":"notes-app","devDependencies":{"jest":"^29.0.0"}}\n')
            self.assertFalse(assistant._maybe_auto_install_missing_local_node_package(failed_checks))
            install_call = assistant._inner_execute_tool.call_args
            self.assertIsNotNone(install_call)
            self.assertEqual(
                install_call.args[1]["command"],
                'cd /d "notes-app" && npm install',
            )

        assistant._record_phase_promotion.assert_not_called()
        assistant._apply_session_phase.assert_not_called()


if __name__ == "__main__":
    unittest.main()
