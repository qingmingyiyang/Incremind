from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .ports import ObjectStorePort


@dataclass(frozen=True, slots=True)
class SourceActivityEvent:
    event_id: str
    type: str
    label: str
    summary: str
    revision: int | None
    created_at: str


@dataclass(frozen=True, slots=True)
class SourceActivityResult:
    status: str
    source_id: str
    events: tuple[SourceActivityEvent, ...]


# library 域审计事件白名单：确定性 event id 覆盖写语义下，
# 每种 type 只保留该 source 最新一条，读取端如实呈现"最近操作"。
_EVENT_LABELS: dict[str, str] = {
    "library_series_moved": "移动系列",
    "library_project_moved": "移动分组",
    "library_tags_attached": "添加标签",
    "library_source_edited": "编辑资料信息",
    "source_series_assigned": "确认系列归属",
}


class QuerySourceActivity:
    """读取一个 Source 的最近操作（activity_events 审计投影）。"""

    def __init__(self, store: ObjectStorePort) -> None:
        self._store = store

    def execute(self, *, source_id: str, limit: int = 20) -> SourceActivityResult:
        clean_id = (source_id or "").strip()
        if not clean_id:
            return SourceActivityResult("rejected", clean_id, ())
        if not isinstance(limit, int) or limit < 1:
            return SourceActivityResult("rejected", clean_id, ())
        try:
            source = self._store.read("sources", clean_id)
        except Exception:  # noqa: BLE001 - read 失败视为不存在
            source = None
        if not isinstance(source, Mapping):
            return SourceActivityResult("not_found", clean_id, ())

        events: list[SourceActivityEvent] = []
        try:
            records = self._store.list("activity_events")
        except Exception:  # noqa: BLE001 - 审计投影读取失败降级为空
            records = ()
        for record in records:
            if not isinstance(record, Mapping):
                continue
            if str(record.get("source_id") or "") != clean_id:
                continue
            event_type = str(record.get("type") or "")
            label = _EVENT_LABELS.get(event_type)
            if label is None:
                continue
            details = record.get("details") if isinstance(record.get("details"), Mapping) else {}
            revision = details.get("source_revision")
            events.append(SourceActivityEvent(
                event_id=str(record.get("id") or ""),
                type=event_type,
                label=label,
                summary=_summary(event_type, details),
                revision=revision if isinstance(revision, int) else None,
                created_at=str(record.get("created_at") or ""),
            ))
        events.sort(key=lambda event: (event.created_at, event.event_id), reverse=True)
        return SourceActivityResult("completed", clean_id, tuple(events[:limit]))


def _summary(event_type: str, details: Mapping[str, object]) -> str:
    series_name = str(details.get("series_name") or "").strip()
    project_id = str(details.get("project_id") or "").strip()
    attached = [str(tag) for tag in (details.get("attached_tags") or ()) if str(tag).strip()]
    if event_type == "library_series_moved":
        return f"系列调整为「{series_name}」" if series_name else "调整了所属系列"
    if event_type == "library_project_moved":
        return f"分组调整为「{project_id}」" if project_id else "调整了所属分组"
    if event_type == "library_tags_attached":
        return f"添加标签：{'、'.join(attached)}" if attached else "添加了标签"
    if event_type == "library_source_edited":
        title = str(details.get("title") or "").strip()
        return f"更新资料信息（{title}）" if title else "更新了资料信息"
    if event_type == "source_series_assigned":
        return f"系列归属确认：「{series_name}」" if series_name else "确认了系列归属"
    return "更新了资料"


def serialize_source_activity_result(result: SourceActivityResult) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "events": [
            {
                "event_id": event.event_id,
                "type": event.type,
                "label": event.label,
                "summary": event.summary,
                "revision": event.revision,
                "created_at": event.created_at,
            }
            for event in result.events
        ],
    }
