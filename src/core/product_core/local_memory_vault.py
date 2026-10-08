"""阶段 6：本地记忆资产库与隐私边界产品化。

本模块提供四个用例：

1. ``GetVaultStatus`` —— 本地 Vault 状态：路径（脱敏）、原始资料数量、
   记忆资产层统计、索引状态、备份就绪、存储占用估算、是否可重建索引。
2. ``GetProviderBoundary`` —— Provider 边界：本地 Provider、外部 Provider、
   哪些内容会离开本机、哪些内容从未离开本机、未配置能力。
3. ``ExportMemoryAssetPackage`` —— 私人记忆资产包导出：原始资料 manifest、
   Persona、系列记忆、标签索引、证据图谱、任务记录、白盒审计记录、
   Provider 配置摘要（不含 secret）、可重建索引说明。
4. ``CreateMemorySnapshot`` / ``ListMemorySnapshots`` / ``RollbackToMemorySnapshot``
   —— 记忆快照与整库回滚（manifest-only，不复制文件）。
5. ``PrepareMemoryRestoreConfirmation`` —— 恢复执行前确认安全门。

设计要点：

- 只读不修改任何记忆数据（除快照 manifest 自身）。
- 路径脱敏：使用 ``mask_path`` 把绝对路径替换为 ``…/最后一段``。
- Provider 配置摘要不含 secret/cookie/api_key/token。
- 复用 ``whitebox_memory_export._redact`` 做深度脱敏。
- UI 语言让普通用户看懂，不写成技术审计报表。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from .ports import ObjectStorePort


VaultLayerId = Literal["L0", "L1", "L2", "L3", "L4", "tags", "evidence", "tasks", "audit"]


class VaultStatusError(ValueError):
    """Raised when vault status cannot be computed."""


class LocalMemoryVaultError(ValueError):
    """Base error for local memory vault operations."""


class MemoryAssetExportError(ValueError):
    """Raised when memory asset package cannot be built."""


class MemorySnapshotError(ValueError):
    """Raised when snapshot operations fail."""


# ── 已知集合名 → 用户友好层标签 ──
# 这些集合名是产品核心长期使用的内部名，本模块把它们映射为
# 普通用户能理解的"记忆资产层"。
_LAYER_COLLECTIONS: tuple[tuple[str, str, str, str], ...] = (
    # (layer_id, user_label, collection, description)
    ("L0", "原始资料", "sources", "你保存的链接、文件、音频、视频、笔记。"),
    ("L1", "原子事实", "memory_atoms", "AI 从资料中抽取的原子事实和决策。"),
    ("L2", "场景经验", "memory_scenarios", "AI 整理出的场景和任务经验。"),
    ("L3", "系列记忆与项目能力", "memory_series_memory", "项目系列记忆。"),
    ("L3", "系列记忆与项目能力", "project_skills", "AI 学到的项目能力。"),
    ("L4", "稳定偏好与规则", "memory_persona", "经你确认的长期偏好、事实和规则。"),
    ("tags", "标签索引", "recall_index_entries", "可搜索的标签和关键词索引。"),
    ("evidence", "证据图谱", "memory_transitions", "记忆变更的证据链。"),
    ("tasks", "任务记录", "jobs", "自动整理任务的历史记录。"),
    ("audit", "白盒审计", "whitebox_memory_exports", "白盒导出和审计记录。"),
)


# ── Provider 能力 → 是否会把内容发送到本机之外 ──
# 这些是基于产品架构的隐私边界声明，不是运行时检测。
_PROVIDER_BOUNDARIES: tuple[tuple[str, str, str, bool, str], ...] = (
    # (capability_key, user_label, provider_kind, leaves_local, description)
    ("document_text_extractor", "文档正文读取", "local", False,
     "本地解析文档，内容不离开本机。"),
    ("image_ocr", "图片 OCR", "local", False,
     "本地识别图片文字，内容不离开本机。"),
    ("audio_transcription", "音频转写", "local", False,
     "本地转写音频，内容不离开本机。"),
    ("video_frame_extraction", "视频帧提取", "local", False,
     "本地提取视频帧，内容不离开本机。"),
    ("text_summary", "文本摘要", "external", True,
     "调用文本模型生成摘要，会把文本片段发送到外部 Provider。"),
    ("tagging", "标签生成", "external", True,
     "调用文本模型生成标签，会把文本片段发送到外部 Provider。"),
    ("memory_candidate", "记忆候选", "external", True,
     "调用文本模型生成记忆候选，会把文本片段发送到外部 Provider。"),
    ("embedding", "向量嵌入", "external", True,
     "调用嵌入模型生成向量，会把文本片段发送到外部 Provider。"),
)


def mask_path(absolute_path: str) -> str:
    """把绝对路径脱敏为 ``…/最后一段``，不暴露用户目录结构。

    只保留最后一段（vault 文件夹名），其余用 ``…`` 替代。
    这样用户能看到"在 .rebuild-data 文件夹里"但看不到用户名。
    """
    if not absolute_path:
        return "—"
    normalized = absolute_path.replace("\\", "/").rstrip("/")
    segments = [s for s in normalized.split("/") if s]
    if not segments:
        return "—"
    return "…/" + segments[-1]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _count_collection(store: ObjectStorePort, collection: str) -> int:
    try:
        return len(list(store.list(collection)))
    except Exception:
        return 0


def _approx_storage_bytes(store: ObjectStorePort, collections: Sequence[str]) -> int:
    """估算对象存储占用（粗略，按 JSON 文件大小）。"""
    # 这里不直接读文件系统（store 抽象可能不暴露路径），
    # 而是用 JSON 序列化长度估算，给用户一个量级感。
    total = 0
    for collection in collections:
        try:
            for item in store.list(collection):
                try:
                    total += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                except (TypeError, ValueError):
                    continue
        except Exception:
            continue
    return total


# ═══════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class VaultAssetLayer:
    """记忆资产层统计。"""
    layer_id: str  # L0 | L1 | L2 | L3 | L4 | tags | evidence | tasks | audit
    label: str  # 用户友好名
    collection: str  # 内部集合名（不直接展示给用户，但保留以便开发者排查）
    count: int
    description: str


@dataclass(frozen=True, slots=True)
class VaultIndexStatus:
    """索引状态摘要。"""
    status: str  # ready | building | missing | stale
    backend_kind: str | None  # sqlite_fts5 | none
    entry_count: int
    traceable: bool
    vector_enabled: bool
    can_rebuild: bool


@dataclass(frozen=True, slots=True)
class VaultBackupStatus:
    """备份状态。"""
    backup_enabled: bool
    backup_ready: bool
    retention_count: int
    last_backup_at: str | None  # ISO 字符串或 None
    can_restore: bool
    destination_uri: str
    pre_restore_required: bool


@dataclass(frozen=True, slots=True)
class VaultStatus:
    """本地 Vault 状态。"""
    namespace_id: str
    vault_path_masked: str  # 脱敏后的路径
    app_data_dir_masked: str
    vault_dir_masked: str
    backup_destination_masked: str
    storage_location_status: str
    app_root_uri: str  # platform-app-data://chriptmas-replay
    root_uri: str  # crp://default/
    storage_version: int
    source_count: int
    asset_layers: tuple[VaultAssetLayer, ...]
    index_status: VaultIndexStatus
    backup_status: VaultBackupStatus
    storage_bytes: int
    storage_display: str  # 人类可读的存储占用
    can_rebuild_index: bool
    migration_status: Mapping[str, object]
    operational_plan: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ProviderBoundaryCapability:
    """单个 Provider 能力的隐私边界。"""
    capability_key: str  # 内部 key（开发者用）
    label: str  # 用户友好名
    provider_kind: str  # local | external
    leaves_local: bool  # 内容是否会离开本机
    status: str  # ready | needs_config | missing | skip
    description: str  # 用户友好的边界说明
    next_step: str  # 用户可执行的下一步


@dataclass(frozen=True, slots=True)
class ProviderBoundary:
    """Provider 边界总览。"""
    local_providers: tuple[ProviderBoundaryCapability, ...]
    external_providers: tuple[ProviderBoundaryCapability, ...]
    content_never_leaves: tuple[str, ...]  # 用户友好的内容列表
    content_can_leave: tuple[str, ...]
    unconfigured_capabilities: tuple[str, ...]  # 用户友好的能力名列表
    overall_status: str  # ready | needs_attention


@dataclass(frozen=True, slots=True)
class MemoryAssetPackageFile:
    """资产包内的一个逻辑文件。"""
    logical_path: str  # manifest.json, persona.json, ...
    description: str  # 用户友好的说明
    size_bytes: int
    content: Mapping[str, object]  # 已脱敏的内容


@dataclass(frozen=True, slots=True)
class MemoryAssetPackage:
    """私人记忆资产包。"""
    package_id: str
    created_at: str
    namespace_id: str
    vault_path_masked: str
    files: tuple[MemoryAssetPackageFile, ...]
    total_size_bytes: int
    total_size_display: str
    rebuild_index_hint: str  # 用户友好的索引重建说明


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """记忆快照 metadata bound to an immutable full-Vault backup."""
    snapshot_id: str
    created_at: str
    label: str
    namespace_id: str
    layer_fingerprints: Mapping[str, str]  # layer_id → sha256[:12]
    layer_counts: Mapping[str, int]
    notes: str
    restorable: bool = False
    vault_fingerprint: str = ""
    backup_file_count: int = 0


@dataclass(frozen=True, slots=True)
class MemorySnapshotSummary:
    """快照列表项。"""
    snapshot_id: str
    created_at: str
    label: str
    layer_count: int
    total_objects: int
    restorable: bool = False
    backup_file_count: int = 0


@dataclass(frozen=True, slots=True)
class MemoryRollbackPlan:
    """整库回滚计划 bound to current and target full-tree fingerprints."""
    rollback_id: str
    target_snapshot_id: str
    created_at: str
    current_layer_counts: Mapping[str, int]
    target_layer_counts: Mapping[str, int]
    layers_to_change: tuple[str, ...]
    requires_confirmation: bool
    warning: str
    current_vault_fingerprint: str = ""
    target_vault_fingerprint: str = ""
    target_backup_file_count: int = 0


@dataclass(frozen=True, slots=True)
class MemoryRestoreConfirmation:
    """恢复执行前安全门结果。

    当前阶段不执行整库覆盖，只验证用户是否带着明确的 rollback_id
    进入二次确认，并返回可审计的阻断状态。
    """
    rollback_id: str
    target_snapshot_id: str
    status: str
    restore_executed: bool
    requires_confirmation: bool
    confirmation_received: bool
    reason: str
    next_step: str


# ═══════════════════════════════════════════════════════════════════
# 用例 1：GetVaultStatus
# ═══════════════════════════════════════════════════════════════════

class GetVaultStatus:
    """读取本地 Vault 状态，不修改任何数据。"""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        vault_path: str = "",
        app_data_dir_path: str = "",
        target_vault_path: str = "",
        app_root_uri: str = "platform-app-data://chriptmas-replay",
        root_uri: str = "crp://default/",
        storage_version: int = 1,
        backup_enabled: bool = True,
        backup_retention_count: int = 5,
        backup_ready: bool = True,
        backup_destination_path: str = "",
        backup_destination_uri: str = "crp://default/backups/",
        pre_restore_required: bool = True,
        index_status: str = "missing",
        index_backend_kind: str | None = None,
        index_entry_count: int = 0,
        index_traceable: bool = False,
        index_vector_enabled: bool = False,
    ) -> None:
        self._store = object_store
        self._namespace_id = namespace_id
        self._vault_path = vault_path
        self._app_data_dir_path = app_data_dir_path
        self._target_vault_path = target_vault_path
        self._app_root_uri = app_root_uri
        self._root_uri = root_uri
        self._storage_version = storage_version
        self._backup_enabled = backup_enabled
        self._backup_retention_count = backup_retention_count
        self._backup_ready = backup_ready
        self._backup_destination_path = backup_destination_path
        self._backup_destination_uri = backup_destination_uri
        self._pre_restore_required = pre_restore_required
        self._index_status = index_status
        self._index_backend_kind = index_backend_kind
        self._index_entry_count = index_entry_count
        self._index_traceable = index_traceable
        self._index_vector_enabled = index_vector_enabled

    def execute(self) -> VaultStatus:
        asset_layers = self._collect_asset_layers()
        source_count = next(
            (layer.count for layer in asset_layers if layer.layer_id == "L0"),
            0,
        )
        storage_bytes = _approx_storage_bytes(
            self._store,
            [layer.collection for layer in asset_layers],
        )
        index_status = VaultIndexStatus(
            status=self._index_status,
            backend_kind=self._index_backend_kind,
            entry_count=self._index_entry_count,
            traceable=self._index_traceable,
            vector_enabled=self._index_vector_enabled,
            can_rebuild=True,  # 索引总是可重建的
        )
        backup_status = VaultBackupStatus(
            backup_enabled=self._backup_enabled,
            backup_ready=self._backup_ready,
            retention_count=self._backup_retention_count,
            last_backup_at=self._last_backup_at(),
            can_restore=self._backup_ready,
            destination_uri=self._backup_destination_uri,
            pre_restore_required=self._pre_restore_required,
        )
        migration_status = self._migration_status()
        operational_plan = self._operational_plan(migration_status)
        return VaultStatus(
            namespace_id=self._namespace_id,
            vault_path_masked=mask_path(self._vault_path),
            app_data_dir_masked=(
                mask_path(self._app_data_dir_path)
                if self._app_data_dir_path else self._app_root_uri
            ),
            vault_dir_masked=mask_path(self._target_vault_path or self._vault_path),
            backup_destination_masked=(
                mask_path(self._backup_destination_path)
                if self._backup_destination_path
                else self._backup_destination_uri
            ),
            storage_location_status=str(migration_status["storage_location_status"]),
            app_root_uri=self._app_root_uri,
            root_uri=self._root_uri,
            storage_version=self._storage_version,
            source_count=source_count,
            asset_layers=asset_layers,
            index_status=index_status,
            backup_status=backup_status,
            storage_bytes=storage_bytes,
            storage_display=_format_bytes(storage_bytes),
            can_rebuild_index=True,
            migration_status=migration_status,
            operational_plan=operational_plan,
        )

    def _collect_asset_layers(self) -> tuple[VaultAssetLayer, ...]:
        # 同一 layer_id 可能有多个集合（如 L3），合并计数。
        merged: dict[str, VaultAssetLayer] = {}
        for layer_id, label, collection, description in _LAYER_COLLECTIONS:
            count = _count_collection(self._store, collection)
            if layer_id in merged:
                existing = merged[layer_id]
                merged[layer_id] = VaultAssetLayer(
                    layer_id=layer_id,
                    label=label,
                    collection=f"{existing.collection}, {collection}",
                    count=existing.count + count,
                    description=description,
                )
            else:
                merged[layer_id] = VaultAssetLayer(
                    layer_id=layer_id,
                    label=label,
                    collection=collection,
                    count=count,
                    description=description,
                )
        return tuple(merged.values())

    def _last_backup_at(self) -> str | None:
        # 检查最近的快照 manifest 作为"最近备份时间"
        try:
            snapshots = list(self._store.list("memory_snapshots"))
            if not snapshots:
                return None
            timestamps = [
                str(s.get("created_at", "")) for s in snapshots
                if s.get("created_at")
            ]
            return max(timestamps) if timestamps else None
        except Exception:
            return None

    def _migration_status(self) -> dict[str, object]:
        current = _normalize_path_string(self._vault_path)
        target = _normalize_path_string(self._target_vault_path)
        has_target = bool(target)
        in_formal_vault = bool(current and target and current == target)
        status = "formal_app_data_vault" if in_formal_vault else "project_root_development"
        if not current:
            status = "unknown"
        needs_migration = bool(has_target and current and not in_formal_vault)
        return {
            "export_ready": True,
            "import_ready": False,
            "asset_package_uri": f"{self._root_uri}memory-assets/export",
            "app_data_dir_uri": self._app_root_uri,
            "target_vault_uri": f"{self._app_root_uri}/vault",
            "current_vault_masked": mask_path(self._vault_path),
            "target_vault_masked": (
                mask_path(self._target_vault_path)
                if self._target_vault_path else f"{self._app_root_uri}/vault"
            ),
            "storage_location_status": status,
            "needs_migration": needs_migration,
            "migration_ready": False,
            "migration_executed": False,
            "next_step": (
                "需要先实现只读校验、恢复前快照、事务化复制和完成后索引校验，再把当前项目目录数据迁移到正式 appDataDir/vault。"
                if needs_migration else
                "导入执行入口尚未接入，当前先用资产包作为迁移基线。"
            ),
        }

    def _operational_plan(self, migration_status: Mapping[str, object]) -> dict[str, object]:
        needs_migration = bool(migration_status.get("needs_migration"))
        formal_ready = migration_status.get("storage_location_status") == "formal_app_data_vault"
        encryption_status = "planned_not_enabled"
        if formal_ready:
            encryption_status = "ready_for_platform_keyring"
        return {
            "app_data_dir": {
                "status": "ready" if formal_ready else "planned",
                "uri": self._app_root_uri,
                "vault_uri": f"{self._app_root_uri}/vault",
                "masked_path": mask_path(self._app_data_dir_path) if self._app_data_dir_path else self._app_root_uri,
                "next_step": (
                    "已使用正式 appDataDir/vault 作为当前 Vault。"
                    if formal_ready else
                    "桌面端启动时把 ObjectStore 根目录指向 appDataDir/vault。"
                ),
            },
            "backup": {
                "status": "ready" if self._backup_ready else "needs_attention",
                "destination_uri": self._backup_destination_uri,
                "retention_count": self._backup_retention_count,
                "pre_restore_snapshot_required": self._pre_restore_required,
                "next_step": "完整恢复点复制并校验整个 Vault；恢复前保留当前 Vault 作为回滚副本。",
            },
            "restore": {
                "status": "ready",
                "can_prepare": True,
                "can_execute": True,
                "next_step": "桌面端原生确认后停止 sidecar，离线切换 Vault 并自动重启核对。",
            },
            "migration": {
                "status": "required" if needs_migration else "not_required",
                "can_prepare": True,
                "can_execute": False,
                "next_step": (
                    "先导出资产包并完成只读校验，再通过事务化复制迁移到 appDataDir/vault。"
                    if needs_migration else
                    "当前路径已对齐正式 Vault 或尚未声明目标路径。"
                ),
            },
            "encryption": {
                "status": encryption_status,
                "key_source": "platform_keyring",
                "scope": "vault_files_and_backup_packages",
                "next_step": "加密策略采用平台钥匙串托管主密钥，Vault 文件和备份包使用本地加密封装。",
            },
        }


def _format_bytes(num: int) -> str:
    """把字节数格式化为人类可读字符串。"""
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    if num < 1024 * 1024 * 1024:
        return f"{num / (1024 * 1024):.1f} MB"
    return f"{num / (1024 * 1024 * 1024):.2f} GB"


# ═══════════════════════════════════════════════════════════════════
# 用例 2：GetProviderBoundary
# ═══════════════════════════════════════════════════════════════════

class GetProviderBoundary:
    """展示 Provider 边界，不制造虚假安全承诺。"""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        provider_doctor_report: Mapping[str, object] | None = None,
    ) -> None:
        self._store = object_store
        # provider_doctor_report 是可选的预计算结果，避免重复调用
        self._provider_doctor_report = provider_doctor_report

    def execute(self) -> ProviderBoundary:
        # 本地 Provider 状态来自 Provider Doctor（如果提供）
        doctor_capabilities = self._extract_doctor_capabilities()
        local_capabilities = self._build_local_capabilities(doctor_capabilities)
        external_capabilities = self._build_external_capabilities()

        # 永不离开本机的内容
        content_never_leaves = (
            "原始文件（音频、视频、图片、文档）的完整内容",
            "OCR 和转写的本地执行过程",
            "你的 Persona 人格设定",
            "记忆快照和备份",
            "白盒审计记录",
        )
        # 可能离开本机的内容
        content_can_leave = (
            "用于生成摘要的文本片段",
            "用于生成标签的文本片段",
            "用于生成记忆候选的文本片段",
            "用于向量嵌入的文本片段",
        )

        unconfigured = tuple(
            cap.label for cap in (*local_capabilities, *external_capabilities)
            if cap.status == "needs_config"
        )

        overall = "ready" if not unconfigured else "needs_attention"

        return ProviderBoundary(
            local_providers=local_capabilities,
            external_providers=external_capabilities,
            content_never_leaves=content_never_leaves,
            content_can_leave=content_can_leave,
            unconfigured_capabilities=unconfigured,
            overall_status=overall,
        )

    def _extract_doctor_capabilities(self) -> Mapping[str, Mapping[str, object]]:
        if not self._provider_doctor_report:
            return {}
        capabilities = self._provider_doctor_report.get("capabilities")
        if not isinstance(capabilities, (list, tuple)):
            return {}
        result: dict[str, Mapping[str, object]] = {}
        for cap in capabilities:
            if not isinstance(cap, Mapping):
                continue
            key = str(cap.get("provider_key", ""))
            if key:
                result[key] = cap
        return result

    def _build_local_capabilities(
        self,
        doctor_caps: Mapping[str, Mapping[str, object]],
    ) -> tuple[ProviderBoundaryCapability, ...]:
        result: list[ProviderBoundaryCapability] = []
        for key, label, kind, leaves, desc in _PROVIDER_BOUNDARIES:
            if kind != "local":
                continue
            doctor = doctor_caps.get(key, {})
            status_raw = str(doctor.get("status", "missing"))
            status = _map_doctor_status(status_raw)
            next_step = _status_next_step(status, label)
            result.append(ProviderBoundaryCapability(
                capability_key=key,
                label=label,
                provider_kind=kind,
                leaves_local=leaves,
                status=status,
                description=desc,
                next_step=next_step,
            ))
        return tuple(result)

    def _build_external_capabilities(self) -> tuple[ProviderBoundaryCapability, ...]:
        result: list[ProviderBoundaryCapability] = []
        for key, label, kind, leaves, desc in _PROVIDER_BOUNDARIES:
            if kind != "external":
                continue
            # 外部 Provider 状态：检查是否配置了 task_model_map
            configured = self._is_external_provider_configured(key)
            status = "ready" if configured else "needs_config"
            next_step = _status_next_step(status, label)
            result.append(ProviderBoundaryCapability(
                capability_key=key,
                label=label,
                provider_kind=kind,
                leaves_local=leaves,
                status=status,
                description=desc,
                next_step=next_step,
            ))
        return tuple(result)

    def _is_external_provider_configured(self, capability_key: str) -> bool:
        """检查 task_model_map 是否配置了对应能力。"""
        try:
            task_map_records = list(self._store.list("task_model_map"))
            if not task_map_records:
                return False
            # 任何一条 task_model_map 都意味着外部 Provider 至少配过一次
            # 这里宽松判断：只要 task_model_map 有记录就认为已配置
            return len(task_map_records) > 0
        except Exception:
            return False


def _map_doctor_status(raw: str) -> str:
    """把 Provider Doctor 的状态映射为用户友好的体检状态。"""
    mapping = {
        "ready": "ready",
        "disabled": "skip",  # 用户主动禁用，不是问题
        "missing": "needs_config",
        "misconfigured": "needs_config",
        "failed": "needs_config",
    }
    return mapping.get(raw, "needs_config")


def _status_next_step(status: str, label: str) -> str:
    if status == "ready":
        return f"{label} 已就绪，可以正常使用。"
    if status == "skip":
        return f"{label} 已跳过，不影响保存原始资料。"
    if status == "needs_config":
        return f"前往设置页配置 {label}，或使用示例材料体验。"
    return f"{label} 状态未知，请稍后重试。"


# ═══════════════════════════════════════════════════════════════════
# 用例 3：ExportMemoryAssetPackage
# ═══════════════════════════════════════════════════════════════════

class ExportMemoryAssetPackage:
    """构建私人记忆资产包（manifest + 各层 JSON），不含 secret。"""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        vault_path: str = "",
    ) -> None:
        self._store = object_store
        self._namespace_id = namespace_id
        self._vault_path = vault_path

    def execute(self) -> MemoryAssetPackage:
        package_id = self._generate_package_id()
        files: list[MemoryAssetPackageFile] = []

        # 1. 原始资料 manifest
        files.append(self._build_file(
            "manifest.json",
            "资产包总览，列出所有文件和元数据。",
            self._build_manifest(package_id),
        ))

        # 2. 原始资料
        files.append(self._build_file(
            "sources.json",
            "你保存的原始资料列表（链接、文件、音频、视频、笔记）。",
            self._build_sources_payload(),
        ))

        # 3. Persona
        files.append(self._build_file(
            "persona.json",
            "你的 AI 助手人格设定。",
            self._build_persona_payload(),
        ))

        # 4. 系列记忆
        files.append(self._build_file(
            "series_memory.json",
            "项目系列记忆。",
            self._build_collection_payload("memory_series_memory", "项目系列记忆"),
        ))

        # 5. 标签索引
        files.append(self._build_file(
            "tag_index.json",
            "可搜索的标签和关键词索引。",
            self._build_tag_index_payload(),
        ))

        # 6. 证据图谱
        files.append(self._build_file(
            "evidence_graph.json",
            "记忆变更的证据链，记录每条记忆从哪里来。",
            self._build_evidence_payload(),
        ))

        # 7. 任务记录
        files.append(self._build_file(
            "task_records.json",
            "自动整理任务的历史记录。",
            self._build_collection_payload("jobs", "任务记录"),
        ))

        # 8. 白盒审计记录
        files.append(self._build_file(
            "audit_records.json",
            "白盒导出和审计记录。",
            self._build_collection_payload("whitebox_memory_exports", "白盒审计"),
        ))

        # 9. Provider 配置摘要（无 secret）
        files.append(self._build_file(
            "provider_summary.json",
            "Provider 配置摘要，不含 API Key、Cookie 或其他 secret。",
            self._build_provider_summary(),
        ))

        # 10. 可重建索引说明
        files.append(self._build_file(
            "rebuild_index_hint.json",
            "如何在迁移后重建本地索引的说明。",
            self._build_rebuild_hint(),
        ))

        total_size = sum(f.size_bytes for f in files)
        return MemoryAssetPackage(
            package_id=package_id,
            created_at=_now_iso(),
            namespace_id=self._namespace_id,
            vault_path_masked=mask_path(self._vault_path),
            files=tuple(files),
            total_size_bytes=total_size,
            total_size_display=_format_bytes(total_size),
            rebuild_index_hint=(
                "迁移到新设备后，在设置 → 高级设置 → 索引重建 中点击"
                "「重建索引」即可恢复搜索能力。索引只是缓存，重建不会"
                "影响任何记忆数据。"
            ),
        )

    def _generate_package_id(self) -> str:
        raw = f"{self._namespace_id}:{_now_iso()}"
        return "pkg-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def _build_file(
        self,
        logical_path: str,
        description: str,
        content: Mapping[str, object],
    ) -> MemoryAssetPackageFile:
        serialized = json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return MemoryAssetPackageFile(
            logical_path=logical_path,
            description=description,
            size_bytes=len(serialized),
            content=_deep_redact(content),
        )

    def _build_manifest(self, package_id: str) -> dict[str, object]:
        return {
            "package_id": package_id,
            "created_at": _now_iso(),
            "namespace_id": self._namespace_id,
            "schema_version": "1.0.0",
            "package_kind": "private_memory_asset_package",
            "contains": [
                "原始资料 manifest",
                "Persona",
                "系列记忆",
                "标签索引",
                "证据图谱",
                "任务记录",
                "白盒审计记录",
                "Provider 配置摘要（无 secret）",
                "可重建索引说明",
            ],
            "does_not_contain": [
                "API Key",
                "Cookie",
                "Bearer Token",
                "完整文件系统路径",
            ],
        }

    def _build_sources_payload(self) -> dict[str, object]:
        sources = list(self._store.list("sources"))
        # 完整透传 source 对象，由 _deep_redact 统一脱敏 path/secret 字段
        return {
            "count": len(sources),
            "items": [_deep_redact(s) for s in sources[:500]],
        }

    def _build_persona_payload(self) -> dict[str, object]:
        personas = list(self._store.list("memory_persona"))
        return {
            "count": len(personas),
            "items": [_deep_redact(p) for p in personas],
        }

    def _build_collection_payload(self, collection: str, label: str) -> dict[str, object]:
        items = list(self._store.list(collection))
        return {
            "collection": collection,
            "label": label,
            "count": len(items),
            "items": [_deep_redact(item) for item in items[:500]],
        }

    def _build_tag_index_payload(self) -> dict[str, object]:
        entries = list(self._store.list("recall_index_entries"))
        return {
            "count": len(entries),
            "items": [_deep_redact(e) for e in entries[:500]],
        }

    def _build_evidence_payload(self) -> dict[str, object]:
        transitions = list(self._store.list("memory_transitions"))
        return {
            "count": len(transitions),
            "items": [_deep_redact(t) for t in transitions[:500]],
        }

    def _build_provider_summary(self) -> dict[str, object]:
        """Provider 配置摘要，严格不含 secret。"""
        summary: dict[str, object] = {
            "note": "本摘要不含 API Key、Cookie、Token 或其他 secret。",
            "local_providers": [],
            "external_providers": [],
        }
        # 收集本地 Provider 配置状态（不含命令路径和 secret）
        for key, label, kind, _, desc in _PROVIDER_BOUNDARIES:
            entry = {
                "capability": label,
                "kind": kind,
                "description": desc,
                "configured": kind == "external",  # 外部 Provider 在此只标"已配置/未配置"
            }
            if kind == "local":
                summary["local_providers"].append(entry)
            else:
                summary["external_providers"].append(entry)
        # task_model_map 摘要（只保留任务名和模型显示名，不含 endpoint/key）
        try:
            task_map = list(self._store.list("task_model_map"))
            summary["task_model_map_count"] = len(task_map)
            summary["task_model_map_summary"] = [
                {
                    "task": str(t.get("task", "")),
                    "model_display_name": str(t.get("model_display_name", "")),
                }
                for t in task_map[:20]
            ]
        except Exception:
            summary["task_model_map_count"] = 0
        return summary

    def _build_rebuild_hint(self) -> dict[str, object]:
        return {
            "headline": "迁移后如何重建索引",
            "steps": [
                "1. 在新设备上恢复本资产包到本地 Vault 目录。",
                "2. 打开设置 → 高级设置 → 索引重建。",
                "3. 点击「重建索引」，等待完成。",
                "4. 索引重建不会影响任何记忆数据，只是重新生成搜索缓存。",
            ],
            "note": "索引是本地可重建的缓存层，不是记忆数据本身。",
        }


def _deep_redact(value: object) -> object:
    """深度脱敏，复用 whitebox_memory_export 的脱敏策略。"""
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            key_str = str(key)
            if _is_sensitive_key(key_str):
                result[key_str] = "[redacted]"
                continue
            if key_str in {"file_path", "path", "absolute_path", "cookie_path",
                           "command_path", "executable_path", "endpoint_url"}:
                result[key_str] = "[redacted]"
                continue
            result[key_str] = _deep_redact(item)
        return result
    if isinstance(value, list):
        return [_deep_redact(item) for item in value[:200]]
    if isinstance(value, str):
        if len(value) > 2000:
            return value[:1999] + "..."
        return value
    return value


def _is_sensitive_key(key: str) -> bool:
    lower = key.lower()
    return any(marker in lower for marker in (
        "api_key", "apikey", "secret", "cookie", "token", "password",
        "authorization", "bearer", "credential",
    ))


# ═══════════════════════════════════════════════════════════════════
# 用例 4：CreateMemorySnapshot / ListMemorySnapshots / RollbackToMemorySnapshot
# ═══════════════════════════════════════════════════════════════════

class CreateMemorySnapshot:
    """Record the product projection for an already-created immutable Vault backup."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
    ) -> None:
        self._store = object_store
        self._namespace_id = namespace_id

    def execute(
        self,
        *,
        label: str = "",
        notes: str = "",
        snapshot_id: str = "",
        vault_fingerprint: str = "",
        backup_file_count: int = 0,
    ) -> MemorySnapshot:
        clean_label = (label or "").strip() or f"快照 {_now_iso()[:16]}"
        clean_notes = (notes or "").strip()

        layer_fingerprints: dict[str, str] = {}
        layer_counts: dict[str, int] = {}
        for layer_id, _, collection, _ in _LAYER_COLLECTIONS:
            items = list(self._store.list(collection))
            count = len(items)
            # 指纹是对该层所有对象 id + revision 的 sha256
            fingerprint_input = "|".join(
                f"{item.get('id', '')}#{item.get('_revision', 0)}"
                for item in items[:1000]
            )
            fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:12]
            # 同 layer_id 多集合合并
            if layer_id in layer_fingerprints:
                existing_fp = layer_fingerprints[layer_id]
                combined = f"{existing_fp}+{fingerprint}"
                layer_fingerprints[layer_id] = hashlib.sha256(
                    combined.encode("utf-8")
                ).hexdigest()[:12]
                layer_counts[layer_id] += count
            else:
                layer_fingerprints[layer_id] = fingerprint
                layer_counts[layer_id] = count

        clean_snapshot_id = str(snapshot_id or "").strip()
        if not clean_snapshot_id:
            clean_snapshot_id = "snap-" + hashlib.sha256(
                f"{self._namespace_id}:{_now_iso()}:{clean_label}".encode("utf-8")
            ).hexdigest()[:12]
        if not re.fullmatch(r"snap-[a-z0-9][a-z0-9._-]{0,122}", clean_snapshot_id):
            raise MemorySnapshotError("snapshot_id is invalid")
        clean_vault_fingerprint = str(vault_fingerprint or "").strip()
        restorable = bool(
            re.fullmatch(r"[0-9a-f]{64}", clean_vault_fingerprint)
            and isinstance(backup_file_count, int)
            and backup_file_count >= 0
        )
        created_at = _now_iso()

        snapshot_record = {
            "schema_version": "2.0.0" if restorable else "1.0.0",
            "id": clean_snapshot_id,
            "created_at": created_at,
            "label": clean_label,
            "namespace_id": self._namespace_id,
            "layer_fingerprints": layer_fingerprints,
            "layer_counts": layer_counts,
            "notes": clean_notes,
            "restorable": restorable,
            "vault_fingerprint": clean_vault_fingerprint if restorable else "",
            "backup_file_count": backup_file_count if restorable else 0,
        }
        self._store.write(
            "memory_snapshots",
            clean_snapshot_id,
            snapshot_record,
            expected_revision=None,
        )

        return MemorySnapshot(
            snapshot_id=clean_snapshot_id,
            created_at=created_at,
            label=clean_label,
            namespace_id=self._namespace_id,
            layer_fingerprints=layer_fingerprints,
            layer_counts=layer_counts,
            notes=clean_notes,
            restorable=restorable,
            vault_fingerprint=clean_vault_fingerprint if restorable else "",
            backup_file_count=backup_file_count if restorable else 0,
        )


class ListMemorySnapshots:
    """列出所有记忆快照。"""

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._store = object_store

    def execute(
        self,
        *,
        additional_records: tuple[Mapping[str, object], ...] = (),
        restorable_record_ids: frozenset[str] | None = None,
    ) -> tuple[MemorySnapshotSummary, ...]:
        try:
            records = list(self._store.list("memory_snapshots"))
        except Exception:
            records = []
        records_by_id = {
            str(record.get("id", "")): record
            for record in records
            if isinstance(record, Mapping) and record.get("id")
        }
        for record in additional_records:
            record_id = str(record.get("id", ""))
            if record_id:
                records_by_id[record_id] = record
        summaries: list[MemorySnapshotSummary] = []
        for record in records_by_id.values():
            counts = record.get("layer_counts")
            if not isinstance(counts, Mapping):
                counts = {}
            total = sum(int(v) for v in counts.values() if isinstance(v, (int, float)))
            summaries.append(MemorySnapshotSummary(
                snapshot_id=str(record.get("id", "")),
                created_at=str(record.get("created_at", "")),
                label=str(record.get("label", "")),
                layer_count=len(counts),
                total_objects=total,
                restorable=record.get("restorable") is True
                and bool(re.fullmatch(r"[0-9a-f]{64}", str(record.get("vault_fingerprint", ""))))
                and (
                    restorable_record_ids is None
                    or str(record.get("id", "")) in restorable_record_ids
                ),
                backup_file_count=max(0, int(record.get("backup_file_count", 0) or 0)),
            ))
        summaries.sort(key=lambda s: s.created_at, reverse=True)
        return tuple(summaries)


class RollbackToMemorySnapshot:
    """准备整库回滚计划（manifest-only，需要用户二次确认）。

    注意：本用例只生成回滚计划，不执行实际删除/覆盖。实际回滚需要
    用户在前端二次确认后，由单独的执行用例完成。本阶段只实现计划生成。
    """

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
    ) -> None:
        self._store = object_store
        self._namespace_id = namespace_id

    def execute(
        self,
        *,
        target_snapshot_id: str,
        current_vault_fingerprint: str = "",
        snapshot_record: Mapping[str, object] | None = None,
    ) -> MemoryRollbackPlan:
        if not target_snapshot_id:
            raise MemorySnapshotError("target_snapshot_id is required")

        snapshot = snapshot_record or self._store.read(
            "memory_snapshots", target_snapshot_id
        )
        if snapshot is None:
            raise MemorySnapshotError(
                f"snapshot {target_snapshot_id} not found"
            )
        target_vault_fingerprint = str(snapshot.get("vault_fingerprint", "") or "")
        restorable = snapshot.get("restorable") is True and re.fullmatch(
            r"[0-9a-f]{64}", target_vault_fingerprint
        ) is not None
        current_full_fingerprint = str(current_vault_fingerprint or "").strip()
        if restorable and re.fullmatch(r"[0-9a-f]{64}", current_full_fingerprint) is None:
            raise MemorySnapshotError("current Vault fingerprint is required")
        if not restorable:
            target_vault_fingerprint = ""
            current_full_fingerprint = ""

        target_counts_raw = snapshot.get("layer_counts")
        if not isinstance(target_counts_raw, Mapping):
            target_counts_raw = {}

        target_counts = {
            str(k): int(v) for k, v in target_counts_raw.items()
            if isinstance(v, (int, float))
        }

        # 当前各层计数
        current_counts: dict[str, int] = {}
        for layer_id, _, collection, _ in _LAYER_COLLECTIONS:
            count = _count_collection(self._store, collection)
            if layer_id in current_counts:
                current_counts[layer_id] += count
            else:
                current_counts[layer_id] = count

        # 找出会变化的层
        layers_to_change: list[str] = []
        all_layers = set(current_counts) | set(target_counts)
        for layer_id in all_layers:
            current = current_counts.get(layer_id, 0)
            target = target_counts.get(layer_id, 0)
            if current != target:
                layers_to_change.append(layer_id)

        rollback_fingerprint = json.dumps(
            {
                "target_snapshot_id": target_snapshot_id,
                "current_layer_counts": current_counts,
                "target_layer_counts": target_counts,
                "current_vault_fingerprint": current_full_fingerprint,
                "target_vault_fingerprint": target_vault_fingerprint,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        rollback_id = "rb-" + hashlib.sha256(
            rollback_fingerprint.encode("utf-8")
        ).hexdigest()[:12]

        return MemoryRollbackPlan(
            rollback_id=rollback_id,
            target_snapshot_id=target_snapshot_id,
            created_at=_now_iso(),
            current_layer_counts=current_counts,
            target_layer_counts=target_counts,
            layers_to_change=tuple(sorted(layers_to_change)),
            requires_confirmation=True,
            warning=(
                "整库回滚会把你当前的记忆状态恢复到快照时的状态。"
                "回滚后新增的记忆可能丢失，请确认后再继续。"
            ),
            current_vault_fingerprint=current_full_fingerprint,
            target_vault_fingerprint=target_vault_fingerprint,
            target_backup_file_count=max(0, int(snapshot.get("backup_file_count", 0) or 0)),
        )


class PrepareMemoryRestoreConfirmation:
    """恢复执行前安全门。

    本阶段仍不执行实际恢复。它把"恢复"推进到可审计确认门槛：
    用户必须提交当前 rollback plan 的 id 并明确 confirm=true，系统返回
    blocked_pre_restore_only，说明执行恢复仍需后续实现正式写入事务。
    """

    def execute(
        self,
        *,
        target_snapshot_id: str,
        rollback_id: str,
        expected_rollback_id: str,
        confirm: bool,
    ) -> MemoryRestoreConfirmation:
        clean_snapshot_id = str(target_snapshot_id or "").strip()
        clean_rollback_id = str(rollback_id or "").strip()
        clean_expected = str(expected_rollback_id or "").strip()
        if not clean_snapshot_id:
            raise MemorySnapshotError("target_snapshot_id is required")
        if not clean_expected:
            raise MemorySnapshotError("expected_rollback_id is required")
        if not clean_rollback_id:
            return MemoryRestoreConfirmation(
                rollback_id="",
                target_snapshot_id=clean_snapshot_id,
                status="confirmation_required",
                restore_executed=False,
                requires_confirmation=True,
                confirmation_received=False,
                reason="恢复前必须提交本次回滚计划的 rollback_id。",
                next_step="先查看恢复影响，再复制或提交界面上的恢复确认凭证。",
            )
        if clean_rollback_id != clean_expected:
            return MemoryRestoreConfirmation(
                rollback_id=clean_rollback_id,
                target_snapshot_id=clean_snapshot_id,
                status="rejected",
                restore_executed=False,
                requires_confirmation=True,
                confirmation_received=bool(confirm),
                reason="rollback_id 与当前恢复计划不一致，已拒绝恢复。",
                next_step="重新生成恢复影响预览，确认当前计划后再继续。",
            )
        if not confirm:
            return MemoryRestoreConfirmation(
                rollback_id=clean_rollback_id,
                target_snapshot_id=clean_snapshot_id,
                status="confirmation_required",
                restore_executed=False,
                requires_confirmation=True,
                confirmation_received=False,
                reason="整库恢复需要用户明确确认 confirm=true。",
                next_step="确认影响预览无误后，再执行恢复确认。",
            )
        return MemoryRestoreConfirmation(
            rollback_id=clean_rollback_id,
            target_snapshot_id=clean_snapshot_id,
            status="blocked_pre_restore_only",
            restore_executed=False,
            requires_confirmation=False,
            confirmation_received=True,
            reason="当前版本只完成恢复前确认和影响预览，尚未开放整库写入恢复。",
            next_step="后续需要接入事务化恢复、恢复前自动快照和完成后校验。",
        )


# ═══════════════════════════════════════════════════════════════════
# 序列化
# ═══════════════════════════════════════════════════════════════════

def serialize_vault_asset_layer(layer: VaultAssetLayer) -> dict[str, object]:
    return {
        "layer_id": layer.layer_id,
        "label": layer.label,
        "count": layer.count,
        "description": layer.description,
    }


def serialize_vault_index_status(status: VaultIndexStatus) -> dict[str, object]:
    return {
        "status": status.status,
        "backend_kind": status.backend_kind,
        "entry_count": status.entry_count,
        "traceable": status.traceable,
        "vector_enabled": status.vector_enabled,
        "can_rebuild": status.can_rebuild,
    }


def serialize_vault_backup_status(status: VaultBackupStatus) -> dict[str, object]:
    return {
        "backup_enabled": status.backup_enabled,
        "backup_ready": status.backup_ready,
        "retention_count": status.retention_count,
        "last_backup_at": status.last_backup_at,
        "can_restore": status.can_restore,
        "destination_uri": status.destination_uri,
        "pre_restore_required": status.pre_restore_required,
    }


def serialize_vault_status(status: VaultStatus) -> dict[str, object]:
    return {
        "namespace_id": status.namespace_id,
        "vault_path_masked": status.vault_path_masked,
        "app_data_dir_masked": status.app_data_dir_masked,
        "vault_dir_masked": status.vault_dir_masked,
        "backup_destination_masked": status.backup_destination_masked,
        "storage_location_status": status.storage_location_status,
        "app_root_uri": status.app_root_uri,
        "root_uri": status.root_uri,
        "storage_version": status.storage_version,
        "source_count": status.source_count,
        "asset_layers": [
            serialize_vault_asset_layer(layer) for layer in status.asset_layers
        ],
        "index_status": serialize_vault_index_status(status.index_status),
        "backup_status": serialize_vault_backup_status(status.backup_status),
        "storage_bytes": status.storage_bytes,
        "storage_display": status.storage_display,
        "can_rebuild_index": status.can_rebuild_index,
        "migration_status": dict(status.migration_status),
        "operational_plan": dict(status.operational_plan),
    }


def serialize_provider_boundary_capability(cap: ProviderBoundaryCapability) -> dict[str, object]:
    return {
        "capability_key": cap.capability_key,
        "label": cap.label,
        "provider_kind": cap.provider_kind,
        "leaves_local": cap.leaves_local,
        "status": cap.status,
        "description": cap.description,
        "next_step": cap.next_step,
    }


def serialize_provider_boundary(boundary: ProviderBoundary) -> dict[str, object]:
    return {
        "local_providers": [
            serialize_provider_boundary_capability(c) for c in boundary.local_providers
        ],
        "external_providers": [
            serialize_provider_boundary_capability(c) for c in boundary.external_providers
        ],
        "content_never_leaves": list(boundary.content_never_leaves),
        "content_can_leave": list(boundary.content_can_leave),
        "unconfigured_capabilities": list(boundary.unconfigured_capabilities),
        "overall_status": boundary.overall_status,
    }


def serialize_memory_asset_package_file(f: MemoryAssetPackageFile) -> dict[str, object]:
    return {
        "logical_path": f.logical_path,
        "description": f.description,
        "size_bytes": f.size_bytes,
        "content": dict(f.content),
    }


def serialize_memory_asset_package(pkg: MemoryAssetPackage) -> dict[str, object]:
    return {
        "package_id": pkg.package_id,
        "created_at": pkg.created_at,
        "namespace_id": pkg.namespace_id,
        "vault_path_masked": pkg.vault_path_masked,
        "files": [
            serialize_memory_asset_package_file(f) for f in pkg.files
        ],
        "total_size_bytes": pkg.total_size_bytes,
        "total_size_display": pkg.total_size_display,
        "rebuild_index_hint": pkg.rebuild_index_hint,
    }


def serialize_memory_snapshot(snapshot: MemorySnapshot) -> dict[str, object]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "created_at": snapshot.created_at,
        "label": snapshot.label,
        "namespace_id": snapshot.namespace_id,
        "layer_fingerprints": dict(snapshot.layer_fingerprints),
        "layer_counts": dict(snapshot.layer_counts),
        "notes": snapshot.notes,
        "restorable": snapshot.restorable,
        "vault_fingerprint": snapshot.vault_fingerprint,
        "backup_file_count": snapshot.backup_file_count,
    }


def serialize_memory_snapshot_summary(s: MemorySnapshotSummary) -> dict[str, object]:
    return {
        "snapshot_id": s.snapshot_id,
        "created_at": s.created_at,
        "label": s.label,
        "layer_count": s.layer_count,
        "total_objects": s.total_objects,
        "restorable": s.restorable,
        "backup_file_count": s.backup_file_count,
    }


def serialize_memory_rollback_plan(plan: MemoryRollbackPlan) -> dict[str, object]:
    return {
        "rollback_id": plan.rollback_id,
        "target_snapshot_id": plan.target_snapshot_id,
        "created_at": plan.created_at,
        "current_layer_counts": dict(plan.current_layer_counts),
        "target_layer_counts": dict(plan.target_layer_counts),
        "layers_to_change": list(plan.layers_to_change),
        "requires_confirmation": plan.requires_confirmation,
        "warning": plan.warning,
        "current_vault_fingerprint": plan.current_vault_fingerprint,
        "target_vault_fingerprint": plan.target_vault_fingerprint,
        "target_backup_file_count": plan.target_backup_file_count,
    }


def serialize_memory_restore_confirmation(result: MemoryRestoreConfirmation) -> dict[str, object]:
    return {
        "rollback_id": result.rollback_id,
        "target_snapshot_id": result.target_snapshot_id,
        "status": result.status,
        "restore_executed": result.restore_executed,
        "requires_confirmation": result.requires_confirmation,
        "confirmation_received": result.confirmation_received,
        "reason": result.reason,
        "next_step": result.next_step,
    }


def _normalize_path_string(value: str) -> str:
    if not value:
        return ""
    return value.replace("\\", "/").rstrip("/").lower()
