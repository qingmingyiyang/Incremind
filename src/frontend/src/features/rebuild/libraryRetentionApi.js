import { productFetch as fetch } from '../../shared/api/deviceTransport';
import { libraryBackendUrl, responseJsonOrEmpty } from "./libraryOverviewTransport";

export const SOURCE_RETENTION_PURGE_ENDPOINT = "/api/rebuild/retention/source-purge";
export const ORIGINAL_ASSET_RETENTION_ENDPOINT = "/api/rebuild/retention/original-assets";
export const ASSET_OWNERSHIP_ENDPOINT = "/api/rebuild/retention/asset-ownership";

async function sourceRetentionRequest(path, options, fetchImpl) {
  const response = await fetchImpl(libraryBackendUrl(path), options);
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    throw new Error(payload.detail || `Source retention request failed with ${response.status}`);
  }
  return payload;
}

export function loadSourceRetentionCandidates({ fetchImpl = fetch } = {}) {
  return sourceRetentionRequest(
    `${SOURCE_RETENTION_PURGE_ENDPOINT}/candidates`,
    { headers: { Accept: "application/json" } },
    fetchImpl,
  );
}

export function createSourceRetentionPurgePlan({ sourceId, fetchImpl = fetch } = {}) {
  if (!sourceId) throw new Error("sourceId is required");
  return sourceRetentionRequest(
    `${SOURCE_RETENTION_PURGE_ENDPOINT}/plan`,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: sourceId }),
    },
    fetchImpl,
  );
}

export function executeSourceRetentionPurge({
  sourceId,
  planId,
  expectedRevision,
  confirm,
  fetchImpl = fetch,
} = {}) {
  if (!sourceId || !planId || !Number.isInteger(expectedRevision) || confirm !== true) {
    throw new Error("sourceId, planId, expectedRevision and explicit confirmation are required");
  }
  return sourceRetentionRequest(
    SOURCE_RETENTION_PURGE_ENDPOINT,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        source_id: sourceId,
        plan_id: planId,
        expected_revision: expectedRevision,
        confirm: true,
      }),
    },
    fetchImpl,
  );
}

async function originalAssetRetentionRequest(path, options, fetchImpl) {
  const response = await fetchImpl(libraryBackendUrl(path), options);
  const payload = await responseJsonOrEmpty(response);
  if (!response.ok) {
    throw new Error(payload.detail || `Original asset retention request failed with ${response.status}`);
  }
  return payload;
}

export function reconcileOriginalAssetRetention({ fetchImpl = fetch } = {}) {
  return originalAssetRetentionRequest(
    `${ORIGINAL_ASSET_RETENTION_ENDPOINT}/reconcile`,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: "{}",
    },
    fetchImpl,
  );
}

export function loadOriginalAssetRetentionCandidates({ fetchImpl = fetch } = {}) {
  return originalAssetRetentionRequest(
    `${ORIGINAL_ASSET_RETENTION_ENDPOINT}/candidates`,
    { headers: { Accept: "application/json" } },
    fetchImpl,
  );
}

export function loadAssetOwnershipGraph({ fetchImpl = fetch } = {}) {
  return originalAssetRetentionRequest(
    ASSET_OWNERSHIP_ENDPOINT,
    { headers: { Accept: "application/json" } },
    fetchImpl,
  );
}

export function createOriginalAssetRetentionPlan({ assetId, fetchImpl = fetch } = {}) {
  if (!assetId) throw new Error("assetId is required");
  return originalAssetRetentionRequest(
    `${ORIGINAL_ASSET_RETENTION_ENDPOINT}/plan`,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ asset_id: assetId }),
    },
    fetchImpl,
  );
}

export function executeOriginalAssetRetention({
  assetId,
  planId,
  expectedRevision,
  confirm,
  fetchImpl = fetch,
} = {}) {
  if (!assetId || !planId || !Number.isInteger(expectedRevision) || confirm !== true) {
    throw new Error("assetId, planId, expectedRevision and explicit confirmation are required");
  }
  return originalAssetRetentionRequest(
    ORIGINAL_ASSET_RETENTION_ENDPOINT,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        asset_id: assetId,
        plan_id: planId,
        expected_revision: expectedRevision,
        confirm: true,
      }),
    },
    fetchImpl,
  );
}
