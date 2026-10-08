from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.product_core.library_item_deletion import (
    DeleteLibraryItem,
    LibraryItemDeletionResult,
    _SUPPORTED_ITEM_TYPES,
)
from .ports import ObjectStorePort
from .tag_index import (
    MANUAL_REF_ORIGIN,
    build_manual_tag_ref,
    build_tag_index_record,
    merge_tag_index_refs,
    tag_index_id,
)


@dataclass(frozen=True, slots=True)
class BulkItemResult:
    item_id: str
    status: str
    error: str | None
    operation_id: str | None = None
    revision: int | None = None
    undo_expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class BulkLibraryItemActionResult:
    action: str
    status: str
    total: int
    succeeded: int
    failed: int
    results: tuple[BulkItemResult, ...]
    error: str | None


_SUPPORTED_ACTIONS = ("delete", "move_series", "move_project", "attach_tags")


class BulkLibraryItemAction:
    """对资料库条目执行批量操作。

    支持 4 种 action：
    - delete: 复用 DeleteLibraryItem，删除 source 主记录及衍生数据
    - move_series: 修改 source.metadata.series_assignment 的 series_id/series_name（轻量级，不触发 L3 候选生成）
    - move_project: 修改 source 顶层 project_id 字段
    - attach_tags: 追加 source.metadata.manual_tags，并同步 tag_index collection

    设计原则：
    - 每个 item 独立执行，单条失败不影响其他 item
    - 返回每个 item 的结果，前端可展示成功/失败明细
    - 仅支持 source 类型，其他类型返回 unsupported
    - 重复 item ID 只执行一次，结果按首次出现顺序返回
    - Source 更新使用读取 revision 的 CAS，并发冲突返回单项 conflict
    """

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-03T21:30:00+08:00",
    ) -> None:
        self._store = object_store
        self._namespace_id = namespace_id
        self._now = now
        self._deleter = DeleteLibraryItem(object_store)

    def execute(
        self,
        *,
        action: str,
        item_ids: Sequence[str],
        series_name: str | None = None,
        project_id: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> BulkLibraryItemActionResult:
        clean_action = (action or "").strip().lower()
        clean_ids = list(dict.fromkeys(
            _id for _id in ((_raw or "").strip() for _raw in item_ids) if _id
        ))

        if not clean_action:
            return BulkLibraryItemActionResult(
                action=clean_action, status="rejected", total=0, succeeded=0, failed=0,
                results=(), error="action is required",
            )
        if clean_action not in _SUPPORTED_ACTIONS:
            return BulkLibraryItemActionResult(
                action=clean_action, status="unsupported", total=0, succeeded=0, failed=0,
                results=(),
                error=f"action '{clean_action}' is not supported; supported: {', '.join(_SUPPORTED_ACTIONS)}",
            )
        if not clean_ids:
            return BulkLibraryItemActionResult(
                action=clean_action, status="rejected", total=0, succeeded=0, failed=0,
                results=(), error="item_ids must be a non-empty list",
            )

        # action 专属参数校验
        if clean_action == "move_series" and not (series_name or "").strip():
            return BulkLibraryItemActionResult(
                action=clean_action, status="rejected", total=len(clean_ids),
                succeeded=0, failed=len(clean_ids), results=(),
                error="series_name is required for move_series action",
            )
        if clean_action == "move_project" and not (project_id or "").strip():
            return BulkLibraryItemActionResult(
                action=clean_action, status="rejected", total=len(clean_ids),
                succeeded=0, failed=len(clean_ids), results=(),
                error="project_id is required for move_project action",
            )
        if clean_action == "attach_tags":
            clean_tags = list(dict.fromkeys(
                _t for _t in ((_raw or "").strip() for _raw in (tags or ())) if _t
            ))
            if not clean_tags:
                return BulkLibraryItemActionResult(
                    action=clean_action, status="rejected", total=len(clean_ids),
                    succeeded=0, failed=len(clean_ids), results=(),
                    error="tags must be a non-empty list for attach_tags action",
                )
        else:
            clean_tags = []

        results: list[BulkItemResult] = []
        succeeded = 0
        failed = 0

        for item_id in clean_ids:
            deletion: LibraryItemDeletionResult | None = None
            revision: int | None = None
            try:
                if clean_action == "delete":
                    deletion = self._do_delete(item_id)
                    status = "ok" if deletion.status == "deleted" else deletion.status
                    revision = deletion.revision
                elif clean_action == "move_series":
                    status, revision = self._do_move_series(item_id, (series_name or "").strip())
                elif clean_action == "move_project":
                    status, revision = self._do_move_project(item_id, (project_id or "").strip())
                else:  # attach_tags
                    status, revision = self._do_attach_tags(item_id, clean_tags)
            except Exception as exc:  # noqa: BLE001 - 批量场景下单条异常不应中断整体
                results.append(BulkItemResult(item_id=item_id, status="error", error=str(exc)))
                failed += 1
                continue

            if status == "ok":
                results.append(BulkItemResult(
                    item_id=item_id,
                    status=status,
                    error=None,
                    operation_id=deletion.operation_id if deletion is not None else None,
                    revision=revision,
                    undo_expires_at=deletion.undo_expires_at if deletion is not None else None,
                ))
                succeeded += 1
            else:
                error = "source revision changed" if status == "conflict" else status
                results.append(BulkItemResult(item_id=item_id, status=status, error=error, revision=revision))
                failed += 1

        overall = "completed" if failed == 0 else ("partial" if succeeded > 0 else "failed")
        return BulkLibraryItemActionResult(
            action=clean_action,
            status=overall,
            total=len(clean_ids),
            succeeded=succeeded,
            failed=failed,
            results=tuple(results),
            error=None,
        )

    def _do_delete(self, item_id: str) -> LibraryItemDeletionResult:
        return self._deleter.execute(item_type=_SUPPORTED_ITEM_TYPES[0], item_id=item_id)

    def _do_move_series(self, item_id: str, series_name: str) -> tuple[str, int | None]:
        source = self._read_source(item_id)
        if source is None:
            return "not_found", None
        series_id = _derive_series_id(series_name)
        metadata = dict(source.get("metadata") or {})
        series_assignment = dict(metadata.get("series_assignment") or {})
        revision: int | None = None
        # 目标状态已收敛时跳过 CAS 写，receipt 失败后的同请求重试不重复写 source。
        if not (
            series_assignment.get("series_name") == series_name
            and series_assignment.get("series_id") == series_id
            and series_assignment.get("status") == "assigned"
        ):
            series_assignment.update({
                "series_name": series_name,
                "series_id": series_id,
                "status": "assigned",
            })
            metadata["series_assignment"] = series_assignment
            updated = dict(source)
            updated["metadata"] = metadata
            status, revision = self._write_source_with_cas(item_id, updated)
            if status != "ok":
                return status, revision
        self._write_receipt(
            "library_series_moved", item_id,
            details={"series_name": series_name, "series_id": series_id},
        )
        return "ok", revision

    def _do_move_project(self, item_id: str, project_id: str) -> tuple[str, int | None]:
        source = self._read_source(item_id)
        if source is None:
            return "not_found", None
        revision: int | None = None
        if source.get("project_id") != project_id:
            updated = dict(source)
            updated["project_id"] = project_id
            status, revision = self._write_source_with_cas(item_id, updated)
            if status != "ok":
                return status, revision
        self._write_receipt(
            "library_project_moved", item_id,
            details={"project_id": project_id},
        )
        return "ok", revision

    def _do_attach_tags(self, item_id: str, tags: list[str]) -> tuple[str, int | None]:
        source = self._read_source(item_id)
        if source is None:
            return "not_found", None
        metadata = dict(source.get("metadata") or {})
        existing_tags = [str(tag) for tag in (metadata.get("manual_tags") or [])]
        merged_tags = list(existing_tags)
        for tag in tags:
            if tag not in merged_tags:
                merged_tags.append(tag)
        revision: int | None = None
        if merged_tags != existing_tags:
            updated = dict(source)
            metadata["manual_tags"] = merged_tags
            updated["metadata"] = metadata
            status, revision = self._write_source_with_cas(item_id, updated)
            if status != "ok":
                return status, revision

        # 从权威 manual_tags 收敛派生 tag_index；已有 ref 时跳过写入，
        # 因此同请求重试不会重复 tag、source ref 或计数。
        self._sync_tag_index(item_id, merged_tags)
        self._write_receipt(
            "library_tags_attached", item_id,
            details={"attached_tags": list(tags), "manual_tags": list(merged_tags)},
        )
        return "ok", revision

    def _write_source_with_cas(self, item_id: str, updated: Mapping[str, object]) -> tuple[str, int | None]:
        expected_revision = self._store.revision("sources", item_id)
        try:
            new_revision = self._store.write("sources", item_id, updated, expected_revision=expected_revision)
        except ValueError:
            current_revision = self._store.revision("sources", item_id)
            if current_revision == expected_revision:
                raise
            return "conflict", current_revision
        return "ok", new_revision

    def _read_source(self, item_id: str) -> Mapping[str, object] | None:
        try:
            source = self._store.read("sources", item_id)
        except Exception:  # noqa: BLE001 - read 失败视为不存在
            return None
        if not isinstance(source, Mapping):
            return None
        return source

    def _sync_tag_index(self, source_id: str, tags: Sequence[str]) -> None:
        # 从权威 manual_tags 收敛派生 tag_index；merge 语义为同 source 同 origin
        # 整体替换，因此同请求重试不会重复 ref 或计数。
        for tag in tags:
            index_id = tag_index_id(tag)
            try:
                existing = self._store.read("tag_index", index_id)
            except Exception:  # noqa: BLE001
                existing = None
            merged_refs = merge_tag_index_refs(
                existing,
                source_id=source_id,
                origin=MANUAL_REF_ORIGIN,
                new_refs=[build_manual_tag_ref(source_id)],
            )
            record = build_tag_index_record(
                index_id=index_id,
                tag=tag,
                refs=merged_refs,
                namespace_id=self._namespace_id,
                updated_at=self._now,
            )
            self._store.write("tag_index", index_id, record, expected_revision=None)

    def _write_receipt(
        self,
        event_type: str,
        source_id: str,
        *,
        details: Mapping[str, object],
    ) -> None:
        # 内部审计投影：确定性 event id 覆盖写，重试收敛不产生重复记录；
        # 写失败由 execute 的单条异常隔离兜底，item 报 error 后重试可补齐。
        event_id = f"event-{event_type.replace('_', '-')}-{source_id}"
        self._store.write(
            "activity_events",
            event_id,
            {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": event_type,
                "source_id": source_id,
                "status": "completed",
                "details": {
                    **details,
                    "source_revision": self._store.revision("sources", source_id),
                },
                "created_at": self._now,
                "ref": f"crp://{self._namespace_id}/activity/{event_id}.json",
            },
            expected_revision=None,
        )


def _derive_series_id(series_name: str) -> str:
    digest = hashlib.sha256(series_name.lower().encode("utf-8")).hexdigest()[:12]
    return f"series-{digest}"


def serialize_bulk_library_item_action_result(result: BulkLibraryItemActionResult) -> dict[str, object]:
    return {
        "action": result.action,
        "status": result.status,
        "total": result.total,
        "succeeded": result.succeeded,
        "failed": result.failed,
        "results": [
            {
                "item_id": r.item_id,
                "status": r.status,
                "error": r.error,
                "operation_id": r.operation_id,
                "revision": r.revision,
                "undo_expires_at": r.undo_expires_at,
            }
            for r in result.results
        ],
        "error": result.error,
    }
