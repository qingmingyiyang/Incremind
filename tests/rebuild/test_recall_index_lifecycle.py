from __future__ import annotations

import pytest

from core.search_and_recall import (
    IndexLifecyclePolicyError,
    IndexSourceRecord,
    create_index_rebuild_request,
    evaluate_index_freshness,
    manifest_with_source_fingerprint,
    select_default_recall_backend_policy,
    source_ledger_fingerprint,
)


def _sources() -> tuple[IndexSourceRecord, ...]:
    return (
        IndexSourceRecord(
            source_id="source-alpha",
            revision=1,
            content_hash="sha256-alpha",
            updated_at="2026-07-01T00:20:00+08:00",
        ),
        IndexSourceRecord(
            source_id="source-beta",
            revision=2,
            content_hash="sha256-beta",
            updated_at="2026-07-01T00:21:00+08:00",
        ),
    )


def _manifest(sources: tuple[IndexSourceRecord, ...] | None = None) -> dict[str, object]:
    return manifest_with_source_fingerprint(
        {
            "schema_version": "1.0.0",
            "id": "active",
            "backend_kind": "object_store_lexical",
            "source": "unit-test",
            "entry_count": 2,
            "vector": {"enabled": False, "provider": None, "dimension": None},
            "rebuilt_at": "2026-07-01T00:22:00+08:00",
        },
        sources or _sources(),
    )


def test_index_freshness_is_fresh_when_manifest_matches_source_ledger() -> None:
    sources = _sources()
    manifest = _manifest(sources)

    freshness = evaluate_index_freshness(manifest, sources)

    assert freshness.status == "fresh"
    assert freshness.reason is None
    assert freshness.current_fingerprint == freshness.indexed_fingerprint


def test_index_freshness_is_stale_when_source_revision_changes() -> None:
    sources = _sources()
    changed = (
        sources[0],
        IndexSourceRecord(
            source_id="source-beta",
            revision=3,
            content_hash="sha256-beta-v2",
            updated_at="2026-07-01T00:25:00+08:00",
        ),
    )

    freshness = evaluate_index_freshness(_manifest(sources), changed)

    assert freshness.status == "stale"
    assert freshness.reason == "source_changed"
    assert freshness.current_fingerprint != freshness.indexed_fingerprint


def test_index_freshness_reports_missing_and_degraded_manifest() -> None:
    sources = _sources()

    missing = evaluate_index_freshness(None, sources)
    degraded = evaluate_index_freshness({"backend_kind": "object_store_lexical"}, sources)

    assert missing.status == "missing"
    assert missing.reason == "missing_manifest"
    assert degraded.status == "degraded"
    assert degraded.reason == "degraded_manifest"


def test_rebuild_request_records_backend_reason_fingerprint_and_traceable_refs() -> None:
    sources = _sources()
    changed = (
        sources[0],
        IndexSourceRecord(
            source_id="source-beta",
            revision=3,
            content_hash="sha256-beta-v2",
            updated_at="2026-07-01T00:25:00+08:00",
        ),
    )
    freshness = evaluate_index_freshness(_manifest(sources), changed)

    request = create_index_rebuild_request(
        freshness=freshness,
        backend_selection=select_default_recall_backend_policy(),
        sources=changed,
        requested_at="2026-07-01T00:26:00+08:00",
    )

    assert request.backend_kind == "sqlite_fts5"
    assert request.reason == "source_changed"
    assert request.source_fingerprint == source_ledger_fingerprint(changed)
    assert request.source_count == 2
    assert request.source_refs == ("source-alpha#rev:1", "source-beta#rev:3")
    assert request.requested_at == "2026-07-01T00:26:00+08:00"


def test_rebuild_request_rejects_fresh_index_and_empty_source_ledger() -> None:
    sources = _sources()
    fresh = evaluate_index_freshness(_manifest(sources), sources)
    missing = evaluate_index_freshness(None, sources)

    with pytest.raises(IndexLifecyclePolicyError, match="fresh index"):
        create_index_rebuild_request(
            freshness=fresh,
            backend_selection=select_default_recall_backend_policy(),
            sources=sources,
        )

    with pytest.raises(IndexLifecyclePolicyError, match="source ledger"):
        create_index_rebuild_request(
            freshness=missing,
            backend_selection=select_default_recall_backend_policy(),
            sources=(),
        )


def test_source_ledger_fingerprint_is_order_stable() -> None:
    sources = _sources()

    assert source_ledger_fingerprint(sources) == source_ledger_fingerprint(tuple(reversed(sources)))
