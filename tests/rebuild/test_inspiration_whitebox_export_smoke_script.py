from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "work" / "scripts" / "run_inspiration_whitebox_export_smoke.py"


def test_inspiration_whitebox_export_smoke_script_runs_end_to_end(tmp_path) -> None:
    output_dir = tmp_path / "smoke"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--output-dir", str(output_dir)],
        check=True,
        capture_output=True,
        text=True,
    )

    stdout = json.loads(completed.stdout)
    assert stdout["status"] == "passed"
    report = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
    assert report["content_read"]["status"] == "completed"
    assert report["structure"]["tag_count"] >= 1
    assert report["inspiration"]["status"] == "recorded"
    assert report["collision"]["prompt_count"] >= 1
    assert report["whitebox_export"]["inspiration_index_record_count"] >= 1
    assert report["whitebox_export"]["inspiration_index_collision_count"] >= 1
    assert report["whitebox_export"]["inspiration_index_write_target"] == "inspiration_collisions"
    assert report["whitebox_export"]["series_summaries_include_inspiration"] is True
    assert report["whitebox_export"]["tags_include_paragraph_tags"] is True
    assert report["whitebox_export"]["desktop_shell_status"] == "ready"
    assert report["whitebox_export"]["platform_adapter_status"] == "ready"
    assert report["privacy_boundary"]["external_model_provider_called"] is False
    assert report["privacy_boundary"]["long_term_memory_published"] is False
