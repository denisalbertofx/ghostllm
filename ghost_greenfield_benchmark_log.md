# Ghost Greenfield Benchmark Log

Workspace under test: `C:\Users\denis\OneDrive\Escritorio\ghost-bench-cli`

Goal:
- Build a complete Python task manager CLI with SQLite from plain-language prompts through Ghost CLI.
- Exercise `do`, `plan`, `fix`, and related runtime behavior on a real greenfield repo.

Format:
- `Problem`: what blocked the benchmark in the real Ghost run.
- `Fix`: what was changed in Ghost.
- `Result`: what changed in the next benchmark attempt.

## 2026-03-27

### 1. Fresh git repo was blocked as an active project
- Problem: a repo with only `.git` was treated as a protected active project, so in-place scaffold could not start.
- Fix: relaxed `WorkingDirectoryGuard` to allow fresh repo shells and repo shells containing only Ghost metadata.
- Result: Ghost could start a greenfield scaffold inside `ghost-bench-cli`.

### 2. Read-only evidence shadowed `ls .` as `ls apps/cli`
- Problem: the exploration cache aligned root listings with unrelated paths like `apps/cli`, pushing the model toward nonexistent GhostLLM files.
- Fix: tightened path alignment in read-only evidence so `.` no longer matches arbitrary nested paths.
- Result: Ghost stopped inventing `apps/cli/main.py` from a blank repo listing.

### 3. Greenfield prompt was misclassified as Ghost CLI maintenance
- Problem: prompts like "crea desde cero una CLI..." inferred Ghost internal targets such as `apps/cli/main.py`.
- Fix: greenfield prompts now classify as `scaffold`, skip CLI maintenance target inference, and carry an explicit "create files from repo root" reasoning line.
- Result: Ghost stopped assuming the GhostLLM repo structure for a user app.

### 4. Ghost explored `.ghost` metadata instead of building the app
- Problem: on an empty repo Ghost read `.ghost`, `ghost_memory.db`, and continuity artifacts before writing the first project files.
- Fix: bootstrap greenfield tasks now start in `ACT`, hide internal Ghost metadata from root listings, and block metadata reads with guided errors.
- Result: the runtime moved toward project creation instead of introspecting its own state.

### 5. Greenfield scaffold jumped to VERIFY after the first file
- Problem: once any diff existed, `ACT` promoted immediately to `VERIFY`, even when only one file had been created.
- Fix: greenfield scaffold now defers verify until a minimal structure is present. If the prompt requests tests or README, Ghost must write those before verifying.
- Result: one-file scaffolds no longer count as "ready to verify".

### 6. Python greenfield scaffold had no real verification contract
- Problem: the TaskSpec for the benchmark prompt left `verification_policy.tests=False`, so verify fell back to `manual_only` and executed nothing.
- Fix: Python greenfield prompts that ask for pytest/tests now get `verification_policy.tests=True`.
- Result: once tests exist, Ghost is expected to run real verification instead of immediately skipping it.

### 7. Partial greenfield repo became blocked again after README + source file
- Problem: after Ghost created `README.md` and `task_manager.py`, the working directory guard reclassified the repo as an active project and blocked continued scaffolding.
- Fix: added a `bootstrap_partial` context for small greenfield repos with weak markers and a small number of source files, allowing scaffold continuation in place.
- Result: Ghost can keep building the same demo repo instead of forcing a fresh directory after partial progress.

### 8. Scaffold continuation prompt fell back to refactor + explore
- Problem: prompts like `continua este proyecto y terminalo...` were classified as `refactor`, so Ghost went back into `EXPLORE` and churned on `ls/tests` instead of continuing the scaffold in `ACT`.
- Fix: continuation prompts for partial greenfield projects now classify as `scaffold extend`; partial scaffold repos can start directly in `ACT`, and TaskSpec continuation prompts preserve `tests=True` when pytest is requested.
- Result: Ghost can resume the same greenfield project as a scaffold continuation instead of treating it like a generic refactor.

### 9. First complete scaffold reached real verify and failed on missing pytest
- Problem: Ghost successfully created `pyproject.toml`, updated `task_manager.py`, wrote `README.md`, and created `tests/test_task_manager.py`, but verify failed because the environment did not have `pytest` available.
- Fix: no runtime patch yet at this point; this exposed the next product gap to iterate on: better dependency scaffolding / repair after verify failure.
- Result: the benchmark crossed from lifecycle issues into real project-quality issues, which is progress. The next loop is to make Ghost repair the failed verification correctly.

### 10. Top-level `ghost fix` behaved like read-only analysis
- Problem: `ghost fix` entered with `mode="Fix"` but the TaskSpec contract stayed read-only, so the command audited the repo instead of repairing it.
- Fix: the write-contract override now applies to all write modes (`Execute`, `Patch`, `Fix`), not only slash commands.
- Result: `ghost fix` started running real shell checks and behaving like a repair loop.

### 11. Python bugfix prompts on repos with `tests/` still produced invalid TaskSpec
- Problem: bugfix prompts on the demo repo fell back to legacy because the TaskSpec validator required at least one verification check, but Python repos with `tests/` still inferred `tests=False`.
- Fix: Python repo verification hints now infer `tests=True` from `tests/` and `pyproject.toml` evidence, and continuation prompts preserve scaffold semantics.
- Result: bugfix prompts for the benchmark repo now stay on the TaskSpec path and keep `must_write + tests=True`.

### 12. End-to-end benchmark reached a working software artifact
- Problem: after the core fixes, the remaining failures were now in the generated app itself: missing `pytest` metadata for `uv`, bad PEP 621 author syntax, SQLite datetime deprecation, and a Windows Unicode output crash.
- Fix: iterated through Ghost with natural-language prompts until the project passed `uv run python -m pytest` and a real CLI smoke (`add`, `list`, `complete`, `list`, `delete`, `list`) on Windows.
- Result: `ghost-bench-cli` now works end-to-end as a real demo app generated and repaired through Ghost CLI.

### 13. Remaining gap after app completion: `ghost plan` is still unstable on the demo repo
- Problem: a planning/audit run on the completed demo repo surfaced two residual product issues: planner model resolution drifted to `openai/gpt-oss-120b`, and the run aborted with provider/read-only tool issues instead of returning a clean plan.
- Fix: not solved in this benchmark cycle.
- Result: the software-creation benchmark succeeded, but `plan` remains the next product-grade fix to make the full CLI story feel complete.

## 2026-03-28

### 14. `safe` / architect profile still routed planner-strength roles to a non-coder model
- Problem: even after moving the coding stack to Qwen3 Coder, the `strong` tier inside operational profiles still mapped to `smart`, so planning and repair paths could drift back to `openai/gpt-oss-120b`.
- Fix: changed the default `strong` tier to `coder` and added regression coverage for `Profile.architect` / `safe`.
- Result: planner, execution, and repair now stay aligned on `qwen/qwen3-coder-480b-a35b-instruct`.

### 15. Greenfield API scaffold counted `tests/__init__.py` as a real test suite
- Problem: in `ghost-bench-api`, Ghost created `README.md`, `pyproject.toml`, `src/main.py`, and `tests/__init__.py`, then promoted to verify even though there were no actual test cases yet.
- Fix: tightened scaffold verify readiness so package markers like `tests/__init__.py` and `conftest.py` do not satisfy the "tests exist" requirement; also added an optional project-config requirement when the prompt explicitly asks for `pyproject` / `uv`.
- Result: greenfield Python/API scaffolds now stay in `ACT` until they contain real test files and the requested config scaffolding.

### 16. Failed pytest checks were treated as non-repairable
- Problem: verification coordinator only marked `Build`, `TypeCheck`, and `Lint` failures as repairable, so a failed `Python Tests (uv)` batch could close the session instead of entering repair.
- Fix: extended the coordinator to classify executed test failures as repairable too.
- Result: Ghost can now enter the repair phase after real test failures instead of immediately aborting the session.

### 17. Top-level `ghost fix` did not accept a natural-language task
- Problem: `ghost fix` only exposed flags and always used the hard-coded task `"Busca errores y corrígelos."`, which broke the benchmark workflow for targeted fixes from plain-language prompts.
- Fix: added an optional positional `task` argument to `ghost fix` and covered it with CLI tests.
- Result: `ghost fix -y "..."` now works as a real natural-language entrypoint, consistent with `ghost do` and `ghost plan`.

### 18. Verification failure summaries for pytest preferred harmless stderr warnings over the real test failure
- Problem: when pytest failed, outcome synthesis often used `stderr` first, so the `next_action` could blame `requires-python` warnings instead of the actual failing assertion that lived in pytest `stdout`.
- Fix: outcome synthesis now prefers `stdout` for test-family checks and added regression coverage around pytest failures with warning-only stderr.
- Result: failed test sessions now point at the actual broken assertion or failing test node instead of a misleading environment warning.

### 19. `already_implemented` could close a fix task even when the prompt explicitly demanded verification
- Problem: a `ghost fix` prompt that said "verifica con pytest dos veces seguidas" could still close as `already_implemented` after read-only exploration, with verify skipped.
- Fix: added a guard so implementation/modification tasks that explicitly request structured verification cannot close as `already_implemented` when zero checks actually ran.
- Result: Ghost no longer treats "verify skipped" as a valid successful close for prompts that explicitly required verification.

### 20. Second benchmark reached a stable FastAPI + SQLite app through repeated natural-language `do`/`fix`
- Problem: the first API scaffold was incomplete, then the first repair made tests flaky on reruns, and later fixes still needed targeted prompts to normalize API responses and repeated verification behavior.
- Fix: iterated with Ghost using plain-language prompts until the app returned created ticket IDs, the test suite became repeatable, and `pyproject.toml` declared `requires-python`.
- Result: `ghost-bench-api` now passes `uv run python -m pytest` twice consecutively, which gives a second end-to-end demo app created and repaired through Ghost CLI.

### 21. Auto-install for missing local Node tools fired but silently succeeded on Windows
- Problem: when verify reported `"jest" no se reconoce...`, Ghost launched an auto-install, but the generated command used PowerShell syntax (`Set-Location ...; npm install`) while the runtime actually executes through `cmd.exe`. The failed install was also treated as success because only `error` was checked, not `exit_code`.
- Fix: switched scoped install commands to `cd /d "...\" && ...`, and made the auto-install bridge require `exit_code == 0` before promoting back to `VERIFY`.
- Result: Ghost can now install missing local CLI tools like `jest` instead of fake-progressing to verify.

### 22. Shell verification could crash on Windows Unicode output
- Problem: `run_shell` and the verification subprocess runner both captured output with `text=True`, so Windows `cp1252` decoding could explode on UTF-8 bytes emitted by Jest/Next output.
- Fix: both shell paths now capture raw bytes and decode with a fallback chain (`utf-8`, locale, `cp1252`, replacement).
- Result: Ghost can read real build/test failures instead of losing the session to a decode crash.

### 23. Recoverable `edit_file` tool calls could abort a repair session
- Problem: if the model emitted `edit_file` without `old_str/new_str`, Ghost returned `Missing required arguments` and the session could terminate even though a simple `read_file` retry would recover it.
- Fix: missing-argument `edit_file` failures now attach a recovery payload, set `_pending_edit_recovery`, and participate in the same guided-retry path already used for `old_str` mismatches.
- Result: malformed patch attempts no longer hard-stop the session; Ghost can self-correct by rereading the file and retrying the patch.

### 24. Repair Specialist misdiagnosed Jest TypeScript failures and over-focused on the test file
- Problem: when Jest failed with `Jest encountered an unexpected token` on `.ts/.tsx`, the primary-failure summary collapsed to `SyntaxError: Missing semicolon`, and the allowlist only exposed the failing test file. The repair loop kept mutating the test instead of configuring Jest.
- Fix: added a Jest/TypeScript transform heuristic to the repair specialist. It now summarizes this class of failure as a missing Jest TS/TSX transform/config issue, and extends the allowlist with `package.json`, `jest.config.js`, `jest.setup.js`, and `tsconfig.json` inside the subproject.
- Result: Ghost started editing Jest configuration instead of chasing bogus semicolon fixes in the test source.

### 25. Missing Jest presets/modules were not treated as auto-installable dependencies
- Problem: after Ghost created `jest.config.js`, verify failed on `Preset ts-jest not found`, but the auto-install bridge only recognized missing shell commands, not missing Node packages referenced from config/runtime output.
- Fix: extended dependency inference to parse errors like `Preset ts-jest not found`, `Cannot find module 'ts-jest'`, and missing Jest environments; also raised the per-session auto-install cap to allow multiple distinct package installs.
- Result: Ghost can now automatically install packages like `ts-jest` during the same repair flow.

### 26. Structured repairs applied near the iteration cap could die before re-verification
- Problem: Ghost could apply the correct last-mile repair, set the `REPAIR -> ACT -> VERIFY` bridge, and still terminate because the loop had no remaining iterations to actually run the final verify.
- Fix: when a structured repair applies a patch at the edge of the current cap, Ghost now grants tailroom specifically for the verify bridge.
- Result: the web benchmark was able to continue past `ts-jest` installation, fix the final `window is not defined` issue in the test, and close as `implemented` with `TypeCheck`, `Build`, and `Tests` all passing in `ghost-bench-web/notes-app`.
