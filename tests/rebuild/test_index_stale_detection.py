"""3.14 验收第三条测试：新 source 入库后 index stale 能被检测。

验证：
- build_source_ledger_from_object_store 从 ObjectStore 读 sources 构造 ledger。
- LibrarySearchService 接收 source_ledger 后，_is_index_stale 调用 evaluate_index_freshness。
- 新 source 入库后（source ledger 变化），index_stale 返回 True。
- manifest 与 source ledger 匹配时，index_stale 返回 False。
- source_ledger 为空时，fallback 到旧逻辑（向后兼容）。
- 不泄露敏感字段。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.search_and_recall import (
    IndexSourceRecord,
    LibrarySearchService,
    ObjectStoreRecallIndex,
    build_recall_authority_ledger,
    build_recall_entries_from_object_store,
    build_source_ledger_from_object_store,
    evaluate_index_freshness,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _write_source(
    store: JsonObjectStore,
    *,
    source_id: str,
    content_hash: str,
    created_at: str = "2026-07-01T00:00:00+08:00",
) -> None:
    store.write(
        "sources",
        source_id,
        {
            "schema_version": "1.0.0",
            "id": source_id,
            "type": "text",
            "title": f"测试 {source_id}",
            "content_hash": content_hash,
            "created_at": created_at,
            "trust_status": "user_confirmed",
            "metadata": {},
        },
        expected_revision=None,
    )


def test_build_source_ledger_reads_sources_from_object_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, source_id="source-a", content_hash="hash-a")
    _write_source(store, source_id="source-b", content_hash="hash-b")

    ledger = build_source_ledger_from_object_store(store)

    assert len(ledger) == 2
    source_ids = {record.source_id for record in ledger}
    assert source_ids == {"source-a", "source-b"}
    for record in ledger:
        assert record.revision == 1  # MVP 简化：revision 固定为 1
        assert record.content_hash in {"hash-a", "hash-b"}
        assert isinstance(record.updated_at, str) and record.updated_at


def test_build_source_ledger_returns_empty_when_no_sources(tmp_path: Path) -> None:
    store = _store(tmp_path)

    ledger = build_source_ledger_from_object_store(store)

    assert ledger == ()


def test_build_source_ledger_skips_sources_missing_required_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, source_id="source-good", content_hash="hash-good")
    # 写一条缺 content_hash 的坏数据
    store.write(
        "sources",
        "source-bad",
        {
            "id": "source-bad",
            "type": "text",
            # 缺 content_hash
            "created_at": "2026-07-01T00:00:00+08:00",
        },
        expected_revision=None,
    )

    ledger = build_source_ledger_from_object_store(store)

    # 坏数据被跳过，只返回 good source
    assert len(ledger) == 1
    assert ledger[0].source_id == "source-good"


def test_recall_authority_ledger_changes_when_published_memory_changes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, source_id="source-a", content_hash="hash-a")
    before_entries = build_recall_entries_from_object_store(store)
    before = build_recall_authority_ledger(before_entries)
    store.write(
        "memory_atoms",
        "atom-recall-ledger",
        {
            "id": "atom-recall-ledger",
            "project_id": "default",
            "content": "已发布记忆霜鲸七号",
            "source_refs": [{"source_id": "message:user:ledger", "locator": "companion:message"}],
            "trust_status": "user_confirmed",
            "confidence": 0.9,
        },
        expected_revision=None,
    )

    after_entries = build_recall_entries_from_object_store(store)
    after = build_recall_authority_ledger(after_entries)

    assert len(after_entries) == len(before_entries) + 1
    assert _fingerprint(after) != _fingerprint(before)
    assert any(record.source_id.endswith(":atom-recall-ledger") for record in after)


def test_recall_authority_ledger_excludes_soft_deleted_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, source_id="source-deleted", content_hash="hash-deleted")
    before = build_recall_authority_ledger(build_recall_entries_from_object_store(store))
    current = store.read("sources", "source-deleted")
    assert current is not None
    store.write(
        "sources",
        "source-deleted",
        {
            **current,
            "library_lifecycle": {
                "status": "soft_deleted",
                "deleted_at": "2026-08-01T01:00:00+08:00",
            },
        },
        expected_revision=1,
    )

    after = build_recall_authority_ledger(build_recall_entries_from_object_store(store))

    assert len(before) == 1
    assert after == ()


def test_index_stale_true_when_manifest_is_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recall_index = ObjectStoreRecallIndex(store)
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=None,
        source_ledger=(),
    )

    assert service._is_index_stale() is True


def test_index_stale_true_when_source_ledger_changes_after_new_source(tmp_path: Path) -> None:
    """验收3：新 source 入库后 index stale。"""
    store = _store(tmp_path)
    # 初始：一个 source，manifest 匹配
    _write_source(store, source_id="source-a", content_hash="hash-a", created_at="2026-07-01T00:00:00+08:00")
    ledger = build_source_ledger_from_object_store(store)
    # 构建 manifest 指纹匹配当前 ledger
    manifest = {
        "schema_version": "1.0.0",
        "id": "active",
        "backend_kind": "sqlite_fts5",
        "status": "active",
        "source_fingerprint": _fingerprint(ledger),
        "source_count": len(ledger),
        "source_refs": [f"{r.source_id}#rev:{r.revision}" for r in ledger],
    }
    recall_index = ObjectStoreRecallIndex(store)
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=manifest,
        source_ledger=ledger,
    )
    assert service._is_index_stale() is False  # 初始 fresh

    # 新 source 入库
    _write_source(store, source_id="source-b", content_hash="hash-b", created_at="2026-07-02T00:00:00+08:00")
    new_ledger = build_source_ledger_from_object_store(store)
    service_after = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=manifest,  # manifest 没变
        source_ledger=new_ledger,  # ledger 变了
    )

    # 验收3：新 source 入库后 index stale
    assert service_after._is_index_stale() is True


def test_index_stale_false_when_manifest_matches_source_ledger(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, source_id="source-a", content_hash="hash-a", created_at="2026-07-01T00:00:00+08:00")
    ledger = build_source_ledger_from_object_store(store)
    manifest = {
        "schema_version": "1.0.0",
        "id": "active",
        "backend_kind": "sqlite_fts5",
        "status": "active",
        "source_fingerprint": _fingerprint(ledger),
        "source_count": len(ledger),
        "source_refs": [f"{r.source_id}#rev:{r.revision}" for r in ledger],
    }
    recall_index = ObjectStoreRecallIndex(store)
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=manifest,
        source_ledger=ledger,
    )

    assert service._is_index_stale() is False


def test_index_stale_when_current_authority_ledger_is_empty(tmp_path: Path) -> None:
    """An explicitly empty current ledger invalidates a non-empty active index."""
    store = _store(tmp_path)
    manifest = {
        "schema_version": "1.0.0",
        "id": "active",
        "backend_kind": "sqlite_fts5",
        "status": "active",
        "source_fingerprint": "some-fingerprint",
        "source_count": 5,
        "source_refs": ["source-a#rev:1"],
    }
    recall_index = ObjectStoreRecallIndex(store)
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=manifest,
        source_ledger=(),
    )

    assert service._is_index_stale() is True


def test_index_stale_falls_back_to_legacy_when_source_ledger_invalid(tmp_path: Path) -> None:
    """source_ledger 里有坏数据导致 evaluate_index_freshness 抛异常时 fallback 到旧逻辑。"""
    store = _store(tmp_path)
    manifest = {
        "schema_version": "1.0.0",
        "id": "active",
        "backend_kind": "sqlite_fts5",
        "status": "active",
        "source_fingerprint": "some-fingerprint",
        "source_count": 1,
        "source_refs": ["source-a#rev:1"],
    }
    recall_index = ObjectStoreRecallIndex(store)
    # 坏 ledger：revision 不是 int
    bad_ledger = ({"source_id": "source-a", "revision": "not-int", "content_hash": "hash", "updated_at": "2026-07-01"},)
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=manifest,
        source_ledger=bad_ledger,
    )

    # fallback 到旧逻辑：source_count > 0 → not stale
    assert service._is_index_stale() is False


def test_source_ledger_does_not_leak_sensitive_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, source_id="source-a", content_hash="hash-a")

    ledger = build_source_ledger_from_object_store(store)

    ledger_text = str(ledger).lower()
    for forbidden in ("sk-", "api_key", "cookie", "authorization", "bearer", "password", "token"):
        assert forbidden not in ledger_text, f"source ledger leaked: {forbidden}"


def _fingerprint(ledger: tuple[IndexSourceRecord, ...]) -> str:
    from core.search_and_recall import source_ledger_fingerprint

    return source_ledger_fingerprint(ledger)
