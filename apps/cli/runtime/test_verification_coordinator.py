"""Tests for VerificationCoordinator (single entry wrapping VerificationManager)."""
from __future__ import annotations

import pytest

from apps.cli.runtime.verification import VerificationManager
from apps.cli.runtime.verification_coordinator import (
    VerificationCoordinator,
    attach_coordinator_view,
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


def test_attach_coordinator_view_failed_reparable():
    raw = {
        "status": "failed",
        "checks": [
            {"name": "Build", "status": "failed", "stderr": "err"},
        ],
        "steps_executed_count": 1,
    }
    out = attach_coordinator_view(raw)
    c = out["coordinator"]
    assert c["passed"] is False
    assert len(c["failed_checks"]) == 1
    assert c["reparable"] is True
    assert c["stderr"]


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
