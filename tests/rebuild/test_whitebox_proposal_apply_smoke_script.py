from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "work" / "scripts" / "run_whitebox_proposal_apply_smoke.py"


def test_whitebox_proposal_apply_smoke_applies_three_review_drafts(tmp_path) -> None:
    output_dir = ROOT / "tmp" / "pytest-whitebox-proposal-apply"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--output-dir", str(output_dir)],
        check=True,
        capture_output=True,
        text=True,
    )

    stdout = json.loads(completed.stdout)
    result = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
    assert stdout["status"] == "passed"
    assert result["status"] == "passed"
    assert result["reader"]["status"] == "passed"
    assert result["reader"]["external_agent_write_target"] == "proposal_only"
    assert result["reader"]["external_agent_requires_user_review"] is True
    assert result["reader"]["sensitive_marker_count"] == 0
    assert result["preview"]["preview_count"] == 3
    assert result["preview"]["read_only"] == {
        "document": True,
        "project_skill": True,
        "series": True,
    }
    assert result["preview"]["requires_user_confirmation"] == {
        "document": True,
        "project_skill": True,
        "series": True,
    }
    assert result["preview"]["target_kinds"] == {
        "document": "document",
        "project_skill": "project_skill",
        "series": "series_memory",
    }
    assert result["preview"]["current_revisions"] == {
        "document": 1,
        "project_skill": 1,
        "series": 1,
    }
    assert result["preview"]["proposed_revisions"] == {
        "document": 2,
        "project_skill": 1,
        "series": 2,
    }
    assert "markdown" in result["preview"]["changed_fields"]["document"]
    assert "style_preferences" in result["preview"]["changed_fields"]["project_skill"]
    assert "overview" in result["preview"]["changed_fields"]["series"]
    assert all(
        "apply_without_user_confirmation" in forbidden
        for forbidden in result["preview"]["forbidden_operations"].values()
    )
    assert all(
        "automatic_memory_publication" in forbidden
        for forbidden in result["preview"]["forbidden_operations"].values()
    )
    assert set(result["preview"]["memory_publication_states"].values()) == {"not_published"}
    assert result["apply"]["applied_count"] == 3
    assert result["apply"]["document_revision"] == 2
    assert result["apply"]["project_skill_revision"] == 2
    assert result["apply"]["series_memory_revision"] == 2
    assert result["apply"]["series_long_term_memory_written"] is True
    assert set(result["apply"]["memory_publication_states"].values()) == {"not_published"}
    assert sorted(result["store"]["review_draft_statuses"]) == ["applied", "applied", "candidate_created"]
    assert result["store"]["memory_publication_count"] == 2
    assert result["store"]["staging_series_memory_count"] == 0
    assert result["store"]["staging_project_skill_count"] == 0
    assert result["boundary"]["provider_called"] is False
    assert result["boundary"]["cookie_read"] is False
    assert result["boundary"]["remote_upload_performed"] is False
    assert result["boundary"]["automatic_memory_publication"] is False
