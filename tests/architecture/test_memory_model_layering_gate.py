from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_published_memory_model_layering_is_explicit_and_recall_owned() -> None:
    """Keep L2/L3 out of automatic prompt injection without adding an index."""

    authority = (ROOT / "src/backend/api/published_project_memory_snapshot.py").read_text(
        encoding="utf-8"
    )
    resolver = (ROOT / "src/backend/api/ai_profile_resolvers.py").read_text(
        encoding="utf-8"
    )

    assert 'model_injected_manifest_kinds = frozenset({"project_skill", "memory_r1"})' in authority
    assert '"tool_recall_required"' in authority
    assert "not in PublishedProjectMemorySnapshotAuthority.model_injected_manifest_kinds" in resolver
    assert "memory.recall/drilldown tool" in resolver
