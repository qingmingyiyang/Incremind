const fs = require("node:fs");
const path = require("node:path");

const IDENTITY_FILE = "candidate-identity.json";
const IDENTITY_SCHEMA_VERSION = 1;
const MAX_IDENTITY_BYTES = 8 * 1024;
const BUILD_ID = /^windows-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}$/;
const SOURCE_COMMIT = /^[a-f0-9]{40}$/;
const IDENTITY_KEYS = [
  "build_id",
  "candidate_id",
  "created_at",
  "package_version",
  "payload_revision",
  "schema_version",
  "source_commit",
].sort();

function assertBuildId(buildId) {
  if (!BUILD_ID.test(buildId || "")) throw new Error("candidate build_id is invalid");
  return buildId;
}

function createCandidateIdentity({ buildId, sourceCommit, packageVersion, now = new Date() }) {
  assertBuildId(buildId);
  if (!SOURCE_COMMIT.test(sourceCommit || "")) throw new Error("candidate source_commit is invalid");
  if (typeof packageVersion !== "string" || !packageVersion.trim() || packageVersion.length > 80) {
    throw new Error("candidate package_version is invalid");
  }
  const createdAt = now instanceof Date ? now.toISOString() : "";
  if (!Number.isFinite(Date.parse(createdAt))) throw new Error("candidate created_at is invalid");
  return Object.freeze({
    schema_version: IDENTITY_SCHEMA_VERSION,
    candidate_id: buildId,
    build_id: buildId,
    source_commit: sourceCommit,
    payload_revision: `source:${sourceCommit}`,
    package_version: packageVersion.trim(),
    created_at: createdAt,
  });
}

function writeCandidateIdentity(stageRoot, identity, io = fs) {
  const stage = io.lstatSync(stageRoot, { throwIfNoEntry: false });
  if (!stage?.isDirectory() || stage.isSymbolicLink()) throw new Error("candidate identity stage is unsafe");
  const checked = parseCandidateIdentity(JSON.stringify(identity));
  const target = path.join(stageRoot, IDENTITY_FILE);
  const temporary = `${target}.tmp-${process.pid}`;
  io.rmSync(target, { force: true });
  io.writeFileSync(temporary, `${JSON.stringify(checked, null, 2)}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
  try {
    io.renameSync(temporary, target);
  } finally {
    io.rmSync(temporary, { force: true });
  }
  return target;
}

function parseCandidateIdentity(text) {
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("candidate identity is invalid JSON");
  }
  if (!value || Object.keys(value).sort().join() !== IDENTITY_KEYS.join()) {
    throw new Error("candidate identity has an invalid shape");
  }
  const checked = createCandidateIdentity({
    buildId: value.build_id,
    sourceCommit: value.source_commit,
    packageVersion: value.package_version,
    now: new Date(value.created_at),
  });
  if (value.candidate_id !== checked.candidate_id || value.payload_revision !== checked.payload_revision) {
    throw new Error("candidate identity fields do not agree");
  }
  return checked;
}

function readRuntimeCandidateIdentity({ isPackaged, resourcesPath, io = fs }) {
  if (isPackaged !== true) return Object.freeze({ status: "not_run", reason: "development_runtime" });
  const filePath = path.join(String(resourcesPath || ""), IDENTITY_FILE);
  const stat = io.lstatSync(filePath, { throwIfNoEntry: false });
  if (!stat?.isFile() || stat.isSymbolicLink() || stat.size > MAX_IDENTITY_BYTES) {
    return Object.freeze({ status: "blocked", reason: "candidate_identity_missing_or_unsafe" });
  }
  try {
    return Object.freeze({ status: "ready", identity: parseCandidateIdentity(io.readFileSync(filePath, "utf8")) });
  } catch {
    return Object.freeze({ status: "blocked", reason: "candidate_identity_invalid" });
  }
}

module.exports = {
  BUILD_ID,
  IDENTITY_FILE,
  IDENTITY_SCHEMA_VERSION,
  assertBuildId,
  createCandidateIdentity,
  parseCandidateIdentity,
  readRuntimeCandidateIdentity,
  writeCandidateIdentity,
};
