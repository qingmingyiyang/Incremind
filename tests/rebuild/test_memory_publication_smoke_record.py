from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)
SCRIPT = ROOT / "work" / "scripts" / "run_memory_publication_smoke.py"
RECORD = ROOT / "docs" / "validation" / "manual-memory-publication-smoke.md"


def test_memory_publication_smoke_runs_review_publish_and_rollback(tmp_path: Path) -> None:
    output_dir = tmp_path / "memory-publication-smoke"
    record = tmp_path / "manual-memory-publication-smoke.md"

    subprocess.run(
        [str(PYTHON), str(SCRIPT), "--output-dir", str(output_dir), "--record", str(record)],
        cwd=ROOT,
        check=True,
    )
    report = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))

    assert report["status"] == "passed"
    assert report["initial"]["candidate_status"] == "pending_review"
    assert report["initial"]["auto_promote_allowed"] is False
    assert report["initial"]["staging_atoms_written"] == 0
    assert report["initial"]["memory_atoms_written"] == 0
    assert report["review"]["status"] == "promoted"
    assert report["review"]["memory_publication_state"] == "staging_atom_created_not_published"
    assert report["publication"]["status"] == "published"
    assert report["publication"]["memory_publication_state"] == "published_with_rollback_ref"
    assert report["rollback"]["status"] == "rolled_back"
    assert report["rollback"]["memory_publication_state"] == "rolled_back_not_published"
    assert report["publication_record_status_after_rollback"] == "rolled_back"
    assert report["final"]["candidate_status"] == "promoted"
    assert report["final"]["staging_atoms_written"] == 0
    assert report["final"]["memory_atoms_written"] == 0
    assert report["final"]["memory_publications_written"] == 1
    assert report["final"]["memory_transitions_written"] == 2
    assert report["final"]["legacy_library_written"] is False


def test_memory_publication_smoke_record_documents_boundaries() -> None:
    record = RECORD.read_text(encoding="utf-8")

    assert "status: passed" in record
    assert "review_status: promoted" in record
    assert "publication_status: published" in record
    assert "rollback_status: rolled_back" in record
    assert "final_memory_atoms_written: 0" in record
    assert "final_memory_publications_written: 1" in record
    assert "final_memory_transitions_written: 2" in record
    assert "No Provider was executed" in record
    assert "No user file was read" in record
    assert "Review created only a staging atom" in record
