import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ASSET_OWNERSHIP_ENDPOINT,
  ORIGINAL_ASSET_RETENTION_ENDPOINT,
  SOURCE_RETENTION_PURGE_ENDPOINT,
  createOriginalAssetRetentionPlan,
  createSourceRetentionPurgePlan,
  executeOriginalAssetRetention,
  executeSourceRetentionPurge,
  loadAssetOwnershipGraph,
  loadOriginalAssetRetentionCandidates,
  loadSourceRetentionCandidates,
  reconcileOriginalAssetRetention,
} from "@src/features/rebuild/libraryRetentionApi";

const originalElectronApi = globalThis.electronAPI;

afterEach(() => {
  if (originalElectronApi === undefined) {
    delete globalThis.electronAPI;
  } else {
    globalThis.electronAPI = originalElectronApi;
  }
});

describe("libraryRetentionApi", () => {
  it("owns the retention endpoints and exposes the complete domain contract", () => {
    expect(SOURCE_RETENTION_PURGE_ENDPOINT).toBe("/api/rebuild/retention/source-purge");
    expect(ORIGINAL_ASSET_RETENTION_ENDPOINT).toBe("/api/rebuild/retention/original-assets");
    expect(ASSET_OWNERSHIP_ENDPOINT).toBe("/api/rebuild/retention/asset-ownership");
    expect([
      loadSourceRetentionCandidates,
      createSourceRetentionPurgePlan,
      executeSourceRetentionPurge,
      reconcileOriginalAssetRetention,
      loadOriginalAssetRetentionCandidates,
      loadAssetOwnershipGraph,
      createOriginalAssetRetentionPlan,
      executeOriginalAssetRetention,
    ].every((request) => typeof request === "function")).toBe(true);
  });

  it("uses the shared desktop transport without depending on the facade", async () => {
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:43123/" };
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ items: [] }),
    });

    await expect(loadSourceRetentionCandidates({ fetchImpl })).resolves.toEqual({ items: [] });
    expect(fetchImpl).toHaveBeenCalledWith(
      "http://127.0.0.1:43123/api/rebuild/retention/source-purge/candidates",
      { headers: { Accept: "application/json" } },
    );
  });

  it("keeps failed retention responses bounded to their domain error", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: vi.fn().mockResolvedValue({ detail: "retention revision drift" }),
    });

    await expect(loadOriginalAssetRetentionCandidates({ fetchImpl })).rejects.toThrow(
      "retention revision drift",
    );
  });
});
