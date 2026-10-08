from __future__ import annotations

from pathlib import Path

import pytest

from core.search_and_recall import (
    IndexSourceRecord,
    ObjectStoreRecallIndex,
    RecallIndexEntry,
    RecallQuery,
    SqliteFts5DryRunError,
    SqliteFts5DryRunIndex,
    create_index_rebuild_request,
    create_sqlite_fts5_manifest,
    evaluate_index_freshness,
    select_default_recall_backend_policy,
    sqlite_fts5_manifest_payload,
    sqlite_fts5_verification_query,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _sources() -> tuple[IndexSourceRecord, ...]:
    return (
        IndexSourceRecord(
            source_id="source-alpha",
            revision=1,
            content_hash="sha256-alpha",
            updated_at="2026-07-01T02:00:00+08:00",
        ),
        IndexSourceRecord(
            source_id="source-beta",
            revision=2,
            content_hash="sha256-beta",
            updated_at="2026-07-01T02:01:00+08:00",
        ),
    )


def _candidate_manifest() -> dict[str, object]:
    sources = _sources()
    request = create_index_rebuild_request(
        freshness=evaluate_index_freshness(None, sources),
        backend_selection=select_default_recall_backend_policy(),
        sources=sources,
        requested_at="2026-07-01T02:02:00+08:00",
    )
    return sqlite_fts5_manifest_payload(
        create_sqlite_fts5_manifest(
            rebuild_request=request,
            backend_selection=select_default_recall_backend_policy(),
            created_at="2026-07-01T02:03:00+08:00",
        )
    )


def _entries() -> tuple[RecallIndexEntry, ...]:
    return (
        RecallIndexEntry(
            object_id="skill-alpha",
            project_id="project-alpha",
            layer="l3_project_skill",
            content="project recall evidence for sqlite fts5 dry run",
            source_refs=("source-alpha#char:0-25",),
            trust_status="user_confirmed",
            base_score=0.8,
        ),
        RecallIndexEntry(
            object_id="atom-alpha",
            project_id="project-alpha",
            layer="l1_atom",
            content="sqlite fts5 dry run evidence atom",
            source_refs=("source-alpha#char:26-70",),
            trust_status="system_generated",
            base_score=0.6,
        ),
        RecallIndexEntry(
            object_id="foreign-beta",
            project_id="project-beta",
            layer="l1_atom",
            content="project recall evidence for sqlite fts5 dry run",
            source_refs=("source-beta#char:0-30",),
            trust_status="trusted",
            base_score=1.0,
        ),
    )


def _query() -> RecallQuery:
    return RecallQuery(
        text="project recall evidence sqlite",
        project_id="project-alpha",
        layers=("l3_project_skill", "l1_atom"),
        allowed_trust_statuses=("user_confirmed", "system_generated"),
        limit=5,
    )


def test_sqlite_fts5_dry_run_builds_table_and_returns_traceable_hits(tmp_path: Path) -> None:
    database_path = tmp_path / ".sqlite-dry-run" / "recall_fts5.db"
    result = SqliteFts5DryRunIndex(database_path).rebuild_and_query(
        _entries(),
        manifest=_candidate_manifest(),
        query=_query(),
    )

    assert result.status == "ready"
    assert result.backend_kind == "sqlite_fts5"
    assert result.manifest_id == "sqlite_fts5_candidate"
    assert result.entry_count == 3
    assert result.hit_count == 2
    assert set(result.hit_object_ids) == {"skill-alpha", "atom-alpha"}
    assert result.vector_enabled is False
    assert "source-alpha#char:0-25" in result.source_refs
    assert database_path.exists()
    assert not (tmp_path / "library").exists()


def test_sqlite_fts5_verification_query_uses_complete_underscore_token() -> None:
    assert sqlite_fts5_verification_query(
        "  --- CP_B05_MEMORY_CANARY 用户明确选择的长期偏好"
    ) == "CP_B05_MEMORY_CANARY"


def test_sqlite_fts5_dry_run_does_not_replace_active_object_store_index(tmp_path: Path) -> None:
    active_index = ObjectStoreRecallIndex(_store(tmp_path))
    active_manifest = active_index.rebuild(
        (
            RecallIndexEntry(
                object_id="skill-active",
                project_id="project-alpha",
                layer="l3_project_skill",
                content="active project recall evidence",
                source_refs=("source-alpha#char:0-20",),
                trust_status="user_confirmed",
                base_score=0.8,
            ),
        ),
        source="active-object-store",
        rebuilt_at="2026-07-01T02:04:00+08:00",
    )

    dry_run = SqliteFts5DryRunIndex(tmp_path / ".sqlite-dry-run" / "recall_fts5.db").rebuild_and_query(
        _entries(),
        manifest=_candidate_manifest(),
        query=_query(),
    )
    active_hits = active_index.recall(
        RecallQuery(
            text="active project recall evidence",
            project_id="project-alpha",
            layers=("l3_project_skill",),
            allowed_trust_statuses=("user_confirmed",),
            limit=3,
        )
    )

    assert dry_run.backend_kind == "sqlite_fts5"
    assert active_index.manifest() == active_manifest
    assert active_index.manifest()["backend_kind"] == "object_store_lexical"
    assert [hit.object_id for hit in active_hits] == ["skill-active"]
    assert not (tmp_path / "library").exists()


def test_sqlite_fts5_dry_run_rejects_untraceable_source_refs(tmp_path: Path) -> None:
    with pytest.raises(SqliteFts5DryRunError, match="source_id#locator"):
        SqliteFts5DryRunIndex(tmp_path / ".sqlite-dry-run" / "recall_fts5.db").rebuild_and_query(
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
            manifest=_candidate_manifest(),
            query=_query(),
        )
