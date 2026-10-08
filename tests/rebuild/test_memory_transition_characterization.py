from __future__ import annotations

import json
from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository
from core.memory_core import ObjectStoreMemoryStore, build_manual_publication_context
from core.product_core import (
    ExportMemoryAssetPackage,
    GetProjectBrainOverview,
    PublishStagingAtomToMemory,
    SourceJobMemoryLoop,
)
from core.storage_provider import JsonObjectStore, read_json_object_store_collection
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _stage_atom(store: JsonObjectStore) -> dict[str, object]:
    atom = {
        "schema_version": "1.0.0",
        "id": "atom-transition-canonical-001",
        "source_id": "source-transition-canonical-001",
        "content": "Canonical transition fixture.",
        "atom_type": "fact",
        "tags": ["transition"],
        "confidence": 0.8,
        "source_refs": [
            {
                "source_id": "source-transition-canonical-001",
                "locator": "char:0-29",
                "quote": "Canonical transition fixture.",
            }
        ],
        "revision": 1,
        "created_at": "2026-07-12T00:00:00Z",
        "updated_at": "2026-07-12T00:00:00Z",
        "trust_status": "system_generated",
    }
    ObjectStoreMemoryStore(store).save_candidate(
        "atom",
        atom,
        publication_context=build_manual_publication_context(
            namespace_id="default",
            layer="atom",
            draft_id=str(atom["id"]),
            candidate_id="candidate-atom-transition-canonical-001",
            reviewed_at="2026-07-12T00:01:00Z",
            review_reason="Canonical transition characterization.",
            source_refs=atom["source_refs"],
            evidence_refs=atom["source_refs"],
        ),
    )
    return atom


def test_source_job_memory_loop_writes_legacy_transition_without_canonical_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    loop = SourceJobMemoryLoop(
        source_registrar=ObjectStoreSourceRegistrar(store),
        job_repository=ObjectStoreJobRepository(store),
        memory_reader=ObjectStoreMemoryStore(store),
        memory_writer=ObjectStoreMemoryStore(store),
    )

    result = loop.run_text(
        title="Legacy transition characterization",
        content="One source-loop atom produces one legacy transition.",
    )

    stored = read_json_object_store_collection(
        tmp_path / ".rebuild-data",
        namespace_id="default",
        collection="memory_transitions",
    )
    assert len(stored) == 1
    transition = stored[0]
    assert transition.object_id.startswith("transition-0001-")
    assert dict(transition.payload) == {
        "object_id": result.atom_id,
        "trust_status": "system_generated",
        "reason": "published by Phase 2 Source / Job / Memory loop",
    }
    errors = validate_contract_instance(
        "memory_transition.schema.json",
        _schema("memory_transition.schema.json"),
        transition.payload,
    )
    assert errors
    assert any("id" in error or "schema_version" in error for error in errors)

    brain = GetProjectBrainOverview(store).execute()
    assert all(change.memory_id != result.atom_id for change in brain.recent_changes)
    evidence_file = next(file for file in ExportMemoryAssetPackage(store).execute().files if file.logical_path == "evidence_graph.json")
    assert evidence_file.content["count"] == 1
    assert evidence_file.content["items"] == [dict(transition.payload)]


def test_memory_publication_writes_canonical_transition_with_project_brain_object_type_layer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    atom = _stage_atom(store)

    publication = PublishStagingAtomToMemory(store, now="2026-07-12T00:05:00Z").execute(
        atom_id=str(atom["id"]),
        confirm=True,
        reason="Characterize canonical publication transition.",
    )

    stored = read_json_object_store_collection(
        tmp_path / ".rebuild-data",
        namespace_id="default",
        collection="memory_transitions",
    )
    assert len(stored) == 1
    transition = stored[0]
    assert transition.object_id == publication.transition_id
    assert transition.payload["id"] == publication.transition_id
    assert validate_contract_instance(
        "memory_transition.schema.json",
        _schema("memory_transition.schema.json"),
        transition.payload,
    ) == []
    publication_record = store.read("memory_publications", publication.publication_id)
    assert publication_record is not None
    assert publication_record["transition_ref"] == (
        f"crp://default/memory-transitions/{publication.transition_id}.json"
    )

    brain = GetProjectBrainOverview(store).execute()
    change = next(change for change in brain.recent_changes if change.memory_id == atom["id"])
    assert change.change_type == "new"
    assert change.layer == "atom"
    evidence_file = next(file for file in ExportMemoryAssetPackage(store).execute().files if file.logical_path == "evidence_graph.json")
    assert evidence_file.content["items"] == [dict(transition.payload)]
