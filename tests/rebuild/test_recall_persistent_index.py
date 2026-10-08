from __future__ import annotations

from pathlib import Path

import pytest

from core.search_and_recall import (
    ObjectStoreRecallIndex,
    RecallIndexEntry,
    RecallQuery,
    RecallRepositoryError,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _query(*, project_id: str = "project-alpha", limit: int = 12) -> RecallQuery:
    return RecallQuery(
        text="project recall evidence",
        project_id=project_id,
        layers=("l3_project_skill", "l3_series_memory", "l2_scenario", "l1_atom"),
        allowed_trust_statuses=("trusted", "user_confirmed", "system_generated"),
        limit=limit,
    )


def test_object_store_recall_index_persists_manifest_and_reloads_hits(tmp_path: Path) -> None:
    index = ObjectStoreRecallIndex(_store(tmp_path))

    manifest = index.rebuild(
        (
            RecallIndexEntry(
                object_id="skill-alpha",
                project_id="project-alpha",
                layer="l3_project_skill",
                content="project skill recall evidence",
                source_refs=("source-alpha#char:0-20",),
                trust_status="user_confirmed",
                base_score=0.72,
            ),
            RecallIndexEntry(
                object_id="atom-alpha",
                project_id="project-alpha",
                layer="l1_atom",
                content="project recall evidence atom",
                source_refs=("source-alpha#char:20-60",),
                trust_status="system_generated",
                base_score=0.58,
            ),
            RecallIndexEntry(
                object_id="foreign-beta",
                project_id="project-beta",
                layer="l1_atom",
                content="project recall evidence atom",
                source_refs=("source-beta#char:0-20",),
                trust_status="trusted",
                base_score=1.0,
            ),
        ),
        source="unit-test",
        rebuilt_at="2026-06-30T19:00:00+08:00",
    )
    reloaded = ObjectStoreRecallIndex(_store(tmp_path))
    hits = reloaded.recall(_query())

    assert manifest == {
        "schema_version": "1.0.0",
        "id": "active",
        "backend_kind": "object_store_lexical",
        "source": "unit-test",
        "entry_count": 3,
        "project_ids": ["project-alpha", "project-beta"],
        "layers": ["l3_project_skill", "l1_atom"],
        "trust_statuses": ["user_confirmed", "system_generated", "trusted"],
        "vector": {
            "enabled": False,
            "provider": None,
            "dimension": None,
        },
        "rebuilt_at": "2026-06-30T19:00:00+08:00",
    }
    assert reloaded.manifest() == manifest
    assert [hit.object_id for hit in hits] == ["skill-alpha", "atom-alpha"]
    assert all(hit.object_id != "foreign-beta" for hit in hits)
    assert all(hit.source_refs for hit in hits)
    assert all(hit.trust_status in {"user_confirmed", "system_generated", "trusted"} for hit in hits)
    assert not (tmp_path / "library").exists()


def test_object_store_recall_index_rebuild_replaces_stale_entries(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    index = ObjectStoreRecallIndex(object_store)
    index.rebuild(
        (
            RecallIndexEntry(
                object_id="atom-old",
                project_id="project-alpha",
                layer="l1_atom",
                content="old evidence",
                source_refs=("source-alpha#old",),
                trust_status="trusted",
                base_score=0.6,
            ),
        ),
        source="initial",
        rebuilt_at="2026-06-30T19:01:00+08:00",
    )

    index.rebuild(
        (
            RecallIndexEntry(
                object_id="atom-new",
                project_id="project-alpha",
                layer="l1_atom",
                content="project recall evidence new",
                source_refs=("source-alpha#new",),
                trust_status="trusted",
                base_score=0.6,
            ),
        ),
        source="replacement",
        rebuilt_at="2026-06-30T19:02:00+08:00",
    )
    hits = ObjectStoreRecallIndex(_store(tmp_path)).recall(_query())

    assert [hit.object_id for hit in hits] == ["atom-new"]
    assert object_store.read("recall_index_entries", "atom-old") is None
    assert object_store.read("recall_index_entries", "atom-new") is not None


def test_object_store_recall_index_keeps_trust_layer_and_source_ref_guards(tmp_path: Path) -> None:
    index = ObjectStoreRecallIndex(_store(tmp_path))
    index.rebuild(
        (
            {
                "object_id": "trusted-alpha",
                "project_id": "project-alpha",
                "layer": "l1_atom",
                "content": "project recall evidence",
                "source_refs": ["source-alpha#trusted"],
                "trust_status": "trusted",
                "base_score": 0.6,
            },
            {
                "object_id": "untrusted-alpha",
                "project_id": "project-alpha",
                "layer": "l1_atom",
                "content": "project recall evidence",
                "source_refs": ["source-alpha#untrusted"],
                "trust_status": "imported_unverified",
                "base_score": 1.0,
            },
        ),
        source="guard-test",
        rebuilt_at="2026-06-30T19:03:00+08:00",
    )

    trusted_hits = index.recall(_query())
    atom_only = index.recall(
        RecallQuery(
            text="project recall evidence",
            project_id="project-alpha",
            layers=("l3_project_skill",),
            allowed_trust_statuses=("trusted",),
            limit=12,
        )
    )

    assert [hit.object_id for hit in trusted_hits] == ["trusted-alpha"]
    assert atom_only == ()


def test_object_store_recall_index_rejects_vector_and_unparseable_source_refs(tmp_path: Path) -> None:
    index = ObjectStoreRecallIndex(_store(tmp_path))

    with pytest.raises(RecallRepositoryError, match="vector"):
        index.rebuild(
            (),
            source="vector-test",
            vector_enabled=True,
        )

    with pytest.raises(RecallRepositoryError, match="source_id#locator"):
        index.rebuild(
            (
                RecallIndexEntry(
                    object_id="bad-source-ref",
                    project_id="project-alpha",
                    layer="l1_atom",
                    content="project recall evidence",
                    source_refs=("source-alpha-char-0-20",),
                    trust_status="trusted",
                    base_score=0.6,
                ),
            ),
            source="bad-source-ref-test",
        )

    assert not (tmp_path / "library").exists()
