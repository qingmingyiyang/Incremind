const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { pipeline } = require("node:stream/promises");

const FILE_GRANT_REVISION = "desktop-file-grant-v1";
const FILE_GRANT_TTL_MS = 60_000;
const FILE_GRANT_MAX_BYTES = 16 * 1024 * 1024 * 1024;
const SESSION_HEADER = "X-Chriptmas-Desktop-Session";

async function createFileGrant({ filePath, session, mediaType, sourceKind, now = Date.now }) {
  if (!session?.secret || !session?.instance_id || !session?.origin) throw new Error("file_grant_session_invalid");
  const canonicalPath = await fs.promises.realpath(filePath);
  const before = await fs.promises.stat(canonicalPath);
  if (!before.isFile()) throw new Error("file_grant_not_regular_file");
  if (before.size < 0 || before.size > FILE_GRANT_MAX_BYTES) throw new Error("file_grant_size_invalid");
  const sha256 = await hashFile(canonicalPath);
  const after = await fs.promises.stat(canonicalPath);
  const identity = fileIdentity(after);
  if (!sameFileIdentity(fileIdentity(before), identity)) throw new Error("file_grant_file_changed");
  const grant = {
    grant_id: `file-grant-${crypto.randomBytes(32).toString("base64url")}`,
    session_instance_id: session.instance_id,
    display_name: path.basename(canonicalPath),
    media_type: normalizeMediaType(mediaType),
    source_kind: normalizeSourceKind(sourceKind),
    size_bytes: after.size,
    sha256,
    expires_at_ms: now() + FILE_GRANT_TTL_MS,
  };
  return Object.freeze({ ...grant, file_path: canonicalPath, file_identity: identity, signature: signGrant(grant, session.secret) });
}

async function uploadFileGrant(grant, session, { signal, createReadStream = fs.createReadStream, endpointPath = "/api/rebuild/workbench/original-asset-stream" } = {}) {
  if (!grant?.file_path || grant.session_instance_id !== session?.instance_id) throw new Error("file_grant_session_mismatch");
  if (grant.expires_at_ms <= Date.now()) throw new Error("file_grant_expired");
  const current = await fs.promises.stat(grant.file_path);
  if (!sameFileIdentity(grant.file_identity, fileIdentity(current))) throw new Error("file_grant_file_changed");
  if (typeof endpointPath !== "string" || !endpointPath.startsWith("/") || endpointPath.startsWith("//") || endpointPath.includes("\\") || endpointPath.includes("?") || endpointPath.includes("#") || /[\x00-\x1f\x7f]/.test(endpointPath)) throw new Error("file_grant_endpoint_invalid");
  const endpoint = new URL(endpointPath, session.origin);
  const headers = grantHeaders(grant, session.secret);
  const response = await streamRequest(endpoint, headers, grant.file_path, signal, createReadStream);
  if (response.status < 200 || response.status >= 300) {
    const detail = typeof response.body?.detail === "string" ? response.body.detail : `HTTP ${response.status}`;
    throw new Error(`file_grant_upload_failed: ${detail}`);
  }
  return Object.freeze({ ...response.body, grant_id: grant.grant_id });
}


async function hashFile(filePath) {
  const hash = crypto.createHash("sha256");
  for await (const chunk of fs.createReadStream(filePath, { highWaterMark: 256 * 1024 })) hash.update(chunk);
  return hash.digest("hex");
}

function grantHeaders(grant, secret) {
  return {
    [SESSION_HEADER]: secret,
    "Content-Type": "application/octet-stream",
    "Content-Length": String(grant.size_bytes),
    "X-Chriptmas-File-Grant": grant.grant_id,
    "X-Chriptmas-File-Session": grant.session_instance_id,
    "X-Chriptmas-File-Name": Buffer.from(grant.display_name, "utf8").toString("base64url"),
    "X-Chriptmas-File-Media-Type": grant.media_type,
    "X-Chriptmas-File-Source-Kind": grant.source_kind,
    "X-Chriptmas-File-Size": String(grant.size_bytes),
    "X-Chriptmas-File-Sha256": grant.sha256,
    "X-Chriptmas-File-Expires": String(grant.expires_at_ms),
    "X-Chriptmas-File-Signature": signGrant(grant, secret),
  };
}

function signGrant(grant, secret) {
  const canonical = JSON.stringify({
    display_name: grant.display_name,
    expires_at_ms: grant.expires_at_ms,
    grant_id: grant.grant_id,
    media_type: grant.media_type,
    revision: FILE_GRANT_REVISION,
    session_instance_id: grant.session_instance_id,
    sha256: grant.sha256,
    size_bytes: grant.size_bytes,
    source_kind: grant.source_kind,
  });
  return crypto.createHmac("sha256", secret).update(canonical, "utf8").digest("hex");
}

function streamRequest(endpoint, headers, filePath, signal, createReadStream) {
  return new Promise((resolve, reject) => {
    const request = http.request(endpoint, { method: "POST", headers, signal }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
        if (body.length > 1024 * 1024) response.destroy(new Error("file_grant_response_too_large"));
      });
      response.on("end", () => {
        try { resolve({ status: response.statusCode || 0, body: JSON.parse(body) }); }
        catch (error) { reject(new Error("file_grant_response_invalid", { cause: error })); }
      });
    });
    request.once("error", reject);
    pipeline(createReadStream(filePath, { highWaterMark: 256 * 1024 }), request).catch(reject);
  });
}

function fileIdentity(stat) {
  return Object.freeze({ dev: stat.dev, ino: stat.ino, size: stat.size, mtimeMs: stat.mtimeMs, ctimeMs: stat.ctimeMs });
}

function sameFileIdentity(left, right) {
  return ["dev", "ino", "size", "mtimeMs", "ctimeMs"].every((key) => left?.[key] === right?.[key]);
}

function normalizeMediaType(value) {
  const normalized = typeof value === "string" ? value.trim().toLowerCase() : "";
  return /^[a-z0-9.+-]+\/[a-z0-9.+-]+$/.test(normalized) ? normalized : "application/octet-stream";
}

function normalizeSourceKind(value) {
  return ["file", "image", "audio", "video"].includes(value) ? value : "file";
}

module.exports = {
  FILE_GRANT_MAX_BYTES,
  FILE_GRANT_TTL_MS,
  createFileGrant,
  grantHeaders,
  sameFileIdentity,
  signGrant,
  uploadFileGrant,
};
