from __future__ import annotations

from pathlib import Path

from core.capability_packages.timeline_preview import timeline_preview
from core.context_graph import CapabilityPackageLoader


def test_timeline_preview_is_discovered_and_remains_read_only() -> None:
    package = Path(__file__).resolve().parents[3] / "src/core/capability_packages/timeline_preview"
    manifest = CapabilityPackageLoader().load_manifest(package / "manifest.json")

    assert manifest.capability_id == "timeline_preview"
    assert manifest.capability_revision == "2.0.0"
    assert manifest.contributions == ("read_only_preview",)
    source = [
        {"ref": "crp://event/b", "title": "B", "occurred_at": "2026-08-29T02:00:00Z"},
        {"ref": "crp://event/a", "title": "A", "occurred_at": "2026-08-29T01:00:00Z"},
    ]
    assert timeline_preview(source) == (
        {"ref": "crp://event/a", "title": "A", "occurred_at": "2026-08-29T01:00:00Z"},
        {"ref": "crp://event/b", "title": "B", "occurred_at": "2026-08-29T02:00:00Z"},
    )
    assert source[0]["ref"] == "crp://event/b"
