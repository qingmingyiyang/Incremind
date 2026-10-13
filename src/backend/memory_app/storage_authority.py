"""通过唯一聚合工厂选择认识记录库，保留原装配入口。"""
from __future__ import annotations

from pathlib import Path

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    activate_empty_document_authority as _activate_empty_document_authority,
)
from core.storage_provider import SQLiteStructuredRecordStore


def resolve_recognition_document_store(runtime_root: Path) -> tuple[SQLiteStructuredRecordStore, str]:
    """兼容原装配入口，空库激活与选库只由同一个 Core 工厂决定。"""
    root = Path(runtime_root).resolve()
    store, settings = build_rebuild_object_store(root)
    return AggregateRepositoryFactory(
        runtime_root=root, namespace_id=settings.namespace_id, json_store=store
    ).recognition_record_store_resolution()
