from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import stat


STORAGE_GOVERNANCE_VERSION = "storage-governance-v1"
_MAX_FILES = 250_000
_CATEGORY_ORDER = (
    "structured_database",
    "object_store",
    "derived_indexes",
    "runtime_support",
    "original_assets",
    "recovery_snapshots",
    "original_asset_backups",
    "operation_receipts",
)
_CATEGORY_METADATA = {
    "structured_database": (
        "结构化权威数据库",
        "protected_authority",
        "备份与恢复，不做目录级直接清理",
    ),
    "object_store": (
        "对象与版本记录",
        "source_retention_plan",
        "仅已删除 Source 可走引用检查、备份、确认和 receipt",
    ),
    "derived_indexes": (
        "可重建索引与投影",
        "rebuildable",
        "只允许经重建作业替换，不直接删除当前 generation",
    ),
    "runtime_support": (
        "运行状态与审计",
        "protected_runtime",
        "保持审计、幂等与恢复所需状态",
    ),
    "original_assets": (
        "原始文件资产",
        "original_asset_retention_plan",
        "仅孤立且超过保留期的资产可备份后清理",
    ),
    "recovery_snapshots": (
        "Vault 恢复点",
        "retention_count",
        "按配置保留最近恢复点，活跃操作引用的快照受保护",
    ),
    "original_asset_backups": (
        "原始文件字节备份",
        "restore_protected",
        "随对应恢复点生命周期治理",
    ),
    "operation_receipts": (
        "操作计划与回执",
        "audit_protected",
        "保留用于幂等恢复与审计",
    ),
}


class StorageGovernanceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StorageCategory:
    category_id: str
    byte_count: int
    file_count: int
    candidate_count: int = 0
    reclaimable_bytes: int = 0
    reclaimable_bytes_known: bool = True

    def to_payload(self) -> dict[str, object]:
        label, cleanup_mode, policy = _CATEGORY_METADATA[self.category_id]
        return {
            "category_id": self.category_id,
            "label": label,
            "byte_count": self.byte_count,
            "file_count": self.file_count,
            "candidate_count": self.candidate_count,
            "reclaimable_bytes": self.reclaimable_bytes,
            "reclaimable_bytes_known": self.reclaimable_bytes_known,
            "cleanup_mode": cleanup_mode,
            "policy": policy,
        }


@dataclass(frozen=True, slots=True)
class StorageGovernanceReport:
    categories: tuple[StorageCategory, ...]
    blockers: tuple[str, ...]
    source_trash_count: int
    source_purge_ready_count: int
    original_asset_candidate_count: int
    original_asset_reclaimable_bytes: int
    archived_document_count: int | None
    recovery_point_count: int
    backup_retention_count: int

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": STORAGE_GOVERNANCE_VERSION,
            "total_bytes": sum(item.byte_count for item in self.categories),
            "total_files": sum(item.file_count for item in self.categories),
            "known_reclaimable_bytes": sum(
                item.reclaimable_bytes for item in self.categories
            ),
            "categories": [item.to_payload() for item in self.categories],
            "lifecycle_summary": {
                "source_trash_count": self.source_trash_count,
                "source_purge_ready_count": self.source_purge_ready_count,
                "original_asset_candidate_count": (
                    self.original_asset_candidate_count
                ),
                "original_asset_reclaimable_bytes": (
                    self.original_asset_reclaimable_bytes
                ),
                "archived_document_count": self.archived_document_count,
                "recovery_point_count": self.recovery_point_count,
                "backup_retention_count": self.backup_retention_count,
            },
            "cleanup_capabilities": {
                "source": {
                    "trash_restore": True,
                    "plan_endpoint": (
                        "/api/rebuild/retention/source-purge/plan"
                    ),
                    "execute_endpoint": (
                        "/api/rebuild/retention/source-purge"
                    ),
                    "requires_backup": True,
                    "requires_confirmation": True,
                },
                "original_asset": {
                    "trash_restore": True,
                    "plan_endpoint": (
                        "/api/rebuild/retention/original-assets/plan"
                    ),
                    "execute_endpoint": (
                        "/api/rebuild/retention/original-assets"
                    ),
                    "requires_backup": True,
                    "requires_confirmation": True,
                },
                "document": {
                    "trash_restore": True,
                    "physical_cleanup": False,
                    "reason": (
                        "archived Documents remain recoverable until a "
                        "revision-safe purge contract exists"
                    ),
                },
                "memory_revision": {
                    "rollback": True,
                    "physical_cleanup": False,
                    "reason": (
                        "published Memory revisions remain protected by "
                        "publication receipts and hard-forget authority"
                    ),
                },
                "project_skill_revision": {
                    "rollback": True,
                    "physical_cleanup": False,
                    "reason": (
                        "Project Skill revisions remain protected by "
                        "publication history"
                    ),
                },
            },
            "blockers": list(self.blockers),
            "content_included": False,
            "paths_included": False,
            "writes_performed": False,
            "network_called": False,
        }


def build_storage_governance_report(
    *,
    active_vault_root: Path,
    recovery_root: Path,
    library_asset_root: Path,
    source_candidates: Sequence[Mapping[str, object]],
    original_asset_candidates: Sequence[Mapping[str, object]],
    archived_document_count: int | None,
    recovery_point_count: int,
    backup_retention_count: int,
) -> StorageGovernanceReport:
    if (
        archived_document_count is not None
        and (
            not isinstance(archived_document_count, int)
            or isinstance(archived_document_count, bool)
            or archived_document_count < 0
        )
    ):
        raise StorageGovernanceError(
            "archived document count is invalid"
        )
    for value, field in (
        (recovery_point_count, "recovery point count"),
        (backup_retention_count, "backup retention count"),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise StorageGovernanceError(f"{field} is invalid")
    source_items = _mappings(source_candidates, "source candidates")
    asset_items = _mappings(
        original_asset_candidates,
        "original asset candidates",
    )
    totals = {
        category_id: [0, 0]
        for category_id in _CATEGORY_ORDER
    }
    blockers: set[str] = set()
    _scan_root(
        root=active_vault_root,
        classify=_classify_active,
        totals=totals,
        blockers=blockers,
        root_code="active_vault",
    )
    _scan_root(
        root=recovery_root,
        classify=_classify_recovery,
        totals=totals,
        blockers=blockers,
        root_code="recovery",
    )
    _scan_root(
        root=library_asset_root,
        classify=lambda _relative: "original_assets",
        totals=totals,
        blockers=blockers,
        root_code="original_assets",
    )
    source_ready = sum(
        item.get("retention_elapsed") is True
        for item in source_items
    )
    asset_ready = [
        item for item in asset_items
        if item.get("retention_elapsed") is True
        and item.get("link_status") == "orphaned"
    ]
    reclaimable_assets = sum(
        _non_negative_int(item.get("byte_count"))
        for item in asset_ready
    )
    categories: list[StorageCategory] = []
    for category_id in _CATEGORY_ORDER:
        byte_count, file_count = totals[category_id]
        candidate_count = 0
        reclaimable_bytes = 0
        known = True
        if category_id == "object_store":
            candidate_count = source_ready
            known = source_ready == 0
        elif category_id == "original_assets":
            candidate_count = len(asset_ready)
            reclaimable_bytes = reclaimable_assets
        categories.append(
            StorageCategory(
                category_id=category_id,
                byte_count=byte_count,
                file_count=file_count,
                candidate_count=candidate_count,
                reclaimable_bytes=reclaimable_bytes,
                reclaimable_bytes_known=known,
            )
        )
    if archived_document_count is None:
        blockers.add("document_inventory_unavailable")
    return StorageGovernanceReport(
        categories=tuple(categories),
        blockers=tuple(sorted(blockers)),
        source_trash_count=len(source_items),
        source_purge_ready_count=source_ready,
        original_asset_candidate_count=len(asset_ready),
        original_asset_reclaimable_bytes=reclaimable_assets,
        archived_document_count=archived_document_count,
        recovery_point_count=recovery_point_count,
        backup_retention_count=backup_retention_count,
    )


def _scan_root(
    *,
    root: Path,
    classify,
    totals: dict[str, list[int]],
    blockers: set[str],
    root_code: str,
) -> None:
    lexical_root = Path(
        os.path.abspath(root.expanduser())
    )
    if not lexical_root.exists():
        return
    if (
        not lexical_root.is_dir()
        or _has_reparse_ancestor(lexical_root)
    ):
        blockers.add(f"{root_code}_root_unsafe")
        return
    root = lexical_root.resolve(strict=False)
    seen = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError:
            blockers.add(f"{root_code}_scan_failed")
            continue
        for entry in entries:
            seen += 1
            if seen > _MAX_FILES:
                blockers.add(f"{root_code}_scan_limit")
                return
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                blockers.add(f"{root_code}_entry_unreadable")
                continue
            if entry.is_symlink() or _stat_is_reparse(metadata):
                blockers.add(f"{root_code}_reparse_skipped")
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                blockers.add(f"{root_code}_special_file_skipped")
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                blockers.add(f"{root_code}_path_escape")
                continue
            category_id = classify(relative)
            totals[category_id][0] += max(0, int(metadata.st_size))
            totals[category_id][1] += 1


def _classify_active(relative: Path) -> str:
    parts = tuple(part.casefold() for part in relative.parts)
    name = relative.name.casefold()
    if parts and parts[0] == "objects":
        return "object_store"
    if (
        name.endswith((".sqlite", ".sqlite3", ".db", "-wal", "-shm"))
        or ".sqlite-" in name
        or ".db-" in name
    ):
        return "structured_database"
    if any(
        token in part
        for part in parts
        for token in ("projection", "index", "fts")
    ):
        return "derived_indexes"
    return "runtime_support"


def _classify_recovery(relative: Path) -> str:
    first = relative.parts[0].casefold() if relative.parts else ""
    if first == "snapshots":
        return "recovery_snapshots"
    if first == "original-asset-backups":
        return "original_asset_backups"
    return "operation_receipts"


def _is_reparse(path: Path) -> bool:
    try:
        return path.is_symlink() or _stat_is_reparse(
            path.stat(follow_symlinks=False)
        )
    except OSError:
        return True


def _has_reparse_ancestor(path: Path) -> bool:
    current = path
    while True:
        if _is_reparse(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _stat_is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _mappings(
    values: Sequence[Mapping[str, object]],
    field: str,
) -> tuple[Mapping[str, object], ...]:
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
        or len(values) > 100_000
        or not all(isinstance(item, Mapping) for item in values)
    ):
        raise StorageGovernanceError(f"{field} are invalid")
    return tuple(values)


def _non_negative_int(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        return 0
    return value
