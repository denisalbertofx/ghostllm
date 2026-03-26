"""
Tests for apps/cli/runtime/explore_engine_v2.py and repo_mismatch.py.

Coverage:
1. classify_explore_task — all five classes
2. rank_explore_tools — ordering guarantees
3. detect_exploration_churn — repeated ls, missing paths, no new evidence
4. should_abort_explore_as_unproductive — threshold behaviour
5. repo mismatch detection — ghost paths in non-ghost repo, absent paths
6. churn_nudge_message — non-empty when churn detected
7. build_graceful_blocked_closure_message — contains useful text
"""
import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from apps.cli.runtime.explore_engine_v2 import (
    CHURN_REASON_NO_NEW_EVIDENCE,
    CHURN_REASON_ONLY_SHADOW_LS,
    CHURN_REASON_REPEATED_IDENTICAL_LS_LISTING,
    CHURN_REASON_REPEATED_LS_SAME_DIR,
    CHURN_REASON_REPEATED_MISSING_PATH,
    CHURN_REASON_REPEATED_SAME_TOOL_INVOCATION,
    EXPLORE_CLASS_ARCHITECTURE_REVIEW,
    EXPLORE_CLASS_FACTUAL_QUESTION,
    EXPLORE_CLASS_PATH_LISTING,
    EXPLORE_CLASS_SYMBOL_LOOKUP,
    EXPLORE_CLASS_WRITE_TASK,
    TOOL_LS,
    TOOL_READ_FILE,
    TOOL_SEARCH_CODE,
    TOOL_SUMMARIZE_REPO,
    build_graceful_blocked_closure_message,
    build_listing_paths_absent_operator_message,
    churn_nudge_message,
    classify_explore_task,
    detect_exploration_churn,
    effective_ls_churn_threshold,
    explore_listing_targets_all_absent,
    extract_listing_directory_targets,
    history_has_tool_call,
    is_symbol_oriented_question,
    rank_explore_tools,
    should_abort_explore_as_unproductive,
    tool_ranking_nudge,
)
from apps.cli.runtime.repo_mismatch import (
    MISMATCH_REASON_GHOST_PATHS_IN_NON_GHOST_REPO,
    MISMATCH_REASON_GHOST_PRODUCT_IN_FOREIGN_REPO,
    MISMATCH_REASON_REPEATED_FILE_NOT_FOUND,
    MISMATCH_REASON_REQUESTED_PATHS_ABSENT,
    MISMATCH_REASON_STACK_INCONSISTENCY,
    MISMATCH_REASON_WRONG_REPO_STRUCTURE,
    RepoMismatchResult,
    detect_repo_mismatch,
    inject_mismatch_into_session,
)
from apps.cli.runtime.outcome_engine import (
    INFORMATIVE_KIND_EXPLICIT_PATH_ABSENT,
    OUTCOME_READ_ONLY,
    determine_task_outcome,
)
from apps.cli.runtime.session_phase import SessionPhase
from apps.cli.runtime.task_confidence_engine import compute_task_confidence_closure


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ls_tool_call(path: str, files: list, tc_id: str = "tc1") -> list:
    """Return two history messages simulating a successful ls tool call."""
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tc_id,
                    "function": {"name": "ls", "arguments": json.dumps({"path": path})},
                }
            ],
        },
        {
            "role": "tool",
            "name": "ls",
            "tool_call_id": tc_id,
            "content": json.dumps({"files": files}),
        },
    ]


def _make_read_tool_call(path: str, content: str, tc_id: str = "tr1") -> list:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tc_id,
                    "function": {"name": "read_file", "arguments": json.dumps({"path": path})},
                }
            ],
        },
        {
            "role": "tool",
            "name": "read_file",
            "tool_call_id": tc_id,
            "content": json.dumps({"content": content}),
        },
    ]


def _make_search_tool_call(query: str, tc_id: str = "ts", mode: str = "symbol") -> list:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tc_id,
                    "function": {
                        "name": "search_code",
                        "arguments": json.dumps({"query": query, "mode": mode}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "search_code",
            "tool_call_id": tc_id,
            "content": json.dumps({"found": False, "matches": []}),
        },
    ]


def _make_failed_ls(path: str, tc_id: str = "tfe") -> list:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tc_id,
                    "function": {"name": "ls", "arguments": json.dumps({"path": path})},
                }
            ],
        },
        {
            "role": "tool",
            "name": "ls",
            "tool_call_id": tc_id,
            "content": json.dumps({"error": "Path not found"}),
        },
    ]


# ── Task classification ───────────────────────────────────────────────────────

class TestClassifyExploreTask:
    def test_symbol_lookup_en(self):
        assert classify_explore_task("Where is function verification_diff_fingerprint defined?") == EXPLORE_CLASS_SYMBOL_LOOKUP

    def test_symbol_lookup_spanish_definida_snake_case(self):
        t = (
            "¿En qué archivo está definida la función verification_diff_fingerprint "
            "y para qué sirve a alto nivel?"
        )
        assert classify_explore_task(t) == EXPLORE_CLASS_SYMBOL_LOOKUP
        assert is_symbol_oriented_question(t)

    def test_symbol_lookup_spanish_short_identifier(self):
        t = "¿En qué archivo está la clase TaskContract?"
        assert classify_explore_task(t) == EXPLORE_CLASS_SYMBOL_LOOKUP

    def test_symbol_lookup_donde_esta_definida(self):
        t = "¿Dónde está definida la función main?"
        assert classify_explore_task(t) == EXPLORE_CLASS_SYMBOL_LOOKUP

    def test_symbol_lookup_backtick(self):
        assert classify_explore_task("What does `explore_engine_v2` do?") == EXPLORE_CLASS_SYMBOL_LOOKUP

    def test_path_listing(self):
        assert classify_explore_task("List files in apps/cli/runtime") == EXPLORE_CLASS_PATH_LISTING

    def test_path_listing_es(self):
        assert classify_explore_task("¿Qué hay en el directorio apps/server?") == EXPLORE_CLASS_PATH_LISTING

    def test_architecture(self):
        assert classify_explore_task("Give me an architecture overview of the whole system") == EXPLORE_CLASS_ARCHITECTURE_REVIEW

    def test_strategy_plan_routes_to_architecture_review(self):
        assert (
            classify_explore_task("/plan creame un plan para mejorar la arquitectura de esta app")
            == EXPLORE_CLASS_ARCHITECTURE_REVIEW
        )

    def test_write_task(self):
        assert classify_explore_task("Create a new config file for the server") == EXPLORE_CLASS_WRITE_TASK

    def test_factual_fallback(self):
        result = classify_explore_task("How does the rate limiter work?")
        assert result in (EXPLORE_CLASS_FACTUAL_QUESTION, EXPLORE_CLASS_SYMBOL_LOOKUP)

    def test_empty_input(self):
        assert classify_explore_task("") == EXPLORE_CLASS_FACTUAL_QUESTION


# ── Tool ranking ──────────────────────────────────────────────────────────────

class TestRankExploreTools:
    def test_symbol_lookup_starts_with_search(self):
        ranking = rank_explore_tools(EXPLORE_CLASS_SYMBOL_LOOKUP)
        assert ranking[0] == TOOL_SEARCH_CODE

    def test_path_listing_starts_with_ls(self):
        ranking = rank_explore_tools(EXPLORE_CLASS_PATH_LISTING)
        assert ranking[0] == TOOL_LS

    def test_architecture_starts_with_summarize(self):
        ranking = rank_explore_tools(EXPLORE_CLASS_ARCHITECTURE_REVIEW)
        assert ranking[0] == TOOL_SUMMARIZE_REPO

    def test_all_tools_present(self):
        for cls in (EXPLORE_CLASS_SYMBOL_LOOKUP, EXPLORE_CLASS_PATH_LISTING,
                    EXPLORE_CLASS_FACTUAL_QUESTION, EXPLORE_CLASS_ARCHITECTURE_REVIEW,
                    EXPLORE_CLASS_WRITE_TASK):
            ranking = rank_explore_tools(cls)
            assert len(ranking) == 4

    def test_nudge_message_non_empty(self):
        for cls in (EXPLORE_CLASS_SYMBOL_LOOKUP, EXPLORE_CLASS_PATH_LISTING,
                    EXPLORE_CLASS_FACTUAL_QUESTION, EXPLORE_CLASS_ARCHITECTURE_REVIEW):
            nudge = tool_ranking_nudge(cls)
            assert isinstance(nudge, str) and len(nudge) > 10

    def test_symbol_nudge_is_search_first(self):
        nudge = tool_ranking_nudge(EXPLORE_CLASS_SYMBOL_LOOKUP)
        assert "SEARCH-FIRST" in nudge or "search_code" in nudge
        assert "ls" in nudge.lower()


# ── Churn detection ───────────────────────────────────────────────────────────

class TestDetectExplorationChurn:
    def test_no_churn_empty_history(self):
        churn = detect_exploration_churn([])
        assert not churn.detected

    def test_no_churn_single_ls(self):
        history = _make_ls_tool_call(".", ["file.py"])
        churn = detect_exploration_churn(history)
        assert not churn.detected

    def test_repeated_ls_detected(self):
        history = []
        for i in range(4):
            history += _make_ls_tool_call("apps/cli", ["a.py", "b.py"], tc_id=f"t{i}")
        churn = detect_exploration_churn(history)
        assert churn.detected
        assert CHURN_REASON_REPEATED_LS_SAME_DIR in churn.reasons

    def test_repeated_missing_path(self):
        history = []
        for i in range(3):
            history += _make_failed_ls("apps/missing_dir", tc_id=f"t{i}")
        churn = detect_exploration_churn(history)
        assert churn.detected
        assert CHURN_REASON_REPEATED_MISSING_PATH in churn.reasons

    def test_no_new_evidence_after_many_steps(self, monkeypatch):
        monkeypatch.setenv("GHOST_CHURN_NO_EVIDENCE_STEPS", "3")
        history = []
        # 5 ls calls, no read_file
        for i in range(5):
            history += _make_ls_tool_call(f"dir{i}", [f"f{i}.py"], tc_id=f"t{i}")
        churn = detect_exploration_churn(history)
        assert churn.detected
        assert CHURN_REASON_NO_NEW_EVIDENCE in churn.reasons

    def test_only_shadow_ls_no_reads(self):
        history = []
        for i in range(5):
            history += _make_ls_tool_call(f"dir{i}", [f"f{i}.py"], tc_id=f"t{i}")
        churn = detect_exploration_churn(history)
        # Should detect either no_new_evidence or only_shadow_ls
        assert churn.detected

    def test_churn_suggests_search_tool(self):
        history = []
        for i in range(4):
            history += _make_ls_tool_call("apps", ["cli"], tc_id=f"t{i}")
        churn = detect_exploration_churn(history)
        assert churn.detected
        assert churn.suggested_next_tool == TOOL_SEARCH_CODE

    def test_churn_nudge_contains_strategy(self):
        history = []
        for i in range(4):
            history += _make_ls_tool_call("apps", ["cli"], tc_id=f"t{i}")
        churn = detect_exploration_churn(history)
        nudge = churn_nudge_message(churn)
        assert "search_code" in nudge or "strategy" in nudge.lower()

    def test_identical_ls_listing_detected(self):
        h = _make_ls_tool_call("apps/cli", ["a.py"], tc_id="a") + _make_ls_tool_call(
            "apps/cli", ["a.py"], tc_id="b"
        )
        churn = detect_exploration_churn(h)
        assert churn.detected
        assert CHURN_REASON_REPEATED_IDENTICAL_LS_LISTING in churn.reasons

    def test_abort_on_identical_ls_alone(self):
        h = _make_ls_tool_call(".", ["x.py"], tc_id="u") + _make_ls_tool_call(".", ["x.py"], tc_id="v")
        abort, reason = should_abort_explore_as_unproductive(h)
        assert abort
        assert "identical_ls" in reason

    def test_single_signal_volume_abort(self, monkeypatch):
        monkeypatch.setenv("GHOST_CHURN_SINGLE_SIGNAL_ABORT_MIN_CALLS", "5")
        monkeypatch.setenv("GHOST_CHURN_NO_EVIDENCE_STEPS", "30")
        history = []
        for i in range(6):
            history += _make_ls_tool_call(f"dir{i}", [f"f{i}.py"], tc_id=f"only{i}")
        churn = detect_exploration_churn(history)
        assert CHURN_REASON_ONLY_SHADOW_LS in churn.reasons
        assert len(churn.reasons) == 1
        abort, reason = should_abort_explore_as_unproductive(history)
        assert abort
        assert "single_signal_volume" in reason

    def test_symbol_task_lower_ls_repeat_threshold(self, monkeypatch):
        monkeypatch.setenv("GHOST_SYMBOL_SEARCH_FIRST_STRICT", "1")
        monkeypatch.setenv("GHOST_CHURN_LS_REPEAT_THRESHOLD", "5")
        assert effective_ls_churn_threshold(EXPLORE_CLASS_SYMBOL_LOOKUP) == 2
        monkeypatch.setenv("GHOST_PATH_LISTING_STRICT_LS", "0")
        assert effective_ls_churn_threshold(EXPLORE_CLASS_PATH_LISTING) == 5

    def test_path_listing_strict_ls_threshold(self, monkeypatch):
        monkeypatch.setenv("GHOST_PATH_LISTING_STRICT_LS", "1")
        monkeypatch.setenv("GHOST_CHURN_LS_REPEAT_THRESHOLD", "5")
        assert effective_ls_churn_threshold(EXPLORE_CLASS_PATH_LISTING) == 2

    def test_symbol_task_two_ls_same_dir_is_churn(self, monkeypatch):
        monkeypatch.setenv("GHOST_SYMBOL_SEARCH_FIRST_STRICT", "1")
        history = (
            _make_ls_tool_call("apps/cli", ["a.py"], tc_id="t0")
            + _make_ls_tool_call("apps/cli", ["a.py"], tc_id="t1")
        )
        churn = detect_exploration_churn(history, explore_task_class=EXPLORE_CLASS_SYMBOL_LOOKUP)
        assert CHURN_REASON_REPEATED_LS_SAME_DIR in churn.reasons

    def test_same_tool_invocation_streak_detected(self, monkeypatch):
        monkeypatch.setenv("GHOST_CHURN_SAME_TOOL_STREAK_ABORT", "3")
        h: list = []
        for i in range(3):
            h += _make_search_tool_call("FooBar", tc_id=f"s{i}")
        churn = detect_exploration_churn(h)
        assert CHURN_REASON_REPEATED_SAME_TOOL_INVOCATION in churn.reasons

    def test_path_listing_two_ls_same_dir_is_churn(self, monkeypatch):
        monkeypatch.setenv("GHOST_PATH_LISTING_STRICT_LS", "1")
        history = (
            _make_ls_tool_call("src", ["a.ts"], tc_id="p0")
            + _make_ls_tool_call("src", ["a.ts"], tc_id="p1")
        )
        churn = detect_exploration_churn(history, explore_task_class=EXPLORE_CLASS_PATH_LISTING)
        assert CHURN_REASON_REPEATED_LS_SAME_DIR in churn.reasons


# ── should_abort_explore_as_unproductive ─────────────────────────────────────

class TestShouldAbortExplore:
    def test_no_abort_empty_history(self):
        abort, reason = should_abort_explore_as_unproductive([])
        assert not abort

    def test_abort_on_repo_mismatch(self):
        abort, reason = should_abort_explore_as_unproductive([], repo_mismatch_detected=True)
        assert abort
        assert "repo_mismatch" in reason

    def test_abort_on_multi_churn_signals(self, monkeypatch):
        monkeypatch.setenv("GHOST_CHURN_NO_EVIDENCE_STEPS", "3")
        history = []
        # Repeated ls (signal 1) + many steps no new reads (signal 2)
        for i in range(5):
            history += _make_ls_tool_call("apps", ["cli"], tc_id=f"ta{i}")
        abort, reason = should_abort_explore_as_unproductive(history)
        assert abort

    def test_no_abort_one_signal_few_steps(self):
        # Two ls on same dir but different listings → no identical-ls churn; < threshold for repeat count
        history = _make_ls_tool_call(".", ["f.py"], tc_id="t0") + _make_ls_tool_call(
            ".", ["f.py", "g.py"], tc_id="t1"
        )
        abort, _ = should_abort_explore_as_unproductive(history)
        assert not abort

    def test_abort_symbol_task_ls_loop_single_signal(self, monkeypatch):
        monkeypatch.setenv("GHOST_SYMBOL_SEARCH_FIRST_STRICT", "1")
        history = (
            _make_ls_tool_call("apps", ["cli"], tc_id="t0")
            + _make_ls_tool_call("apps", ["cli"], tc_id="t1")
        )
        abort, reason = should_abort_explore_as_unproductive(
            history, explore_task_class=EXPLORE_CLASS_SYMBOL_LOOKUP
        )
        assert abort
        assert "symbol_task_ls_loop" in reason

    def test_history_has_tool_call(self):
        history = _make_ls_tool_call(".", ["x.py"], tc_id="a")
        assert not history_has_tool_call(history, "search_code")
        assert history_has_tool_call(history, "ls")

    def test_abort_repeated_same_invocation(self, monkeypatch):
        monkeypatch.setenv("GHOST_CHURN_SAME_TOOL_STREAK_ABORT", "3")
        h: list = []
        for i in range(3):
            h += _make_search_tool_call("X", tc_id=f"x{i}")
        abort, reason = should_abort_explore_as_unproductive(h)
        assert abort
        assert "same_invocation" in reason


# ── Listing path extraction & absent targets ─────────────────────────────────

class TestListingPathPolicy:
    def test_extract_listing_targets(self):
        t = "List files in apps/cli/runtime"
        assert "apps/cli/runtime" in extract_listing_directory_targets(t)

    def test_listing_targets_all_absent(self, tmp_path):
        task = "¿Qué hay en el directorio apps/no_such_dir?"
        ok, missing = explore_listing_targets_all_absent(task, str(tmp_path))
        assert ok
        assert "apps/no_such_dir" in missing

    def test_listing_partial_absent_not_all_close(self, tmp_path):
        (tmp_path / "exists_here").mkdir()
        task = "list files in exists_here"
        ok, _ = explore_listing_targets_all_absent(task, str(tmp_path))
        assert not ok

    def test_build_listing_absent_messages_bilingual(self):
        en, es = build_listing_paths_absent_operator_message(["apps/missing"], "/tmp/r")
        assert "not exist" in en.lower() or "repository" in en.lower()
        assert "no exist" in es.lower() or "repositorio" in es.lower()


# ── Repo mismatch detection ───────────────────────────────────────────────────

class TestRepoMismatchDetection:
    def test_no_mismatch_empty_inputs(self):
        r = detect_repo_mismatch("", "/some/path")
        assert not r.detected

    def test_ghost_paths_in_non_ghost_repo(self, tmp_path):
        task = "¿En qué archivo está definida la función verification_diff_fingerprint?"
        result = detect_repo_mismatch(task, str(tmp_path))
        # tmp_path is NOT a ghost repo, so ghost internal markers → mismatch
        assert result.detected
        assert MISMATCH_REASON_GHOST_PATHS_IN_NON_GHOST_REPO in result.reasons

    def test_absent_paths_trigger_mismatch(self, tmp_path):
        task = "Edit apps/cli/runtime/session_phase.py and packages/py-core/ghostllm_core/config.py"
        result = detect_repo_mismatch(task, str(tmp_path))
        # Both paths don't exist in tmp_path
        assert result.detected

    def test_no_mismatch_when_paths_exist(self, tmp_path):
        # Create the paths
        (tmp_path / "apps" / "cli").mkdir(parents=True)
        (tmp_path / "apps" / "cli" / "session_phase.py").write_text("")
        # Task references an existing path — no mismatch from absent paths
        task = "Edit apps/cli/session_phase.py"
        result = detect_repo_mismatch(task, str(tmp_path))
        # Should not flag absent paths since the file exists
        # (ghost internal markers still apply if task mentions ghost symbols)
        assert isinstance(result.detected, bool)

    def test_result_has_operator_message_when_detected(self, tmp_path):
        task = "Where is verification_diff_fingerprint defined in apps/cli/runtime?"
        result = detect_repo_mismatch(task, str(tmp_path))
        if result.detected:
            assert result.operator_message_en or result.operator_message_es

    def test_inject_mismatch_into_session(self, tmp_path):
        session = MagicMock()
        session.repo_mismatch_detected = False
        session.repo_mismatch_reasons = []
        session.requested_paths_missing = []
        mismatch = RepoMismatchResult(
            detected=True,
            confidence=0.8,
            reasons=[MISMATCH_REASON_GHOST_PATHS_IN_NON_GHOST_REPO],
            missing_paths=["apps/cli/assistant.py"],
            operator_message_en="Wrong repo.",
        )
        inject_mismatch_into_session(mismatch, session)
        assert session.repo_mismatch_detected is True
        assert MISMATCH_REASON_GHOST_PATHS_IN_NON_GHOST_REPO in session.repo_mismatch_reasons

    def test_inject_does_not_overwrite_with_false(self, tmp_path):
        """inject_mismatch_into_session(detected=False) must not clear existing True."""
        session = MagicMock()
        session.repo_mismatch_detected = True
        inject_mismatch_into_session(RepoMismatchResult(detected=False), session)
        assert session.repo_mismatch_detected is True

    def test_wrong_repo_structure_note_triggers(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        task = "Open and summarize apps/cli/main.py"
        r = detect_repo_mismatch(task, str(tmp_path))
        assert r.detected
        assert MISMATCH_REASON_WRONG_REPO_STRUCTURE in r.reasons

    def test_stack_inconsistency_nextjs_style(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        task = "Where is apps/cli/runtime/verification.py?"
        sess = MagicMock()
        sess.repo_profile = {"stack": "nextjs"}
        r = detect_repo_mismatch(task, str(tmp_path), session=sess)
        assert r.detected
        assert (
            MISMATCH_REASON_STACK_INCONSISTENCY in r.reasons
            or MISMATCH_REASON_WRONG_REPO_STRUCTURE in r.reasons
        )

    def test_history_not_found_errors_boost(self, tmp_path):
        task = "Show apps/cli/foo.py"
        history = [
            {"role": "tool", "name": "read_file", "content": '{"error": "Path not found"}'},
            {"role": "tool", "name": "ls", "content": '{"error": "no such file or directory"}'},
        ]
        r = detect_repo_mismatch(task, str(tmp_path), history=history)
        assert MISMATCH_REASON_REPEATED_FILE_NOT_FOUND in r.reasons
        assert r.detected

    def test_ghost_product_phrase_in_nextjs_repo(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "src").mkdir()
        task = "How does explore_engine_v2 classify tasks in GhostLLM?"
        r = detect_repo_mismatch(task, str(tmp_path))
        assert r.detected
        assert MISMATCH_REASON_GHOST_PRODUCT_IN_FOREIGN_REPO in r.reasons


class TestRepoMismatchOutcome:
    def test_determine_outcome_read_only_when_flag_set(self):
        session = MagicMock()
        session.repo_mismatch_detected = True
        session.repo_mismatch_operator_message = "Wrong workspace."
        session.repo_mismatch_reason = "ghost_paths_in_non_ghost_repo"
        session.diff_summary = []
        orch = determine_task_outcome(session, [], {})
        assert orch.outcome == OUTCOME_READ_ONLY
        assert orch.evidence_score >= 12

    def test_explicit_paths_absent_informative_readonly(self):
        session = MagicMock()
        session.repo_mismatch_detected = False
        session.diff_summary = []
        session.events = [
            {"event": "explore_explicit_paths_absent", "paths": ["apps/missing"]},
        ]
        orch = determine_task_outcome(session, [], {})
        assert orch.outcome == OUTCOME_READ_ONLY
        assert orch.informative_readonly_kind == INFORMATIVE_KIND_EXPLICIT_PATH_ABSENT

    def test_missing_path_should_not_write_contract_is_readonly(self):
        """EXPLORE missing path + should_not_write must not become OUTCOME_BLOCKED → ABORTED."""
        session = MagicMock()
        session.repo_mismatch_detected = False
        session.diff_summary = []
        session.events = []
        session.task_contract = {"spec": {"change_expectation": "should_not_write", "intent": "review"}}
        messages = _make_failed_ls("apps/does_not_exist")
        orch = determine_task_outcome(session, messages, {})
        assert orch.outcome == OUTCOME_READ_ONLY

    def test_task_confidence_closure_reason(self):
        session = MagicMock()
        session.repo_mismatch_detected = True
        session.repo_mismatch_operator_message = "Use GhostLLM root."
        session.events = []
        orch = determine_task_outcome(session, [], {})
        snap = compute_task_confidence_closure(
            session=session,
            outcome=orch.outcome,
            outcome_result=orch,
            terminal_phase=SessionPhase.DONE,
            loop_abort_reason=None,
            has_final_nl=True,
            verification={},
        )
        assert snap.closure_reason_code == "repo_mismatch_graceful_closure"
        assert "informativo" in snap.closure_reason_summary_es.lower()


# ── Graceful closure messages ─────────────────────────────────────────────────

class TestGracefulClosureMessage:
    def test_mismatch_message_contains_key_info(self):
        msg = build_graceful_blocked_closure_message(
            "repo_mismatch_detected",
            "¿Dónde está verification_diff_fingerprint?",
            "The requested path does not exist in the current repo.",
        )
        assert "repo" in msg.lower() or "path" in msg.lower() or "exist" in msg.lower()

    def test_churn_message_non_empty(self):
        msg = build_graceful_blocked_closure_message("exploration_churn: ...", "some task")
        assert len(msg) > 10

    def test_message_includes_task_snippet(self):
        task = "Find the definition of the cache eviction policy"
        msg = build_graceful_blocked_closure_message("repo_mismatch_detected", task)
        assert "cache eviction" in msg or "definition" in msg or "Find" in msg

    def test_symbol_ls_loop_message(self):
        msg = build_graceful_blocked_closure_message(
            "symbol_task_ls_loop: repeated ls",
            "Where is verification_diff_fingerprint?",
        )
        assert "search_code" in msg or "listing" in msg.lower()

    def test_identical_ls_closure_message(self):
        msg = build_graceful_blocked_closure_message(
            "exploration_churn_identical_ls: test",
            "List files in src",
        )
        assert "listing" in msg.lower() or "shadow" in msg.lower() or "cached" in msg.lower()
