from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = ROOT / "work" / "scripts" / "run_real_document_extraction_smoke.py"
MANUAL_RECORD = ROOT / "docs" / "validation" / "manual-real-document-extraction.md"


def test_real_document_extraction_smoke_runs_without_memory_publication(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(SMOKE_SCRIPT), "--output-dir", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    report = json.loads((Path(output["report"])).read_text(encoding="utf-8"))

    assert report["status"] == "passed"
    assert report["mode"] == "generated_non_private_docx"
    assert report["settings"]["status"] == "ready"
    assert report["settings"]["enabled"] is True
    assert report["settings"]["remote_processing"] is False
    assert report["settings"]["memory_publication"] == "not_started"
    assert report["read_result"]["status"] == "completed"
    assert report["read_result"]["content_read"] is True
    assert report["candidate_status"] == "pending_review"
    assert report["candidate_auto_promote_allowed"] is False
    assert report["memory_atoms_written"] == 0
    assert report["staging_atoms_written"] == 0
    assert report["memory_publications_written"] == 0
    assert report["privacy_boundary"]["user_private_file_read"] is False
    assert report["privacy_boundary"]["os_path_in_source_contract"] is False
    assert report["privacy_boundary"]["run_endpoint_command_from_ui"] is False
    assert report["privacy_boundary"]["long_term_memory_published"] is False


def test_real_document_extraction_manual_record_captures_boundaries() -> None:
    record = MANUAL_RECORD.read_text(encoding="utf-8")

    assert "generated non-private DOCX" in record
    assert "AuthorizeLocalDocumentFileForSource" in record
    assert "RunConfiguredLocalDocumentTextExtractorForSource" in record
    assert "source_content_read" in record
    assert "pending_review" in record
    assert "auto_promote_allowed" in record
    assert "memory_atoms_written: 0" in record
    assert "staging_atoms_written: 0" in record
    assert "memory_publications_written: 0" in record
    assert "user_private_file_read: false" in record
    assert "os_path_in_source_contract: false" in record
