from __future__ import annotations

import sys

from tools.scripts import check_quality_baseline as quality


def _finding(*, row: int = 1) -> dict[str, object]:
    return {
        "path": "src/core/product_core/example.py",
        "code": "F401",
        "message": "unused import",
        "row": row,
        "column": 1,
    }


def test_quality_gate_rejects_new_finding(monkeypatch, tmp_path) -> None:
    baseline = tmp_path / "ruff-baseline.json"
    monkeypatch.setattr(quality, "BASELINE_PATH", baseline)
    monkeypatch.setattr(quality, "_findings", lambda: [_finding(), _finding(row=2)])
    quality._write_baseline([_finding()])
    monkeypatch.setattr(sys, "argv", ["check_quality_baseline.py"])

    assert quality.main() == 1


def test_quality_gate_accepts_resolved_baseline_finding(monkeypatch, tmp_path) -> None:
    baseline = tmp_path / "ruff-baseline.json"
    monkeypatch.setattr(quality, "BASELINE_PATH", baseline)
    quality._write_baseline([_finding(), _finding(row=2)])
    monkeypatch.setattr(quality, "_findings", lambda: [_finding()])
    monkeypatch.setattr(sys, "argv", ["check_quality_baseline.py"])

    assert quality.main() == 0
