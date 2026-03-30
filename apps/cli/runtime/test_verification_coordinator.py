"""Tests for VerificationCoordinator (single entry wrapping VerificationManager)."""
from __future__ import annotations

from unittest import mock

import pytest

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.verification import VerificationManager
from apps.cli.runtime.verification_coordinator import (
    VerificationCoordinator,
    attach_coordinator_view,
    format_integrity_verify_preamble,
    verification_diff_fingerprint,
    verification_fingerprint_eligible_for_reuse,
)


class _Sess:
    def __init__(self) -> None:
        self.verification: dict = {}
        self.verification_diff_fingerprint: str = ""
        self.verification_reuse_decisions: list = []


def _ok_verification() -> dict:
    return attach_coordinator_view(
        {"status": "success", "checks": [], "steps_executed_count": 1, "justification": "ok"}
    )


def test_verification_diff_fingerprint_stable():
    d1 = [
        {"file": "a.ts", "type": "edit", "content_sha256": "aa"},
        {"file": "b.ts", "type": "write", "content_sha256": "bb"},
    ]
    d2 = [
        {"file": "b.ts", "type": "write", "content_sha256": "bb"},
        {"file": "a.ts", "type": "edit", "content_sha256": "aa"},
    ]
    assert verification_diff_fingerprint(d1) == verification_diff_fingerprint(d2)


def test_verification_diff_fingerprint_same_path_different_content():
    d1 = [{"file": "x.ts", "type": "Edit File", "content_sha256": "aaa"}]
    d2 = [{"file": "x.ts", "type": "Edit File", "content_sha256": "bbb"}]
    assert verification_diff_fingerprint(d1) != verification_diff_fingerprint(d2)


def test_verification_diff_fingerprint_incomplete_without_sha():
    d = [{"file": "x.ts", "type": "Edit File"}]
    fp = verification_diff_fingerprint(d)
    assert fp.startswith("incomplete_")
    assert not verification_fingerprint_eligible_for_reuse(fp)


def test_fingerprint_eligible_for_complete():
    fp = verification_diff_fingerprint(
        [{"file": "a.ts", "type": "write", "content_sha256": "deadbeef"}]
    )
    assert verification_fingerprint_eligible_for_reuse(fp)


def test_attach_coordinator_view_success():
    raw = {
        "status": "success",
        "checks": [{"name": "Lint", "status": "passed"}],
        "steps_executed_count": 1,
    }
    out = attach_coordinator_view(raw)
    c = out["coordinator"]
    assert c["passed"] is True
    assert c["failed_checks"] == []
    assert c["reparable"] is False
    assert out["verification_source"] == "VerificationCoordinator"


def test_format_integrity_verify_preamble_no_placeholder():
    s = format_integrity_verify_preamble(
        ["Python Tests (explicit, targeted) @ .: uv run pytest x.py -q"],
        pytest_focus_n=2,
    )
    assert "explicit, targeted" in s
    assert "pytest focus: 2 path(s)" in s
    assert "Build/TypeCheck/Lint" not in s


def test_format_integrity_verify_preamble_empty_plan():
    s = format_integrity_verify_preamble([], pytest_focus_n=0)
    assert "No checks resolved" in s


def test_planned_check_labels_match_resolve_ordered_checks_explicit_targeted():
    mgr = mock.MagicMock(spec=VerificationManager)
    mgr.resolve_ordered_checks.return_value = (
        [
            {
                "name": "Python Tests (explicit, targeted)",
                "cwd": ".",
                "command": "uv run pytest apps/cli/runtime/test_x.py -q",
            }
        ],
        "python_targeted",
    )
    coord = VerificationCoordinator(mgr)
    labels = coord.planned_check_labels(
        task_type="fix",
        scope="api",
        files_changed=[{"file": "apps/cli/runtime/x.py"}],
        budget_remaining=100,
    )
    assert len(labels) == 1
    assert "explicit, targeted" in labels[0]
    assert "uv run pytest" in labels[0]
    mgr.resolve_ordered_checks.assert_called_once()


def test_attach_coordinator_view_failed_reparable():
    raw = {
        "status": "failed",
        "checks": [
            {"name": "Build", "status": "failed", "stderr": "err", "cwd": "apps/web"},
        ],
        "steps_executed_count": 1,
        "verification_scope": "node",
        "planned_check_cwds": {"Build": "apps/web"},
    }
    out = attach_coordinator_view(raw)
    c = out["coordinator"]
    assert c["passed"] is False
    assert len(c["failed_checks"]) == 1
    assert c["reparable"] is True
    assert c["stderr"]
    assert out["verification_scope"] == "node"
    assert out["planned_check_cwds"] == {"Build": "apps/web"}


def test_try_reuse_same_paths_same_content_reuses():
    """Full content_sha256 on rows: identical diff -> reuse stored verification."""
    mgr = VerificationManager(".")
    coord = VerificationCoordinator(mgr)
    sess = _Sess()
    diff = [
        {"file": "src/a.ts", "type": "Write File", "status": "success", "content_sha256": "sha111"},
    ]
    fp = verification_diff_fingerprint(diff)
    assert verification_fingerprint_eligible_for_reuse(fp)
    sess.verification_diff_fingerprint = fp
    sess.verification = _ok_verification()
    r = coord.try_reuse_stored_verification(sess, list(diff), verification_completed=True)
    assert r is not None
    assert r["coordinator"]["passed"] is True
    assert len(sess.verification_reuse_decisions) == 1
    assert sess.verification_reuse_decisions[0]["reused"] is True
    assert sess.verification_reuse_decisions[0]["reason"] == "fingerprint_match"


def test_try_reuse_same_paths_different_content_no_reuse():
    """Same path + op metadata but different post-write content hash -> do not reuse."""
    mgr = VerificationManager(".")
    coord = VerificationCoordinator(mgr)
    sess = _Sess()
    stored_diff = [
        {"file": "src/x.ts", "type": "Edit File", "status": "success", "content_sha256": "aaa"},
    ]
    sess.verification_diff_fingerprint = verification_diff_fingerprint(stored_diff)
    sess.verification = _ok_verification()
    new_diff = [
        {"file": "src/x.ts", "type": "Edit File", "status": "success", "content_sha256": "bbb"},
    ]
    assert coord.try_reuse_stored_verification(sess, new_diff, verification_completed=True) is None


def test_try_reuse_different_paths_no_reuse():
    mgr = VerificationManager(".")
    coord = VerificationCoordinator(mgr)
    sess = _Sess()
    sess.verification_diff_fingerprint = verification_diff_fingerprint(
        [{"file": "x.py", "type": "write", "content_sha256": "01"}]
    )
    sess.verification = _ok_verification()
    r = coord.try_reuse_stored_verification(
        sess,
        [{"file": "y.py", "type": "write", "content_sha256": "02"}],
        verification_completed=True,
    )
    assert r is None


def test_try_reuse_incomplete_new_fingerprint_no_reuse():
    """Missing content_sha256 on current diff: never reuse (unsafe)."""
    mgr = VerificationManager(".")
    coord = VerificationCoordinator(mgr)
    sess = _Sess()
    good = [{"file": "a.ts", "type": "write", "content_sha256": "full"}]
    sess.verification_diff_fingerprint = verification_diff_fingerprint(good)
    sess.verification = _ok_verification()
    bad = [{"file": "a.ts", "type": "write"}]
    assert verification_diff_fingerprint(bad).startswith("incomplete_")
    assert coord.try_reuse_stored_verification(sess, bad, verification_completed=True) is None


def test_try_reuse_stored_incomplete_fingerprint_no_reuse():
    """Prior verification used weak fingerprint: do not reuse even if new diff is strong."""
    mgr = VerificationManager(".")
    coord = VerificationCoordinator(mgr)
    sess = _Sess()
    weak_fp = verification_diff_fingerprint([{"file": "a.ts", "type": "write"}])
    assert weak_fp.startswith("incomplete_")
    sess.verification_diff_fingerprint = weak_fp
    sess.verification = _ok_verification()
    strong = [{"file": "a.ts", "type": "write", "content_sha256": "abc"}]
    assert coord.try_reuse_stored_verification(sess, strong, verification_completed=True) is None


def test_skip_verify_on_contract(tmp_path):
    pkg = tmp_path / "package.json"
    pkg.write_text('{"scripts":{"build":"exit 1"}}', encoding="utf-8")
    mgr = VerificationManager(str(tmp_path))
    coord = VerificationCoordinator(mgr)
    sess = _Sess()
    res = coord.run(
        diff_summary=[{"file": "a.ts"}],
        task_contract={"skip_verify": True, "spec": None},
        task_type="direct_edit",
        scope="unknown",
        budget_remaining=50,
        session=sess,
        reuse_if_verified=False,
    )
    assert res.get("status") == "incomplete"
    assert res.get("steps_executed_count") == 0
    c = res.get("coordinator")
    assert isinstance(c, dict)


def test_artifact_session_records_incremental_verification_batches():
    sess = ArtifactSession("s1", "task")

    sess.record_incremental_verification(
        {
            "status": "failed",
            "verification_scope": "python_targeted",
            "checks": [{"name": "Tests", "status": "failed"}],
            "steps_executed_count": 1,
        },
        diff_summary=[{"file": "apps/cli/main.py", "status": "success"}],
        context_label="iter_1",
    )
    sess.record_incremental_verification(
        {
            "status": "success",
            "verification_scope": "python_targeted",
            "checks": [{"name": "Tests", "status": "passed"}],
            "steps_executed_count": 1,
        },
        diff_summary=[{"file": "apps/cli/main.py", "status": "success"}],
        context_label="iter_2",
    )

    state = sess.incremental_verify_state
    assert state["batches_run"] == 2
    assert state["last_status"] == "success"
    assert state["last_context_label"] == "iter_2"
    assert state["files_covered"] == ["apps/cli/main.py"]
