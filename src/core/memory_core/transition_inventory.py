from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.storage_provider import ObjectStorePathError, read_json_object_store_collection


@dataclass(frozen=True, slots=True)
class MemoryTransitionInventoryItem:
    object_id: str
    classification: str
    reason: str


@dataclass(frozen=True, slots=True)
class MemoryTransitionInventory:
    items: tuple[MemoryTransitionInventoryItem, ...]

    @property
    def is_migratable(self) -> bool:
        return all(item.classification == "canonical_complete" for item in self.items)


class MemoryTransitionInventoryError(ValueError):
    pass


def scan_memory_transition_inventory(root: Path, *, namespace_id: str = "default") -> MemoryTransitionInventory:
    try:
        transitions = read_json_object_store_collection(root, namespace_id=namespace_id, collection="memory_transitions")
        publications = read_json_object_store_collection(root, namespace_id=namespace_id, collection="memory_publications")
    except ObjectStorePathError as exc:
        raise MemoryTransitionInventoryError("memory trust audit JSON layout is invalid") from exc
    refs = _referenced_transition_ids(publications, namespace_id)
    items = []
    for record in transitions:
        payload = record.payload
        payload_id = payload.get("id")
        if not isinstance(payload_id, str) or not payload_id:
            items.append(MemoryTransitionInventoryItem(record.object_id, "legacy_schema_incomplete", "payload id is missing"))
        elif payload_id != record.object_id:
            items.append(MemoryTransitionInventoryItem(record.object_id, "legacy_identity_mismatch", "payload id differs from logical id"))
        elif not _canonical(payload):
            items.append(MemoryTransitionInventoryItem(record.object_id, "legacy_schema_incomplete", "canonical fields are missing"))
        elif payload_id not in refs:
            items.append(MemoryTransitionInventoryItem(record.object_id, "canonical_ref_mismatch", "publication reference is missing"))
        else:
            items.append(MemoryTransitionInventoryItem(record.object_id, "canonical_complete", "canonical publication evidence is complete"))
    return MemoryTransitionInventory(tuple(sorted(items, key=lambda item: item.object_id)))


def _canonical(payload: Mapping[str, object]) -> bool:
    required = {"schema_version", "id", "object_type", "object_id", "transition_type", "from_trust_status", "to_trust_status", "from_revision", "to_revision", "actor", "reason", "evidence_refs", "created_at"}
    return required <= set(payload) and isinstance(payload.get("evidence_refs"), list) and bool(payload["evidence_refs"])


def _referenced_transition_ids(publications, namespace_id: str) -> set[str]:
    prefix = f"crp://{namespace_id}/memory-transitions/"
    result: set[str] = set()
    for record in publications:
        for field in ("transition_ref", "rollback_transition_ref"):
            value = record.payload.get(field)
            if isinstance(value, str) and value.startswith(prefix) and value.endswith(".json"):
                result.add(value[len(prefix):-5])
    return result
