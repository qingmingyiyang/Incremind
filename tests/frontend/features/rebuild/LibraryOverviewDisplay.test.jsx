import { describe, expect, it, vi } from "vitest";
import { AUDIO_ASSET_TRANSCRIPTION_ENDPOINT_PREFIX, BILIBILI_VIDEO_DOWNLOAD_PLAN_ENDPOINT, BOOKMARK_COLLECTION_WEB_CONTENT_RUN_ENDPOINT_PREFIX, DOCUMENT_DETAIL_ENDPOINT_PREFIX, EXTERNAL_AGENT_REVIEW_DRAFT_ENDPOINT_PREFIX, FOUR_LAYER_PROVIDER_CANDIDATE_ENDPOINT_PREFIX, FOUR_LAYER_PROVIDER_STATUS_ENDPOINT, MEDIA_OUTPUT_TEMPLATE_DOCUMENT_ENDPOINT_PREFIX, PROVIDER_SOURCE_TEMPLATE_DOCUMENT_ENDPOINT_PREFIX, SOURCE_CONTENT_STRUCTURE_ENDPOINT_PREFIX, SOURCE_TEMPLATE_MEMORY_CANDIDATE_ENDPOINT_PREFIX, SOURCE_TEMPLATE_DOCUMENT_ENDPOINT_PREFIX, SOURCE_FILE_AUTHORIZATION_ENDPOINT_PREFIX, SOURCE_OUTPUT_MEMORY_CANDIDATE_ENDPOINT_PREFIX, TRANSCRIPT_SUMMARY_ENDPOINT_PREFIX, VIDEO_AUDIO_EXTRACTION_ENDPOINT_PREFIX, LIBRARY_OVERVIEW_ENDPOINT, LOCAL_ASR_PROVIDER_SETTINGS_ENDPOINT, LOCAL_DOCUMENT_TEXT_EXTRACTOR_SETTINGS_ENDPOINT, LOCAL_OCR_PROVIDER_SETTINGS_ENDPOINT, LOCAL_VIDEO_PROVIDER_SETTINGS_ENDPOINT, authorizeLocalFileForSource, bookmarkCollectionWebContentRunUrl, createBilibiliVideoDownloadPlan, createFourLayerProviderCandidates, createMediaOutputTemplateDocument, createProviderSourceTemplateDocument, applyExternalAgentReviewDraft, createLocalExtractiveAnswer, createDocumentFromModelResult, createMemoryCandidateFromModelResult, createSourceContentQaRecall, createSourceTemplateMemoryCandidate, createSourceTemplateDocument, confirmSourceSeriesAssignment, structureSourceContent, createSourceOutputMemoryCandidate, extractVideoAudioTrack, documentDetailUrl, externalAgentReviewDraftApplyUrl, externalAgentReviewDraftPreviewUrl, libraryOverviewUrl, loadLocalAsrProviderSettings, loadLocalDocumentTextExtractorSettings, loadLocalOcrProviderSettings, loadLocalVideoProviderSettings, loadLibraryOverview, loadEditableDocument, loadEditableDocumentHtml, exportEditableDocumentHtml, createEditableDocumentDelivery, loadEditableDocumentDeliveryArtifact, loadDocumentPdfArtifact, documentHtmlUrl, documentHtmlExportUrl, documentDeliveryUrl, documentDeliveryArtifactUrl, documentPdfArtifactUrl, loadExternalAgentReviewDraftPreview, loadFourLayerProviderStatus, linkWebContentRunUrl, mediaOutputTemplateDocumentUrl, localAsrRunUrl, localDocumentTextRunUrl, localOcrRunUrl, localVideoRunUrl, memoryPublicationUrl, memoryCandidateReviewUrl, memoryRollbackUrl, publishStagingMemory, publishStagingAtom, reviewMemoryCandidate, rollbackMemoryPublication, summarizeTranscriptOutput, transcribeAudioAsset, runLocalAsrForSource, readLinkWebContentForSource, readBookmarkCollectionWebContentForSource, runLocalDocumentTextExtractorForSource, runLocalOcrForSource, runLocalVideoForSource, saveEditableDocument, sourceOutputMemoryCandidateUrl, fourLayerProviderCandidateUrl, sourceContentQaRecallUrl, sourceContentStructureUrl, sourceTemplateDocumentUrl, providerSourceTemplateDocumentUrl, sourceTemplateMemoryCandidateUrl, sourceSeriesAssignmentUrl, localExtractiveAnswerUrl, modelResultDocumentUrl, modelResultMemoryCandidateUrl, sourceFileAuthorizationUrl, audioAssetTranscriptionUrl, bilibiliVideoDownloadPlanUrl, transcriptSummaryUrl, videoAudioExtractionUrl } from "@src/features/rebuild/libraryOverviewApi";

function libraryOverview() {
  return {
    status: "ready",
    scope: "all",
    project_id: null,
    counts: {
      total: 4,
      source: 1,
      document: 1,
      memory_candidate: 1,
      atom: 1,
    },
    blocked_operations: [
      "source_content_read",
      "parser_execution",
      "media_processing_provider_execution",
      "model_provider_execution",
      "memory_publication",
      "legacy_library_write",
    ],
    next_step_boundary: "library_all_items_ready_without_content_read",
    items: [
      {
        item_id: "source-text-001",
        item_type: "source",
        title: "Library overview source",
        project_id: null,
        status: "captured",
        trust_status: null,
        source_refs: ["source-text-001#source:metadata"],
        trace_refs: ["crp://default/sources/source-text-001"],
        blocked_operations: ["source_content_read", "parser_execution", "memory_publication"],
      },
      {
        item_id: "document-summary-001",
        item_type: "document",
        title: "Library overview document",
        project_id: "project-alpha",
        status: "draft",
        trust_status: null,
        source_refs: [
          "source-text-001#source:metadata",
          "source-text-001#job:job-library-overview-001",
        ],
        trace_refs: ["crp://default/documents/document-summary-001.json"],
        blocked_operations: ["document_overwrite", "source_content_read", "memory_publication"],
      },
      {
        item_id: "memory-candidate-001",
        item_type: "memory_candidate",
        title: "Library overview candidate",
        project_id: "project-alpha",
        status: "pending_review",
        import_batch_id: "roundtrip-a1b2c3d4e5f6",
        target_layer: "atom",
        trust_status: null,
        source_refs: ["source-text-001#source:metadata"],
        trace_refs: ["crp://default/memory-candidates/memory-candidate-001.json"],
        blocked_operations: ["auto_promote_memory", "memory_publication", "source_content_read"],
      },
      {
        item_id: "atom-library-overview-001",
        item_type: "atom",
        title: "Published Atom remains traceable",
        project_id: "project-alpha",
        status: "published",
        trust_status: "user_confirmed",
        source_refs: ["source-text-001#source:metadata"],
        trace_refs: ["crp://default/memory/atom/atom-library-overview-001.json"],
        blocked_operations: ["memory_mutation", "source_content_read"],
      },
    ],
  };
}

describe("loadLibraryOverview", () => {
it("loads the overview from the narrow backend endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => libraryOverview(),
      }),
    );

    const result = await loadLibraryOverview({ fetchImpl });

    expect(result.status).toBe("ready");
    expect(fetchImpl).toHaveBeenCalledWith(
      LIBRARY_OVERVIEW_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });
it("fails safely when an unavailable endpoint has no JSON body", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: false,
        status: 503,
      }),
    );

    await expect(loadLibraryOverview({ fetchImpl })).rejects.toThrow("暂时无法读取资料库，请稍后重试。");
  });
it("targets the worker backend URL in desktop mode", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(libraryOverviewUrl({ projectId: "project alpha" })).toBe(
        `http://127.0.0.1:8001${LIBRARY_OVERVIEW_ENDPOINT}?project_id=project%20alpha`,
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("posts memory candidate review actions to the narrow backend endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          candidate_id: "memory-candidate-001",
          status: "promoted",
          promoted_object_id: "atom-draft-001",
        }),
      }),
    );

    const result = await reviewMemoryCandidate({
      candidateId: "memory-candidate-001",
      action: "promote_to_atom",
      reason: "用户确认。",
      fetchImpl,
    });

    expect(result.status).toBe("promoted");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/memory-candidates/memory-candidate-001/review",
      expect.objectContaining({
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          action: "promote_to_atom",
          reason: "用户确认。",
        }),
      }),
    );
  });
it("builds desktop memory candidate review URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(memoryCandidateReviewUrl("memory candidate 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/memory-candidates/memory%20candidate%20001/review",
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("loads external Agent review draft previews as a read-only request", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "ready",
          read_only: true,
          preview: {
            target_kind: "project_skill",
            changed_fields: ["style_preferences"],
          },
        }),
      }),
    );

    const result = await loadExternalAgentReviewDraftPreview({
      draftId: "draft 001",
      fetchImpl,
    });

    expect(result.read_only).toBe(true);
    expect(result.preview.changed_fields).toContain("style_preferences");
    expect(fetchImpl).toHaveBeenCalledWith(
      `${EXTERNAL_AGENT_REVIEW_DRAFT_ENDPOINT_PREFIX}/draft%20001/preview`,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });
it("applies external Agent review drafts only with explicit confirmation payload", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "applied",
          draft_id: "draft-001",
        }),
      }),
    );

    const result = await applyExternalAgentReviewDraft({
      draftId: "draft-001",
      expectedRevision: 1,
      reason: "用户确认应用外部 Agent 草稿。",
      fetchImpl,
    });

    expect(result.status).toBe("applied");
    expect(fetchImpl).toHaveBeenCalledWith(
      `${EXTERNAL_AGENT_REVIEW_DRAFT_ENDPOINT_PREFIX}/draft-001/apply`,
      expect.objectContaining({
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          confirm: true,
          expected_revision: 1,
          reason: "用户确认应用外部 Agent 草稿。",
        }),
      }),
    );
  });
it("requires expected revision before applying external Agent review drafts", async () => {
    const fetchImpl = vi.fn();

    await expect(
      applyExternalAgentReviewDraft({
        draftId: "draft-001",
        fetchImpl,
      }),
    ).rejects.toThrow("expectedRevision is required");

    expect(fetchImpl).not.toHaveBeenCalled();
  });
it("builds desktop external Agent review draft URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(externalAgentReviewDraftPreviewUrl("draft 001")).toBe(
        `http://127.0.0.1:8001${EXTERNAL_AGENT_REVIEW_DRAFT_ENDPOINT_PREFIX}/draft%20001/preview`,
      );
      expect(externalAgentReviewDraftApplyUrl("draft 001")).toBe(
        `http://127.0.0.1:8001${EXTERNAL_AGENT_REVIEW_DRAFT_ENDPOINT_PREFIX}/draft%20001/apply`,
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("posts staging atom publication with explicit confirmation", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "published",
          published_object_id: "atom-draft-001",
        }),
      }),
    );

    const result = await publishStagingAtom({
      atomId: "atom-draft-001",
      reason: "用户确认发布。",
      fetchImpl,
    });

    expect(result.status).toBe("published");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/staging-atoms/atom-draft-001/publication",
      expect.objectContaining({
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          confirm: true,
          reason: "用户确认发布。",
        }),
      }),
    );
  });
it("posts layered staging memory publication with explicit confirmation", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "published",
          layer: "scenario",
          published_object_id: "scenario-draft-001",
        }),
      }),
    );

    const result = await publishStagingMemory({
      objectId: "scenario-draft-001",
      layer: "scenario",
      reason: "用户确认发布场景记忆。",
      fetchImpl,
    });

    expect(result.status).toBe("published");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/staging-scenarios/scenario-draft-001/publication",
      expect.objectContaining({
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          confirm: true,
          reason: "用户确认发布场景记忆。",
        }),
      }),
    );
  });
it("posts memory rollback with explicit confirmation", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "rolled_back",
          object_id: "atom-draft-001",
        }),
      }),
    );

    const result = await rollbackMemoryPublication({
      publicationId: "memory-publication-atom-draft-001",
      reason: "用户撤回长期记忆。",
      fetchImpl,
    });

    expect(result.status).toBe("rolled_back");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/memory-publications/memory-publication-atom-draft-001/rollback",
      expect.objectContaining({
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          confirm: true,
          reason: "用户撤回长期记忆。",
        }),
      }),
    );
  });
it("builds desktop staging atom publication URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(memoryPublicationUrl("atom draft 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/staging-atoms/atom%20draft%20001/publication",
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop layered staging memory publication URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(memoryPublicationUrl("scenario draft 001", "scenario")).toBe(
        "http://127.0.0.1:8001/api/rebuild/staging-scenarios/scenario%20draft%20001/publication",
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop memory rollback URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(memoryRollbackUrl("memory publication 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/memory-publications/memory%20publication%20001/rollback",
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop local document text extraction URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(localDocumentTextRunUrl("source pdf 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/sources/source%20pdf%20001/document-text",
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop link web content read URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(linkWebContentRunUrl("source link 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/sources/source%20link%20001/web-content",
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop bookmark collection web content read URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(bookmarkCollectionWebContentRunUrl("source collection 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/sources/source%20collection%20001/collection-web-content",
      );
      expect(BOOKMARK_COLLECTION_WEB_CONTENT_RUN_ENDPOINT_PREFIX).toBe("/api/rebuild/sources");
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop source content QA recall URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(sourceContentQaRecallUrl("source docx 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/sources/source%20docx%20001/qa-recall",
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop source content structure URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(sourceContentStructureUrl("source docx 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/sources/source%20docx%20001/structure-content",
      );
      expect(SOURCE_CONTENT_STRUCTURE_ENDPOINT_PREFIX).toBe("/api/rebuild/sources");
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop local extractive answer URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(localExtractiveAnswerUrl("model request 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/model-requests/model%20request%20001/local-answer",
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop model result handoff URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(modelResultDocumentUrl("model result 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/model-results/model%20result%20001/document",
      );
      expect(modelResultMemoryCandidateUrl("model result 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/model-results/model%20result%20001/memory-candidate",
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("loads local OCR Provider settings from the narrow backend endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "disabled",
          enabled: false,
          diagnostic: "disabled_until_explicit_enable",
        }),
      }),
    );

    const result = await loadLocalOcrProviderSettings({ fetchImpl });

    expect(result.status).toBe("disabled");
    expect(fetchImpl).toHaveBeenCalledWith(
      LOCAL_OCR_PROVIDER_SETTINGS_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });
it("loads local ASR Provider settings from the narrow backend endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "disabled",
          enabled: false,
          diagnostic: "disabled_until_explicit_enable",
        }),
      }),
    );

    const result = await loadLocalAsrProviderSettings({ fetchImpl });

    expect(result.status).toBe("disabled");
    expect(fetchImpl).toHaveBeenCalledWith(
      LOCAL_ASR_PROVIDER_SETTINGS_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });
it("loads local video Provider settings from the narrow backend endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "disabled",
          enabled: false,
          diagnostic: "disabled_until_explicit_enable",
        }),
      }),
    );

    const result = await loadLocalVideoProviderSettings({ fetchImpl });

    expect(result.status).toBe("disabled");
    expect(fetchImpl).toHaveBeenCalledWith(
      LOCAL_VIDEO_PROVIDER_SETTINGS_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });
it("loads local document text extractor settings from the narrow backend endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "disabled",
          enabled: false,
          diagnostic: "disabled_until_explicit_enable",
        }),
      }),
    );

    const result = await loadLocalDocumentTextExtractorSettings({ fetchImpl });

    expect(result.status).toBe("disabled");
    expect(fetchImpl).toHaveBeenCalledWith(
      LOCAL_DOCUMENT_TEXT_EXTRACTOR_SETTINGS_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });
it("posts local OCR run without sending a provider command", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "completed",
          output_preview: "OCR 输出",
        }),
      }),
    );

    const result = await runLocalOcrForSource({
      sourceId: "source image 001",
      fetchImpl,
    });

    expect(result.status).toBe("completed");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20image%20001/ocr",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      }),
    );
  });
it("posts local ASR run without sending a provider command", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "completed",
          output_preview: "转写输出",
        }),
      }),
    );

    const result = await runLocalAsrForSource({
      sourceId: "source audio 001",
      fetchImpl,
    });

    expect(result.status).toBe("completed");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20audio%20001/transcription",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      }),
    );
  });
it("posts local video run without sending a provider command", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "completed",
          output_preview: "视频输出",
        }),
      }),
    );

    const result = await runLocalVideoForSource({
      sourceId: "source video 001",
      fetchImpl,
    });

    expect(result.status).toBe("completed");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20video%20001/frame-extraction",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      }),
    );
  });
it("posts local document text extraction without sending a provider command", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "completed",
          preview: "文档正文输出",
        }),
      }),
    );

    const result = await runLocalDocumentTextExtractorForSource({
      sourceId: "source pdf 001",
      fetchImpl,
    });

    expect(result.status).toBe("completed");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20pdf%20001/document-text",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      }),
    );
  });
it("posts link web content read without publishing memory", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "completed",
          content_read: true,
          preview: "网页正文输出",
        }),
      }),
    );

    const result = await readLinkWebContentForSource({
      sourceId: "source link 001",
      fetchImpl,
    });

    expect(result.status).toBe("completed");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20link%20001/web-content",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("publish");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("api");
  });
it("posts bookmark collection web content read without cookie provider or memory publication", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "completed",
          completed_count: 2,
          collection_item_count: 2,
          memory_publication_state: "not_published",
        }),
      }),
    );

    const result = await readBookmarkCollectionWebContentForSource({
      sourceId: "source collection 001",
      fetchImpl,
    });

    expect(result.status).toBe("completed");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20collection%20001/collection-web-content",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("cookie");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("provider");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("publish");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("api");
  });
it("posts Bilibili video download plan without downloading media", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "planned",
          video_id: "BV1abcDEF234",
          downloads_video: false,
        }),
      }),
    );

    const result = await createBilibiliVideoDownloadPlan({
      url: " https://www.bilibili.com/video/BV1abcDEF234 ",
      fetchImpl,
    });

    expect(result.status).toBe("planned");
    expect(fetchImpl).toHaveBeenCalledWith(
      BILIBILI_VIDEO_DOWNLOAD_PLAN_ENDPOINT,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          url: "https://www.bilibili.com/video/BV1abcDEF234",
          video_id: undefined,
        }),
      }),
    );
  });
it("posts video audio extraction without sending a provider command", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "completed",
          audio_asset_id: "audio-track-001",
        }),
      }),
    );

    const result = await extractVideoAudioTrack({
      sourceId: "source video 001",
      fetchImpl,
    });

    expect(result.audio_asset_id).toBe("audio-track-001");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20video%20001/audio-track",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      }),
    );
  });
it("posts generated audio asset transcription without sending a provider command", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "completed",
          output_id: "media-output-transcript-001",
        }),
      }),
    );

    const result = await transcribeAudioAsset({
      audioAssetId: "audio asset 001",
      fetchImpl,
    });

    expect(result.output_id).toBe("media-output-transcript-001");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/audio-assets/audio%20asset%20001/transcription",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      }),
    );
  });
it("posts transcript summary without sending a provider command", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "completed",
          output_id: "media-output-summary-001",
        }),
      }),
    );

    const result = await summarizeTranscriptOutput({
      transcriptOutputId: "media output transcript 001",
      fetchImpl,
    });

    expect(result.output_id).toBe("media-output-summary-001");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/media-processing-outputs/media%20output%20transcript%20001/summary",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      }),
    );
  });
it("posts source file authorization without sending a provider command", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "authorized",
          authorization_id: "authorized-image-source-image-001",
        }),
      }),
    );

    const result = await authorizeLocalFileForSource({
      sourceId: "source image 001",
      filePath: "D:\\media\\whiteboard.png",
      fetchImpl,
    });

    expect(result.status).toBe("authorized");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20image%20001/file-authorization",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ file_path: "D:\\media\\whiteboard.png" }),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("command");
  });
it("posts source output Memory Candidate creation without publication confirmation", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "candidate_created",
          candidate_id: "memory-candidate-from-output-001",
          candidate_status: "pending_review",
        }),
      }),
    );

    const result = await createSourceOutputMemoryCandidate({
      sourceId: "source image 001",
      evidenceKind: "media_processing_output",
      evidenceId: "media-output-ocr-source-image-001",
      projectId: "project-alpha",
      proposedContent: "OCR 输出候选。",
      fetchImpl,
    });

    expect(result.status).toBe("candidate_created");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20image%20001/memory-candidate",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          evidence_kind: "media_processing_output",
          evidence_id: "media-output-ocr-source-image-001",
          project_id: "project-alpha",
          proposed_content: "OCR 输出候选。",
          target_layer: "atom",
          candidate_type: "other",
        }),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("confirm");
  });
it("posts four-layer Provider Candidate creation without secrets or publication confirmation", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "provider_candidates_imported",
          import_result: {
            candidate_ids: ["memory-candidate-four-layer-001"],
            candidate_count: 1,
          },
        }),
      }),
    );

    const result = await createFourLayerProviderCandidates({
      sourceId: "source image 001",
      evidenceKind: "media_processing_output",
      evidenceId: "media-output-ocr-source-image-001",
      projectId: "project-alpha",
      fetchImpl,
    });

    expect(result.status).toBe("provider_candidates_imported");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20image%20001/four-layer-candidates",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          evidence_kind: "media_processing_output",
          evidence_id: "media-output-ocr-source-image-001",
          project_id: "project-alpha",
        }),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("api");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("confirm");
  });
it("posts source content QA recall without provider execution or publication confirmation", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "model_request_created",
          model_request_id: "model-request-answer-source-content-qa-001",
          qa_answer_state: "model_request_ready_no_answer_generated",
        }),
      }),
    );

    const result = await createSourceContentQaRecall({
      sourceId: "source docx 001",
      question: "资料库如何回答真实项目问题？",
      projectId: "project-alpha",
      projectSkillId: "skill-product-design",
      contentReadId: "content-read-source-docx-001",
      fetchImpl,
    });

    expect(result.status).toBe("model_request_created");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20docx%20001/qa-recall",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          question: "资料库如何回答真实项目问题？",
          project_id: "project-alpha",
          project_skill_id: "skill-product-design",
          content_read_id: "content-read-source-docx-001",
        }),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("command");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("api");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("confirm");
  });
it("posts source content structure without provider execution or publication confirmation", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "completed",
          tags: ["Memory", "Product"],
          series_candidate: "个人 AI 记忆工作台",
        }),
      }),
    );

    const result = await structureSourceContent({
      sourceId: "source docx 001",
      contentReadId: "content-read-source-docx-001",
      fetchImpl,
    });

    expect(result.status).toBe("completed");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20docx%20001/structure-content",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          content_read_id: "content-read-source-docx-001",
        }),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("command");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("api");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("confirm");
  });
it("posts source series assignment with explicit confirmation", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "confirmed",
          series_name: "个人 AI 记忆工作台",
          assignment_ref:
            "crp://default/source-series-assignments/series-assignment-source-docx-001.json",
        }),
      }),
    );

    const result = await confirmSourceSeriesAssignment({
      sourceId: "source docx 001",
      seriesName: "个人 AI 记忆工作台",
      reason: "用户确认",
      fetchImpl,
    });

    expect(result.status).toBe("confirmed");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20docx%20001/series-assignment",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          confirm: true,
          project_id: "default",
          series_name: "个人 AI 记忆工作台",
          reason: "用户确认",
        }),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("sk-");
    expect(sourceSeriesAssignmentUrl("source docx 001")).toBe(
      "/api/rebuild/sources/source%20docx%20001/series-assignment",
    );
  });
it("posts source template document creation without provider command or memory publication", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "document_created",
          template_type: "review",
          document_id: "document-review-001",
          document_revision: 1,
        }),
      }),
    );

    const result = await createSourceTemplateDocument({
      sourceId: "source docx 001",
      templateType: "review",
      fetchImpl,
    });

    expect(result.document_id).toBe("document-review-001");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20docx%20001/template-document",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          template_type: "review",
        }),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("command");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("api");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("confirm");
    expect(sourceTemplateDocumentUrl("source docx 001")).toBe(
      "/api/rebuild/sources/source%20docx%20001/template-document",
    );
    expect(SOURCE_TEMPLATE_DOCUMENT_ENDPOINT_PREFIX).toBe("/api/rebuild/sources");
  });
it("posts provider source template document without API key or cookie payload", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "document_created",
          template_type: "project_summary",
          document_id: "document-provider-template-001",
          provider_enhanced: true,
        }),
      }),
    );

    const result = await createProviderSourceTemplateDocument({
      sourceId: "source provider 001",
      templateType: "project_summary",
      fetchImpl,
    });

    expect(result.document_id).toBe("document-provider-template-001");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/sources/source%20provider%20001/provider-template-document",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          template_type: "project_summary",
        }),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("api_key");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("cookie");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("sk-");
    expect(providerSourceTemplateDocumentUrl("source provider 001")).toBe(
      "/api/rebuild/sources/source%20provider%20001/provider-template-document",
    );
    expect(PROVIDER_SOURCE_TEMPLATE_DOCUMENT_ENDPOINT_PREFIX).toBe("/api/rebuild/sources");
  });
it("posts media output template document without provider command or secret payload", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "document_created",
          template_type: "project_summary",
          media_output_id: "media-output-summary-001",
          document_id: "document-media-template-001",
        }),
      }),
    );

    const result = await createMediaOutputTemplateDocument({
      outputId: "media output summary 001",
      templateType: "project_summary",
      fetchImpl,
    });

    expect(result.document_id).toBe("document-media-template-001");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/media-processing-outputs/media%20output%20summary%20001/template-document",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          template_type: "project_summary",
        }),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("command");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("api_key");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("cookie");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("sk-");
    expect(mediaOutputTemplateDocumentUrl("media output summary 001")).toBe(
      "/api/rebuild/media-processing-outputs/media%20output%20summary%20001/template-document",
    );
    expect(MEDIA_OUTPUT_TEMPLATE_DOCUMENT_ENDPOINT_PREFIX).toBe("/api/rebuild/media-processing-outputs");
  });
it("posts source template memory candidate without provider command or secret payload", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "candidate_created",
          candidate_id: "memory-candidate-template-001",
          target_layer: "project_skill",
        }),
      }),
    );

    const result = await createSourceTemplateMemoryCandidate({
      documentId: "document template 001",
      documentRevision: 3,
      targetLayer: "project_skill",
      candidateType: "document_takeaway",
      fetchImpl,
    });

    expect(result.candidate_id).toBe("memory-candidate-template-001");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/documents/document%20template%20001/template-memory-candidate",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          document_revision: 3,
          target_layer: "project_skill",
          candidate_type: "document_takeaway",
        }),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("command");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("api");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("cookie");
    expect(sourceTemplateMemoryCandidateUrl("document template 001")).toBe(
      "/api/rebuild/documents/document%20template%20001/template-memory-candidate",
    );
    expect(SOURCE_TEMPLATE_MEMORY_CANDIDATE_ENDPOINT_PREFIX).toBe("/api/rebuild/documents");
  });
it("posts local extractive answer without provider command or API key", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "completed",
          model_result_id: "model-result-local-answer-001",
          output_preview: "基于已召回证据的本地回答。",
        }),
      }),
    );

    const result = await createLocalExtractiveAnswer({
      modelRequestId: "model request 001",
      fetchImpl,
    });

    expect(result.model_result_id).toBe("model-result-local-answer-001");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/model-requests/model%20request%20001/local-answer",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("command");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("api");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("confirm");
  });
it("posts model result document handoff without publishing memory", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "document_created",
          document_id: "document-answer-001",
          document_revision: 1,
        }),
      }),
    );

    const result = await createDocumentFromModelResult({
      modelResultId: "model result 001",
      title: "回答文档",
      fetchImpl,
    });

    expect(result.document_id).toBe("document-answer-001");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/model-results/model%20result%20001/document",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ title: "回答文档" }),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("confirm");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("api");
  });
it("posts model result Memory Candidate handoff without publication confirmation", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "candidate_created",
          candidate_id: "memory-candidate-answer-001",
          candidate_status: "pending_review",
        }),
      }),
    );

    const result = await createMemoryCandidateFromModelResult({
      modelResultId: "model result 001",
      targetLayer: "atom",
      candidateType: "answer_fact",
      fetchImpl,
    });

    expect(result.candidate_id).toBe("memory-candidate-answer-001");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/model-results/model%20result%20001/memory-candidate",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          target_layer: "atom",
          candidate_type: "answer_fact",
        }),
      }),
    );
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("confirm");
    expect(fetchImpl.mock.calls[0][1].body).not.toContain("publish");
  });
it("loads four-layer Provider status without returning key material", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "ready",
          has_api_key: true,
          credential_source: "secret",
          key_material_returned: false,
        }),
      }),
    );

    const result = await loadFourLayerProviderStatus({ fetchImpl });

    expect(result.status).toBe("ready");
    expect(result.key_material_returned).toBe(false);
    expect(fetchImpl).toHaveBeenCalledWith(
      FOUR_LAYER_PROVIDER_STATUS_ENDPOINT,
      expect.objectContaining({
        headers: { Accept: "application/json" },
      }),
    );
    expect(JSON.stringify(result)).not.toContain("sk-");
  });
it("loads editable document detail through the document endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "document_ready",
          document_id: "document 001",
          revision: 1,
          markdown: "# Editable document",
        }),
      }),
    );

    const result = await loadEditableDocument({
      documentId: "document 001",
      fetchImpl,
    });

    expect(result.revision).toBe(1);
    expect(documentDetailUrl("document 001")).toBe("/api/rebuild/documents/document%20001");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/rebuild/documents/document%20001",
      expect.objectContaining({
        headers: { Accept: "application/json" },
      }),
    );
  });
it("saves editable document markdown with expected revision", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "document_saved",
          document_id: "document 001",
          revision: 2,
          markdown: "# Edited",
        }),
      }),
    );

    const result = await saveEditableDocument({
      documentId: "document 001",
      title: "Edited",
      markdown: "# Edited",
      expectedRevision: 1,
      fetchImpl,
    });

    expect(result.revision).toBe(2);
    expect(fetchImpl).toHaveBeenCalledWith(
      `${DOCUMENT_DETAIL_ENDPOINT_PREFIX}/document%20001`,
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          title: "Edited",
          markdown: "# Edited",
          expected_revision: 1,
        }),
      }),
    );
  });
it("preserves Document 409 status and current revision payload", async () => {
    const payload = {
      detail: "document revision conflict",
      reason: "expected revision 1, found 2",
      current_revision: 2,
      current_document: {
        document_id: "document 001",
        revision: 2,
        markdown: "# Concurrent revision",
      },
    };
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => payload,
    });

    await expect(saveEditableDocument({
      documentId: "document 001",
      markdown: "# Local draft",
      expectedRevision: 1,
      fetchImpl,
    })).rejects.toMatchObject({
      message: "expected revision 1, found 2",
      status: 409,
      payload,
    });
  });
it("builds document HTML preview and export URLs with encoded document id", () => {
    expect(documentHtmlUrl("document 001")).toBe(
      `${DOCUMENT_DETAIL_ENDPOINT_PREFIX}/document%20001/html`,
    );
    expect(documentHtmlExportUrl("document 001")).toBe(
      `${DOCUMENT_DETAIL_ENDPOINT_PREFIX}/document%20001/html-export`,
    );
    expect(documentDeliveryUrl()).toBe("/api/rebuild/document-deliveries");
    expect(documentDeliveryArtifactUrl("delivery 001", "markdown")).toBe(
      "/api/rebuild/document-deliveries/delivery%20001/artifacts/markdown",
    );
    expect(documentPdfArtifactUrl("pdf operation 001")).toBe(
      "/api/rebuild/document-pdf-operations/pdf%20operation%20001/artifact",
    );
  });
it("creates an exact-revision delivery and loads a verified artifact", async () => {
    const createFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ delivery_id: "delivery-001", status: "completed" }),
    });
    const result = await createEditableDocumentDelivery({
      documentId: "document 001",
      expectedRevision: 3,
      formats: ["markdown", "html"],
      fetchImpl: createFetch,
    });
    expect(result.status).toBe("completed");
    expect(createFetch).toHaveBeenCalledWith(
      "/api/rebuild/document-deliveries",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          document_id: "document 001",
          expected_document_revision: 3,
          formats: ["markdown", "html"],
        }),
      }),
    );

    const blob = new Blob(["# exact"]);
    const artifactFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: () => 'attachment; filename="document-r3.md"' },
      blob: async () => blob,
    });
    const artifact = await loadEditableDocumentDeliveryArtifact({
      deliveryId: "delivery-001",
      format: "markdown",
      fetchImpl: artifactFetch,
    });
    expect(artifact).toEqual({ blob, fileName: "document-r3.md" });
    expect(artifactFetch).toHaveBeenCalledWith(
      "/api/rebuild/document-deliveries/delivery-001/artifacts/markdown",
      { headers: { Accept: "application/octet-stream" } },
    );

    const pdfBlob = new Blob(["%PDF-exact"]);
    const pdfFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: () => 'attachment; filename="document-r3.pdf"' },
      blob: async () => pdfBlob,
    });
    await expect(loadDocumentPdfArtifact({
      operationId: "pdf-operation-1",
      fetchImpl: pdfFetch,
    })).resolves.toEqual({ blob: pdfBlob, fileName: "document-r3.pdf" });
    expect(pdfFetch).toHaveBeenCalledWith(
      "/api/rebuild/document-pdf-operations/pdf-operation-1/artifact",
      { headers: { Accept: "application/pdf" } },
    );
  });
it("loads editable document HTML through the html endpoint as text", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        text: async () => "<!doctype html><html><body>HTML</body></html>",
      }),
    );

    const html = await loadEditableDocumentHtml({
      documentId: "document 001",
      fetchImpl,
    });

    expect(html).toContain("<!doctype html>");
    expect(fetchImpl).toHaveBeenCalledWith(
      `${DOCUMENT_DETAIL_ENDPOINT_PREFIX}/document%20001/html`,
      expect.objectContaining({
        headers: { Accept: "text/html" },
      }),
    );
  });
it("rejects loadEditableDocumentHtml when documentId is missing", async () => {
    await expect(loadEditableDocumentHtml({})).rejects.toThrow("documentId is required");
  });
it("rejects loadEditableDocumentHtml when backend returns non-ok status", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({ ok: false, status: 404, text: async () => "" }),
    );
    await expect(
      loadEditableDocumentHtml({ documentId: "doc-1", fetchImpl }),
    ).rejects.toThrow("Document HTML failed with 404");
  });
it("exports editable document HTML through the html-export endpoint", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          status: "exported",
          document_id: "document 001",
          revision: 1,
          title: "Doc",
          file_name: "document 001-r1.html",
          file_path: "/exports/document-html/document 001-r1.html",
          output_ref: "crp://default/document-html/document 001-r1.html",
        }),
      }),
    );

    const result = await exportEditableDocumentHtml({
      documentId: "document 001",
      fetchImpl,
    });

    expect(result.status).toBe("exported");
    expect(result.file_name).toBe("document 001-r1.html");
    expect(fetchImpl).toHaveBeenCalledWith(
      `${DOCUMENT_DETAIL_ENDPOINT_PREFIX}/document%20001/html-export`,
      expect.objectContaining({
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({}),
      }),
    );
  });
it("rejects exportEditableDocumentHtml when documentId is missing", async () => {
    await expect(exportEditableDocumentHtml({})).rejects.toThrow("documentId is required");
  });
it("rejects exportEditableDocumentHtml when backend returns non-ok status", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({ ok: false, status: 500, json: async () => ({}) }),
    );
    await expect(
      exportEditableDocumentHtml({ documentId: "doc-1", fetchImpl }),
    ).rejects.toThrow("Document HTML export failed with 500");
  });
it("builds desktop source file authorization URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(sourceFileAuthorizationUrl("source image 001")).toBe(
        `http://127.0.0.1:8001${SOURCE_FILE_AUTHORIZATION_ENDPOINT_PREFIX}/source%20image%20001/file-authorization`,
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop source output Memory Candidate URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(sourceOutputMemoryCandidateUrl("source image 001")).toBe(
        `http://127.0.0.1:8001${SOURCE_OUTPUT_MEMORY_CANDIDATE_ENDPOINT_PREFIX}/source%20image%20001/memory-candidate`,
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop four-layer Provider Candidate URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(fourLayerProviderCandidateUrl("source image 001")).toBe(
        `http://127.0.0.1:8001${FOUR_LAYER_PROVIDER_CANDIDATE_ENDPOINT_PREFIX}/source%20image%20001/four-layer-candidates`,
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop local OCR run URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(localOcrRunUrl("source image 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/sources/source%20image%20001/ocr",
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop local ASR run URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(localAsrRunUrl("source audio 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/sources/source%20audio%20001/transcription",
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop local video run URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(localVideoRunUrl("source video 001")).toBe(
        "http://127.0.0.1:8001/api/rebuild/sources/source%20video%20001/frame-extraction",
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
it("builds desktop video workflow URLs", () => {
    const previous = globalThis.electronAPI;
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    try {
      expect(bilibiliVideoDownloadPlanUrl()).toBe(
        `http://127.0.0.1:8001${BILIBILI_VIDEO_DOWNLOAD_PLAN_ENDPOINT}`,
      );
      expect(videoAudioExtractionUrl("source video 001")).toBe(
        `http://127.0.0.1:8001${VIDEO_AUDIO_EXTRACTION_ENDPOINT_PREFIX}/source%20video%20001/audio-track`,
      );
      expect(audioAssetTranscriptionUrl("audio asset 001")).toBe(
        `http://127.0.0.1:8001${AUDIO_ASSET_TRANSCRIPTION_ENDPOINT_PREFIX}/audio%20asset%20001/transcription`,
      );
      expect(transcriptSummaryUrl("media output transcript 001")).toBe(
        `http://127.0.0.1:8001${TRANSCRIPT_SUMMARY_ENDPOINT_PREFIX}/media%20output%20transcript%20001/summary`,
      );
    } finally {
      if (previous === undefined) {
        delete globalThis.electronAPI;
      } else {
        globalThis.electronAPI = previous;
      }
    }
  });
});
