import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DAILY_REMINDERS_ENDPOINT,
  ARCHIVED_DOCUMENTS_ENDPOINT,
  LIBRARY_SEARCH_ENDPOINT,
  dailyRemindersUrl,
  archivedDocumentsUrl,
  assignLegacyMemoryCandidateProject,
  documentLifecycleUrl,
  documentDetailUrl,
  documentHtmlUrl,
  documentHtmlExportUrl,
  documentDeliveryUrl,
  documentDeliveryArtifactUrl,
  documentPdfArtifactUrl,
  documentRevisionsUrl,
  sourceTemplateMemoryCandidateUrl,
  loadArchivedDocuments,
  archiveDocument,
  createMemoryHierarchyUpdateCandidate,
  createMemoryHierarchyUpdateCandidateBatch,
  createMemoryScenarioTransferPlan,
  restoreDocument,
  librarySearchUrl,
  loadMemoryHierarchyOptions,
  loadMemoryMaintenancePlan,
  loadMemoryCandidateProjectOptions,
  loadMemoryScenarioTransferPlan,
  loadMemoryScenarioTransferPreview,
  loadMemorySeriesFreshness,
  loadMemorySeriesSuggestions,
  loadDailyReminders,
  searchLibrary,
  reviewMemoryCandidate,
  updateLibrarySourceMetadata,
  loadSourceRetentionCandidates,
  createSourceRetentionPurgePlan,
  executeSourceRetentionPurge,
  reconcileOriginalAssetRetention,
  loadOriginalAssetRetentionCandidates,
  createOriginalAssetRetentionPlan,
  executeOriginalAssetRetention,
  loadAssetOwnershipGraph,
  libraryOverviewSafeErrorMessage,
} from "@src/features/rebuild/libraryOverviewApi";

describe("legacy document project scope", () => {
  it("adds the active project to each document and delivery endpoint, preserving default URLs when omitted", () => {
    const projectId = "project-alpha";
    expect(archivedDocumentsUrl(projectId)).toBe("/api/rebuild/documents-archived?project_id=project-alpha");
    expect(documentLifecycleUrl("doc-1", "archive", projectId)).toBe("/api/rebuild/documents/doc-1/archive?project_id=project-alpha");
    expect(documentDetailUrl("doc-1", projectId)).toBe("/api/rebuild/documents/doc-1?project_id=project-alpha");
    expect(documentHtmlUrl("doc-1", projectId)).toBe("/api/rebuild/documents/doc-1/html?project_id=project-alpha");
    expect(documentHtmlExportUrl("doc-1", projectId)).toBe("/api/rebuild/documents/doc-1/html-export?project_id=project-alpha");
    expect(documentRevisionsUrl("doc-1", projectId)).toBe("/api/rebuild/documents/doc-1/revisions?project_id=project-alpha");
    expect(sourceTemplateMemoryCandidateUrl("doc-1", projectId)).toBe("/api/rebuild/documents/doc-1/template-memory-candidate?project_id=project-alpha");
    expect(documentDeliveryUrl(projectId)).toBe("/api/rebuild/document-deliveries?project_id=project-alpha");
    expect(documentDeliveryArtifactUrl("delivery-1", "html", projectId)).toBe("/api/rebuild/document-deliveries/delivery-1/artifacts/html?project_id=project-alpha");
    expect(documentPdfArtifactUrl("pdf-1", projectId)).toBe("/api/rebuild/document-pdf-operations/pdf-1/artifact?project_id=project-alpha");
    expect(documentDetailUrl("doc-1")).toBe("/api/rebuild/documents/doc-1");
  });
});

describe("libraryOverviewApi safe errors", () => {
  it("maps authority and credential failures to stable Chinese copy without exposing internals", () => {
    expect(libraryOverviewSafeErrorMessage(409, {
      detail: "secret_lease_boundary_drift",
      reason: "provider:example",
    })).toBe("资料库状态已变化或正在恢复，请刷新后重试。");
    expect(libraryOverviewSafeErrorMessage(418, {
      detail: "secret lease unavailable",
    })).toBe("模型连接暂时不可用，请检查连接设置后重试。");
  });
});

describe("libraryOverviewApi memory maintenance plan", () => {
  it("loads a project-scoped read-only plan", async () => {
    const payload = {
      project_id: "project alpha",
      suggestion_count: 0,
      suggestions: [],
      writes_performed: false,
    };
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue(payload),
    });

    await expect(loadMemoryMaintenancePlan({
      projectId: "project alpha",
      fetchImpl,
    })).resolves.toBe(payload);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/projects/project%20alpha/memory-maintenance-plan",
      { headers: { Accept: "application/json" } },
    );
  });

  it("rejects an empty scope and reports backend failures", async () => {
    await expect(loadMemoryMaintenancePlan({
      projectId: " ",
      fetchImpl: vi.fn(),
    })).rejects.toThrow("Memory maintenance project is invalid");

    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: vi.fn().mockResolvedValue({ detail: "revision drift" }),
    });
    await expect(loadMemoryMaintenancePlan({
      projectId: "project-alpha",
      fetchImpl,
    })).rejects.toThrow("revision drift");
  });
});

describe("libraryOverviewApi Source retention purge", () => {
  it("loads the body-free asset ownership graph", async () => {
    const payload = { complete: true, nodes: [], edges: [], blockers: [] };
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue(payload),
    });

    await expect(loadAssetOwnershipGraph({ fetchImpl })).resolves.toBe(payload);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/retention/asset-ownership",
      { headers: { Accept: "application/json" } },
    );
  });

  it("lists, plans and explicitly executes without sending Source content", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: vi.fn().mockResolvedValue({ items: [] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: vi.fn().mockResolvedValue({ plan_id: "retention-dry-run-1" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: vi.fn().mockResolvedValue({ status: "completed" }),
      });

    await loadSourceRetentionCandidates({ fetchImpl });
    await createSourceRetentionPurgePlan({
      sourceId: "source-retention-ui",
      fetchImpl,
    });
    await executeSourceRetentionPurge({
      sourceId: "source-retention-ui",
      planId: "retention-dry-run-1",
      expectedRevision: 3,
      confirm: true,
      fetchImpl,
    });

    expect(fetchImpl.mock.calls).toEqual([
      [
        "/api/rebuild/retention/source-purge/candidates",
        { headers: { Accept: "application/json" } },
      ],
      [
        "/api/rebuild/retention/source-purge/plan",
        {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({ source_id: "source-retention-ui" }),
        },
      ],
      [
        "/api/rebuild/retention/source-purge",
        {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({
            source_id: "source-retention-ui",
            plan_id: "retention-dry-run-1",
            expected_revision: 3,
            confirm: true,
          }),
        },
      ],
    ]);
    expect(fetchImpl.mock.calls.flat().join(" ")).not.toContain("private body");
  });

  it("requires explicit confirmation before issuing a purge request", async () => {
    const fetchImpl = vi.fn();
    expect(() => executeSourceRetentionPurge({
      sourceId: "source-retention-ui",
      planId: "retention-dry-run-1",
      expectedRevision: 3,
      confirm: false,
      fetchImpl,
    })).toThrow("explicit confirmation");
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("libraryOverviewApi original asset retention", () => {
  it("reconciles, lists, plans and explicitly executes without sending bytes", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ items: [] }),
    });

    await reconcileOriginalAssetRetention({ fetchImpl });
    await loadOriginalAssetRetentionCandidates({ fetchImpl });
    await createOriginalAssetRetentionPlan({
      assetId: "original-file-safe",
      fetchImpl,
    });
    await executeOriginalAssetRetention({
      assetId: "original-file-safe",
      planId: `original-asset-retention-${"a".repeat(64)}`,
      expectedRevision: 4,
      confirm: true,
      fetchImpl,
    });

    expect(fetchImpl.mock.calls).toEqual([
      [
        "/api/rebuild/retention/original-assets/reconcile",
        {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: "{}",
        },
      ],
      [
        "/api/rebuild/retention/original-assets/candidates",
        { headers: { Accept: "application/json" } },
      ],
      [
        "/api/rebuild/retention/original-assets/plan",
        {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({ asset_id: "original-file-safe" }),
        },
      ],
      [
        "/api/rebuild/retention/original-assets",
        {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({
            asset_id: "original-file-safe",
            plan_id: `original-asset-retention-${"a".repeat(64)}`,
            expected_revision: 4,
            confirm: true,
          }),
        },
      ],
    ]);
    expect(fetchImpl.mock.calls.flat().join(" ")).not.toContain("file bytes");
  });

  it("requires explicit confirmation before deleting an original asset", () => {
    const fetchImpl = vi.fn();
    expect(() => executeOriginalAssetRetention({
      assetId: "original-file-safe",
      planId: `original-asset-retention-${"a".repeat(64)}`,
      expectedRevision: 4,
      confirm: false,
      fetchImpl,
    })).toThrow("explicit confirmation");
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("libraryOverviewApi Memory hierarchy", () => {
  it("loads encoded project options without sending content", async () => {
    const payload = {
      project_id: "项目 A/2026",
      series: [],
      scenarios: [],
      atoms: [],
      content_included: false,
      network_called: false,
    };
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue(payload),
    });

    await expect(loadMemoryHierarchyOptions({
      projectId: "项目 A/2026",
      fetchImpl,
    })).resolves.toBe(payload);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/projects/%E9%A1%B9%E7%9B%AE%20A%2F2026/memory-hierarchy-options",
      { headers: { Accept: "application/json" } },
    );
  });

  it("loads one Scenario series suggestion with encoded project and object IDs", async () => {
    const payload = {
      project_id: "项目 A/2026",
      suggestions: [],
      unresolved: [],
      content_included: false,
      writes_performed: false,
      network_called: false,
    };
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue(payload),
    });

    await expect(loadMemorySeriesSuggestions({
      projectId: "项目 A/2026",
      scenarioId: "场景 1/冷启",
      fetchImpl,
    })).resolves.toBe(payload);
    expect(fetchImpl).toHaveBeenCalledWith(
      (
        "/api/rebuild/projects/%E9%A1%B9%E7%9B%AE%20A%2F2026/"
        + "memory-series-suggestions?scenario_id=%E5%9C%BA%E6%99%AF%201%2F%E5%86%B7%E5%90%AF"
      ),
      { headers: { Accept: "application/json" } },
    );
  });

  it("loads one Series freshness analysis without a write request", async () => {
    const payload = {
      project_id: "项目 A/2026",
      items: [],
      writes_performed: false,
      network_called: false,
    };
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue(payload),
    });

    await expect(loadMemorySeriesFreshness({
      projectId: "项目 A/2026",
      seriesObjectId: "系列 1/总览",
      fetchImpl,
    })).resolves.toBe(payload);
    expect(fetchImpl).toHaveBeenCalledWith(
      (
        "/api/rebuild/projects/%E9%A1%B9%E7%9B%AE%20A%2F2026/"
        + "memory-series-freshness?series_object_id=%E7%B3%BB%E5%88%97%201%2F%E6%80%BB%E8%A7%88"
      ),
      { headers: { Accept: "application/json" } },
    );
  });

  it("previews and creates a revision-locked Scenario transfer plan", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: vi.fn().mockResolvedValue({ target_options: [] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: vi.fn().mockResolvedValue({ plan_id: "transfer-1" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: vi.fn().mockResolvedValue({ status: "series_refresh_required" }),
      });

    await loadMemoryScenarioTransferPreview({
      projectId: "项目 A/2026",
      scenarioObjectId: "场景 1/冷启",
      targetSeriesObjectId: "系列 2/恢复",
      fetchImpl,
    });
    expect(fetchImpl.mock.calls[0][0]).toBe(
      (
        "/api/rebuild/projects/%E9%A1%B9%E7%9B%AE%20A%2F2026/"
        + "memory-scenario-transfer-preview?"
        + "scenario_object_id=%E5%9C%BA%E6%99%AF+1%2F%E5%86%B7%E5%90%AF"
        + "&target_series_object_id=%E7%B3%BB%E5%88%97+2%2F%E6%81%A2%E5%A4%8D"
      ),
    );

    await createMemoryScenarioTransferPlan({
      projectId: "project-alpha",
      scenarioObjectId: "scenario-1",
      targetSeriesObjectId: "series-object-2",
      expectedScenarioObjectRevision: 8,
      expectedScenarioRevision: 3,
      expectedSourceSeriesObjectRevision: 5,
      expectedSourceSeriesRevision: 2,
      expectedTargetSeriesObjectRevision: 9,
      expectedTargetSeriesRevision: 4,
      fetchImpl,
    });
    expect(JSON.parse(fetchImpl.mock.calls[1][1].body)).toEqual({
      project_id: "project-alpha",
      scenario_object_id: "scenario-1",
      target_series_object_id: "series-object-2",
      expected_scenario_object_revision: 8,
      expected_scenario_revision: 3,
      expected_source_series_object_revision: 5,
      expected_source_series_revision: 2,
      expected_target_series_object_revision: 9,
      expected_target_series_revision: 4,
      confirmed: true,
    });

    await loadMemoryScenarioTransferPlan({ planId: "transfer 1", fetchImpl });
    expect(fetchImpl.mock.calls[2][0]).toBe(
      "/api/rebuild/memory-scenario-transfer-plans/transfer%201",
    );
  });

  it("sends explicit Scenario hierarchy bindings during review", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ status: "promoted" }),
    });

    await reviewMemoryCandidate({
      candidateId: "candidate 1",
      action: "promote_to_scenario",
      reason: "用户确认。",
      seriesId: "series-1",
      atomIds: ["atom-1"],
      fetchImpl,
    });

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/memory-candidates/candidate%201/review",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          action: "promote_to_scenario",
          reason: "用户确认。",
          series_id: "series-1",
          atom_ids: ["atom-1"],
        }),
      }),
    );
  });

  it("assigns an imported legacy candidate with explicit project CAS confirmation", async () => {
    const payload = {
      status: "project_assigned",
      candidate_revision: 4,
      candidate: { project_id: "project-alpha" },
    };
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue(payload),
    });

    await expect(assignLegacyMemoryCandidateProject({
      candidateId: "legacy candidate",
      projectId: "project-alpha",
      expectedRevision: 3,
      fetchImpl,
    })).resolves.toBe(payload);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/memory/candidates/review",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          candidate_id: "legacy candidate",
          action: "assign_project",
          project_id: "project-alpha",
          expected_revision: 3,
          confirm: true,
        }),
      }),
    );
  });

  it("loads read-only project options without candidate content", async () => {
    const payload = {
      projects: ["project-alpha"],
      content_included: false,
      read_only: true,
    };
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue(payload),
    });

    await expect(loadMemoryCandidateProjectOptions({ fetchImpl })).resolves.toBe(payload);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/memory/project-options",
      { headers: { Accept: "application/json" } },
    );
  });

  it("creates a confirmed hierarchy update candidate with both CAS revisions", async () => {
    const payload = { status: "pending_review", candidate_id: "candidate-update-1" };
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue(payload),
    });

    await expect(createMemoryHierarchyUpdateCandidate({
      projectId: "project-alpha",
      layer: "scenario",
      objectId: "scenario-1",
      expectedObjectRevision: 4,
      expectedDomainRevision: 2,
      seriesId: "series-1",
      atomIds: ["atom-2"],
      fetchImpl,
    })).resolves.toBe(payload);

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/memory-hierarchy/update-candidates",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          project_id: "project-alpha",
          layer: "scenario",
          object_id: "scenario-1",
          expected_object_revision: 4,
          expected_domain_revision: 2,
          series_id: "series-1",
          atom_ids: ["atom-2"],
          scenario_ids: [],
          confirmed: true,
        }),
      }),
    );
  });

  it("includes an edited overview in a Series refresh candidate", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ candidate_id: "refresh-1" }),
    });

    await createMemoryHierarchyUpdateCandidate({
      projectId: "project-alpha",
      layer: "series_memory",
      objectId: "series-1",
      expectedObjectRevision: 8,
      expectedDomainRevision: 2,
      scenarioIds: ["scenario-1"],
      proposedOverview: "用户编辑后的 L3 总览。",
      fetchImpl,
    });

    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual({
      project_id: "project-alpha",
      layer: "series_memory",
      object_id: "series-1",
      expected_object_revision: 8,
      expected_domain_revision: 2,
      atom_ids: [],
      scenario_ids: ["scenario-1"],
      proposed_overview: "用户编辑后的 L3 总览。",
      confirmed: true,
    });
  });

  it("preserves hierarchy update conflict details for the editor", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: vi.fn().mockResolvedValue({
        detail: "stale hierarchy revision",
        current_object_revision: 5,
      }),
    });

    await expect(createMemoryHierarchyUpdateCandidate({
      projectId: "project-alpha",
      layer: "series_memory",
      objectId: "series-1",
      expectedObjectRevision: 3,
      expectedDomainRevision: 1,
      scenarioIds: [],
      fetchImpl,
    })).rejects.toMatchObject({
      message: "stale hierarchy revision",
      status: 409,
      payload: {
        current_object_revision: 5,
      },
    });
  });

  it("creates a confirmed batch with independent Scenario CAS inputs", async () => {
    const payload = {
      status: "partially_completed",
      succeeded_count: 1,
      failed_count: 1,
      results: [],
    };
    const items = [
      {
        scenario_id: "scenario-1",
        expected_object_revision: 7,
        expected_domain_revision: 2,
        target_series_id: "series-2",
        current_atom_ids: ["atom-1"],
      },
      {
        scenario_id: "scenario-2",
        expected_object_revision: 4,
        expected_domain_revision: 1,
        target_series_id: "series-3",
        current_atom_ids: [],
      },
    ];
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue(payload),
    });

    await expect(createMemoryHierarchyUpdateCandidateBatch({
      projectId: "project-alpha",
      items,
      fetchImpl,
    })).resolves.toBe(payload);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/memory-hierarchy/update-candidates/batch",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          project_id: "project-alpha",
          items,
          confirmed: true,
        }),
      }),
    );
  });
});

describe("libraryOverviewApi Document archive helpers", () => {
  afterEach(() => {
    delete globalThis.electronAPI;
    vi.restoreAllMocks();
  });

  it("builds dedicated archived and lifecycle URLs", () => {
    expect(archivedDocumentsUrl()).toBe(ARCHIVED_DOCUMENTS_ENDPOINT);
    expect(documentLifecycleUrl("document 1", "archive")).toBe(
      "/api/rebuild/documents/document%201/archive",
    );
  });

  it("loads archived documents and archives with expected revision", async () => {
    const archivedPayload = { status: "archived_documents", items: [] };
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: vi.fn().mockResolvedValue(archivedPayload) })
      .mockResolvedValueOnce({ ok: true, json: vi.fn().mockResolvedValue({ revision: 2 }) });

    await expect(loadArchivedDocuments({ fetchImpl })).resolves.toBe(archivedPayload);
    await expect(
      archiveDocument({ documentId: "document 1", expectedRevision: 1, fetchImpl }),
    ).resolves.toEqual({ revision: 2 });

    expect(fetchImpl).toHaveBeenNthCalledWith(1, ARCHIVED_DOCUMENTS_ENDPOINT, {
      headers: { Accept: "application/json" },
    });
    expect(fetchImpl).toHaveBeenNthCalledWith(
      2,
      "/api/rebuild/documents/document%201/archive",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ expected_revision: 1 }) }),
    );
  });

  it("restores with CAS and exposes server conflicts", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: vi.fn().mockResolvedValue({ reason: "expected revision 2, found 3" }),
    });

    await expect(
      restoreDocument({ documentId: "doc-1", expectedRevision: 2, fetchImpl }),
    ).rejects.toMatchObject({ status: 409, message: "expected revision 2, found 3" });
  });
});

describe("libraryOverviewApi Source metadata edit", () => {
  it("sends a bounded CAS payload", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ status: "updated", revision: 3 }),
    });
    await updateLibrarySourceMetadata({
      sourceId: "source 1", expectedRevision: 2, title: " 新标题 ", seriesName: " 系列 ", tags: ["A"], fetchImpl,
    });
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/library/sources/source%201/metadata",
      expect.objectContaining({ method: "PUT" }),
    );
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual({
      expected_revision: 2, title: "新标题", series_name: "系列", tags: ["A"],
    });
  });

  it("preserves 409 conflict details", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false, status: 409, json: vi.fn().mockResolvedValue({ error: "source revision changed", revision: 3 }),
    });
    await expect(updateLibrarySourceMetadata({
      sourceId: "source-1", expectedRevision: 2, title: "旧稿", fetchImpl,
    })).rejects.toMatchObject({ status: 409, payload: { revision: 3 } });
  });
});

describe("libraryOverviewApi daily reminders helpers", () => {
  afterEach(() => {
    delete globalThis.electronAPI;
    delete globalThis.electronAPI;
    vi.restoreAllMocks();
  });

  it("builds the default daily reminders endpoint", () => {
    expect(dailyRemindersUrl()).toBe(DAILY_REMINDERS_ENDPOINT);
  });

  it("adds project and limit query params", () => {
    expect(dailyRemindersUrl({ projectId: "project alpha", limit: 6 })).toBe(
      "/api/rebuild/reminders/today?project_id=project+alpha&limit=6",
    );
  });

  it("uses the electron backend base url when available", () => {
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001/" };

    expect(dailyRemindersUrl({ projectId: "project-alpha" })).toBe(
      "http://127.0.0.1:8001/api/rebuild/reminders/today?project_id=project-alpha",
    );
  });

  it("loads daily reminders as read-only json", async () => {
    const payload = {
      status: "ready",
      reminders: [{ reminder_type: "source_content_read_needed" }],
    };
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue(payload),
    });

    await expect(loadDailyReminders({ projectId: "project-alpha", limit: 5, fetchImpl })).resolves.toBe(
      payload,
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/reminders/today?project_id=project-alpha&limit=5",
      {
        headers: { Accept: "application/json" },
      },
    );
  });

  it("throws a clear error for failed daily reminders requests", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, status: 503 });

    await expect(loadDailyReminders({ fetchImpl })).rejects.toThrow(
      "Daily reminders failed with 503",
    );
  });
});

// ── Phase 5 子项1：搜索优先 active FTS5 ──
describe("libraryOverviewApi library search helpers", () => {
  afterEach(() => {
    delete globalThis.electronAPI;
    delete globalThis.electronAPI;
    vi.restoreAllMocks();
  });

  it("exposes the library search endpoint constant", () => {
    expect(LIBRARY_SEARCH_ENDPOINT).toBe("/api/rebuild/library/search");
  });

  it("builds a minimal search url with only query", () => {
    expect(librarySearchUrl({ query: "react hooks" })).toBe(
      "/api/rebuild/library/search?q=react+hooks",
    );
  });

  it("trims whitespace from query before encoding", () => {
    expect(librarySearchUrl({ query: "  react  " })).toBe(
      "/api/rebuild/library/search?q=react",
    );
  });

  it("builds url without q when query is empty", () => {
    expect(librarySearchUrl({ query: "" })).toBe(LIBRARY_SEARCH_ENDPOINT);
    expect(librarySearchUrl({ query: "   " })).toBe(LIBRARY_SEARCH_ENDPOINT);
    expect(librarySearchUrl({})).toBe(LIBRARY_SEARCH_ENDPOINT);
  });

  it("appends project_id, layers, trust, and limit params", () => {
    const url = librarySearchUrl({
      query: "记忆",
      projectId: "project-alpha",
      layers: ["atom", "scenario"],
      trust: ["user_confirmed"],
      limit: 20,
    });
    expect(url).toContain("q=" + encodeURIComponent("记忆"));
    expect(url).toContain("project_id=project-alpha");
    expect(url).toContain("layers=atom%2Cscenario");
    expect(url).toContain("trust=user_confirmed");
    expect(url).toContain("limit=20");
  });

  it("passes library page filters and offset without changing legacy search URLs", () => {
    const url = librarySearchUrl({
      query: "视频正文", projectId: "project-alpha", scope: "overview",
      filterId: "video", tag: "课程", importBatchId: "batch-1", offset: 30, limit: 30,
    });
    const params = new URL(url, "http://localhost").searchParams;
    expect(Object.fromEntries(params)).toMatchObject({
      q: "视频正文", project_id: "project-alpha", scope: "overview", filter_id: "video",
      tag: "课程", import_batch_id: "batch-1", offset: "30", limit: "30",
    });
    expect(librarySearchUrl({ query: "视频正文", offset: 30 })).not.toContain("offset=");
  });

  it("filters out falsy layer and trust values", () => {
    const url = librarySearchUrl({
      query: "test",
      layers: ["atom", "", null, "scenario"],
      trust: ["", undefined, "user_confirmed"],
    });
    expect(url).toContain("layers=atom%2Cscenario");
    expect(url).toContain("trust=user_confirmed");
  });

  it("omits layers and trust params when arrays are empty", () => {
    const url = librarySearchUrl({
      query: "test",
      layers: [],
      trust: [],
    });
    expect(url).not.toContain("layers=");
    expect(url).not.toContain("trust=");
  });

  it("omits limit when not a positive integer", () => {
    expect(librarySearchUrl({ query: "test", limit: 0 })).not.toContain("limit=");
    expect(librarySearchUrl({ query: "test", limit: -1 })).not.toContain("limit=");
    expect(librarySearchUrl({ query: "test", limit: 1.5 })).not.toContain("limit=");
    expect(librarySearchUrl({ query: "test", limit: "12" })).not.toContain("limit=");
  });

  it("uses electron backend base url when available", () => {
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001/" };
    expect(librarySearchUrl({ query: "test" })).toBe(
      "http://127.0.0.1:8001/api/rebuild/library/search?q=test",
    );
  });

  it("uses Electron backend base url when available", () => {
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    expect(librarySearchUrl({ query: "test" })).toBe(
      "http://127.0.0.1:8001/api/rebuild/library/search?q=test",
    );
  });

  it("searchLibrary sends GET with Accept json header", async () => {
    const payload = {
      status: "ready",
      backend: "sqlite_fts5",
      query: "react hooks",
      total: 2,
      index_stale: false,
      reason: "",
      hits: [
        {
          object_id: "atom-001",
          layer: "atom",
          content: "React hooks are useful",
          source_refs: [],
          trust_status: "user_confirmed",
          score: 0.95,
          backend: "sqlite_fts5",
        },
      ],
    };
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue(payload),
    });

    await expect(searchLibrary({ query: "react hooks", fetchImpl })).resolves.toBe(payload);

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/library/search?q=react+hooks",
      { headers: { Accept: "application/json" } },
    );
  });

  it("searchLibrary throws a clear error for failed requests", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, status: 500 });
    await expect(searchLibrary({ query: "test", fetchImpl })).rejects.toThrow(
      "Library search failed with 500",
    );
  });

  it("searchLibrary does not leak sensitive fields in the request url", () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({ hits: [] }),
    });
    searchLibrary({
      query: "test",
      projectId: "project-secret",
      fetchImpl,
    });
    const calledUrl = fetchImpl.mock.calls[0][0];
    expect(calledUrl).not.toMatch(/sk-|api_key|cookie|authorization|bearer|password|token/i);
  });
});
