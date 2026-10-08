from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.product_core.memory_projection_repository import (
    ObjectStoreMemoryProjectionRepository,
)
from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshotPort,
    authority_snapshot_fingerprint,
)


@dataclass(frozen=True, slots=True)
class ProjectBrainProjectionStatus:
    project_id: str
    status: str
    label: str
    message: str
    source_label: str
    series_count: int
    source_count: int
    generated_at: str | None
    diagnostic_code: str

    def to_payload(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "status": self.status,
            "label": self.label,
            "message": self.message,
            "source": {
                "kind": "published_project_memory",
                "label": self.source_label,
                "series_count": self.series_count,
                "source_count": self.source_count,
            },
            "generated_at": self.generated_at,
            "diagnostic_code": self.diagnostic_code,
            "safety": {
                "read_only": True,
                "content_included": False,
                "source_ids_included": False,
                "locators_included": False,
                "paths_included": False,
                "urls_included": False,
                "business_writes_allowed": False,
            },
        }


def get_project_brain_projection_status(
    *,
    project_id: str,
    authority: MemoryProjectionAuthoritySnapshotPort,
    projections: ObjectStoreMemoryProjectionRepository,
) -> ProjectBrainProjectionStatus:
    clean_project_id = _required_text(project_id, "project_id")
    try:
        snapshot = authority.load(clean_project_id)
        fingerprint = authority_snapshot_fingerprint(snapshot)
        result = projections.load_current(
            project_id=clean_project_id,
            authority_identity=snapshot.authority_identity,
            authority_fingerprint=fingerprint,
        )
    except Exception:
        return _status(
            project_id=clean_project_id,
            status="unavailable",
            diagnostic_code="projection_status_unavailable",
        )
    if result.status != "fresh" or not isinstance(result.projection, Mapping):
        return _status(
            project_id=clean_project_id,
            status=_public_status(result.status),
            diagnostic_code=result.reason_code,
        )
    projection = result.projection
    r1_items = _mapping_sequence(projection.get("r1_items"))
    source_refs = {
        str(ref.get("source_id"))
        for item in r1_items
        for ref in _mapping_sequence(item.get("source_refs"))
        if ref.get("source_id")
    }
    generated_at = projection.get("generated_at")
    return ProjectBrainProjectionStatus(
        project_id=clean_project_id,
        status="ready",
        label="项目概况已准备",
        message="回答问题时可以先从项目系列概况开始，再按需要查看详细资料。",
        source_label="已发布项目记忆",
        series_count=len(r1_items),
        source_count=len(source_refs),
        generated_at=generated_at if isinstance(generated_at, str) else None,
        diagnostic_code="projection_fingerprint_current",
    )


def _public_status(value: str) -> str:
    return {
        "missing": "preparing",
        "rebuilding": "preparing",
        "stale": "needs_refresh",
        "failed": "needs_refresh",
        "corrupt": "unavailable",
    }.get(value, "unavailable")


def _status(
    *,
    project_id: str,
    status: str,
    diagnostic_code: str,
) -> ProjectBrainProjectionStatus:
    label, message = {
        "preparing": (
            "项目概况正在整理",
            "现有记忆仍可正常查看；完成一次项目问答后会在后台准备系列概况。",
        ),
        "needs_refresh": (
            "项目概况需要更新",
            "已发布记忆发生变化，回答会暂时使用当前权威资料并在后台更新概况。",
        ),
        "unavailable": (
            "项目概况暂不可用",
            "当前仍可逐层查看已发布记忆，系统不会使用损坏或无法核验的概况。",
        ),
    }[status]
    return ProjectBrainProjectionStatus(
        project_id=project_id,
        status=status,
        label=label,
        message=message,
        source_label="已发布项目记忆",
        series_count=0,
        source_count=0,
        generated_at=None,
        diagnostic_code=diagnostic_code,
    )


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()
