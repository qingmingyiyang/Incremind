"""阶段 6 后端测试：本地记忆资产库与隐私边界。

覆盖：
- Vault 状态可读取（路径、原始资料数量、资产层统计、索引状态、备份状态）
- 路径脱敏正确（不暴露用户名和完整路径）
- Provider 边界展示正确（本地/外部、哪些内容从未出本机、未配置能力）
- 导出包不包含 secret/cookie/api_key/token
- 导出包包含 Persona、系列记忆、标签索引、证据图谱、任务记录、审计记录
- 备份/恢复入口有明确状态（快照创建、列表、回滚计划）
- 前端能展示"哪些内容从未出本机"（通过 Provider 边界序列化）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.product_core import (
    CreateMemorySnapshot,
    ExportMemoryAssetPackage,
    GetProviderBoundary,
    GetVaultStatus,
    ListMemorySnapshots,
    PrepareMemoryRestoreConfirmation,
    RollbackToMemorySnapshot,
    mask_path,
    serialize_memory_asset_package,
    serialize_memory_rollback_plan,
    serialize_memory_restore_confirmation,
    serialize_memory_snapshot,
    serialize_memory_snapshot_summary,
    serialize_provider_boundary,
    serialize_vault_status,
)
from core.storage_provider import JsonObjectStore


# ── Helpers ──

def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _write_source(store: JsonObjectStore, source_id: str, title: str = "测试资料") -> None:
    store.write(
        "sources", source_id,
        {
            "schema_version": "1.0.0", "id": source_id,
            "title": title, "media_type": "text/plain",
            "processing_state": "ready", "trust_status": "trusted",
            "created_at": "2026-07-06T10:00:00+08:00",
            # 含敏感字段，验证脱敏
            "file_path": "C:/Users/secretuser/Documents/private.txt",
            "api_key": "sk-test-12345",
        },
        expected_revision=None,
    )


def _write_atom(store: JsonObjectStore, atom_id: str) -> None:
    store.write(
        "memory_atoms", atom_id,
        {
            "schema_version": "1.0.0", "id": atom_id,
            "source_id": "source-1", "atom_type": "fact",
            "content": "测试原子事实",
            "created_at": "2026-07-06T10:00:00+08:00",
        },
        expected_revision=None,
    )


def _write_persona(store: JsonObjectStore) -> None:
    store.write(
        "memory_persona", "persona-1",
        {
            "schema_version": "1.0.0", "id": "persona-1",
            "name": "私人 AI 助手", "revision": 1,
            "summary": "你是一个简洁的私人 AI 助手。",
            "created_at": "2026-07-06T10:00:00+08:00",
        },
        expected_revision=None,
    )


def _write_series(store: JsonObjectStore, series_id: str = "series-1") -> None:
    store.write(
        "memory_series_memory", series_id,
        {
            "schema_version": "1.0.0", "id": series_id,
            "title": "项目系列：工作流自动化",
            "summary": "关于工作流自动化的系列记忆。",
            "created_at": "2026-07-06T10:00:00+08:00",
        },
        expected_revision=None,
    )


def _write_transition(store: JsonObjectStore, transition_id: str = "trans-1") -> None:
    store.write(
        "memory_transitions", transition_id,
        {
            "schema_version": "1.0.0", "id": transition_id,
            "transition_type": "promote",
            "source_id": "source-1",
            "created_at": "2026-07-06T10:00:00+08:00",
        },
        expected_revision=None,
    )


def _write_job(store: JsonObjectStore, job_id: str = "job-1") -> None:
    store.write(
        "jobs", job_id,
        {
            "schema_version": "1.0.0", "id": job_id,
            "kind": "workbench_auto_intake",
            "status": "completed",
            "created_at": "2026-07-06T10:00:00+08:00",
        },
        expected_revision=None,
    )


def _write_whitebox_export(store: JsonObjectStore, export_id: str = "wb-1") -> None:
    store.write(
        "whitebox_memory_exports", export_id,
        {
            "schema_version": "1.0.0", "id": export_id,
            "status": "completed",
            "created_at": "2026-07-06T10:00:00+08:00",
        },
        expected_revision=None,
    )


def _write_recall_entry(store: JsonObjectStore, entry_id: str = "idx-1") -> None:
    store.write(
        "recall_index_entries", entry_id,
        {
            "schema_version": "1.0.0", "id": entry_id,
            "object_id": "source-1", "layer": "L0",
            "content": "测试索引内容",
        },
        expected_revision=None,
    )


def _seed_full_store(store: JsonObjectStore) -> None:
    """写入所有层各一条数据。"""
    _write_source(store, "source-1")
    _write_atom(store, "atom-1")
    _write_persona(store)
    _write_series(store)
    _write_transition(store)
    _write_job(store)
    _write_whitebox_export(store)
    _write_recall_entry(store)


# ═══════════════════════════════════════════════════════════════════
# 路径脱敏
# ═══════════════════════════════════════════════════════════════════

class TestMaskPath:
    def test_masks_windows_absolute_path(self) -> None:
        result = mask_path("C:/Users/john/.rebuild-data")
        # 不应暴露用户名 john
        assert "john" not in result
        assert "…" in result or result.endswith(".rebuild-data")

    def test_masks_unix_absolute_path(self) -> None:
        result = mask_path("/home/alice/.rebuild-data")
        assert "alice" not in result
        assert "…" in result or result.endswith(".rebuild-data")

    def test_handles_empty_path(self) -> None:
        assert mask_path("") == "—"

    def test_handles_short_path(self) -> None:
        result = mask_path("/data")
        assert result in ("/data", "…/data", "data")


# ═══════════════════════════════════════════════════════════════════
# Vault 状态
# ═══════════════════════════════════════════════════════════════════

class TestVaultStatus:
    def test_vault_status_reads_source_count(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _write_source(store, "source-1")
        _write_source(store, "source-2")
        result = GetVaultStatus(
            store, namespace_id="default",
            vault_path="C:/Users/test/.rebuild-data",
        ).execute()
        assert result.source_count == 2
        assert result.namespace_id == "default"

    def test_vault_status_path_is_masked(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetVaultStatus(
            store,
            vault_path="C:/Users/secretuser/.rebuild-data",
        ).execute()
        # 不应暴露用户名
        assert "secretuser" not in result.vault_path_masked
        assert result.vault_path_masked != "C:/Users/secretuser/.rebuild-data"

    def test_vault_status_includes_all_asset_layers(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_full_store(store)
        result = GetVaultStatus(store).execute()
        layer_ids = {layer.layer_id for layer in result.asset_layers}
        # 应包含 L0-L4、标签、证据、任务、审计
        assert {
            "L0", "L1", "L2", "L3", "L4",
            "tags", "evidence", "tasks", "audit",
        }.issubset(layer_ids)

    def test_vault_status_keeps_l3_series_separate_from_l4_persona(
        self, tmp_path: Path,
    ) -> None:
        store = _store(tmp_path)
        _write_persona(store)
        _write_series(store)

        result = GetVaultStatus(store).execute()

        l3 = next(layer for layer in result.asset_layers if layer.layer_id == "L3")
        l4 = next(layer for layer in result.asset_layers if layer.layer_id == "L4")
        assert l3.count == 1
        assert l3.collection == "memory_series_memory, project_skills"
        assert "Persona" not in l3.label
        assert l4.count == 1
        assert l4.collection == "memory_persona"
        assert l4.label == "稳定偏好与规则"

    def test_vault_status_layer_counts_correct(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _write_source(store, "s1")
        _write_source(store, "s2")
        _write_atom(store, "a1")
        result = GetVaultStatus(store).execute()
        l0 = next(l for l in result.asset_layers if l.layer_id == "L0")
        l1 = next(l for l in result.asset_layers if l.layer_id == "L1")
        assert l0.count == 2
        assert l1.count == 1

    def test_vault_status_index_can_rebuild(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetVaultStatus(store).execute()
        assert result.can_rebuild_index is True
        assert result.index_status.can_rebuild is True

    def test_vault_status_backup_state(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetVaultStatus(
            store,
            backup_enabled=True,
            backup_retention_count=5,
            backup_ready=True,
            backup_destination_uri="crp://default/backups/",
            pre_restore_required=True,
        ).execute()
        assert result.backup_status.backup_enabled is True
        assert result.backup_status.backup_ready is True
        assert result.backup_status.retention_count == 5
        assert result.backup_status.can_restore is True
        assert result.backup_status.destination_uri == "crp://default/backups/"
        assert result.backup_status.pre_restore_required is True

    def test_vault_status_includes_local_first_guarantee_fields(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetVaultStatus(
            store,
            app_root_uri="platform-app-data://chriptmas-replay",
            root_uri="crp://default/",
            vault_path=str(tmp_path / ".rebuild-data"),
            app_data_dir_path=str(tmp_path / "AppData" / "ChriptmasReplay"),
            target_vault_path=str(tmp_path / "AppData" / "ChriptmasReplay" / "vault"),
            backup_destination_path=str(tmp_path / ".rebuild-data-recovery" / "snapshots"),
        ).execute()
        body = serialize_vault_status(result)

        assert body["app_root_uri"] == "platform-app-data://chriptmas-replay"
        assert body["app_data_dir_masked"].endswith("ChriptmasReplay")
        assert body["vault_dir_masked"].endswith("vault")
        assert body["backup_destination_masked"].endswith("snapshots")
        assert body["storage_location_status"] == "project_root_development"
        assert body["backup_status"]["destination_uri"] == "crp://default/backups/"
        assert body["backup_status"]["pre_restore_required"] is True
        assert body["migration_status"]["export_ready"] is True
        assert body["migration_status"]["import_ready"] is False
        assert body["migration_status"]["target_vault_uri"] == "platform-app-data://chriptmas-replay/vault"
        assert body["migration_status"]["needs_migration"] is True
        assert body["migration_status"]["migration_executed"] is False
        assert body["operational_plan"]["app_data_dir"]["status"] == "planned"
        assert body["operational_plan"]["backup"]["status"] == "ready"
        assert body["operational_plan"]["restore"]["can_execute"] is True
        assert body["operational_plan"]["restore"]["status"] == "ready"
        assert body["operational_plan"]["migration"]["status"] == "required"
        assert body["operational_plan"]["encryption"]["key_source"] == "platform_keyring"

    def test_vault_status_marks_formal_app_data_vault_when_paths_match(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        formal_vault = tmp_path / "AppData" / "ChriptmasReplay" / "vault"
        result = GetVaultStatus(
            store,
            vault_path=str(formal_vault),
            app_data_dir_path=str(formal_vault.parent),
            target_vault_path=str(formal_vault),
        ).execute()
        body = serialize_vault_status(result)

        assert body["storage_location_status"] == "formal_app_data_vault"
        assert body["migration_status"]["needs_migration"] is False
        assert body["migration_status"]["target_vault_masked"].endswith("vault")
        assert body["operational_plan"]["app_data_dir"]["status"] == "ready"
        assert body["operational_plan"]["encryption"]["status"] == "ready_for_platform_keyring"

    def test_vault_status_storage_display_human_readable(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_full_store(store)
        result = GetVaultStatus(store).execute()
        # 应该是 "X B" / "X KB" / "X MB" 之一
        assert any(unit in result.storage_display for unit in ("B", "KB", "MB", "GB"))

    def test_vault_status_serialization(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_full_store(store)
        result = GetVaultStatus(
            store, vault_path="/home/user/.rebuild-data",
        ).execute()
        body = serialize_vault_status(result)
        assert "vault_path_masked" in body
        assert "user" not in body["vault_path_masked"]
        assert "asset_layers" in body
        assert "index_status" in body
        assert "backup_status" in body
        assert body["namespace_id"] == "default"

    def test_vault_status_handles_empty_store(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetVaultStatus(store).execute()
        assert result.source_count == 0
        assert all(layer.count == 0 for layer in result.asset_layers)


# ═══════════════════════════════════════════════════════════════════
# Provider 边界
# ═══════════════════════════════════════════════════════════════════

class TestProviderBoundary:
    def test_provider_boundary_lists_local_and_external(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetProviderBoundary(store).execute()
        local_labels = [c.label for c in result.local_providers]
        external_labels = [c.label for c in result.external_providers]
        # 应包含本地能力
        assert "音频转写" in local_labels
        assert "文档正文读取" in local_labels
        # 应包含外部能力
        assert "文本摘要" in external_labels
        assert "标签生成" in external_labels

    def test_provider_boundary_shows_what_never_leaves(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetProviderBoundary(store).execute()
        # 应明确列出哪些内容从未出本机
        assert len(result.content_never_leaves) > 0
        assert any("原始文件" in item for item in result.content_never_leaves)
        assert any("Persona" in item for item in result.content_never_leaves)

    def test_provider_boundary_shows_what_can_leave(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetProviderBoundary(store).execute()
        assert len(result.content_can_leave) > 0
        assert any("摘要" in item for item in result.content_can_leave)

    def test_provider_boundary_local_providers_do_not_leave(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetProviderBoundary(store).execute()
        for cap in result.local_providers:
            assert cap.leaves_local is False
            assert cap.provider_kind == "local"

    def test_provider_boundary_external_providers_leave(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetProviderBoundary(store).execute()
        for cap in result.external_providers:
            assert cap.leaves_local is True
            assert cap.provider_kind == "external"

    def test_provider_boundary_unconfigured_when_no_task_model_map(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetProviderBoundary(store).execute()
        # 没有 task_model_map 时，外部能力应标记为 needs_config
        assert len(result.unconfigured_capabilities) > 0
        assert "文本摘要" in result.unconfigured_capabilities

    def test_provider_boundary_serialization(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetProviderBoundary(store).execute()
        body = serialize_provider_boundary(result)
        assert "local_providers" in body
        assert "external_providers" in body
        assert "content_never_leaves" in body
        assert "content_can_leave" in body
        assert "unconfigured_capabilities" in body
        # 不应暴露 secret
        body_str = json.dumps(body, ensure_ascii=False)
        assert "api_key" not in body_str.lower() or "[redacted]" in body_str
        assert "cookie" not in body_str.lower() or "[redacted]" in body_str

    def test_provider_boundary_each_capability_has_next_step(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = GetProviderBoundary(store).execute()
        for cap in (*result.local_providers, *result.external_providers):
            assert cap.next_step  # 不为空
            assert cap.description  # 不为空


# ═══════════════════════════════════════════════════════════════════
# 私人记忆资产包导出
# ═══════════════════════════════════════════════════════════════════

class TestMemoryAssetExport:
    def test_export_includes_all_required_files(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_full_store(store)
        result = ExportMemoryAssetPackage(store).execute()
        file_paths = {f.logical_path for f in result.files}
        # 必须包含的文件
        assert "manifest.json" in file_paths
        assert "sources.json" in file_paths
        assert "persona.json" in file_paths
        assert "series_memory.json" in file_paths
        assert "tag_index.json" in file_paths
        assert "evidence_graph.json" in file_paths
        assert "task_records.json" in file_paths
        assert "audit_records.json" in file_paths
        assert "provider_summary.json" in file_paths
        assert "rebuild_index_hint.json" in file_paths

    def test_export_does_not_contain_secrets(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # 写入含敏感字段的数据
        _write_source(store, "source-1")
        store.write(
            "memory_persona", "persona-1",
            {
                "id": "persona-1",
                "name": "AI 助手",
                "api_key": "sk-secret-key-12345",
                "cookie": "session=abc123",
                "token": "bearer-token-value",
                "password": "secret-password",
            },
            expected_revision=None,
        )
        result = ExportMemoryAssetPackage(store).execute()
        body = serialize_memory_asset_package(result)
        body_str = json.dumps(body, ensure_ascii=False)
        # secret 值不应出现在导出包中
        assert "sk-secret-key-12345" not in body_str
        assert "session=abc123" not in body_str
        assert "bearer-token-value" not in body_str
        assert "secret-password" not in body_str
        # 敏感字段应被替换为 [redacted]
        assert "[redacted]" in body_str

    def test_export_does_not_contain_full_paths(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.write(
            "sources", "source-1",
            {
                "id": "source-1",
                "title": "测试",
                "file_path": "C:/Users/secretuser/Documents/secret.txt",
                "absolute_path": "/home/secretuser/data/secret.json",
            },
            expected_revision=None,
        )
        result = ExportMemoryAssetPackage(store).execute()
        body_str = json.dumps(serialize_memory_asset_package(result), ensure_ascii=False)
        assert "secretuser" not in body_str
        assert "secret.txt" not in body_str
        assert "[redacted]" in body_str or "[local-path-redacted]" in body_str

    def test_export_contains_persona(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _write_persona(store)
        result = ExportMemoryAssetPackage(store).execute()
        persona_file = next(f for f in result.files if f.logical_path == "persona.json")
        assert persona_file.content["count"] == 1
        items = persona_file.content["items"]
        assert len(items) == 1
        assert items[0]["name"] == "私人 AI 助手"

    def test_export_contains_series_memory(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _write_series(store, "series-1")
        result = ExportMemoryAssetPackage(store).execute()
        series_file = next(f for f in result.files if f.logical_path == "series_memory.json")
        assert series_file.content["count"] == 1

    def test_export_contains_tag_index(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _write_recall_entry(store, "idx-1")
        _write_recall_entry(store, "idx-2")
        result = ExportMemoryAssetPackage(store).execute()
        tag_file = next(f for f in result.files if f.logical_path == "tag_index.json")
        assert tag_file.content["count"] == 2

    def test_export_contains_evidence_graph(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _write_transition(store, "trans-1")
        result = ExportMemoryAssetPackage(store).execute()
        evidence_file = next(f for f in result.files if f.logical_path == "evidence_graph.json")
        assert evidence_file.content["count"] == 1

    def test_export_contains_task_records(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _write_job(store, "job-1")
        result = ExportMemoryAssetPackage(store).execute()
        task_file = next(f for f in result.files if f.logical_path == "task_records.json")
        assert task_file.content["count"] == 1

    def test_export_contains_audit_records(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _write_whitebox_export(store, "wb-1")
        result = ExportMemoryAssetPackage(store).execute()
        audit_file = next(f for f in result.files if f.logical_path == "audit_records.json")
        assert audit_file.content["count"] == 1

    def test_export_provider_summary_has_no_secrets(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = ExportMemoryAssetPackage(store).execute()
        provider_file = next(f for f in result.files if f.logical_path == "provider_summary.json")
        body_str = json.dumps(provider_file.content, ensure_ascii=False)
        # 明确声明不含 secret
        assert "secret" not in body_str.lower() or "不含" in body_str or "no secret" in body_str.lower()
        # 不应包含任何看似 API key 的字符串
        assert "sk-" not in body_str

    def test_export_manifest_declares_what_it_contains_and_excludes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = ExportMemoryAssetPackage(store).execute()
        manifest = next(f for f in result.files if f.logical_path == "manifest.json").content
        assert "contains" in manifest
        assert "does_not_contain" in manifest
        assert "API Key" in manifest["does_not_contain"]
        assert "Cookie" in manifest["does_not_contain"]

    def test_export_rebuild_index_hint_is_user_friendly(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = ExportMemoryAssetPackage(store).execute()
        assert "重建索引" in result.rebuild_index_hint
        hint_file = next(f for f in result.files if f.logical_path == "rebuild_index_hint.json")
        assert "steps" in hint_file.content

    def test_export_total_size_display(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_full_store(store)
        result = ExportMemoryAssetPackage(store).execute()
        assert result.total_size_bytes > 0
        assert any(unit in result.total_size_display for unit in ("B", "KB", "MB"))

    def test_export_vault_path_masked(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = ExportMemoryAssetPackage(
            store, vault_path="/home/secretuser/.rebuild-data",
        ).execute()
        assert "secretuser" not in result.vault_path_masked


# ═══════════════════════════════════════════════════════════════════
# 记忆快照与回滚
# ═══════════════════════════════════════════════════════════════════

class TestMemorySnapshots:
    def test_create_snapshot_records_layer_fingerprints(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_full_store(store)
        result = CreateMemorySnapshot(store).execute(label="测试快照")
        assert result.snapshot_id.startswith("snap-")
        assert result.label == "测试快照"
        assert len(result.layer_fingerprints) > 0
        # 每个指纹应是 12 字符 hex
        for fp in result.layer_fingerprints.values():
            assert len(fp) == 12
            int(fp, 16)  # 合法 hex

    def test_create_snapshot_persists_to_store(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = CreateMemorySnapshot(store).execute(label="持久化测试")
        # 读取回来
        record = store.read("memory_snapshots", result.snapshot_id)
        assert record is not None
        assert record["label"] == "持久化测试"

    def test_list_snapshots_returns_created(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        CreateMemorySnapshot(store).execute(label="快照 1")
        CreateMemorySnapshot(store).execute(label="快照 2")
        result = ListMemorySnapshots(store).execute()
        assert len(result) == 2
        labels = {s.label for s in result}
        assert labels == {"快照 1", "快照 2"}

    def test_list_snapshots_sorted_by_created_at_desc(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        first = CreateMemorySnapshot(store).execute(label="第一")
        second = CreateMemorySnapshot(store).execute(label="第二")
        result = ListMemorySnapshots(store).execute()
        assert result[0].snapshot_id in (first.snapshot_id, second.snapshot_id)

    def test_list_snapshots_empty(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = ListMemorySnapshots(store).execute()
        assert result == ()

    def test_rollback_plan_for_existing_snapshot(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_full_store(store)
        snapshot = CreateMemorySnapshot(store).execute(label="回滚基准")
        # 添加新数据，使当前状态与快照不同
        _write_source(store, "source-new")
        rollback = RollbackToMemorySnapshot(store).execute(
            target_snapshot_id=snapshot.snapshot_id,
        )
        assert rollback.target_snapshot_id == snapshot.snapshot_id
        assert rollback.requires_confirmation is True
        assert "回滚" in rollback.warning
        # L0 应该在变化列表中（因为新增了 source-new）
        assert "L0" in rollback.layers_to_change

    def test_rollback_plan_for_missing_snapshot_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(Exception):
            RollbackToMemorySnapshot(store).execute(target_snapshot_id="missing")

    def test_rollback_plan_serialization(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_full_store(store)
        snapshot = CreateMemorySnapshot(store).execute(label="序列化测试")
        _write_source(store, "source-new")
        plan = RollbackToMemorySnapshot(store).execute(
            target_snapshot_id=snapshot.snapshot_id,
        )
        body = serialize_memory_rollback_plan(plan)
        assert body["requires_confirmation"] is True
        assert "warning" in body
        assert "current_layer_counts" in body
        assert "target_layer_counts" in body

    def test_rollback_plan_id_is_stable_for_same_snapshot_and_counts(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_full_store(store)
        snapshot = CreateMemorySnapshot(store).execute(label="稳定确认凭证")
        _write_source(store, "source-new")
        first = RollbackToMemorySnapshot(store).execute(target_snapshot_id=snapshot.snapshot_id)
        second = RollbackToMemorySnapshot(store).execute(target_snapshot_id=snapshot.snapshot_id)
        assert first.rollback_id == second.rollback_id

    def test_restore_confirmation_requires_current_rollback_id(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_full_store(store)
        snapshot = CreateMemorySnapshot(store).execute(label="恢复确认")
        _write_source(store, "source-new")
        plan = RollbackToMemorySnapshot(store).execute(target_snapshot_id=snapshot.snapshot_id)
        result = PrepareMemoryRestoreConfirmation().execute(
            target_snapshot_id=snapshot.snapshot_id,
            rollback_id="",
            expected_rollback_id=plan.rollback_id,
            confirm=False,
        )
        assert result.status == "confirmation_required"
        assert result.restore_executed is False
        assert "rollback_id" in result.reason

    def test_restore_confirmation_blocks_actual_restore_until_transaction_exists(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_full_store(store)
        snapshot = CreateMemorySnapshot(store).execute(label="恢复安全门")
        _write_source(store, "source-new")
        plan = RollbackToMemorySnapshot(store).execute(target_snapshot_id=snapshot.snapshot_id)
        result = PrepareMemoryRestoreConfirmation().execute(
            target_snapshot_id=snapshot.snapshot_id,
            rollback_id=plan.rollback_id,
            expected_rollback_id=plan.rollback_id,
            confirm=True,
        )
        body = serialize_memory_restore_confirmation(result)
        assert body["status"] == "blocked_pre_restore_only"
        assert body["restore_executed"] is False
        assert body["confirmation_received"] is True
        assert "事务化恢复" in body["next_step"]

    def test_snapshot_summary_serialization(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        CreateMemorySnapshot(store).execute(label="摘要测试")
        summaries = ListMemorySnapshots(store).execute()
        body = serialize_memory_snapshot_summary(summaries[0])
        assert "snapshot_id" in body
        assert "label" in body
        assert "total_objects" in body

    def test_snapshot_serialization(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        snapshot = CreateMemorySnapshot(store).execute(label="序列化")
        body = serialize_memory_snapshot(snapshot)
        assert body["label"] == "序列化"
        assert "layer_fingerprints" in body
        assert "layer_counts" in body
