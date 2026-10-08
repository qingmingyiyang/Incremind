from __future__ import annotations

from core.memory_core.transition_inventory import scan_memory_transition_inventory
from core.storage_provider import JsonObjectStore


def _canonical(identifier: str) -> dict[str, object]:
    return {"schema_version": "1.0.0", "id": identifier, "object_type": "atom", "object_id": "atom-1", "transition_type": "confirm", "from_trust_status": "system_generated", "to_trust_status": "user_confirmed", "from_revision": 0, "to_revision": 1, "actor": "user", "reason": "test", "evidence_refs": [{"object_type": "atom", "object_id": "atom-1", "source_refs": [{"source_id": "source-1", "locator": "text:0"}]}], "created_at": "2026-07-12T00:00:00Z"}


def test_inventory_classifies_canonical_legacy_identity_and_reference_mismatches(tmp_path):
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    canonical = _canonical("transition-canonical")
    store.write("memory_transitions", "transition-canonical", canonical, expected_revision=0)
    store.write("memory_transitions", "transition-legacy", {"object_id": "atom-legacy"}, expected_revision=0)
    store.write("memory_transitions", "transition-name", _canonical("transition-other"), expected_revision=0)
    store.write("memory_transitions", "transition-unreferenced", _canonical("transition-unreferenced"), expected_revision=0)
    store.write("memory_publications", "publication-1", {"id": "publication-1", "transition_ref": "crp://default/memory-transitions/transition-canonical.json"}, expected_revision=0)
    inventory = scan_memory_transition_inventory(tmp_path / ".rebuild-data")
    assert [(item.object_id, item.classification) for item in inventory.items] == [("transition-canonical", "canonical_complete"), ("transition-legacy", "legacy_schema_incomplete"), ("transition-name", "legacy_identity_mismatch"), ("transition-unreferenced", "canonical_ref_mismatch")]
    assert inventory.is_migratable is False
