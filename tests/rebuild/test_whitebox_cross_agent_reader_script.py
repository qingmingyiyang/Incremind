from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = ROOT / "work" / "scripts" / "run_inspiration_whitebox_export_smoke.py"
READER_SCRIPT = ROOT / "work" / "scripts" / "read_whitebox_memory_export.py"


def test_cross_agent_reader_consumes_whitebox_export_package(tmp_path) -> None:
    output_dir = tmp_path / "smoke"
    subprocess.run(
        [sys.executable, str(SMOKE_SCRIPT), "--output-dir", str(output_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    smoke_report = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
    export_dir = Path(smoke_report["whitebox_export"]["output_dir"])
    reader_report_path = tmp_path / "reader-result.json"

    completed = subprocess.run(
        [sys.executable, str(READER_SCRIPT), str(export_dir), "--report", str(reader_report_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    stdout = json.loads(completed.stdout)
    report = json.loads(reader_report_path.read_text(encoding="utf-8"))
    assert stdout["status"] == "passed"
    assert report["export_id"] == "whitebox-export-chriptmas-os"
    assert report["required_files_present"] is True
    assert report["manifest_files_match_read_order"] is True
    assert report["library_activity_status"] in {"ready", "empty"}
    assert report["library_activity_recent_day_count"] >= 1
    assert report["library_activity_year_count"] >= 1
    assert "model_provider_execution" in report["library_activity_blocked_operations"]
    assert "memory_publication" in report["library_activity_blocked_operations"]
    assert report["inspiration_index_status"] == "ready"
    assert report["inspiration_index_read_only"] is True
    assert report["inspiration_record_count"] >= 1
    assert report["inspiration_collision_count"] >= 1
    assert report["inspiration_records_have_fragments"] is True
    assert report["inspiration_collision_entrypoint_write_target"] == "inspiration_collisions"
    assert report["has_inspiration_series"] is True
    assert report["paragraph_tag_count"] >= 1
    assert report["reminder_memory_publication_state"] == "not_published"
    assert "model_provider_execution" in report["reminder_blocked_operations"]
    assert report["mvp_readiness_item_count"] == 17
    assert report["mvp_readiness_read_only"] is True
    assert "model_provider_execution" in report["mvp_readiness_blocked_operations"]
    assert report["mvp_desktop_shell_status"] == "ready"
    assert "electron:doctor_status:passed" in report["mvp_desktop_shell_evidence"]
    assert report["mvp_platform_adapter_status"] == "ready"
    assert "electron:platform_adapter_status:passed" in report["mvp_platform_adapter_evidence"]
    assert report["external_agent_task_count"] >= 1
    assert report["external_agent_requires_user_review"] is True
    assert report["external_agent_write_target"] == "proposal_only"
    assert report["reader_boundary"]["object_store_opened"] is False
    assert report["reader_boundary"]["memory_write_performed"] is False
