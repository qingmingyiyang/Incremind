import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { STORAGE_GOVERNANCE_ENDPOINT, VAULT_STATUS_ENDPOINT, PROVIDER_BOUNDARY_ENDPOINT, confirmRestoreSafetyGate, createMemorySnapshot, createRollbackPlan, exportMemoryAssetPackage, loadProviderBoundary, loadStorageGovernance, loadVaultStatus, listMemorySnapshots } from "@src/features/rebuild/localMemoryVaultApi";

function vaultStatusData(overrides = {}) {
  return {
    namespace_id: "default",
    vault_path_masked: "…/.rebuild-data",
    root_uri: "crp://default/",
    storage_version: 1,
    source_count: 5,
    asset_layers: [
      { layer_id: "L0", label: "原始资料", count: 5, description: "你保存的原始资料。" },
      { layer_id: "L1", label: "原子事实", count: 8, description: "AI 从资料中抽取的原子事实。" },
      { layer_id: "L2", label: "场景经验", count: 3, description: "AI 整理出的场景经验。" },
      { layer_id: "L3", label: "系列记忆与项目能力", count: 2, description: "项目系列和可复用能力。" },
      { layer_id: "L4", label: "稳定偏好与规则", count: 1, description: "经用户确认的长期偏好、事实和规则。" },
      { layer_id: "tags", label: "标签索引", count: 12, description: "可搜索的标签索引。" },
      { layer_id: "evidence", label: "证据图谱", count: 6, description: "记忆变更的证据链。" },
      { layer_id: "tasks", label: "任务记录", count: 4, description: "自动整理任务的历史。" },
      { layer_id: "audit", label: "白盒审计", count: 1, description: "白盒导出和审计记录。" },
    ],
    index_status: {
      status: "ready",
      backend_kind: "sqlite_fts5",
      entry_count: 42,
      traceable: true,
      vector_enabled: false,
      can_rebuild: true,
    },
    backup_status: {
      backup_enabled: true,
      backup_ready: true,
      retention_count: 5,
      last_backup_at: "2026-07-06T10:00:00+08:00",
      can_restore: true,
      destination_masked: "…/Chriptmas_Replay/backups",
    },
    app_data_dir_masked: "…/Chriptmas_Replay",
    migration_status: {
      export_ready: true,
      import_ready: false,
      target_vault_masked: "…/Chriptmas_Replay/vault",
      target_vault_uri: "platform-app-data://chriptmas-replay/vault",
      storage_location_status: "project_root_development",
      needs_migration: true,
      migration_ready: false,
      migration_executed: false,
      next_step: "需要先实现只读校验、恢复前快照、事务化复制和完成后索引校验。",
    },
    encryption_status: {
      enabled: false,
      detail: "当前仍依赖系统账户权限保护本地文件。",
    },
    storage_bytes: 102400,
    storage_display: "100.0 KB",
    can_rebuild_index: true,
    ...overrides,
  };
}

function boundaryData(overrides = {}) {
  return {
    local_providers: [
      {
        capability_key: "audio_transcription",
        label: "音频转写",
        provider_kind: "local",
        leaves_local: false,
        status: "needs_config",
        description: "本地转写音频，内容不离开本机。",
        next_step: "前往设置页配置 音频转写，或使用示例材料体验。",
      },
      {
        capability_key: "document_text_extractor",
        label: "文档正文读取",
        provider_kind: "local",
        leaves_local: false,
        status: "ready",
        description: "本地解析文档，内容不离开本机。",
        next_step: "文档正文读取 已就绪，可以正常使用。",
      },
    ],
    external_providers: [
      {
        capability_key: "text_summary",
        label: "文本摘要",
        provider_kind: "external",
        leaves_local: true,
        status: "needs_config",
        description: "调用文本模型生成摘要，会把文本片段发送到外部 Provider。",
        next_step: "前往设置页配置 文本摘要，或使用示例材料体验。",
      },
    ],
    content_never_leaves: [
      "原始文件（音频、视频、图片、文档）的完整内容",
      "你的 Persona 人格设定",
    ],
    content_can_leave: ["用于生成摘要的文本片段"],
    unconfigured_capabilities: ["音频转写", "文本摘要"],
    overall_status: "needs_attention",
    ...overrides,
  };
}

function snapshotsData(overrides = {}) {
  return {
    snapshots: [
      {
        snapshot_id: "snap-001",
        created_at: "2026-07-06T10:00:00+08:00",
        label: "第一个快照",
        layer_count: 4,
        total_objects: 18,
        restorable: true,
        backup_file_count: 7,
      },
    ],
    ...overrides,
  };
}

describe("localMemoryVaultApi", () => {
it("loadVaultStatus calls the vault status endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({ ok: true, status: 200, json: async () => vaultStatusData() }),
    );
    const result = await loadVaultStatus({ fetchImpl });
    expect(result.source_count).toBe(5);
    expect(fetchImpl).toHaveBeenCalledWith(
      VAULT_STATUS_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });
it("loadStorageGovernance loads body-free actual storage metadata", async () => {
    const payload = {
      total_bytes: 12,
      content_included: false,
      paths_included: false,
    };
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => payload,
      }),
    );

    await expect(loadStorageGovernance({ fetchImpl })).resolves.toBe(payload);
    expect(fetchImpl).toHaveBeenCalledWith(
      STORAGE_GOVERNANCE_ENDPOINT,
      { headers: { Accept: "application/json" } },
    );
  });
it("loadProviderBoundary calls the provider boundary endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({ ok: true, status: 200, json: async () => boundaryData() }),
    );
    const result = await loadProviderBoundary({ fetchImpl });
    expect(result.local_providers).toHaveLength(2);
    expect(fetchImpl).toHaveBeenCalledWith(
      PROVIDER_BOUNDARY_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });
it("exportMemoryAssetPackage posts to export endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ package_id: "pkg-1", files: [] }),
      }),
    );
    const result = await exportMemoryAssetPackage({ fetchImpl });
    expect(result.package_id).toBe("pkg-1");
    expect(fetchImpl.mock.calls[0][1].method).toBe("POST");
  });
it("listMemorySnapshots calls snapshots endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({ ok: true, status: 200, json: async () => snapshotsData() }),
    );
    const result = await listMemorySnapshots({ fetchImpl });
    expect(result.snapshots).toHaveLength(1);
  });
it("createMemorySnapshot posts label and notes", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ snapshot_id: "snap-new" }),
      }),
    );
    const result = await createMemorySnapshot({
      label: "测试快照",
      notes: "备注",
      fetchImpl,
    });
    expect(result.snapshot_id).toBe("snap-new");
    const body = JSON.parse(fetchImpl.mock.calls[0][1].body);
    expect(body.label).toBe("测试快照");
  });
it("createRollbackPlan posts to snapshot-specific endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          rollback_id: "rb-1",
          target_snapshot_id: "snap-1",
          warning: "warning",
          layers_to_change: [],
          current_layer_counts: {},
          target_layer_counts: {},
        }),
      }),
    );
    const result = await createRollbackPlan({ snapshotId: "snap-1", fetchImpl });
    expect(result.rollback_id).toBe("rb-1");
    expect(fetchImpl.mock.calls[0][0]).toContain("/snap-1/rollback-plan");
  });
it("createRollbackPlan rejects empty snapshotId", async () => {
    await expect(createRollbackPlan({ snapshotId: "", fetchImpl: vi.fn() })).rejects.toThrow(
      "snapshotId is required",
    );
  });
it("confirmRestoreSafetyGate posts to snapshot rollback endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "blocked_pre_restore_only",
          restore_executed: false,
        }),
      }),
    );
    const result = await confirmRestoreSafetyGate({
      snapshotId: "snap-1",
      rollbackId: "rb-1",
      fetchImpl,
    });
    expect(result.restore_executed).toBe(false);
    expect(fetchImpl.mock.calls[0][0]).toContain("/snap-1/rollback");
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toMatchObject({
      rollback_id: "rb-1",
      confirm: true,
    });
  });
it("confirmRestoreSafetyGate rejects missing rollbackId", async () => {
    await expect(
      confirmRestoreSafetyGate({ snapshotId: "snap-1", rollbackId: "", fetchImpl: vi.fn() }),
    ).rejects.toThrow("rollbackId is required");
  });
it("fails explicitly when endpoint is unavailable", async () => {
    const fetchImpl = vi.fn(() => Promise.resolve({ ok: false, status: 503 }));
    await expect(loadVaultStatus({ fetchImpl })).rejects.toThrow("503");
    await expect(loadProviderBoundary({ fetchImpl })).rejects.toThrow("503");
    await expect(exportMemoryAssetPackage({ fetchImpl })).rejects.toThrow("503");
    await expect(listMemorySnapshots({ fetchImpl })).rejects.toThrow("503");
  });
});
