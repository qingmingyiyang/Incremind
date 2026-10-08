import { productFetch as fetch } from '../../shared/api/deviceTransport';
import { selectLocalFile as platformSelectLocalFile } from "./platformAdapter";
import { libraryBackendUrl as workerUrl, responseJsonOrEmpty } from "./libraryOverviewTransport";

export {
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
} from "./libraryRetentionApi";

export {
  DAILY_REMINDERS_ENDPOINT,
  INSPIRATION_OVERVIEW_ENDPOINT,
  LIBRARY_ACTIVITY_OVERVIEW_ENDPOINT,
  LIBRARY_OVERVIEW_ENDPOINT,
  LIBRARY_SEARCH_ENDPOINT,
  dailyRemindersUrl,
  inspirationOverviewUrl,
  libraryActivityOverviewUrl,
  libraryOverviewSafeErrorMessage,
  libraryOverviewUrl,
  librarySearchUrl,
  loadDailyReminders,
  loadInspirationOverview,
  loadLibraryActivityOverview,
  loadLibraryOverview,
  searchLibrary,
} from "./libraryOverviewReadApi";

export const LIBRARY_INDEX_FRESHNESS_ENDPOINT = "/api/rebuild/index/freshness";
export const LIBRARY_INDEX_REBUILD_ENDPOINT = "/api/rebuild/index/rebuild";
export const LIBRARY_ITEM_DELETE_ENDPOINT_PREFIX = "/api/rebuild/library/items";
export const LIBRARY_BULK_ACTION_ENDPOINT = "/api/rebuild/library/items/bulk-action";
export const LIBRARY_SOURCE_ACTIVITY_ENDPOINT_PREFIX = "/api/rebuild/library/sources";
export const MEMORY_CANDIDATE_REVIEW_ENDPOINT_PREFIX = "/api/rebuild/memory-candidates";
export const MEMORY_PUBLICATION_ENDPOINT_PREFIXES = {
  atom: "/api/rebuild/staging-atoms",
  scenario: "/api/rebuild/staging-scenarios",
  series_memory: "/api/rebuild/staging-series-memory",
  project_skill: "/api/rebuild/staging-project-skills",
};
export const MEMORY_PUBLICATION_ENDPOINT_PREFIX = MEMORY_PUBLICATION_ENDPOINT_PREFIXES.atom;
export const MEMORY_ROLLBACK_ENDPOINT_PREFIX = "/api/rebuild/memory-publications";
export const SOURCE_OUTPUT_MEMORY_CANDIDATE_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const FOUR_LAYER_PROVIDER_CANDIDATE_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const SOURCE_CONTENT_QA_RECALL_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const SOURCE_CONTENT_STRUCTURE_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const SOURCE_SERIES_ASSIGNMENT_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const INSPIRATION_COLLISION_ENDPOINT = "/api/rebuild/inspirations/collision";
export const SERIES_MEMORY_SKILL_DRAFT_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const SOURCE_TEMPLATE_DOCUMENT_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const PROVIDER_SOURCE_TEMPLATE_DOCUMENT_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const MEDIA_OUTPUT_TEMPLATE_DOCUMENT_ENDPOINT_PREFIX = "/api/rebuild/media-processing-outputs";
export const SOURCE_TEMPLATE_MEMORY_CANDIDATE_ENDPOINT_PREFIX = "/api/rebuild/documents";
export const LOCAL_EXTRACTIVE_ANSWER_ENDPOINT_PREFIX = "/api/rebuild/model-requests";
export const MODEL_RESULT_HANDOFF_ENDPOINT_PREFIX = "/api/rebuild/model-results";
export const DOCUMENT_DETAIL_ENDPOINT_PREFIX = "/api/rebuild/documents";
export const ARCHIVED_DOCUMENTS_ENDPOINT = "/api/rebuild/documents-archived";
export const EXTERNAL_AGENT_REVIEW_DRAFT_ENDPOINT_PREFIX = "/api/rebuild/external-agent/review-drafts";
export const FOUR_LAYER_PROVIDER_STATUS_ENDPOINT = "/api/rebuild/providers/deepseek/status";
export const LOCAL_OCR_PROVIDER_SETTINGS_ENDPOINT = "/api/rebuild/settings/local-ocr-provider";
export const LOCAL_ASR_PROVIDER_SETTINGS_ENDPOINT = "/api/rebuild/settings/local-asr-provider";
export const LOCAL_VIDEO_PROVIDER_SETTINGS_ENDPOINT = "/api/rebuild/settings/local-video-provider";
export const LOCAL_DOCUMENT_TEXT_EXTRACTOR_SETTINGS_ENDPOINT = "/api/rebuild/settings/local-document-text-extractor";
export const LOCAL_OCR_RUN_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const LOCAL_ASR_RUN_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const LOCAL_VIDEO_RUN_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const LOCAL_DOCUMENT_TEXT_RUN_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const LINK_WEB_CONTENT_RUN_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const BOOKMARK_COLLECTION_WEB_CONTENT_RUN_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const SOURCE_FILE_AUTHORIZATION_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const BILIBILI_VIDEO_DOWNLOAD_PLAN_ENDPOINT = "/api/rebuild/video-links/bilibili/download-plan";
export const BILIBILI_AUTHORIZED_DOWNLOAD_ENDPOINT = "/api/rebuild/video-links/bilibili/authorized-download";
export const VIDEO_AUDIO_EXTRACTION_ENDPOINT_PREFIX = "/api/rebuild/sources";
export const AUDIO_ASSET_TRANSCRIPTION_ENDPOINT_PREFIX = "/api/rebuild/audio-assets";
export const TRANSCRIPT_SUMMARY_ENDPOINT_PREFIX = "/api/rebuild/media-processing-outputs";

function withDocumentProject(path, projectId) {
  return workerUrl(projectId ? `${path}?project_id=${encodeURIComponent(projectId)}` : path);
}

export function archivedDocumentsUrl(projectId) {
  return withDocumentProject(ARCHIVED_DOCUMENTS_ENDPOINT, projectId);
}

export function documentLifecycleUrl(documentId, operation, projectId) {
  return withDocumentProject(
    `${DOCUMENT_DETAIL_ENDPOINT_PREFIX}/${encodeURIComponent(documentId)}/${operation}`,
    projectId,
  );
}

export async function loadLibraryIndexFreshness({ fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(LIBRARY_INDEX_FRESHNESS_ENDPOINT), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Library index freshness failed with ${response.status}`);
  }
  return response.json();
}

export async function rebuildLibraryIndex({ fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(LIBRARY_INDEX_REBUILD_ENDPOINT), {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    throw new Error(payload.detail || `Library index rebuild failed with ${response.status}`);
  }
  return payload;
}

// 资料库条目删除：DELETE /api/rebuild/library/items/{item_id}?item_type=source
// 后端会删除 source 主记录及衍生数据（content_read/structure/series_assignment/jobs/source_outputs/tag_index）。
export function libraryItemDeleteUrl(itemId, itemType) {
  const endpoint = workerUrl(
    `${LIBRARY_ITEM_DELETE_ENDPOINT_PREFIX}/${encodeURIComponent(itemId)}`,
  );
  const params = new URLSearchParams();
  if (itemType) {
    params.set("item_type", String(itemType).trim().toLowerCase());
  }
  const query = params.toString();
  return query ? `${endpoint}?${query}` : endpoint;
}

export async function deleteLibraryItem({
  itemId,
  itemType,
  fetchImpl = fetch,
} = {}) {
  if (!itemId) {
    throw new Error("itemId is required");
  }
  if (!itemType) {
    throw new Error("itemType is required");
  }
  const response = await fetchImpl(libraryItemDeleteUrl(itemId, itemType), {
    method: "DELETE",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Library item delete failed with ${response.status}`);
  }
  return response.json();
}

export async function undoLibraryItemDeletion({
  itemId,
  itemType = "source",
  operationId,
  expectedRevision,
  fetchImpl = fetch,
} = {}) {
  if (!itemId || !operationId || !Number.isInteger(expectedRevision)) {
    throw new Error("itemId, operationId and expectedRevision are required");
  }
  const response = await fetchImpl(
    workerUrl(`${LIBRARY_ITEM_DELETE_ENDPOINT_PREFIX}/${encodeURIComponent(itemId)}/undo-delete`),
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        item_type: itemType,
        operation_id: operationId,
        expected_revision: expectedRevision,
      }),
    },
  );
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    throw new Error(payload.error || payload.detail || `Library item undo failed with ${response.status}`);
  }
  return payload;
}

// 资料库批量操作：POST /api/rebuild/library/items/bulk-action
// 请求体: {action: "delete|move_series|move_project|attach_tags", item_ids: [str], series_name?, project_id?, tags?}
// 响应: {action, status: "completed|partial|failed", total, succeeded, failed, results: [{item_id, status, error}], error}
export async function bulkLibraryAction({
  action,
  itemIds,
  seriesName,
  projectId,
  tags,
  fetchImpl = fetch,
} = {}) {
  if (!action) {
    throw new Error("action is required");
  }
  if (!Array.isArray(itemIds) || itemIds.length === 0) {
    throw new Error("itemIds must be a non-empty array");
  }
  const body = { action, item_ids: itemIds };
  if (seriesName) body.series_name = seriesName;
  if (projectId) body.project_id = projectId;
  if (Array.isArray(tags) && tags.length > 0) body.tags = tags;
  const response = await fetchImpl(workerUrl(LIBRARY_BULK_ACTION_ENDPOINT), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    const error = new Error(payload.error || `Library bulk action failed with ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

// 资料最近操作：GET /api/rebuild/library/sources/{source_id}/activity
// 响应: {status: "completed", source_id, events: [{event_id, type, label, summary, revision, created_at}]}
export async function fetchLibrarySourceActivity({
  sourceId,
  limit = 20,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const url = `${workerUrl(LIBRARY_SOURCE_ACTIVITY_ENDPOINT_PREFIX)}/${encodeURIComponent(sourceId)}/activity`
    + `?limit=${encodeURIComponent(limit)}`;
  const response = await fetchImpl(url, { headers: { Accept: "application/json" } });
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    const error = new Error(payload.error || `Library source activity failed with ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export async function updateLibrarySourceMetadata({
  sourceId,
  expectedRevision,
  title,
  seriesName = "",
  tags = [],
  fetchImpl = fetch,
} = {}) {
  if (!sourceId || !Number.isInteger(expectedRevision) || expectedRevision < 1 || !title?.trim()) {
    throw new Error("sourceId, positive expectedRevision and title are required");
  }
  const response = await fetchImpl(
    workerUrl(`/api/rebuild/library/sources/${encodeURIComponent(sourceId)}/metadata`),
    {
      method: "PUT",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_revision: expectedRevision,
        title: title.trim(),
        series_name: seriesName.trim(),
        tags,
      }),
    },
  );
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    const error = new Error(payload.error || payload.detail || `Library source edit failed with ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export function memoryCandidateReviewUrl(candidateId) {
  return workerUrl(
    `${MEMORY_CANDIDATE_REVIEW_ENDPOINT_PREFIX}/${encodeURIComponent(candidateId)}/review`,
  );
}

export async function reviewMemoryCandidate({
  candidateId,
  action,
  reason,
  seriesId,
  atomIds,
  scenarioIds,
  fetchImpl = fetch,
} = {}) {
  if (!candidateId) {
    throw new Error("candidateId is required");
  }
  const response = await fetchImpl(memoryCandidateReviewUrl(candidateId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      action,
      reason,
      ...(seriesId ? { series_id: seriesId } : {}),
      ...(Array.isArray(atomIds) ? { atom_ids: atomIds } : {}),
      ...(Array.isArray(scenarioIds) ? { scenario_ids: scenarioIds } : {}),
    }),
  });
  if (!response.ok) {
    throw new Error(`Memory Candidate review failed with ${response.status}`);
  }
  return response.json();
}

export async function assignLegacyMemoryCandidateProject({
  candidateId,
  projectId,
  expectedRevision,
  fetchImpl = fetch,
} = {}) {
  if (!candidateId || !projectId || !Number.isInteger(expectedRevision) || expectedRevision < 1) {
    throw new Error("candidateId, projectId and expectedRevision are required");
  }
  const response = await fetchImpl(workerUrl("/api/rebuild/memory/candidates/review"), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      candidate_id: candidateId,
      action: "assign_project",
      project_id: projectId.trim(),
      expected_revision: expectedRevision,
      confirm: true,
    }),
  });
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    const error = new Error(
      payload.reason || payload.detail || `Memory Candidate project assignment failed with ${response.status}`,
    );
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export async function loadMemoryCandidateProjectOptions({
  fetchImpl = fetch,
} = {}) {
  const response = await fetchImpl(workerUrl("/api/rebuild/memory/project-options"), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Memory Candidate project options failed with ${response.status}`);
  }
  return response.json();
}

export async function loadMemoryHierarchyOptions({
  projectId,
  fetchImpl = fetch,
} = {}) {
  if (!projectId) {
    throw new Error("projectId is required");
  }
  const response = await fetchImpl(
    workerUrl(`/api/rebuild/projects/${encodeURIComponent(projectId)}/memory-hierarchy-options`),
    { headers: { Accept: "application/json" } },
  );
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    throw new Error(payload.detail || `Memory hierarchy options failed with ${response.status}`);
  }
  return payload;
}

export async function loadMemorySeriesSuggestions({
  projectId,
  scenarioId,
  fetchImpl = fetch,
} = {}) {
  if (!projectId) {
    throw new Error("projectId is required");
  }
  const query = scenarioId
    ? `?scenario_id=${encodeURIComponent(scenarioId)}`
    : "";
  const response = await fetchImpl(
    workerUrl(
      `/api/rebuild/projects/${encodeURIComponent(projectId)}/memory-series-suggestions${query}`,
    ),
    { headers: { Accept: "application/json" } },
  );
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    throw new Error(payload.detail || `Memory series suggestions failed with ${response.status}`);
  }
  return payload;
}

export async function loadMemoryMaintenancePlan({
  projectId,
  fetchImpl = fetch,
}) {
  if (typeof projectId !== "string" || !projectId.trim()) {
    throw new Error("Memory maintenance project is invalid");
  }
  const response = await fetchImpl(
    workerUrl(
      `/api/rebuild/projects/${encodeURIComponent(projectId)}/memory-maintenance-plan`,
    ),
    { headers: { Accept: "application/json" } },
  );
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    throw new Error(
      payload.detail
      || `Memory maintenance plan failed with ${response.status}`,
    );
  }
  return payload;
}

export async function loadMemorySeriesFreshness({
  projectId,
  seriesObjectId,
  fetchImpl = fetch,
} = {}) {
  if (!projectId) {
    throw new Error("projectId is required");
  }
  const query = seriesObjectId
    ? `?series_object_id=${encodeURIComponent(seriesObjectId)}`
    : "";
  const response = await fetchImpl(
    workerUrl(
      `/api/rebuild/projects/${encodeURIComponent(projectId)}/memory-series-freshness${query}`,
    ),
    { headers: { Accept: "application/json" } },
  );
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    throw new Error(payload.detail || `Memory series freshness failed with ${response.status}`);
  }
  return payload;
}

export async function loadMemoryScenarioTransferPreview({
  projectId,
  scenarioObjectId,
  targetSeriesObjectId,
  fetchImpl = fetch,
} = {}) {
  if (!projectId || !scenarioObjectId) {
    throw new Error("Scenario transfer preview target is invalid");
  }
  const query = new URLSearchParams({ scenario_object_id: scenarioObjectId });
  if (targetSeriesObjectId) {
    query.set("target_series_object_id", targetSeriesObjectId);
  }
  const response = await fetchImpl(
    workerUrl(
      `/api/rebuild/projects/${encodeURIComponent(projectId)}/memory-scenario-transfer-preview?${query}`,
    ),
    { headers: { Accept: "application/json" } },
  );
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    const error = new Error(payload.detail || `Scenario transfer preview failed with ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export async function createMemoryScenarioTransferPlan({
  projectId,
  scenarioObjectId,
  targetSeriesObjectId,
  expectedScenarioObjectRevision,
  expectedScenarioRevision,
  expectedSourceSeriesObjectRevision,
  expectedSourceSeriesRevision,
  expectedTargetSeriesObjectRevision,
  expectedTargetSeriesRevision,
  fetchImpl = fetch,
} = {}) {
  if (!projectId || !scenarioObjectId || !targetSeriesObjectId) {
    throw new Error("Scenario transfer plan target is invalid");
  }
  const response = await fetchImpl(workerUrl("/api/rebuild/memory-scenario-transfer-plans"), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      project_id: projectId,
      scenario_object_id: scenarioObjectId,
      target_series_object_id: targetSeriesObjectId,
      expected_scenario_object_revision: expectedScenarioObjectRevision,
      expected_scenario_revision: expectedScenarioRevision,
      expected_source_series_object_revision: expectedSourceSeriesObjectRevision,
      expected_source_series_revision: expectedSourceSeriesRevision,
      expected_target_series_object_revision: expectedTargetSeriesObjectRevision,
      expected_target_series_revision: expectedTargetSeriesRevision,
      confirmed: true,
    }),
  });
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    const error = new Error(payload.detail || `Scenario transfer plan failed with ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export async function loadMemoryScenarioTransferPlan({
  planId,
  fetchImpl = fetch,
} = {}) {
  if (!planId) {
    throw new Error("planId is required");
  }
  const response = await fetchImpl(
    workerUrl(`/api/rebuild/memory-scenario-transfer-plans/${encodeURIComponent(planId)}`),
    { headers: { Accept: "application/json" } },
  );
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    const error = new Error(payload.detail || `Scenario transfer status failed with ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export async function createMemoryHierarchyUpdateCandidate({
  projectId,
  layer,
  objectId,
  expectedObjectRevision,
  expectedDomainRevision,
  seriesId,
  atomIds = [],
  scenarioIds = [],
  proposedOverview,
  fetchImpl = fetch,
} = {}) {
  if (!projectId || !objectId || !["scenario", "series_memory"].includes(layer)) {
    throw new Error("Memory hierarchy update target is invalid");
  }
  const response = await fetchImpl(workerUrl("/api/rebuild/memory-hierarchy/update-candidates"), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      project_id: projectId,
      layer,
      object_id: objectId,
      expected_object_revision: expectedObjectRevision,
      expected_domain_revision: expectedDomainRevision,
      series_id: seriesId,
      atom_ids: atomIds,
      scenario_ids: scenarioIds,
      proposed_overview: proposedOverview,
      confirmed: true,
    }),
  });
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    const error = new Error(payload.detail || `Memory hierarchy update failed with ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export async function createMemoryHierarchyUpdateCandidateBatch({
  projectId,
  items,
  fetchImpl = fetch,
} = {}) {
  if (!projectId || !Array.isArray(items) || items.length === 0) {
    throw new Error("Memory hierarchy batch is invalid");
  }
  const response = await fetchImpl(
    workerUrl("/api/rebuild/memory-hierarchy/update-candidates/batch"),
    {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        project_id: projectId,
        items,
        confirmed: true,
      }),
    },
  );
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    throw new Error(payload.detail || `Memory hierarchy batch failed with ${response.status}`);
  }
  return payload;
}

export function memoryPublicationUrl(objectId, layer = "atom") {
  const prefix = MEMORY_PUBLICATION_ENDPOINT_PREFIXES[layer];
  if (!prefix) {
    throw new Error(`Unsupported memory publication layer: ${layer}`);
  }
  return workerUrl(`${prefix}/${encodeURIComponent(objectId)}/publication`);
}

export function memoryRollbackUrl(publicationId) {
  return workerUrl(`${MEMORY_ROLLBACK_ENDPOINT_PREFIX}/${encodeURIComponent(publicationId)}/rollback`);
}

export async function publishStagingAtom({
  atomId,
  reason,
  fetchImpl = fetch,
} = {}) {
  if (!atomId) {
    throw new Error("atomId is required");
  }
  const response = await fetchImpl(memoryPublicationUrl(atomId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      confirm: true,
      reason,
    }),
  });
  if (!response.ok) {
    throw new Error(`Memory publication failed with ${response.status}`);
  }
  return response.json();
}

export async function publishStagingMemory({
  objectId,
  atomId,
  layer = "atom",
  reason,
  fetchImpl = fetch,
} = {}) {
  const resolvedObjectId = objectId || atomId;
  if (!resolvedObjectId) {
    throw new Error("objectId is required");
  }
  const response = await fetchImpl(memoryPublicationUrl(resolvedObjectId, layer), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      confirm: true,
      reason,
    }),
  });
  if (!response.ok) {
    throw new Error(`Memory publication failed with ${response.status}`);
  }
  return response.json();
}

export async function rollbackMemoryPublication({
  publicationId,
  expectedPublicationRevision,
  expectedProjectSkillRevision,
  reason,
  fetchImpl = fetch,
} = {}) {
  if (!publicationId) {
    throw new Error("publicationId is required");
  }
  const response = await fetchImpl(memoryRollbackUrl(publicationId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      confirm: true,
      reason,
      ...(expectedPublicationRevision == null
        ? {}
        : { expected_publication_revision: expectedPublicationRevision }),
      ...(expectedProjectSkillRevision == null
        ? {}
        : { expected_project_skill_revision: expectedProjectSkillRevision }),
    }),
  });
  if (!response.ok) {
    throw new Error(`Memory rollback failed with ${response.status}`);
  }
  return response.json();
}

export async function loadLocalOcrProviderSettings({ fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(LOCAL_OCR_PROVIDER_SETTINGS_ENDPOINT), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Local OCR Provider settings failed with ${response.status}`);
  }
  return response.json();
}

export async function loadLocalAsrProviderSettings({ fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(LOCAL_ASR_PROVIDER_SETTINGS_ENDPOINT), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Local ASR Provider settings failed with ${response.status}`);
  }
  return response.json();
}

export async function loadLocalVideoProviderSettings({ fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(LOCAL_VIDEO_PROVIDER_SETTINGS_ENDPOINT), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Local video Provider settings failed with ${response.status}`);
  }
  return response.json();
}

export async function loadLocalDocumentTextExtractorSettings({ fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(LOCAL_DOCUMENT_TEXT_EXTRACTOR_SETTINGS_ENDPOINT), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Local document text extractor settings failed with ${response.status}`);
  }
  return response.json();
}

export async function loadFourLayerProviderStatus({ fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(FOUR_LAYER_PROVIDER_STATUS_ENDPOINT), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Four-layer Provider status failed with ${response.status}`);
  }
  return response.json();
}

export function localOcrRunUrl(sourceId) {
  return workerUrl(`${LOCAL_OCR_RUN_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/ocr`);
}

export function localAsrRunUrl(sourceId) {
  return workerUrl(`${LOCAL_ASR_RUN_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/transcription`);
}

export function localVideoRunUrl(sourceId) {
  return workerUrl(`${LOCAL_VIDEO_RUN_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/frame-extraction`);
}

export function localDocumentTextRunUrl(sourceId) {
  return workerUrl(
    `${LOCAL_DOCUMENT_TEXT_RUN_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/document-text`,
  );
}

export function linkWebContentRunUrl(sourceId) {
  return workerUrl(
    `${LINK_WEB_CONTENT_RUN_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/web-content`,
  );
}

export function bookmarkCollectionWebContentRunUrl(sourceId) {
  return workerUrl(
    `${BOOKMARK_COLLECTION_WEB_CONTENT_RUN_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/collection-web-content`,
  );
}

export function sourceFileAuthorizationUrl(sourceId) {
  return workerUrl(
    `${SOURCE_FILE_AUTHORIZATION_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/file-authorization`,
  );
}

export function sourceOutputMemoryCandidateUrl(sourceId) {
  return workerUrl(
    `${SOURCE_OUTPUT_MEMORY_CANDIDATE_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/memory-candidate`,
  );
}

export function fourLayerProviderCandidateUrl(sourceId) {
  return workerUrl(
    `${FOUR_LAYER_PROVIDER_CANDIDATE_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/four-layer-candidates`,
  );
}

export function sourceContentQaRecallUrl(sourceId) {
  return workerUrl(
    `${SOURCE_CONTENT_QA_RECALL_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/qa-recall`,
  );
}

export function sourceContentStructureUrl(sourceId) {
  return workerUrl(
    `${SOURCE_CONTENT_STRUCTURE_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/structure-content`,
  );
}

export function sourceSeriesAssignmentUrl(sourceId) {
  return workerUrl(
    `${SOURCE_SERIES_ASSIGNMENT_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/series-assignment`,
  );
}

export function inspirationCollisionUrl() {
  return workerUrl(INSPIRATION_COLLISION_ENDPOINT);
}

export function seriesMemorySkillDraftUrl(sourceId) {
  return workerUrl(
    `${SERIES_MEMORY_SKILL_DRAFT_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/series-memory-skill-drafts`,
  );
}

export function sourceTemplateDocumentUrl(sourceId) {
  return workerUrl(
    `${SOURCE_TEMPLATE_DOCUMENT_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/template-document`,
  );
}

export function providerSourceTemplateDocumentUrl(sourceId) {
  return workerUrl(
    `${PROVIDER_SOURCE_TEMPLATE_DOCUMENT_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/provider-template-document`,
  );
}

export function mediaOutputTemplateDocumentUrl(outputId) {
  return workerUrl(
    `${MEDIA_OUTPUT_TEMPLATE_DOCUMENT_ENDPOINT_PREFIX}/${encodeURIComponent(outputId)}/template-document`,
  );
}

export function sourceTemplateMemoryCandidateUrl(documentId, projectId) {
  return withDocumentProject(
    `${SOURCE_TEMPLATE_MEMORY_CANDIDATE_ENDPOINT_PREFIX}/${encodeURIComponent(documentId)}/template-memory-candidate`,
    projectId,
  );
}

export function localExtractiveAnswerUrl(modelRequestId) {
  return workerUrl(
    `${LOCAL_EXTRACTIVE_ANSWER_ENDPOINT_PREFIX}/${encodeURIComponent(modelRequestId)}/local-answer`,
  );
}

export function modelResultDocumentUrl(modelResultId) {
  return workerUrl(
    `${MODEL_RESULT_HANDOFF_ENDPOINT_PREFIX}/${encodeURIComponent(modelResultId)}/document`,
  );
}

export function modelResultMemoryCandidateUrl(modelResultId) {
  return workerUrl(
    `${MODEL_RESULT_HANDOFF_ENDPOINT_PREFIX}/${encodeURIComponent(modelResultId)}/memory-candidate`,
  );
}

export function documentDetailUrl(documentId, projectId) {
  return withDocumentProject(`${DOCUMENT_DETAIL_ENDPOINT_PREFIX}/${encodeURIComponent(documentId)}`, projectId);
}

// HTML 预览/导出 URL —— 注意 /html 必须在 catch-all /{document_id:path} 之前匹配，
// 后端路由已按此顺序注册（R132）。
export function documentHtmlUrl(documentId, projectId) {
  return withDocumentProject(`${DOCUMENT_DETAIL_ENDPOINT_PREFIX}/${encodeURIComponent(documentId)}/html`, projectId);
}

export function documentHtmlExportUrl(documentId, projectId) {
  return withDocumentProject(`${DOCUMENT_DETAIL_ENDPOINT_PREFIX}/${encodeURIComponent(documentId)}/html-export`, projectId);
}

export function documentDeliveryUrl(projectId) {
  return withDocumentProject("/api/rebuild/document-deliveries", projectId);
}

export function documentDeliveryArtifactUrl(deliveryId, format, projectId) {
  return withDocumentProject(
    `/api/rebuild/document-deliveries/${encodeURIComponent(deliveryId)}/artifacts/${encodeURIComponent(format)}`,
    projectId,
  );
}

export function documentPdfArtifactUrl(operationId, projectId) {
  return withDocumentProject(
    `/api/rebuild/document-pdf-operations/${encodeURIComponent(operationId)}/artifact`,
    projectId,
  );
}

export function documentRevisionsUrl(documentId, projectId) {
  return withDocumentProject(`${DOCUMENT_DETAIL_ENDPOINT_PREFIX}/${encodeURIComponent(documentId)}/revisions`, projectId);
}

export function externalAgentReviewDraftPreviewUrl(draftId) {
  return workerUrl(
    `${EXTERNAL_AGENT_REVIEW_DRAFT_ENDPOINT_PREFIX}/${encodeURIComponent(draftId)}/preview`,
  );
}

export function externalAgentReviewDraftApplyUrl(draftId) {
  return workerUrl(
    `${EXTERNAL_AGENT_REVIEW_DRAFT_ENDPOINT_PREFIX}/${encodeURIComponent(draftId)}/apply`,
  );
}

export function bilibiliVideoDownloadPlanUrl() {
  return workerUrl(BILIBILI_VIDEO_DOWNLOAD_PLAN_ENDPOINT);
}

export function bilibiliAuthorizedDownloadUrl() {
  return workerUrl(BILIBILI_AUTHORIZED_DOWNLOAD_ENDPOINT);
}

export function videoAudioExtractionUrl(sourceId) {
  return workerUrl(
    `${VIDEO_AUDIO_EXTRACTION_ENDPOINT_PREFIX}/${encodeURIComponent(sourceId)}/audio-track`,
  );
}

export function audioAssetTranscriptionUrl(audioAssetId) {
  return workerUrl(
    `${AUDIO_ASSET_TRANSCRIPTION_ENDPOINT_PREFIX}/${encodeURIComponent(audioAssetId)}/transcription`,
  );
}

export function transcriptSummaryUrl(transcriptOutputId) {
  return workerUrl(
    `${TRANSCRIPT_SUMMARY_ENDPOINT_PREFIX}/${encodeURIComponent(transcriptOutputId)}/summary`,
  );
}

export async function runLocalOcrForSource({ sourceId, fetchImpl = fetch } = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const response = await fetchImpl(localOcrRunUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`Local OCR run failed with ${response.status}`);
  }
  return response.json();
}

export async function runLocalAsrForSource({ sourceId, fetchImpl = fetch } = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const response = await fetchImpl(localAsrRunUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`Local ASR run failed with ${response.status}`);
  }
  return response.json();
}

export async function runLocalVideoForSource({ sourceId, fetchImpl = fetch } = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const response = await fetchImpl(localVideoRunUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`Local video run failed with ${response.status}`);
  }
  return response.json();
}

export async function runLocalDocumentTextExtractorForSource({
  sourceId,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const response = await fetchImpl(localDocumentTextRunUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`Local document text extractor run failed with ${response.status}`);
  }
  return response.json();
}

export async function readLinkWebContentForSource({
  sourceId,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const response = await fetchImpl(linkWebContentRunUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`Link web content read failed with ${response.status}`);
  }
  return response.json();
}

export async function readBookmarkCollectionWebContentForSource({
  sourceId,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const response = await fetchImpl(bookmarkCollectionWebContentRunUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`Bookmark collection web content read failed with ${response.status}`);
  }
  return response.json();
}

export async function createBilibiliVideoDownloadPlan({
  url,
  videoId,
  fetchImpl = fetch,
} = {}) {
  if (!url || !String(url).trim()) {
    throw new Error("url is required");
  }
  const response = await fetchImpl(bilibiliVideoDownloadPlanUrl(), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      url: String(url).trim(),
      video_id: videoId || undefined,
    }),
  });
  if (!response.ok) {
    throw new Error(`Bilibili video download plan failed with ${response.status}`);
  }
  return response.json();
}

export async function runAuthorizedBilibiliDownload({
  plan,
  outputRoot,
  cookieMode = "none",
  cookiesFromBrowser = "",
  cookiesFile = "",
  allowRestrictedContent = false,
  projectId = "",
  fetchImpl = fetch,
} = {}) {
  if (!plan) {
    throw new Error("plan is required");
  }
  if (!outputRoot || !String(outputRoot).trim()) {
    throw new Error("outputRoot is required");
  }
  const response = await fetchImpl(bilibiliAuthorizedDownloadUrl(), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      confirm_download: true,
      project_id: projectId || undefined,
      plan,
      settings: {
        enabled: true,
        provider_name: "yt-dlp-bilibili",
        output_root: String(outputRoot).trim(),
        cookie_mode: cookieMode,
        cookies_from_browser: cookiesFromBrowser || "",
        cookies_file: cookiesFile || undefined,
        allow_restricted_content: Boolean(allowRestrictedContent),
      },
    }),
  });
  if (!response.ok) {
    throw new Error(`Bilibili authorized download failed with ${response.status}`);
  }
  return response.json();
}

export async function extractVideoAudioTrack({
  sourceId,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const response = await fetchImpl(videoAudioExtractionUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`Video audio extraction failed with ${response.status}`);
  }
  return response.json();
}

export async function transcribeAudioAsset({
  audioAssetId,
  fetchImpl = fetch,
} = {}) {
  if (!audioAssetId) {
    throw new Error("audioAssetId is required");
  }
  const response = await fetchImpl(audioAssetTranscriptionUrl(audioAssetId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`Audio asset transcription failed with ${response.status}`);
  }
  return response.json();
}

export async function summarizeTranscriptOutput({
  transcriptOutputId,
  fetchImpl = fetch,
} = {}) {
  if (!transcriptOutputId) {
    throw new Error("transcriptOutputId is required");
  }
  const response = await fetchImpl(transcriptSummaryUrl(transcriptOutputId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`Transcript summary failed with ${response.status}`);
  }
  return response.json();
}

export async function authorizeLocalFileForSource({
  sourceId,
  filePath,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  if (!filePath || !String(filePath).trim()) {
    throw new Error("filePath is required");
  }
  const response = await fetchImpl(sourceFileAuthorizationUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ file_path: String(filePath).trim() }),
  });
  if (!response.ok) {
    throw new Error(`Source file authorization failed with ${response.status}`);
  }
  return response.json();
}

export async function selectLocalFileForAuthorization({ mediaKind } = {}) {
  // 统一走 platformAdapter.selectLocalFile（spec 3.15：调用方不直接判断 Electron/browser）
  // platformAdapter 已被多个首屏模块静态引入，这里使用静态 import 避免触发
  // "dynamically imported but also statically imported" 警告。
  const result = await platformSelectLocalFile({ mediaKind: mediaKind || "file" });
  if (result.status === "selected") {
    return result.path;
  }
  return null;
}

export async function createSourceOutputMemoryCandidate({
  sourceId,
  evidenceKind,
  evidenceId,
  projectId,
  proposedContent,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const body = {
    evidence_kind: evidenceKind,
    evidence_id: evidenceId || undefined,
    project_id: projectId || "default",
    proposed_content: proposedContent || undefined,
    target_layer: "atom",
    candidate_type: "other",
  };
  const response = await fetchImpl(sourceOutputMemoryCandidateUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`Source output Memory Candidate failed with ${response.status}`);
  }
  return response.json();
}

export async function createFourLayerProviderCandidates({
  sourceId,
  evidenceKind,
  evidenceId,
  projectId,
  allowedLayers,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const body = {
    evidence_kind: evidenceKind || "source_content_read",
    evidence_id: evidenceId || undefined,
    project_id: projectId || "default",
    allowed_layers: Array.isArray(allowedLayers) ? allowedLayers : undefined,
  };
  const response = await fetchImpl(fourLayerProviderCandidateUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`Four-layer Provider Candidate failed with ${response.status}`);
  }
  return response.json();
}

export async function createSourceContentQaRecall({
  sourceId,
  question,
  projectId,
  projectSkillId,
  contentReadId,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  if (!question || !String(question).trim()) {
    throw new Error("question is required");
  }
  const response = await fetchImpl(sourceContentQaRecallUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      question: String(question).trim(),
      project_id: projectId || "default",
      project_skill_id: projectSkillId || "skill-default",
      content_read_id: contentReadId || undefined,
    }),
  });
  if (!response.ok) {
    throw new Error(`Source content QA recall failed with ${response.status}`);
  }
  return response.json();
}

export async function structureSourceContent({
  sourceId,
  contentReadId,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const response = await fetchImpl(sourceContentStructureUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      content_read_id: contentReadId || undefined,
    }),
  });
  if (!response.ok) {
    throw new Error(`Source content structure failed with ${response.status}`);
  }
  return response.json();
}

export async function confirmSourceSeriesAssignment({
  sourceId,
  projectId,
  seriesName,
  reason,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const response = await fetchImpl(sourceSeriesAssignmentUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      confirm: true,
      project_id: projectId || "default",
      series_name: seriesName || undefined,
      reason: reason || undefined,
    }),
  });
  if (!response.ok) {
    throw new Error(`Source series assignment failed with ${response.status}`);
  }
  return response.json();
}

export async function createInspirationCollision({
  query,
  themes,
  projectId,
  limit,
  fetchImpl = fetch,
} = {}) {
  const response = await fetchImpl(inspirationCollisionUrl(), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      query: query || undefined,
      themes: Array.isArray(themes) ? themes : [],
      project_id: projectId || undefined,
      limit: Number.isFinite(limit) ? limit : undefined,
    }),
  });
  if (!response.ok) {
    throw new Error(`Inspiration collision failed with ${response.status}`);
  }
  return response.json();
}

export async function createSeriesMemorySkillDrafts({
  sourceId,
  projectId,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const response = await fetchImpl(seriesMemorySkillDraftUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      project_id: projectId || "default",
    }),
  });
  if (!response.ok) {
    throw new Error(`Series memory and Project Skill draft failed with ${response.status}`);
  }
  return response.json();
}

export async function createSourceTemplateDocument({
  sourceId,
  templateType,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const response = await fetchImpl(sourceTemplateDocumentUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      template_type: templateType || "answer_manual",
    }),
  });
  if (!response.ok) {
    throw new Error(`Source template document failed with ${response.status}`);
  }
  return response.json();
}

export async function createProviderSourceTemplateDocument({
  sourceId,
  templateType,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId) {
    throw new Error("sourceId is required");
  }
  const response = await fetchImpl(providerSourceTemplateDocumentUrl(sourceId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      template_type: templateType || "answer_manual",
    }),
  });
  if (!response.ok) {
    throw new Error(`Provider source template document failed with ${response.status}`);
  }
  return response.json();
}

export async function createMediaOutputTemplateDocument({
  outputId,
  templateType,
  fetchImpl = fetch,
} = {}) {
  if (!outputId) {
    throw new Error("outputId is required");
  }
  const response = await fetchImpl(mediaOutputTemplateDocumentUrl(outputId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      template_type: templateType || "answer_manual",
    }),
  });
  if (!response.ok) {
    throw new Error(`Media output template document failed with ${response.status}`);
  }
  return response.json();
}

export async function createSourceTemplateMemoryCandidate({
  documentId,
  projectId,
  documentRevision,
  targetLayer,
  candidateType,
  fetchImpl = fetch,
} = {}) {
  if (!documentId) {
    throw new Error("documentId is required");
  }
  const response = await fetchImpl(sourceTemplateMemoryCandidateUrl(documentId, projectId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      document_revision: documentRevision,
      target_layer: targetLayer || undefined,
      candidate_type: candidateType || undefined,
    }),
  });
  if (!response.ok) {
    throw new Error(`Source template memory candidate failed with ${response.status}`);
  }
  return response.json();
}

export async function createLocalExtractiveAnswer({
  modelRequestId,
  fetchImpl = fetch,
} = {}) {
  if (!modelRequestId) {
    throw new Error("modelRequestId is required");
  }
  const response = await fetchImpl(localExtractiveAnswerUrl(modelRequestId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`Local extractive answer failed with ${response.status}`);
  }
  return response.json();
}

export async function createDocumentFromModelResult({
  modelResultId,
  title,
  fetchImpl = fetch,
} = {}) {
  if (!modelResultId) {
    throw new Error("modelResultId is required");
  }
  const response = await fetchImpl(modelResultDocumentUrl(modelResultId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ title: title || "模型回答文档草稿" }),
  });
  if (!response.ok) {
    throw new Error(`Model result document handoff failed with ${response.status}`);
  }
  return response.json();
}

export async function createMemoryCandidateFromModelResult({
  modelResultId,
  targetLayer = "atom",
  candidateType = "answer_fact",
  fetchImpl = fetch,
} = {}) {
  if (!modelResultId) {
    throw new Error("modelResultId is required");
  }
  const response = await fetchImpl(modelResultMemoryCandidateUrl(modelResultId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      target_layer: targetLayer,
      candidate_type: candidateType,
    }),
  });
  if (!response.ok) {
    throw new Error(`Model result Memory Candidate handoff failed with ${response.status}`);
  }
  return response.json();
}

export async function loadEditableDocument({
  documentId,
  projectId,
  fetchImpl = fetch,
} = {}) {
  if (!documentId) {
    throw new Error("documentId is required");
  }
  const response = await fetchImpl(documentDetailUrl(documentId, projectId), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Document detail failed with ${response.status}`);
  }
  return response.json();
}

export async function loadArchivedDocuments({ projectId, fetchImpl = fetch } = {}) {
  const response = await fetchImpl(archivedDocumentsUrl(projectId), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Archived Documents failed with ${response.status}`);
  }
  return response.json();
}

async function mutateDocumentLifecycle({
  documentId,
  projectId,
  expectedRevision,
  operation,
  fetchImpl = fetch,
} = {}) {
  if (!documentId) throw new Error("documentId is required");
  if (!Number.isInteger(expectedRevision)) throw new Error("expectedRevision is required");
  const response = await fetchImpl(documentLifecycleUrl(documentId, operation, projectId), {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ expected_revision: expectedRevision }),
  });
  if (!response.ok) {
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    const error = new Error(
      payload?.reason || payload?.detail || `Document ${operation} failed with ${response.status}`,
    );
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return response.json();
}

export function archiveDocument(options = {}) {
  return mutateDocumentLifecycle({ ...options, operation: "archive" });
}

export function restoreDocument(options = {}) {
  return mutateDocumentLifecycle({ ...options, operation: "restore" });
}

export async function loadMemoryLifecycleHeads({ projectId, fetchImpl = fetch } = {}) {
  if (!projectId) throw new Error("projectId is required");
  const response = await fetchImpl(workerUrl(
    `/api/rebuild/memory/lifecycle?project_id=${encodeURIComponent(projectId)}`,
  ), { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`Memory lifecycle list failed with ${response.status}`);
  return response.json();
}

export async function previewMemoryBatchSoftRedact({ projectId, items, reason, fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl("/api/rebuild/memory/batches/soft-redact/preview"), {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ project_id: projectId, items, reason }),
  });
  if (!response.ok) throw new Error(`Memory lifecycle preview failed with ${response.status}`);
  return response.json();
}

export async function confirmMemoryBatchSoftRedact({ previewToken, fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl("/api/rebuild/memory/batches/soft-redact/confirm"), {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ preview_token: previewToken, confirm: true }),
  });
  if (!response.ok) throw new Error(`Memory lifecycle confirm failed with ${response.status}`);
  return response.json();
}

export async function undoMemoryBatchSoftRedact({ operationId, fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(
    `/api/rebuild/memory/batches/${encodeURIComponent(operationId)}/undo`,
  ), {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true }),
  });
  if (!response.ok) throw new Error(`Memory lifecycle undo failed with ${response.status}`);
  return response.json();
}

export async function loadExternalAgentReviewDraftPreview({
  draftId,
  fetchImpl = fetch,
} = {}) {
  if (!draftId) {
    throw new Error("draftId is required");
  }
  const response = await fetchImpl(externalAgentReviewDraftPreviewUrl(draftId), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`External Agent review draft preview failed with ${response.status}`);
  }
  return response.json();
}

export async function applyExternalAgentReviewDraft({
  draftId,
  expectedRevision,
  reason,
  fetchImpl = fetch,
} = {}) {
  if (!draftId) {
    throw new Error("draftId is required");
  }
  if (!Number.isInteger(expectedRevision)) {
    throw new Error("expectedRevision is required");
  }
  const response = await fetchImpl(externalAgentReviewDraftApplyUrl(draftId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      confirm: true,
      expected_revision: expectedRevision,
      reason: reason || undefined,
    }),
  });
  if (!response.ok) {
    throw new Error(`External Agent review draft apply failed with ${response.status}`);
  }
  return response.json();
}

export async function saveEditableDocument({
  documentId,
  projectId,
  title,
  markdown,
  expectedRevision,
  fetchImpl = fetch,
} = {}) {
  if (!documentId) {
    throw new Error("documentId is required");
  }
  if (typeof markdown !== "string") {
    throw new Error("markdown is required");
  }
  if (!Number.isInteger(expectedRevision)) {
    throw new Error("expectedRevision is required");
  }
  const response = await fetchImpl(documentDetailUrl(documentId, projectId), {
    method: "PUT",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      title: title || undefined,
      markdown,
      expected_revision: expectedRevision,
    }),
  });
  if (!response.ok) {
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    const error = new Error(
      payload?.reason || payload?.detail || `Document save failed with ${response.status}`,
    );
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return response.json();
}

// 拉取文档的统一风格 HTML（R132 后端端点，返回 text/html）。
// 用于 iframe 预览或 Blob 下载。
export async function loadEditableDocumentHtml({
  documentId,
  projectId,
  fetchImpl = fetch,
} = {}) {
  if (!documentId) {
    throw new Error("documentId is required");
  }
  const response = await fetchImpl(documentHtmlUrl(documentId, projectId), {
    headers: { Accept: "text/html" },
  });
  if (!response.ok) {
    throw new Error(`Document HTML failed with ${response.status}`);
  }
  return response.text();
}

// 触发后端持久化导出 HTML 文件到 exports/document-html/ 目录。
// 返回 JSON：{ status, document_id, revision, title, file_name, file_path, output_ref }。
export async function exportEditableDocumentHtml({
  documentId,
  projectId,
  fetchImpl = fetch,
} = {}) {
  if (!documentId) {
    throw new Error("documentId is required");
  }
  const response = await fetchImpl(documentHtmlExportUrl(documentId, projectId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`Document HTML export failed with ${response.status}`);
  }
  return response.json();
}

export async function createEditableDocumentDelivery({
  documentId,
  projectId,
  expectedRevision,
  formats = ["markdown", "html"],
  fetchImpl = fetch,
} = {}) {
  if (!documentId) throw new Error("documentId is required");
  if (!Number.isInteger(expectedRevision)) throw new Error("expectedRevision is required");
  if (!Array.isArray(formats) || formats.length === 0) throw new Error("formats are required");
  const response = await fetchImpl(documentDeliveryUrl(projectId), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      document_id: documentId,
      expected_document_revision: expectedRevision,
      formats,
    }),
  });
  if (!response.ok) {
    const payload = await responseJsonOrEmpty(response);
    const reason = payload?.reason || payload?.detail || `status ${response.status}`;
    throw new Error(`Document delivery failed: ${reason}`);
  }
  return response.json();
}

export async function loadEditableDocumentDeliveryArtifact({
  deliveryId,
  format,
  projectId,
  fetchImpl = fetch,
} = {}) {
  if (!deliveryId) throw new Error("deliveryId is required");
  if (!format) throw new Error("format is required");
  const response = await fetchImpl(documentDeliveryArtifactUrl(deliveryId, format, projectId), {
    headers: { Accept: "application/octet-stream" },
  });
  if (!response.ok) throw new Error(`Document delivery artifact failed with ${response.status}`);
  const disposition = response.headers?.get?.("Content-Disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/i);
  return {
    blob: await response.blob(),
    fileName: match?.[1] || `${deliveryId}.${format === "markdown" ? "md" : format}`,
  };
}

export async function loadDocumentPdfArtifact({ operationId, projectId, fetchImpl = fetch } = {}) {
  if (!operationId) throw new Error("operationId is required");
  const response = await fetchImpl(documentPdfArtifactUrl(operationId, projectId), {
    headers: { Accept: "application/pdf" },
  });
  if (!response.ok) throw new Error(`Document PDF artifact failed with ${response.status}`);
  const disposition = response.headers?.get?.("Content-Disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/i);
  return { blob: await response.blob(), fileName: match?.[1] || `${operationId}.pdf` };
}

// 应用 AI patch（user-wins 语义）：用户手改的 block 不会被覆盖，
// 冲突时后端写入 conflicted revision 并返回 conflict 信息。
// 返回 JSON：{ status, document_id, revision, document_status, conflict, changed_blocks, ... }
export async function patchEditableDocument({
  documentId,
  projectId,
  blocks,
  expectedRevision,
  reason,
  sourceRefs,
  fetchImpl = fetch,
} = {}) {
  if (!documentId) {
    throw new Error("documentId is required");
  }
  if (!Array.isArray(blocks) || blocks.length === 0) {
    throw new Error("blocks must be a non-empty array");
  }
  if (!Number.isInteger(expectedRevision)) {
    throw new Error("expectedRevision is required");
  }
  const body = {
    blocks,
    expected_revision: expectedRevision,
  };
  if (typeof reason === "string" && reason.length > 0) {
    body.reason = reason;
  }
  if (Array.isArray(sourceRefs)) {
    body.source_refs = sourceRefs;
  }
  const response = await fetchImpl(documentDetailUrl(documentId, projectId), {
    method: "PATCH",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`Document AI patch failed with ${response.status}`);
  }
  return response.json();
}

// 列出 document 的全部 revision 历史。
// 返回 JSON：{ document_id, current_revision, document_status, revisions: [...] }
export async function loadEditableDocumentRevisions({
  documentId,
  projectId,
  fetchImpl = fetch,
} = {}) {
  if (!documentId) {
    throw new Error("documentId is required");
  }
  const response = await fetchImpl(documentRevisionsUrl(documentId, projectId), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Document revisions failed with ${response.status}`);
  }
  return response.json();
}
