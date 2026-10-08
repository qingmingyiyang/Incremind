from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]


def test_api_bootstrap_does_not_eagerly_import_vector_database_stacks(tmp_path: Path) -> None:
    script = """
import json
import sys
import backend.api.bootstrap
print(json.dumps({
    'lancedb': 'lancedb' in sys.modules,
    'llama_index': 'llama_index' in sys.modules,
    'retrieval': 'backend.video_summary.infrastructure.agent_memory.retrieval' in sys.modules,
}))
"""
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "CHRIPTMAS_APP_ROOT": str(tmp_path),
    }
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert json.loads(completed.stdout) == {
        "lancedb": False,
        "llama_index": False,
        "retrieval": False,
    }
