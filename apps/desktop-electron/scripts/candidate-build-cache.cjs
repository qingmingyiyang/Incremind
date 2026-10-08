const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const ELECTRON_ROOT = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(ELECTRON_ROOT, "..", "..");
const FRONTEND_ROOT = path.join(REPO_ROOT, "src", "frontend");
const CANDIDATE_ROOT = path.join(ELECTRON_ROOT, "release", "win-unpacked");
const CACHE_ROOT = path.join(ELECTRON_ROOT, ".cache");
const FRONTEND_CACHE_PATH = path.join(CACHE_ROOT, "frontend-build-v1.json");
const SHELL_CACHE_PATH = path.join(CACHE_ROOT, "windows-candidate-shell-v1.json");
const CACHE_SCHEMA = "1.0.0";
const HASH_CONCURRENCY = 8;
const IMPLEMENTATION_SHA256 = crypto.createHash("sha256").update(fs.readFileSync(__filename)).digest("hex");
const FRONTEND_POLICY = `frontend-build:${IMPLEMENTATION_SHA256}`;
const SHELL_POLICY = `windows-candidate-shell:${IMPLEMENTATION_SHA256}`;
const SHA256 = /^[a-f0-9]{64}$/;

function normalize(relativePath) {
  return relativePath.replaceAll(path.sep, "/");
}

function assertSafePrefix(prefix) {
  if (!prefix
      || prefix.includes("\\")
      || path.posix.normalize(prefix) !== prefix
      || prefix === ".."
      || prefix.startsWith("../")
      || path.win32.isAbsolute(prefix)
      || prefix.includes(":")) {
    throw new Error(`candidate cache prefix is unsafe: ${prefix}`);
  }
}

function collectInput(input, output, io = fs) {
  const { root, prefix, exclude = () => false } = input;
  assertSafePrefix(prefix);
  const rootStat = io.lstatSync(root, { throwIfNoEntry: false });
  if (!rootStat || rootStat.isSymbolicLink()) {
    throw new Error(`candidate cache input is missing or linked: ${root}`);
  }
  if (rootStat.isFile()) {
    if (!exclude(prefix, false)) output.push({ absolute: root, path: prefix, size: Number(rootStat.size) });
    return;
  }
  if (!rootStat.isDirectory()) throw new Error(`candidate cache input is not regular: ${root}`);
  function visit(directory, relativeDirectory = "") {
    for (const entry of io.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      const relative = normalize(path.join(relativeDirectory, entry.name));
      const manifestPath = `${prefix}/${relative}`;
      if (exclude(relative, entry.isDirectory())) continue;
      const stat = io.lstatSync(absolute);
      if (stat.isSymbolicLink()) throw new Error(`candidate cache input contains a linked entry: ${absolute}`);
      if (stat.isDirectory()) visit(absolute, relative);
      else if (stat.isFile()) output.push({ absolute, path: manifestPath, size: Number(stat.size) });
      else throw new Error(`candidate cache input contains a non-regular entry: ${absolute}`);
    }
  }
  visit(root);
}

function collectInputs(inputs, io = fs) {
  const entries = [];
  for (const input of inputs) collectInput(input, entries, io);
  entries.sort((left, right) => left.path.localeCompare(right.path));
  if (entries.length === 0) throw new Error("candidate cache input set is empty");
  if (new Set(entries.map((entry) => entry.path)).size !== entries.length) {
    throw new Error("candidate cache input paths are not unique");
  }
  return entries;
}

function sha256Async(filePath) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash("sha256");
    const stream = fs.createReadStream(filePath);
    stream.on("error", reject);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("end", () => resolve(hash.digest("hex")));
  });
}

function contentSetSha256(files) {
  const canonical = files.map((file) => `${file.path}\0${file.size}\0${file.sha256}`).join("\n");
  return crypto.createHash("sha256").update(canonical, "utf8").digest("hex");
}

async function createIdentity(inputs, {
  io = fs,
  hash = sha256Async,
  concurrency = HASH_CONCURRENCY,
} = {}) {
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 64) {
    throw new TypeError("candidate cache hash concurrency is invalid");
  }
  const entries = collectInputs(inputs, io);
  const files = new Array(entries.length);
  let cursor = 0;
  async function worker() {
    for (;;) {
      const index = cursor;
      cursor += 1;
      if (index >= entries.length) return;
      const entry = entries[index];
      files[index] = { path: entry.path, size: entry.size, sha256: await hash(entry.absolute) };
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, entries.length) }, () => worker()));
  return {
    files: files.length,
    total_size: files.reduce((total, file) => total + file.size, 0),
    content_set_sha256: contentSetSha256(files),
  };
}

function validIdentity(identity) {
  return identity
    && Number.isSafeInteger(identity.files)
    && identity.files > 0
    && Number.isSafeInteger(identity.total_size)
    && identity.total_size >= 0
    && SHA256.test(identity.content_set_sha256 || "");
}

function readProof(proofPath, policy, io = fs) {
  const stat = io.lstatSync(proofPath, { throwIfNoEntry: false });
  if (!stat) return { hit: false, reason: "proof_missing" };
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 16 * 1024) {
    return { hit: false, reason: "proof_unsafe" };
  }
  try {
    const proof = JSON.parse(io.readFileSync(proofPath, "utf8"));
    if (proof?.schema_version !== CACHE_SCHEMA
        || proof?.policy !== policy
        || !validIdentity(proof.source)
        || !validIdentity(proof.target)
        || typeof proof.generated_at !== "string") {
      return { hit: false, reason: "proof_invalid" };
    }
    return { hit: true, reason: "proof_loaded", proof };
  } catch {
    return { hit: false, reason: "proof_invalid" };
  }
}

function identitiesEqual(left, right) {
  return validIdentity(left)
    && validIdentity(right)
    && left.files === right.files
    && left.total_size === right.total_size
    && left.content_set_sha256 === right.content_set_sha256;
}

function writeProof(proofPath, policy, source, target, {
  io = fs,
  now = () => new Date(),
} = {}) {
  if (!validIdentity(source) || !validIdentity(target)) throw new Error("candidate cache proof identity is invalid");
  io.mkdirSync(path.dirname(proofPath), { recursive: true });
  const proof = {
    schema_version: CACHE_SCHEMA,
    policy,
    source,
    target,
    generated_at: now().toISOString(),
  };
  const temporary = `${proofPath}.tmp-${process.pid}-${crypto.randomBytes(8).toString("hex")}`;
  io.writeFileSync(temporary, `${JSON.stringify(proof)}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
  try {
    io.rmSync(proofPath, { force: true });
    io.renameSync(temporary, proofPath);
  } finally {
    io.rmSync(temporary, { force: true });
  }
  return proof;
}

function frontendSourceInputs() {
  return [
    { root: path.join(FRONTEND_ROOT, "src"), prefix: "src" },
    { root: path.join(FRONTEND_ROOT, "public"), prefix: "public" },
    ...["index.html", "package.json", "package-lock.json", "postcss.config.js", "tailwind.config.js", "vite.config.js"]
      .map((name) => ({ root: path.join(FRONTEND_ROOT, name), prefix: name })),
  ];
}

function frontendTargetInputs() {
  return [{ root: path.join(FRONTEND_ROOT, "dist"), prefix: "dist" }];
}

function shellSourceInputs() {
  return [
    { root: path.join(ELECTRON_ROOT, "node_modules", "electron", "dist"), prefix: "electron-dist" },
    { root: path.join(ELECTRON_ROOT, "src"), prefix: "app/src" },
    { root: path.join(ELECTRON_ROOT, "package.json"), prefix: "app/package.json" },
    { root: path.join(ELECTRON_ROOT, "package-lock.json"), prefix: "build/package-lock.json" },
    { root: path.join(ELECTRON_ROOT, "build-resources"), prefix: "build-resources" },
    { root: path.join(ELECTRON_ROOT, ".manual-stage"), prefix: "resources/manual" },
    { root: path.join(REPO_ROOT, "config", "companion"), prefix: "resources/companion-config" },
    { root: path.join(REPO_ROOT, "src", "frontend", "public", "mascots", "items"), prefix: "resources/companion-assets/items" },
  ];
}

function shellTargetInputs() {
  return [{
    root: CANDIDATE_ROOT,
    prefix: "candidate",
    exclude: (relative) => (
      relative === "resources/sidecar"
      || relative.startsWith("resources/sidecar/")
      || relative === "resources/app/frontend-dist"
      || relative.startsWith("resources/app/frontend-dist/")
    ),
  }];
}

async function inspectCache({ proofPath, policy, sourceInputs, targetInputs, refresh = false, useCache = true }) {
  const startedAt = Date.now();
  const source = await createIdentity(sourceInputs);
  if (!useCache) return { hit: false, reason: "cache_disabled", source, elapsed_ms: Date.now() - startedAt };
  if (refresh) return { hit: false, reason: "refresh_requested", source, elapsed_ms: Date.now() - startedAt };
  const loaded = readProof(proofPath, policy);
  if (!loaded.hit) return { hit: false, reason: loaded.reason, source, elapsed_ms: Date.now() - startedAt };
  if (!identitiesEqual(source, loaded.proof.source)) {
    return { hit: false, reason: "source_changed", source, elapsed_ms: Date.now() - startedAt };
  }
  try {
    const target = await createIdentity(targetInputs);
    if (!identitiesEqual(target, loaded.proof.target)) {
      return { hit: false, reason: "target_changed", source, target, elapsed_ms: Date.now() - startedAt };
    }
    return { hit: true, reason: "cache_hit", source, target, elapsed_ms: Date.now() - startedAt };
  } catch {
    return { hit: false, reason: "target_missing_or_unsafe", source, elapsed_ms: Date.now() - startedAt };
  }
}

async function refreshCache({ proofPath, policy, sourceInputs, targetInputs, expectedSource = null }) {
  const [source, target] = await Promise.all([createIdentity(sourceInputs), createIdentity(targetInputs)]);
  if (expectedSource && !identitiesEqual(source, expectedSource)) {
    throw new Error("candidate cache source changed while the build was running");
  }
  writeProof(proofPath, policy, source, target);
  return { source, target };
}

function inspectFrontendBuildCache(options = {}) {
  return inspectCache({
    proofPath: FRONTEND_CACHE_PATH,
    policy: FRONTEND_POLICY,
    sourceInputs: frontendSourceInputs(),
    targetInputs: frontendTargetInputs(),
    ...options,
  });
}

function refreshFrontendBuildCache(options = {}) {
  return refreshCache({
    proofPath: FRONTEND_CACHE_PATH,
    policy: FRONTEND_POLICY,
    sourceInputs: frontendSourceInputs(),
    targetInputs: frontendTargetInputs(),
    ...options,
  });
}

function inspectCandidateShellCache(options = {}) {
  return inspectCache({
    proofPath: SHELL_CACHE_PATH,
    policy: SHELL_POLICY,
    sourceInputs: shellSourceInputs(),
    targetInputs: shellTargetInputs(),
    ...options,
  });
}

function refreshCandidateShellCache(options = {}) {
  return refreshCache({
    proofPath: SHELL_CACHE_PATH,
    policy: SHELL_POLICY,
    sourceInputs: shellSourceInputs(),
    targetInputs: shellTargetInputs(),
    ...options,
  });
}

module.exports = {
  CACHE_SCHEMA,
  CANDIDATE_ROOT,
  FRONTEND_CACHE_PATH,
  FRONTEND_POLICY,
  HASH_CONCURRENCY,
  SHELL_CACHE_PATH,
  SHELL_POLICY,
  collectInputs,
  contentSetSha256,
  createIdentity,
  identitiesEqual,
  inspectCache,
  inspectCandidateShellCache,
  inspectFrontendBuildCache,
  readProof,
  refreshCache,
  refreshCandidateShellCache,
  refreshFrontendBuildCache,
  shellSourceInputs,
  shellTargetInputs,
  validIdentity,
  writeProof,
};
