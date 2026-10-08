from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal

from .backend_policy import RecallBackendSelection
from .ports import RecallIndexEntry


IndexFreshnessStatus = Literal["fresh", "stale", "missing", "degraded"]
RebuildReason = Literal["missing_manifest", "source_changed", "degraded_manifest"]


@dataclass(frozen=True, slots=True)
class IndexSourceRecord:
    source_id: str
    revision: int
    content_hash: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class IndexFreshness:
    status: IndexFreshnessStatus
    current_fingerprint: str
    indexed_fingerprint: str | None
    reason: RebuildReason | None


@dataclass(frozen=True, slots=True)
class IndexRebuildRequest:
    backend_kind: str
    reason: RebuildReason
    source_fingerprint: str
    source_count: int
    source_refs: tuple[str, ...]
    requested_at: str


class IndexLifecyclePolicyError(ValueError):
    """Raised when index lifecycle evaluation would lose traceability."""


def source_ledger_fingerprint(sources: Sequence[IndexSourceRecord | Mapping[str, object]]) -> str:
    normalized = [_source_record(source) for source in sources]
    payload = [
        {
            "source_id": source.source_id,
            "revision": source.revision,
            "content_hash": source.content_hash,
            "updated_at": source.updated_at,
        }
        for source in sorted(normalized, key=lambda item: item.source_id)
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def evaluate_index_freshness(
    manifest: Mapping[str, object] | None,
    sources: Sequence[IndexSourceRecord | Mapping[str, object]],
) -> IndexFreshness:
    current = source_ledger_fingerprint(sources)
    if manifest is None:
        return IndexFreshness("missing", current, None, "missing_manifest")
    indexed = manifest.get("source_fingerprint")
    backend_kind = manifest.get("backend_kind")
    if not isinstance(indexed, str) or not indexed:
        return IndexFreshness("degraded", current, None, "degraded_manifest")
    if not isinstance(backend_kind, str) or not backend_kind:
        return IndexFreshness("degraded", current, indexed, "degraded_manifest")
    if indexed != current:
        return IndexFreshness("stale", current, indexed, "source_changed")
    return IndexFreshness("fresh", current, indexed, None)


def create_index_rebuild_request(
    *,
    freshness: IndexFreshness,
    backend_selection: RecallBackendSelection,
    sources: Sequence[IndexSourceRecord | Mapping[str, object]],
    requested_at: str | None = None,
) -> IndexRebuildRequest:
    if freshness.status == "fresh":
        raise IndexLifecyclePolicyError("fresh index does not need rebuild")
    if freshness.reason is None:
        raise IndexLifecyclePolicyError("rebuild request requires reason")
    backend_kind = backend_selection.selected_backend or backend_selection.fallback_backend
    if backend_kind is None:
        raise IndexLifecyclePolicyError("rebuild request requires selected or fallback backend")
    normalized = tuple(_source_record(source) for source in sources)
    if not normalized:
        raise IndexLifecyclePolicyError("rebuild request requires source ledger")
    source_refs = tuple(f"{source.source_id}#rev:{source.revision}" for source in normalized)
    if any("#" not in ref for ref in source_refs):
        raise IndexLifecyclePolicyError("rebuild source refs must be traceable")
    return IndexRebuildRequest(
        backend_kind=backend_kind,
        reason=__cast_reason(freshness.reason),
        source_fingerprint=freshness.current_fingerprint,
        source_count=len(normalized),
        source_refs=source_refs,
        requested_at=requested_at or _utc_now(),
    )


def manifest_with_source_fingerprint(
    manifest: Mapping[str, object],
    sources: Sequence[IndexSourceRecord | Mapping[str, object]],
) -> dict[str, object]:
    payload = dict(manifest)
    payload["source_fingerprint"] = source_ledger_fingerprint(sources)
    payload["source_count"] = len(sources)
    return payload


def build_source_ledger_from_object_store(
    object_store: object,
    *,
    collection: str = "sources",
) -> tuple[IndexSourceRecord, ...]:
    """从 ObjectStore 读取 sources 构造 source ledger（用于 index freshness 评估）。

    验收3「新 source 入库后 index stale」的核心接入点：搜索路径和 rebuild 路径
    都用这个函数读当前 source ledger，保证 fingerprint 计算口径一致。

    MVP 简化：revision 固定为 1（sources 是 content-addressed，content_hash 是
    主指纹；revision 跟踪留待未来增强）。updated_at 取 source["updated_at"] 或
    fallback 到 source["created_at"]。

    跳过缺少 id / content_hash / created_at 的异常 source 记录，不让单条坏数据
    阻塞搜索路径的 staleness 评估。
    """
    listed = object_store.list(collection)
    records: list[IndexSourceRecord] = []
    for item in listed:
        if not isinstance(item, Mapping):
            continue
        source_id = item.get("id")
        content_hash = item.get("content_hash")
        updated_at = item.get("updated_at") or item.get("created_at")
        if not isinstance(source_id, str) or not source_id:
            continue
        if not isinstance(content_hash, str) or not content_hash:
            continue
        if not isinstance(updated_at, str) or not updated_at:
            continue
        records.append(IndexSourceRecord(source_id, 1, content_hash, updated_at))
    return tuple(records)


def build_recall_authority_ledger(
    entries: Sequence[RecallIndexEntry],
) -> tuple[IndexSourceRecord, ...]:
    """Fingerprint the exact current projection that will be written to Recall.

    The former source-only ledger could stay unchanged while a published
    Memory was added, withdrawn, or moved to SQLite. It could also retain a
    soft-deleted Source that the entry builder had already removed. Deriving
    the ledger from the final entries makes freshness, rebuild and search use
    one content set without storing indexed text in the manifest.
    """

    records: list[IndexSourceRecord] = []
    for entry in entries:
        identity = f"{entry.layer}:{entry.project_id}:{entry.object_id}"
        payload = {
            "object_id": entry.object_id,
            "project_id": entry.project_id,
            "layer": entry.layer,
            "content": entry.content,
            "source_refs": list(entry.source_refs),
            "trust_status": entry.trust_status,
            "base_score": entry.base_score,
        }
        content_hash = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        records.append(
            IndexSourceRecord(
                source_id=f"recall:{identity}",
                revision=1,
                content_hash=content_hash,
                updated_at="recall-authority-ledger-v1",
            )
        )
    return tuple(sorted(records, key=lambda record: record.source_id))


def _source_record(value: IndexSourceRecord | Mapping[str, object]) -> IndexSourceRecord:
    if isinstance(value, IndexSourceRecord):
        return value
    source_id = value.get("source_id") or value.get("id")
    revision = value.get("revision")
    content_hash = value.get("content_hash") or value.get("hash")
    updated_at = value.get("updated_at")
    if not isinstance(source_id, str) or not source_id:
        raise IndexLifecyclePolicyError("source record requires source_id")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise IndexLifecyclePolicyError("source record requires positive revision")
    if not isinstance(content_hash, str) or not content_hash:
        raise IndexLifecyclePolicyError("source record requires content_hash")
    if not isinstance(updated_at, str) or not updated_at:
        raise IndexLifecyclePolicyError("source record requires updated_at")
    return IndexSourceRecord(source_id, revision, content_hash, updated_at)


def __cast_reason(reason: str) -> RebuildReason:
    if reason not in {"missing_manifest", "source_changed", "degraded_manifest"}:
        raise IndexLifecyclePolicyError("unsupported rebuild reason")
    return reason  # type: ignore[return-value]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
