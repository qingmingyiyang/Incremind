from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_model_planner_uses_kernel_frozen_snapshot_contract() -> None:
    source = (ROOT / "src/core/ai_kernel/model_planner.py").read_text(encoding="utf-8")
    assert "from backend.model_routing_snapshot" not in source
    assert "validate_planner_routing_snapshot" in source
    assert "planner_routing_snapshot_revision" in source


def test_session_placement_uses_injected_core_lock_port() -> None:
    source = (ROOT / "src/core/product_core/session_placement.py").read_text(encoding="utf-8")
    assert "backend.shared.interprocess_lock" not in source
    assert "FileAuthorityLockPort" in source
    assert "file_authority_lock: FileAuthorityLockPort" in source
