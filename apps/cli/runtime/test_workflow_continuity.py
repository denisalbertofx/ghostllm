"""Tests: handoff versionado, índice, schema/fallback, review packet, multi-tarea."""
from __future__ import annotations

import json
from pathlib import Path

from apps.cli.runtime.artifacts import ArtifactSession
from apps.cli.runtime.harness_bundle import build_change_proposal_packet
from apps.cli.runtime.workflow_continuity import (
    ROLE_AUTHOR_DEFAULT,
    _safe_slug,
    build_continuity_system_block,
    build_delegation_envelope,
    is_continuation_user_message,
    load_continuity_for_task,
    load_delegation_envelope,
    load_handoff_for_task,
    load_latest_handoff_any,
    normalize_handoff_document,
    persist_plan_and_handoff,
    read_continuity_index,
    save_delegation_envelope,
    save_handoff_json,
    save_operational_plan,
    write_continuity_index,
)


def test_is_continuation_user_message() -> None:
    assert is_continuation_user_message("continua")
    assert is_continuation_user_message("/do continua")
    assert not is_continuation_user_message("haz un fix en foo.py")


def test_strip_yaml_frontmatter() -> None:
    from apps.cli.runtime.workflow_continuity import strip_yaml_frontmatter

    raw = "---\nstatus: x\n---\n\n# Body\nline"
    assert "Body" in strip_yaml_frontmatter(raw)


def test_plan_filename_never_uses_literal_none_slug(tmp_path: Path) -> None:
    """Evita rutas tipo `.ghost/plans/None__...` cuando task_id es null/\"None\"."""
    cwd = str(tmp_path)
    rel = save_operational_plan(
        cwd,
        session_id="sess_z9",
        task_id="None",
        plan_body="# Plan",
        status="partial",
        task_title="x",
    )
    norm = rel.replace("\\", "/")
    assert "None__" not in norm


def test_safe_slug_appends_stable_hash() -> None:
    a = _safe_slug("Task Alpha")
    b = _safe_slug("Task Beta")
    assert a != b
    assert a == _safe_slug("Task Alpha")
    assert len(a.split("_")[-1]) == 8


def test_save_plan_and_handoff_roundtrip_and_index(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    rel = save_operational_plan(
        cwd,
        session_id="task_20260101_aaaa_sess",
        task_id="task_20260101_realid",
        plan_body="## Paso 1\n- editar a.py",
        status="partial",
        task_title="Mi tarea",
    )
    assert ".ghost/plans/" in rel.replace("\\", "/")
    plan_file = tmp_path / Path(rel)
    assert plan_file.is_file()

    payload = {
        "schema_version": 2,
        "task_id": "task_20260101_realid",
        "session_id": "task_20260101_aaaa_sess",
        "mode": "harness_x",
        "harness_mode_label": "harness_x",
        "task": "do thing",
        "files_touched": ["a.py"],
        "next_action": "run tests",
        "plan_reference": rel,
    }
    hrel = save_handoff_json(cwd, payload)
    assert "handoffs" in hrel.replace("\\", "/")
    assert "__latest" not in hrel  # ruta canónica = versionado

    idx = read_continuity_index(cwd)
    ent = idx.get("by_task_id", {}).get("task_20260101_realid")
    assert isinstance(ent, dict)
    assert ent.get("active_handoff_relpath")
    assert ent.get("latest_plan_relpath") == rel.replace("\\", "/")

    ctx = load_continuity_for_task(cwd, "task_20260101_realid")
    assert ctx.plan_body
    assert ctx.handoff.get("mode") == "harness_x" or ctx.handoff.get("harness_mode_label") == "harness_x"


def test_two_tasks_distinct_handoffs(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    for tid, label in [("task_alpha", "alpha work"), ("task_beta", "beta work")]:
        save_handoff_json(
            cwd,
            {
                "schema_version": 2,
                "task_id": tid,
                "session_id": f"sess_{tid}",
                "mode": "m",
                "task": label,
                "files_touched": [f"{tid}.py"],
                "next_action": "next",
            },
        )
    ha, _, _ = load_handoff_for_task(cwd, "task_alpha")
    hb, _, _ = load_handoff_for_task(cwd, "task_beta")
    assert ha.get("task") == "alpha work"
    assert hb.get("task") == "beta work"
    assert "task_alpha.py" in (ha.get("files_touched") or [])


def test_load_latest_handoff_any_prefers_index_recency(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    import time

    save_handoff_json(
        cwd,
        {
            "schema_version": 2,
            "task_id": "old_task",
            "session_id": "s_old",
            "mode": "m",
            "task": "old",
        },
    )
    time.sleep(0.05)
    save_handoff_json(
        cwd,
        {
            "schema_version": 2,
            "task_id": "new_task",
            "session_id": "s_new",
            "mode": "m",
            "task": "newest",
        },
    )
    ho, src = load_latest_handoff_any(cwd)
    assert ho.get("task") == "newest"
    assert "new_task" in src or src


def test_handoff_fallback_when_index_points_to_missing(tmp_path: Path) -> None:
    from apps.cli.runtime.workflow_continuity import _safe_slug, load_handoff_for_task

    cwd = str(tmp_path)
    slug = _safe_slug("tid_a")
    d = tmp_path / ".ghost" / "handoffs"
    d.mkdir(parents=True)
    good = {
        "schema_version": 2,
        "task_id": "tid_a",
        "task": "recovered",
        "session_id": "s1",
        "mode": "m",
    }
    (d / f"{slug}__latest.json").write_text(json.dumps(good), encoding="utf-8")
    write_continuity_index(
        cwd,
        {
            "format_version": 1,
            "by_task_id": {
                "tid_a": {
                    "task_id": "tid_a",
                    "active_handoff_relpath": ".ghost/handoffs/does_not_exist.json",
                    "latest_plan_relpath": "",
                    "updated_at": "2099-01-01T00:00:00+00:00",
                    "updated_at_unix": 9999999999.0,
                }
            },
        },
    )
    h, w, _src = load_handoff_for_task(cwd, "tid_a")
    assert h.get("task") == "recovered"


def test_normalize_handoff_v1_legacy_and_corrupt() -> None:
    n, w = normalize_handoff_document(
        {
            "version": 1,
            "task_id": "t1",
            "session_id": "s",
            "harness_mode_label": "legacy",
            "task_text": "hello",
        }
    )
    assert n.get("schema_version") == 2
    assert n.get("mode") == "legacy"
    assert n.get("task") == "hello"
    assert "missing_task_id" not in w

    empty, w2 = normalize_handoff_document("not a dict")
    assert empty == {}
    assert "not_object" in w2

    thin, w3 = normalize_handoff_document({"schema_version": 2, "task_id": "x"})
    assert thin.get("task_id") == "x"
    assert "thin_task_and_plan" in w3 or "missing_task_id" in w3


def test_normalize_task_id_mismatch_rejects() -> None:
    n, w = normalize_handoff_document(
        {"task_id": "other", "task": "x", "mode": "m"},
        expected_task_id="expected_tid",
    )
    assert n == {}
    assert "task_id_mismatch" in w


def test_persist_plan_handoff_and_review_packet(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    s = ArtifactSession("task_2099_0101_sess99_abcd", "hola", task_id="tid_one")
    s.plan = "## Plan\n- uno"
    s.phase = "DONE"
    s.task_outcome = "implemented"
    s.next_action = "open PR"
    s.task_intent = "modification"
    s.change_expectation = "may_write"
    s.role_author = "ghost_model"
    s.role_operator = "test_operator"
    s.verification = {"status": "passed", "checks": [], "steps_executed_count": 1}
    s.diff_summary = [{"file": "foo.py", "type": "Edit"}]
    s.closure_reason_summary_es = "Listo para revisión."
    pr, hr = persist_plan_and_handoff(cwd, s)
    assert pr
    assert hr
    assert ".envelope.json" in str(getattr(s, "delegation_envelope_path", "")).replace("\\", "/")
    hf = tmp_path / Path(str(hr).replace("\\", "/"))
    assert hf.is_file()
    hj = json.loads(hf.read_text(encoding="utf-8"))
    assert hj.get("actors", {}).get("operator") == "test_operator"
    assert "review_packets" in str(getattr(s, "review_packet_path", "")).replace("\\", "/")
    rp = tmp_path / Path(str(s.review_packet_path).replace("\\", "/"))
    assert rp.is_file()
    data = json.loads(rp.read_text(encoding="utf-8"))
    assert data.get("kind") == "ghost_change_proposal"
    assert data.get("format_version") == 2
    assert data.get("links", {}).get("handoff")
    assert data.get("continuity", {}).get("envelope")
    assert data.get("recommended_reviewer_action")


def test_build_change_proposal_packet_links() -> None:
    s = ArtifactSession("sid", "t", task_id="tid")
    s.phase = "DONE"
    s.task_outcome = "implemented"
    s.diff_summary = [{"file": "a.py"}]
    s.verification = {"status": "passed", "steps_executed_count": 1}
    p = build_change_proposal_packet(
        s, plan_relpath="p.md", handoff_relpath="h.json", envelope_relpath="e.json"
    )
    assert p.get("format_version") == 2
    assert p["links"]["plan"] == "p.md"
    assert p["continuity"]["envelope"] == "e.json"
    assert isinstance(p.get("open_risks"), list)
    assert p["ready_for_review"] is True


def test_delegation_envelope_roundtrip(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    payload = {
        "schema_version": 2,
        "task_id": "tid_x",
        "session_id": "sess_x",
        "task": "job",
        "mode": "m",
        "actors": {"author": "a", "operator": "o", "reviewer": "", "repair_specialist": ""},
    }
    env = build_delegation_envelope(cwd, payload, handoff_ref=".ghost/handoffs/x.json", state="ready")
    erel = save_delegation_envelope(cwd, env)
    loaded, warns = load_delegation_envelope(cwd, erel)
    assert not warns or "unexpected" not in str(warns)
    assert loaded.get("handoff_payload", {}).get("task_id") == "tid_x"
    assert loaded.get("state") == "ready"


def test_corrupt_continuity_index_safe_read(tmp_path: Path) -> None:
    p = tmp_path / ".ghost"
    p.mkdir(parents=True)
    (p / "continuity_index.json").write_text("{not json", encoding="utf-8")
    idx = read_continuity_index(str(tmp_path))
    assert idx.get("by_task_id") == {}
    assert idx.get("index_corrupt") is True


def test_versioned_handoff_glob_order_deterministic(tmp_path: Path) -> None:
    import os

    from apps.cli.runtime.workflow_continuity import _safe_slug

    cwd = str(tmp_path)
    tid = "task_det"
    slug = _safe_slug(tid)
    d = tmp_path / ".ghost" / "handoffs"
    d.mkdir(parents=True)
    for name, body in [("aaa", "first"), ("zzz", "second")]:
        fp = d / f"{slug}__{name}.json"
        fp.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "task_id": tid,
                    "session_id": name,
                    "task": body,
                    "mode": "m",
                }
            ),
            encoding="utf-8",
        )
        os.utime(fp, (1000, 1000))
    h, _, src = load_handoff_for_task(cwd, tid)
    assert h.get("task") == "first"
    assert "aaa" in src


def test_normalize_actors_defaults() -> None:
    n, _ = normalize_handoff_document({"task_id": "t", "task": "x", "mode": "m"})
    assert n["actors"]["author"] == ROLE_AUTHOR_DEFAULT


def test_build_continuity_system_block_uses_mode_and_plan_ref() -> None:
    block = build_continuity_system_block(
        "## Old",
        {
            "mode": "repair",
            "files_touched": ["a.py"],
            "plan_reference": ".ghost/plans/x.md",
            "next_action": "fix",
        },
    )
    assert "repair" in block
    assert ".ghost/plans/x.md" in block


def test_handoff_index_lookup_tolerates_whitespace_task_id(tmp_path: Path) -> None:
    """Índice claveado por task_id exacto: la carga debe aceptar espacios en el parámetro."""
    cwd = str(tmp_path)
    save_handoff_json(
        cwd,
        {
            "schema_version": 2,
            "task_id": "real_tid",
            "session_id": "s1",
            "mode": "m",
            "task": "hello",
        },
    )
    h, w, _ = load_handoff_for_task(cwd, "  real_tid  ")
    assert h.get("task") == "hello"
    assert "task_id_mismatch" not in w


def test_load_continuity_resolves_plan_with_whitespace_task_id(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    save_operational_plan(
        cwd,
        session_id="sess",
        task_id="mytask",
        plan_body="# Plan body",
        status="partial",
        task_title="t",
    )
    ctx = load_continuity_for_task(cwd, "  mytask  ")
    assert "# Plan body" in ctx.plan_body


def test_latest_handoff_task_id_resolves_plan_like_post_session_flow(tmp_path: Path) -> None:
    """Tras cierre, nueva sesión sin task_id: handoff reciente apunta al plan correcto."""
    cwd = str(tmp_path)
    rel = save_operational_plan(
        cwd,
        session_id="s",
        task_id="tid_x",
        plan_body="# Persisted",
        status="partial",
        task_title="t",
    )
    save_handoff_json(
        cwd,
        {
            "schema_version": 2,
            "task_id": "tid_x",
            "session_id": "s",
            "mode": "m",
            "task": "job",
            "plan_reference": rel,
        },
    )
    ho, _ = load_latest_handoff_any(cwd)
    assert ho.get("task_id") == "tid_x"
    ctx = load_continuity_for_task(cwd, str(ho.get("task_id") or ""))
    assert "Persisted" in ctx.plan_body
