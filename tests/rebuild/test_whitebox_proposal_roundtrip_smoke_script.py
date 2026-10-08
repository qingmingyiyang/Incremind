from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "work" / "scripts" / "run_whitebox_proposal_roundtrip_smoke.py"


def test_whitebox_proposal_roundtrip_smoke_exports_reads_and_imports(tmp_path) -> None:
    output_dir = ROOT / "tmp" / "pytest-whitebox-proposal-roundtrip"
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
    assert result["proposal_import"]["status"] == "passed"
    assert result["proposal_import"]["proposal_count"] == 2
    assert result["proposal_import"]["imported_count"] == 2
    assert result["proposal_import"]["rejected_count"] == 0
    assert result["proposal_import"]["memory_candidate_count"] == 1
    assert result["proposal_import"]["draft_count"] == 1
    assert result["boundary"]["provider_called"] is False
    assert result["boundary"]["cookie_read"] is False
    assert result["boundary"]["automatic_memory_publication"] is False
