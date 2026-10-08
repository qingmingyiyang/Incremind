const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const MAX_PACKAGE_BYTES = 64 * 1024 * 1024;
const PACKAGE_ID = /^pkg-[0-9a-f]{12}$/;

function writeMemoryAssetPackage({
  targetPath,
  packagePayload,
  fileSystem = fs,
  randomId = () => crypto.randomUUID(),
}) {
  if (typeof targetPath !== "string" || !path.isAbsolute(targetPath) || path.extname(targetPath).toLowerCase() !== ".json") {
    throw new Error("memory_asset_export_target_invalid");
  }
  validatePackage(packagePayload);
  const compactPackage = JSON.stringify(packagePayload);
  const packageBytes = Buffer.byteLength(compactPackage, "utf8");
  if (packageBytes > MAX_PACKAGE_BYTES) throw new Error("memory_asset_export_too_large");
  const digest = crypto.createHash("sha256").update(compactPackage, "utf8").digest("hex");
  const envelope = {
    schema_version: "1.0.0",
    package_kind: "chriptmas_memory_asset_package",
    integrity: {
      algorithm: "sha256",
      package_sha256: digest,
    },
    package: packagePayload,
  };
  const output = `${JSON.stringify(envelope, null, 2)}\n`;
  const parent = path.dirname(targetPath);
  const parentStat = fileSystem.lstatSync(parent);
  if (!parentStat.isDirectory() || parentStat.isSymbolicLink()) {
    throw new Error("memory_asset_export_parent_invalid");
  }
  if (fileSystem.existsSync(targetPath)) {
    const targetStat = fileSystem.lstatSync(targetPath);
    if (!targetStat.isFile() || targetStat.isSymbolicLink()) {
      throw new Error("memory_asset_export_existing_target_invalid");
    }
  }
  const temporary = path.join(parent, `.${path.basename(targetPath)}.${randomId()}.tmp`);
  let descriptor = null;
  try {
    descriptor = fileSystem.openSync(temporary, "wx", 0o600);
    fileSystem.writeFileSync(descriptor, output, { encoding: "utf8" });
    fileSystem.fsyncSync(descriptor);
    fileSystem.closeSync(descriptor);
    descriptor = null;
    fileSystem.renameSync(temporary, targetPath);
  } catch (error) {
    if (descriptor !== null) {
      try { fileSystem.closeSync(descriptor); } catch {}
    }
    try {
      if (fileSystem.existsSync(temporary)) fileSystem.unlinkSync(temporary);
    } catch {}
    throw error;
  }
  return Object.freeze({
    status: "saved",
    package_id: packagePayload.package_id,
    file_count: packagePayload.files.length,
    package_bytes: packageBytes,
    package_sha256: digest,
    files: Object.freeze(packagePayload.files.map((item) => Object.freeze({
      logical_path: item.logical_path,
      description: item.description,
      size_bytes: item.size_bytes,
    }))),
    total_size_display: packagePayload.total_size_display,
    rebuild_index_hint: packagePayload.rebuild_index_hint,
  });
}

function validatePackage(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || !PACKAGE_ID.test(value.package_id)
    || !Array.isArray(value.files) || value.files.length < 1 || value.files.length > 64
    || typeof value.total_size_display !== "string"
    || typeof value.rebuild_index_hint !== "string") {
    throw new Error("memory_asset_export_payload_invalid");
  }
  for (const item of value.files) {
    if (!item || typeof item !== "object" || Array.isArray(item)
      || typeof item.logical_path !== "string"
      || !/^[a-z0-9][a-z0-9._-]{0,127}\.json$/.test(item.logical_path)
      || typeof item.description !== "string"
      || !Number.isSafeInteger(item.size_bytes) || item.size_bytes < 0
      || !item.content || typeof item.content !== "object" || Array.isArray(item.content)) {
      throw new Error("memory_asset_export_file_invalid");
    }
  }
}

module.exports = { MAX_PACKAGE_BYTES, writeMemoryAssetPackage };
