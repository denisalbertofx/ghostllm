from pathlib import Path

from apps.cli.runtime.artifacts import ArtifactManager


def test_artifact_manager_marks_missing_required_outputs(tmp_path: Path) -> None:
    ghost_dir = tmp_path / ".ghost"
    ghost_dir.mkdir()
    (ghost_dir / "policy.yaml").write_text(
        "artifacts:\n"
        "  required: [review_packet, diff_summary, artifact_json]\n"
        "  format: structured_json\n",
        encoding="utf-8",
    )

    manager = ArtifactManager(str(tmp_path))
    session = manager.start_session("test task")
    session.diff_summary.append({"file": "a.py", "type": "Edit File", "status": "success"})
    manager.persist()

    assert session.artifact_output_format == "structured_json"
    assert session.artifact_requirements_met is False
    assert "review_packet" in session.artifact_requirements_missing
    assert session.artifact_json_path.endswith(".json")


def test_artifact_manager_accepts_required_outputs_when_present(tmp_path: Path) -> None:
    ghost_dir = tmp_path / ".ghost"
    ghost_dir.mkdir()
    (ghost_dir / "policy.yaml").write_text(
        "artifacts:\n"
        "  required: [review_packet, diff_summary, artifact_json, artifact_markdown]\n"
        "  format: structured_json\n",
        encoding="utf-8",
    )

    manager = ArtifactManager(str(tmp_path))
    session = manager.start_session("test task")
    session.review_packet_path = ".ghost/review_packets/task.json"
    session.diff_summary.append({"file": "a.py", "type": "Edit File", "status": "success"})
    manager.persist()

    assert session.artifact_requirements_met is True
    assert session.primary_output_path == ".ghost/review_packets/task.json"
