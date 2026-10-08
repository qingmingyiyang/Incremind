const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const REQUIRED_BASE_CAPABILITIES = new Set([
  "authenticated_sidecar",
  "local_intake",
  "sqlite_authority",
  "document_memory_project_skill",
  "file_grant_streaming",
  "ffmpeg_media_probe",
  "provider_policy",
]);
const REQUIRED_BASE_FILES = new Set([
  "runtime/python.exe",
  "config/codex-hooks.toml",
]);

function sha256(file) {
  const hash = crypto.createHash("sha256");
  const handle = fs.openSync(file, "r");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    for (;;) {
      const bytesRead = fs.readSync(handle, buffer, 0, buffer.length, null);
      if (bytesRead === 0) break;
      hash.update(buffer.subarray(0, bytesRead));
    }
  } finally {
    fs.closeSync(handle);
  }
  return hash.digest("hex");
}

function normalizedRelativePath(value) {
  if (typeof value !== "string" || !value || value.includes("\\") || path.posix.isAbsolute(value) || path.win32.isAbsolute(value) || value.includes(":")) return false;
  const normalized = path.posix.normalize(value);
  return normalized === value && normalized !== ".." && !normalized.startsWith("../");
}

function walkRelative(root, current = root) {
  const files = [];
  for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
    if (current === root && entry.name === "sidecar-manifest.json") continue;
    const full = path.join(current, entry.name);
    if (entry.isSymbolicLink()) throw new Error(`sidecar contains unsupported symlink: ${path.relative(root, full)}`);
    if (entry.isDirectory()) files.push(...walkRelative(root, full));
    else if (entry.isFile()) files.push(path.relative(root, full).replaceAll(path.sep, "/"));
    else throw new Error(`sidecar contains unsupported entry: ${path.relative(root, full)}`);
  }
  return files.sort((left, right) => left.localeCompare(right));
}

function contentSetSha256(files) {
  const canonical = files.map((file) => `${file.path}\0${file.size}\0${file.sha256}`).join("\n");
  return crypto.createHash("sha256").update(canonical, "utf8").digest("hex");
}

function metadataSetSha256(files) {
  const canonical = files.map((file) => [
    file.path,
    file.size,
    file.dev,
    file.ino,
    file.mode,
    file.nlink,
    file.mtime_ns,
    file.ctime_ns,
    file.birthtime_ns,
  ].join("\0")).join("\n");
  return crypto.createHash("sha256").update(canonical, "utf8").digest("hex");
}

function verifyBasePackContract(pack) {
  if (!pack || pack.pack_id !== "chriptmas-windows-cpu-base" || pack.role !== "required" || pack.platform !== "win32" || pack.arch !== "x64") {
    throw new Error("sidecar manifest has an invalid base pack identity");
  }
  if (!Array.isArray(pack.capabilities) || new Set(pack.capabilities).size !== pack.capabilities.length || pack.capabilities.some((item) => typeof item !== "string" || !/^[a-z][a-z0-9_]*$/.test(item))) {
    throw new Error("sidecar manifest has invalid pack capabilities");
  }
  for (const capability of REQUIRED_BASE_CAPABILITIES) {
    if (!pack.capabilities.includes(capability)) throw new Error(`sidecar base pack is missing capability: ${capability}`);
  }
}

function verifySidecarInventory(root, { verifyHashes }) {
  const manifestPath = path.join(root, "sidecar-manifest.json");
  const manifestStat = fs.lstatSync(manifestPath, { throwIfNoEntry: false });
  if (!manifestStat?.isFile() || manifestStat.isSymbolicLink()) {
    throw new Error(`missing or unsafe sidecar manifest: ${manifestPath}`);
  }
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  if (manifest.schema_version !== "2.0.0" || manifest.build_kind !== "windows-cpu-sidecar") throw new Error("unsupported sidecar manifest");
  verifyBasePackContract(manifest.pack);
  if (!Array.isArray(manifest.files) || manifest.files.length === 0) throw new Error("sidecar manifest has no complete file inventory");
  const declaredPaths = new Set();
  const observedMetadata = [];
  let totalSize = 0;
  for (const file of manifest.files) {
    if (!normalizedRelativePath(file?.path) || declaredPaths.has(file.path)) throw new Error(`sidecar manifest has invalid or duplicate path: ${file?.path}`);
    if (!Number.isSafeInteger(file.size) || file.size < 0 || !/^[a-f0-9]{64}$/.test(file.sha256 || "")) throw new Error(`sidecar manifest has invalid metadata: ${file.path}`);
    declaredPaths.add(file.path);
    const target = path.join(root, file.path);
    const targetStat = fs.lstatSync(target, { throwIfNoEntry: false, bigint: true });
    if (!targetStat) throw new Error(`missing sidecar file: ${file.path}`);
    if (!targetStat.isFile() || targetStat.isSymbolicLink()) throw new Error(`sidecar manifest path is not a regular file: ${file.path}`);
    if (targetStat.size !== BigInt(file.size)) throw new Error(`sidecar file hash mismatch: ${file.path}`);
    if (verifyHashes && sha256(target) !== file.sha256) throw new Error(`sidecar file hash mismatch: ${file.path}`);
    observedMetadata.push({
      path: file.path,
      size: targetStat.size.toString(),
      dev: targetStat.dev.toString(),
      ino: targetStat.ino.toString(),
      mode: targetStat.mode.toString(),
      nlink: targetStat.nlink.toString(),
      mtime_ns: targetStat.mtimeNs.toString(),
      ctime_ns: targetStat.ctimeNs.toString(),
      birthtime_ns: targetStat.birthtimeNs.toString(),
    });
    totalSize += file.size;
  }
  for (const requiredPath of REQUIRED_BASE_FILES) {
    if (!declaredPaths.has(requiredPath)) throw new Error(`sidecar base pack is missing required file: ${requiredPath}`);
  }
  const actualPaths = walkRelative(root);
  if (actualPaths.length !== declaredPaths.size || actualPaths.some((entry) => !declaredPaths.has(entry))) throw new Error("sidecar file set does not match manifest");
  if (manifest.total_size !== totalSize) throw new Error("sidecar manifest total size mismatch");
  if (manifest.content_set_sha256 !== contentSetSha256(manifest.files)) throw new Error("sidecar manifest content set hash mismatch");
  return {
    files: manifest.files.length,
    total_size: manifest.total_size,
    content_set_sha256: manifest.content_set_sha256,
    metadata_set_sha256: metadataSetSha256(observedMetadata),
    root,
    verification: verifyHashes ? "full-content-hash" : "metadata-and-manifest",
  };
}

function verifySidecar(root) {
  return verifySidecarInventory(root, { verifyHashes: true });
}

function verifySidecarMetadata(root) {
  return verifySidecarInventory(root, { verifyHashes: false });
}

if (require.main === module) try {
  const root = path.resolve(process.argv[2] || path.join(__dirname, "..", ".sidecar-stage"));
  console.log(JSON.stringify(verifySidecar(root)));
} catch (error) {
  console.error(`[verify-sidecar] ${error.message}`);
  process.exitCode = 1;
}

module.exports = {
  REQUIRED_BASE_FILES,
  contentSetSha256,
  metadataSetSha256,
  normalizedRelativePath,
  sha256,
  verifyBasePackContract,
  verifySidecar,
  verifySidecarMetadata,
  walkRelative,
};
