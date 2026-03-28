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
