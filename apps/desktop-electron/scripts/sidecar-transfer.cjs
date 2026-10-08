const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const ELECTRON_ROOT = path.resolve(__dirname, "..");
const STAGE_ROOT = path.join(ELECTRON_ROOT, ".sidecar-stage");
const MANIFEST_NAME = "sidecar-manifest.json";
const NONCE_ENV = "CHRIPTMAS_SIDECAR_TRANSFER_NONCE";
const MANIFEST_SHA_ENV = "CHRIPTMAS_SIDECAR_MANIFEST_SHA256";
const SHA256 = /^[a-f0-9]{64}$/;
const PROOF_KEYS = [
  "content_set_sha256",
  "created_at",
  "files",
  "manifest_sha256",
  "method",
  "nonce",
  "schema_version",
  "target",
  "total_size",
];

function sha256File(filePath, io = fs) {
  const hash = crypto.createHash("sha256");
  const handle = io.openSync(filePath, "r");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    for (;;) {
      const bytesRead = io.readSync(handle, buffer, 0, buffer.length, null);
      if (bytesRead === 0) break;
      hash.update(buffer.subarray(0, bytesRead));
    }
  } finally {
    io.closeSync(handle);
  }
  return hash.digest("hex");
}

function requireRegularPath(filePath, label, io = fs) {
  const stat = io.lstatSync(filePath, { throwIfNoEntry: false });
  if (!stat?.isFile() || stat.isSymbolicLink()) {
    throw new Error(`${label} must be a regular non-symlink file`);
  }
  return stat;
}

function requireDirectory(directory, label, io = fs) {
  const stat = io.lstatSync(directory, { throwIfNoEntry: false });
  if (!stat?.isDirectory() || stat.isSymbolicLink()) {
    throw new Error(`${label} must be a regular non-symlink directory`);
  }
  return stat;
}

function validateTransferInputs(nonce, expectedManifestSha256) {
  if (!SHA256.test(nonce || "")) {
    throw new Error("sidecar transfer nonce must be a 64-character lowercase SHA-256 value");
  }
  if (!SHA256.test(expectedManifestSha256 || "")) {
    throw new Error("sidecar transfer manifest SHA-256 is invalid");
  }
}

function proofName(nonce) {
  if (!SHA256.test(nonce || "")) {
    throw new Error("sidecar transfer nonce must be a 64-character lowercase SHA-256 value");
  }
  return `.chriptmas-sidecar-transfer-${nonce}.json`;
}

function readManifestSummary(root, expectedManifestSha256, io = fs) {
  const manifestPath = path.join(root, MANIFEST_NAME);
  requireRegularPath(manifestPath, "sidecar manifest", io);
  const observedManifestSha256 = sha256File(manifestPath, io);
  if (observedManifestSha256 !== expectedManifestSha256) {
    throw new Error("sidecar manifest changed after staging");
  }
  const manifest = JSON.parse(io.readFileSync(manifestPath, "utf8"));
  if (manifest?.schema_version !== "2.0.0"
      || manifest?.build_kind !== "windows-cpu-sidecar"
      || !Array.isArray(manifest.files)
      || manifest.files.length === 0
      || !Number.isSafeInteger(manifest.total_size)
      || manifest.total_size < 0
      || !SHA256.test(manifest.content_set_sha256 || "")) {
    throw new Error("sidecar transfer received an invalid manifest");
  }
  return {
    manifest_sha256: observedManifestSha256,
    content_set_sha256: manifest.content_set_sha256,
    files: manifest.files.length,
    total_size: manifest.total_size,
  };
}

function transferSidecar({
  stageRoot,
  targetRoot,
  proofPath,
  nonce,
  expectedManifestSha256,
  io = fs,
  now = () => new Date(),
}) {
  validateTransferInputs(nonce, expectedManifestSha256);
  const stageStat = requireDirectory(stageRoot, "sidecar stage", io);
  const targetParent = path.dirname(targetRoot);
  const targetParentStat = requireDirectory(targetParent, "sidecar target parent", io);
  if (stageStat.dev !== targetParentStat.dev) {
    throw new Error("sidecar atomic transfer requires stage and candidate on the same volume");
  }
  if (io.lstatSync(targetRoot, { throwIfNoEntry: false })) {
    throw new Error("sidecar target already exists before atomic transfer");
  }
  if (io.lstatSync(proofPath, { throwIfNoEntry: false })) {
    throw new Error("sidecar transfer proof already exists");
  }
  const summary = readManifestSummary(stageRoot, expectedManifestSha256, io);

  io.renameSync(stageRoot, targetRoot);
  try {
    const transferred = readManifestSummary(targetRoot, expectedManifestSha256, io);
    if (JSON.stringify(transferred) !== JSON.stringify(summary)) {
      throw new Error("sidecar manifest identity changed during atomic transfer");
    }
    const proof = {
      schema_version: "1.0.0",
      method: "same-volume-atomic-directory-rename",
      nonce,
      manifest_sha256: summary.manifest_sha256,
      content_set_sha256: summary.content_set_sha256,
      files: summary.files,
      total_size: summary.total_size,
      target: "resources/sidecar",
      created_at: now().toISOString(),
    };
    io.writeFileSync(proofPath, `${JSON.stringify(proof, null, 2)}\n`, {
      encoding: "utf8",
      flag: "wx",
      mode: 0o600,
    });
    return proof;
  } catch (error) {
    try {
      if (!io.lstatSync(stageRoot, { throwIfNoEntry: false })
          && io.lstatSync(targetRoot, { throwIfNoEntry: false })) {
        io.renameSync(targetRoot, stageRoot);
      }
    } catch {
      // The original error remains authoritative; a failed rollback leaves the
      // candidate invalid and the next clean stage rebuilds the directory.
    }
    throw error;
  }
}

function beginSidecarReplacement({
  stageRoot,
  targetRoot,
  backupRoot,
  proofPath,
  nonce,
  expectedManifestSha256,
  io = fs,
  now = () => new Date(),
}) {
  validateTransferInputs(nonce, expectedManifestSha256);
  requireDirectory(targetRoot, "existing sidecar target", io);
  const targetParent = path.dirname(targetRoot);
  const backupParent = path.dirname(backupRoot);
  const targetParentStat = requireDirectory(targetParent, "sidecar target parent", io);
  const backupParentStat = requireDirectory(backupParent, "sidecar backup parent", io);
  if (targetParentStat.dev !== backupParentStat.dev) {
    throw new Error("sidecar replacement backup must be on the candidate volume");
  }
  if (io.lstatSync(backupRoot, { throwIfNoEntry: false })) {
    throw new Error("sidecar replacement backup already exists");
  }
  io.renameSync(targetRoot, backupRoot);
  try {
    const proof = transferSidecar({
      stageRoot,
      targetRoot,
      proofPath,
      nonce,
      expectedManifestSha256,
      io,
      now,
    });
    return { backupRoot, proofPath, stageRoot, targetRoot, proof };
  } catch (error) {
    try {
      if (!io.lstatSync(targetRoot, { throwIfNoEntry: false })
          && io.lstatSync(backupRoot, { throwIfNoEntry: false })) {
        io.renameSync(backupRoot, targetRoot);
      }
    } catch {
      // The caller receives the original failure. Both locations remain bounded
      // and no proof can make an incomplete replacement valid.
    }
    throw error;
  }
}

function commitSidecarReplacement(transaction, {
  io = fs,
  retainBackupAsStage = false,
} = {}) {
  requireDirectory(transaction.targetRoot, "replacement sidecar target", io);
  requireRegularPath(transaction.proofPath, "replacement sidecar proof", io);
  requireDirectory(transaction.backupRoot, "replacement sidecar backup", io);
  if (retainBackupAsStage) {
    if (io.lstatSync(transaction.stageRoot, { throwIfNoEntry: false })) {
      throw new Error("replacement stage already exists before backup rotation");
    }
    io.renameSync(transaction.backupRoot, transaction.stageRoot);
  } else {
    io.rmSync(transaction.backupRoot, { recursive: true, force: false });
  }
}

function rollbackSidecarReplacement(transaction, io = fs) {
  const backup = io.lstatSync(transaction.backupRoot, { throwIfNoEntry: false });
  if (!backup?.isDirectory() || backup.isSymbolicLink()) {
    throw new Error("sidecar replacement rollback backup is missing or unsafe");
  }
  const target = io.lstatSync(transaction.targetRoot, { throwIfNoEntry: false });
  if (target) {
    if (!target.isDirectory() || target.isSymbolicLink()) {
      throw new Error("sidecar replacement rollback target is unsafe");
    }
    if (io.lstatSync(transaction.stageRoot, { throwIfNoEntry: false })) {
      throw new Error("sidecar replacement rollback stage already exists");
    }
    io.renameSync(transaction.targetRoot, transaction.stageRoot);
  }
  io.rmSync(transaction.proofPath, { force: true });
  io.renameSync(transaction.backupRoot, transaction.targetRoot);
}

function readTransferProof(proofPath, io = fs) {
  const stat = requireRegularPath(proofPath, "sidecar transfer proof", io);
  if (stat.size > 16 * 1024) throw new Error("sidecar transfer proof is oversized");
  const proof = JSON.parse(io.readFileSync(proofPath, "utf8"));
  if (!proof || Object.keys(proof).sort().join() !== PROOF_KEYS.join()) {
    throw new Error("sidecar transfer proof has an invalid shape");
  }
  return proof;
}

async function afterPack(context) {
  if (context?.electronPlatformName !== "win32") {
    throw new Error("sidecar atomic transfer currently supports Windows candidates only");
  }
  const nonce = process.env[NONCE_ENV] || "";
  const expectedManifestSha256 = process.env[MANIFEST_SHA_ENV] || "";
  const appOutDir = path.resolve(String(context.appOutDir || ""));
  const resources = path.join(appOutDir, "resources");
  const proofPath = path.join(path.resolve(String(context.outDir || "")), proofName(nonce));
  const proof = transferSidecar({
    stageRoot: STAGE_ROOT,
    targetRoot: path.join(resources, "sidecar"),
    proofPath,
    nonce,
    expectedManifestSha256,
  });
  console.log(
    `[sidecar-transfer] atomically transferred ${proof.files} files `
      + `(${proof.total_size} bytes, ${proof.content_set_sha256})`,
  );
}

exports.afterPack = afterPack;
exports.beginSidecarReplacement = beginSidecarReplacement;
exports.commitSidecarReplacement = commitSidecarReplacement;
exports.MANIFEST_NAME = MANIFEST_NAME;
exports.MANIFEST_SHA_ENV = MANIFEST_SHA_ENV;
exports.NONCE_ENV = NONCE_ENV;
exports.PROOF_KEYS = PROOF_KEYS;
exports.proofName = proofName;
exports.readManifestSummary = readManifestSummary;
exports.readTransferProof = readTransferProof;
exports.rollbackSidecarReplacement = rollbackSidecarReplacement;
exports.sha256File = sha256File;
exports.transferSidecar = transferSidecar;
exports.validateTransferInputs = validateTransferInputs;
