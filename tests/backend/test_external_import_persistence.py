from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from backend.api.external_import_persistence import (
    ExternalImportPersistenceError,
    persist_external_import,
)
from core.product_core.external_import_framework import (
    ExternalImportResult,
    ImportedMemoryCandidate,
    SourceRecord,
    serialize_imported_memory_candidate,
    serialize_source_record,
)
from core.storage_provider import JsonObjectStore


def _result(*, occurred_at: str | None) -> ExternalImportResult:
    source = SourceRecord(
        source_id="external-source-1",
        source_type="conversation",
        source_role="user",
        title="Imported conversation",
        content="The user prefers concise answers.",
        source_ref="external://conversation/1",
        occurred_at=occurred_at,
    )
    candidate = ImportedMemoryCandidate(
        memory_id="external-candidate-1",
        layer="L1",
        type="fact",
        content="The user prefers concise answers.",
        summary="A stable response preference.",
        confidence=0.8,
        trust_level="medium",
        source_platform="generic",
        source_type="conversation",
        source_role="user",
        source_ref=source.source_ref,
        evidence_refs=(source.source_ref,),
        occurred_at=occurred_at,
    )
    return ExternalImportResult(
        import_batch_id="batch-1",
        platform="generic",
        sources=(source,),
        candidates=(candidate,),
        role_stats={"user": 1},
        trust_stats={"medium": 1},
        conflict_count=0,
        needs_review_count=1,
        high_trust_count=0,
        low_trust_count=0,
        summary="test",
    )


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library")


def test_persist_external_import_writes_dual_time_and_keeps_first_recorded_at(tmp_path) -> None:
    store = _store(tmp_path)
    result = _result(occurred_at="2025-01-02T03:04:05Z")

    first = persist_external_import(
        store=store,
        result=result,
        namespace_id="default",
        project_id="project-1",
        batch_id="batch-1",
        created_at="2026-09-01T08:00:00Z",
    )
    second = persist_external_import(
        store=store,
        result=result,
        namespace_id="default",
        project_id="project-1",
        batch_id="batch-2",
        created_at="2026-09-01T09:00:00Z",
    )

    source = first.sources[0]
    candidate = first.candidates[0]
    assert source["occurred_at"] == "2025-01-02T03:04:05Z"
    assert source["recorded_at"] == "2026-09-01T08:00:00Z"
    assert source["created_at"] == source["occurred_at"]
    assert source["observed_at"] == source["recorded_at"]
    schema = json.loads(
        (Path(__file__).parents[2] / "core-contracts" / "rebuild" / "source.schema.json")
        .read_text(encoding="utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(source)
    assert candidate["occurred_at"] == source["occurred_at"]
    assert candidate["recorded_at"] == source["recorded_at"]
    assert second.sources[0]["recorded_at"] == source["recorded_at"]
    assert second.skipped_candidates == 1


def test_persist_external_import_keeps_unknown_occurrence_as_null(tmp_path) -> None:
    persisted = persist_external_import(
        store=_store(tmp_path),
        result=_result(occurred_at=None),
        namespace_id="default",
        project_id="project-1",
        batch_id="batch-1",
        created_at="2026-09-01T08:00:00Z",
    )

    for record in (*persisted.sources, *persisted.candidates):
        assert record["occurred_at"] is None
        assert record["recorded_at"] == "2026-09-01T08:00:00Z"
        assert record["created_at"] == record["recorded_at"]
        assert record["observed_at"] == record["recorded_at"]


def test_external_import_drops_invalid_external_occurrence_and_rejects_invalid_local_clock(
    tmp_path,
) -> None:
    persisted = persist_external_import(
        store=_store(tmp_path),
        result=_result(occurred_at="not-a-time"),
        namespace_id="default",
        project_id="project-1",
        batch_id="batch-1",
        created_at="2026-09-01T08:00:00Z",
    )
    assert persisted.sources[0]["occurred_at"] is None
    assert persisted.candidates[0]["occurred_at"] is None

    with pytest.raises(ExternalImportPersistenceError, match="RFC 3339"):
        persist_external_import(
            store=_store(tmp_path / "invalid-clock"),
            result=_result(occurred_at=None),
            namespace_id="default",
            project_id="project-1",
            batch_id="batch-2",
            created_at="not-a-time",
        )


def test_external_import_serializers_read_legacy_time_aliases() -> None:
    source = SourceRecord(
        source_id="legacy-source",
        source_type="document",
        source_role="user",
        title="Legacy",
        content="content",
        source_ref="legacy://source",
        created_at="2024-01-01T00:00:00Z",
        observed_at="2024-01-02T00:00:00Z",
    )
    candidate = ImportedMemoryCandidate(
        memory_id="legacy-candidate",
        layer="L1",
        type="fact",
        content="content",
        summary="legacy",
        confidence=0.5,
        trust_level="medium",
        source_platform="legacy",
        source_type="document",
        source_role="user",
        source_ref=source.source_ref,
        evidence_refs=(source.source_ref,),
        created_at=source.created_at,
        observed_at=source.observed_at,
    )

    assert serialize_source_record(source)["occurred_at"] == source.created_at
    assert serialize_source_record(source)["recorded_at"] == source.observed_at
    assert serialize_imported_memory_candidate(candidate)["occurred_at"] == candidate.created_at
    assert serialize_imported_memory_candidate(candidate)["recorded_at"] == candidate.observed_at
