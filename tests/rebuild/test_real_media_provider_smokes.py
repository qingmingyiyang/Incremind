from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = ROOT / "work" / "scripts" / "run_real_media_provider_smokes.py"
MANUAL_RECORD = ROOT / "docs" / "validation" / "manual-real-media-provider-smokes.md"


def test_real_media_provider_smokes_run_without_memory_publication() -> None:
    output_dir = ROOT / "docs" / "validation" / "tmp" / "round39-test-media-provider-smokes"
    completed = subprocess.run(
        [sys.executable, str(SMOKE_SCRIPT), "--output-dir", str(output_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    report = json.loads(Path(output["report"]).read_text(encoding="utf-8"))

    assert report["status"] == "passed"
    assert report["mode"] == "generated_non_private_media_fixtures"
    assert {case["kind"] for case in report["cases"]} == {
        "image_ocr",
        "audio_transcription",
        "video_frame_extraction",
    }
    for case in report["cases"]:
        assert case["queued"]["status"] == "queued"
        assert case["settings"]["status"] == "ready"
        assert case["settings"]["enabled"] is True
        assert case["settings"]["remote_processing"] is False
        assert case["settings"]["memory_publication"] == "not_started"
        assert case["run_result"]["status"] == "completed"
        assert case["output_status"] == "completed"
        assert case["output_memory_publication"] == "candidate_created"
        assert case["output_path_stored"] is False
        assert case["candidate_status"] == "pending_review"
        assert case["candidate_auto_promote_allowed"] is False
    assert report["memory_atoms_written"] == 0
    assert report["staging_atoms_written"] == 0
    assert report["memory_publications_written"] == 0
    assert report["privacy_boundary"]["user_private_file_read"] is False
    assert report["privacy_boundary"]["os_path_in_source_contract"] is False
    assert report["privacy_boundary"]["run_endpoint_command_from_ui"] is False
    assert report["privacy_boundary"]["long_term_memory_published"] is False


def test_real_media_provider_manual_record_captures_all_three_boundaries() -> None:
    record = MANUAL_RECORD.read_text(encoding="utf-8")

    assert "generated non-private media fixture files" in record
    assert "Image OCR" in record
    assert "Audio Transcription" in record
    assert "Video Frame Extraction" in record
    assert "media_processing_output" in record
    assert "pending_review" in record
    assert "auto_promote_allowed" in record
    assert "memory_atoms_written: 0" in record
    assert "staging_atoms_written: 0" in record
    assert "memory_publications_written: 0" in record
    assert "user_private_file_read: false" in record
    assert "os_path_in_source_contract: false" in record
