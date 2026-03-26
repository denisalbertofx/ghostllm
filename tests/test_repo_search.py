"""
Tests for apps/cli/runtime/repo_search.py (Exploration Engine v2).

Coverage:
- Python fallback symbol search
- Python fallback filename search
- path_exists mode
- path traversal safety (deny escaping repo_root)
- Result serialization (to_tool_payload)
- Empty/invalid inputs
"""
import os
import pytest
from pathlib import Path

from apps.cli.runtime.repo_search import (
    SEARCH_MODE_FILENAME,
    SEARCH_MODE_PATH_EXISTS,
    SEARCH_MODE_SYMBOL,
    SEARCH_MODE_TEXT,
    SearchCodeResult,
    _check_path_exists,
    _py_search_filename,
    _py_search_symbol,
    _py_search_text,
    search_code,
)


@pytest.fixture
def sample_repo(tmp_path):
    """Create a tiny in-memory repo structure for search tests."""
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "cli").mkdir()
    (tmp_path / "apps" / "cli" / "assistant.py").write_text(
        "def verification_diff_fingerprint(session):\n    return 'fp'\n"
        "class CodexAssistant:\n    pass\n"
    )
    (tmp_path / "apps" / "cli" / "runtime").mkdir()
    (tmp_path / "apps" / "cli" / "runtime" / "session_phase.py").write_text(
        "class SessionPhase:\n    EXPLORE = 'EXPLORE'\n    ACT = 'ACT'\n"
    )
    (tmp_path / "apps" / "cli" / "runtime" / "explore_engine_v2.py").write_text(
        "EXPLORE_CLASS_SYMBOL_LOOKUP = 'symbol_lookup'\n"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "GHOST_EXPLORATION.md").write_text("# Exploration Engine v2\n")
    return tmp_path


class TestSymbolSearch:
    def test_finds_function_definition(self, sample_repo):
        result = _py_search_symbol("verification_diff_fingerprint", sample_repo, max_results=10)
        assert result.found()
        assert any("assistant.py" in m.path for m in result.matches)

    def test_finds_class_name(self, sample_repo):
        result = _py_search_symbol("CodexAssistant", sample_repo, max_results=10)
        assert result.found()

    def test_no_match_returns_empty(self, sample_repo):
        result = _py_search_symbol("nonexistent_function_xyz123", sample_repo, max_results=10)
        assert not result.found()
        assert result.error is None

    def test_line_range_populated(self, sample_repo):
        result = _py_search_symbol("SessionPhase", sample_repo, max_results=10)
        assert result.found()
        m = result.matches[0]
        assert m.line_start is not None and m.line_start >= 1


class TestFilenameSearch:
    def test_finds_file_by_fragment(self, sample_repo):
        result = _py_search_filename("session_phase", sample_repo, max_results=10)
        assert result.found()
        assert any("session_phase.py" in m.path for m in result.matches)

    def test_finds_explore_engine(self, sample_repo):
        result = _py_search_filename("explore_engine", sample_repo, max_results=10)
        assert result.found()

    def test_md_file_found(self, sample_repo):
        result = _py_search_filename("GHOST_EXPLORATION", sample_repo, max_results=10)
        assert result.found()

    def test_no_match_empty(self, sample_repo):
        result = _py_search_filename("totally_absent_xyz", sample_repo, max_results=10)
        assert not result.found()


class TestPathExists:
    def test_existing_file(self, sample_repo):
        result = _check_path_exists("apps/cli/assistant.py", sample_repo)
        assert result.found()

    def test_existing_directory(self, sample_repo):
        result = _check_path_exists("apps/cli/runtime", sample_repo)
        assert result.found()

    def test_missing_path(self, sample_repo):
        result = _check_path_exists("apps/cli/nonexistent.py", sample_repo)
        assert not result.found()
        assert result.error is None

    def test_path_traversal_denied(self, sample_repo):
        result = _check_path_exists("../../etc/passwd", sample_repo)
        # Should either not find it OR deny with error
        assert (not result.found()) or (result.error is not None)


class TestTextSearch:
    def test_finds_regex_pattern(self, sample_repo):
        result = _py_search_text("class\\s+\\w+", sample_repo, max_results=10)
        assert result.found()

    def test_finds_literal_text(self, sample_repo):
        result = _py_search_text("EXPLORE_CLASS_SYMBOL_LOOKUP", sample_repo, max_results=10)
        assert result.found()


class TestSearchCodePublicAPI:
    def test_symbol_mode_public(self, sample_repo):
        result = search_code("verification_diff_fingerprint", str(sample_repo), mode=SEARCH_MODE_SYMBOL)
        assert isinstance(result, SearchCodeResult)
        assert result.found()

    def test_filename_mode_public(self, sample_repo):
        result = search_code("session_phase", str(sample_repo), mode=SEARCH_MODE_FILENAME)
        assert result.found()

    def test_path_exists_mode(self, sample_repo):
        result = search_code("apps/cli/assistant.py", str(sample_repo), mode=SEARCH_MODE_PATH_EXISTS)
        assert result.found()

    def test_path_exists_missing(self, sample_repo):
        result = search_code("apps/cli/missing_file.py", str(sample_repo), mode=SEARCH_MODE_PATH_EXISTS)
        assert not result.found()

    def test_invalid_repo_root(self, tmp_path):
        result = search_code("anything", str(tmp_path / "does_not_exist"), mode=SEARCH_MODE_SYMBOL)
        assert result.error is not None

    def test_empty_query_returns_error(self, sample_repo):
        result = search_code("", str(sample_repo), mode=SEARCH_MODE_SYMBOL)
        assert result.error is not None

    def test_to_tool_payload_structure(self, sample_repo):
        result = search_code("SessionPhase", str(sample_repo), mode=SEARCH_MODE_SYMBOL)
        payload = result.to_tool_payload()
        assert "mode" in payload
        assert "found" in payload
        assert "matches" in payload
        assert isinstance(payload["matches"], list)

    def test_summary_format(self, sample_repo):
        result = search_code("CodexAssistant", str(sample_repo), mode=SEARCH_MODE_SYMBOL)
        s = result.summary()
        assert isinstance(s, str) and len(s) > 0

    def test_max_results_respected(self, sample_repo):
        result = search_code("a", str(sample_repo), mode=SEARCH_MODE_TEXT, max_results=2)
        assert len(result.matches) <= 2
