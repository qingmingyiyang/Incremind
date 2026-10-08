// 打包后验证：检查 release/ 目录下是否生成了 .exe 或解压目录
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const RELEASE_DIR = path.join(__dirname, "..", "release");
const FRONTEND_DIST = path.join(__dirname, "..", "frontend-dist");
const PACKAGE_JSON = path.join(__dirname, "..", "package.json");
const REPOSITORY_MANUAL = path.join(__dirname, "..", "..", "..", "README.md");
const SIDECAR_VERIFY_CACHE_PATH = path.join(__dirname, "..", ".cache", "sidecar-verification-v1.json");
const { verifySidecar, verifySidecarMetadata } = require("./verify-sidecar.cjs");
const { smokePackaged } = require("./smoke-packaged.cjs");
const { assertBuildId, IDENTITY_FILE, parseCandidateIdentity } = require("../src/candidate-identity.cjs");
const { MAX_MANUAL_BYTES } = require("./stage-manual.cjs");
const { assertPackageClean } = require("./package-guard.cjs");
const {
  proofName,
  readTransferProof,
  sha256File,
  validateTransferInputs,
} = require("./sidecar-transfer.cjs");

const SIDECAR_VERIFY_CACHE_SCHEMA = "1.0.0";
const SIDECAR_VERIFY_CACHE_KEYS = [
  "content_set_sha256",
  "files",
  "generated_at",
  "manifest_sha256",
  "metadata_set_sha256",
  "policy",
  "schema_version",
  "target",
  "total_size",
].sort();
const SHA256 = /^[a-f0-9]{64}$/;
const SIDECAR_VERIFY_POLICY = crypto.createHash("sha256")
  .update(fs.readFileSync(__filename))
  .update(fs.readFileSync(path.join(__dirname, "verify-sidecar.cjs")))
  .digest("hex");

function parseArgs(argv) {
  const options = {
    mode: "dir",
    installerNotOlderThanMs: null,
    sidecarTransferNonce: null,
    sidecarManifestSha256: null,
    runPackagedSmoke: true,
    candidateDir: null,
    candidateId: null,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--mode") {
      options.mode = argv[index + 1];
      index += 1;
    } else if (arg === "--installer-not-older-than-ms") {
      options.installerNotOlderThanMs = Number(argv[index + 1]);
      index += 1;
    } else if (arg === "--sidecar-transfer-nonce") {
      options.sidecarTransferNonce = argv[index + 1];
      index += 1;
    } else if (arg === "--sidecar-manifest-sha256") {
      options.sidecarManifestSha256 = argv[index + 1];
      index += 1;
    } else if (arg === "--skip-packaged-smoke") {
      options.runPackagedSmoke = false;
    } else if (arg === "--candidate-dir") {
      options.candidateDir = argv[index + 1] || "";
      index += 1;
    } else if (arg === "--candidate-id") {
      options.candidateId = argv[index + 1] || "";
      index += 1;
    } else {
      throw new Error(`unknown verify-build argument: ${arg}`);
    }
  }
  if (!["dir", "installer"].includes(options.mode)) {
    throw new Error(`verify-build mode must be dir or installer, got: ${options.mode}`);
  }
  if (options.installerNotOlderThanMs !== null
      && (!Number.isFinite(options.installerNotOlderThanMs) || options.installerNotOlderThanMs < 0)) {
    throw new Error("installer-not-older-than-ms must be a non-negative finite number");
  }
  if (options.mode !== "installer" && options.installerNotOlderThanMs !== null) {
    throw new Error("installer-not-older-than-ms is only valid in installer mode");
  }
  if (options.mode === "installer" && !options.runPackagedSmoke) {
    throw new Error("installer verification cannot skip the packaged startup smoke");
  }
  const hasTransferNonce = options.sidecarTransferNonce !== null;
  const hasManifestSha = options.sidecarManifestSha256 !== null;
  if (hasTransferNonce !== hasManifestSha) {
    throw new Error("sidecar transfer nonce and manifest SHA-256 must be supplied together");
  }
  if (hasTransferNonce) {
    validateTransferInputs(options.sidecarTransferNonce, options.sidecarManifestSha256);
  }
  if (options.candidateDir !== null || options.candidateId !== null) {
    if (typeof options.candidateDir !== "string" || typeof options.candidateId !== "string") {
      throw new Error("candidate-dir and candidate-id must be supplied together");
    }
    assertBuildId(options.candidateId);
    const expected = path.resolve(RELEASE_DIR, "candidates", options.candidateId);
    if (path.resolve(options.candidateDir) !== expected) {
      throw new Error("candidate-dir must be the isolated directory for candidate-id");
    }
  }
  return options;
}

function expectedInstallerName(packageJson) {
  const template = packageJson?.build?.win?.artifactName;
  const version = packageJson?.version;
  if (typeof template !== "string" || !template || typeof version !== "string" || !version) {
    throw new Error("package.json must define version and build.win.artifactName");
  }
  const name = template.replaceAll("${version}", version);
  if (name.includes("${") || path.basename(name) !== name || !name.toLowerCase().endsWith(".exe")) {
    throw new Error(`unsupported installer artifactName: ${template}`);
  }
  return name;
}

function verifyReleaseArtifacts({
  releaseDir,
  packageJson,
  mode,
  installerNotOlderThanMs = null,
  candidateDir = releaseDir,
}) {
  if (!fs.existsSync(candidateDir)) {
    throw new Error(`candidate 目录不存在: ${candidateDir}`);
  }
  const unpacked = path.join(candidateDir, "win-unpacked");
  if (!fs.existsSync(unpacked) || !fs.statSync(unpacked).isDirectory()) {
    throw new Error(`缺少当前Windows解压候选: ${unpacked}`);
  }
  if (mode === "dir") {
    return { mode, unpacked, installer: null };
  }
  if (mode !== "installer") {
    throw new Error(`unsupported release verification mode: ${mode}`);
  }
  const installer = path.join(candidateDir, expectedInstallerName(packageJson));
  if (!fs.existsSync(installer) || !fs.statSync(installer).isFile()) {
    throw new Error(`缺少当前版本安装器: ${installer}`);
  }
  const modifiedMs = fs.statSync(installer).mtimeMs;
  if (installerNotOlderThanMs !== null && modifiedMs < installerNotOlderThanMs) {
    throw new Error(
      `安装器不是本轮构建产物: ${path.basename(installer)} mtime=${modifiedMs} < build-start=${installerNotOlderThanMs}`,
    );
  }
  return { mode, unpacked, installer, installerModifiedMs: modifiedMs };
}

function verifyCandidateIdentity(unpackedRoot, candidateId, io = fs) {
  const identityPath = path.join(unpackedRoot, "resources", IDENTITY_FILE);
  const stat = io.lstatSync(identityPath, { throwIfNoEntry: false });
  if (!stat?.isFile() || stat.isSymbolicLink() || stat.size > 8 * 1024) {
    throw new Error("packaged candidate identity is missing or unsafe");
  }
  const identity = parseCandidateIdentity(io.readFileSync(identityPath, "utf8"));
  if (identity.candidate_id !== candidateId || identity.build_id !== candidateId) {
    throw new Error("packaged candidate identity does not match requested candidate-id");
  }
  return identity;
}

function verifyFrontendAssetPaths(indexHtml) {
  const absoluteAssets = [...indexHtml.matchAll(/(?:src|href)=["'](\/assets\/[^"']+)["']/g)]
    .map((match) => match[1]);
  if (absoluteAssets.length) {
    throw new Error(`frontend-dist/index.html contains file-protocol-incompatible absolute assets: ${absoluteAssets.join(", ")}`);
  }
  const relativeAssets = [...indexHtml.matchAll(/(?:src|href)=["']\.\/assets\/[^"']+["']/g)];
  if (!relativeAssets.length) {
    throw new Error("frontend-dist/index.html contains no relative ./assets references");
  }
  return relativeAssets.length;
}

function verifyNoExternalFrontendResourceImports(frontendDist) {
  const files = [];
  function visit(directory) {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const target = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(target);
      else if (/\.(?:css|html)$/i.test(entry.name)) files.push(target);
    }
  }
  visit(frontendDist);
  const externalImports = files.flatMap((file) => {
    const content = fs.readFileSync(file, "utf8");
    const urls = [
      ...content.matchAll(/@import\s+(?:url\(\s*)?['\"]?(https?:\/\/[^'\"\s)]+)/gi),
      ...content.matchAll(/(?:src|href)=['\"](https?:\/\/[^'\"]+)/gi),
    ].map((match) => match[1]);
    return urls.map((url) => `${path.relative(frontendDist, file)}: ${url}`);
  });
  if (externalImports.length) {
    throw new Error(`frontend-dist contains unapproved external HTML/CSS resource imports: ${externalImports.join(", ")}`);
  }
  return files.length;
}

function readManualAuthority(filePath, label, io = fs) {
  const stat = io.lstatSync(filePath, { throwIfNoEntry: false });
  if (!stat?.isFile() || stat.isSymbolicLink()) {
    throw new Error(`${label} must be a regular non-symlink file`);
  }
  if (stat.size > MAX_MANUAL_BYTES) {
    throw new Error(`${label} exceeds the 2 MiB limit`);
  }
  return io.readFileSync(filePath);
}

function verifyPackagedManual(sourcePath, packagedPath, io = fs) {
  const source = readManualAuthority(sourcePath, "manual source", io);
  const packaged = readManualAuthority(packagedPath, "packaged manual", io);
  if (!source.equals(packaged)) {
    throw new Error("packaged manual does not match the current repository README");
  }
  return {
    bytes: packaged.length,
    sha256: crypto.createHash("sha256").update(packaged).digest("hex"),
  };
}

function verifyAtomicSidecarTransfer({
  sidecarRoot,
  proofPath,
  expectedNonce,
  expectedManifestSha256,
  io = fs,
}) {
  validateTransferInputs(expectedNonce, expectedManifestSha256);
  const proof = readTransferProof(proofPath, io);
  if (proof.schema_version !== "1.0.0"
      || proof.method !== "same-volume-atomic-directory-rename"
      || proof.target !== "resources/sidecar"
      || proof.nonce !== expectedNonce
      || proof.manifest_sha256 !== expectedManifestSha256
      || !Number.isFinite(Date.parse(proof.created_at))) {
    throw new Error("sidecar transfer proof does not match this build invocation");
  }
  const manifestPath = path.join(sidecarRoot, "sidecar-manifest.json");
  if (sha256File(manifestPath, io) !== expectedManifestSha256) {
    throw new Error("packaged sidecar manifest does not match the atomically transferred stage");
  }
  const sidecar = verifySidecarMetadata(sidecarRoot);
  if (proof.files !== sidecar.files
      || proof.total_size !== sidecar.total_size
      || proof.content_set_sha256 !== sidecar.content_set_sha256) {
    throw new Error("sidecar transfer proof metadata does not match the packaged candidate");
  }
  return { ...sidecar, verification: "nonce-bound-atomic-transfer" };
}

function readSidecarVerificationProof(proofPath = SIDECAR_VERIFY_CACHE_PATH, io = fs) {
  const stat = io.lstatSync(proofPath, { throwIfNoEntry: false });
  if (!stat) return { hit: false, reason: "proof_missing" };
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 16 * 1024) {
    return { hit: false, reason: "proof_unsafe" };
  }
  try {
    const proof = JSON.parse(io.readFileSync(proofPath, "utf8"));
    if (!proof
        || Object.keys(proof).sort().join() !== SIDECAR_VERIFY_CACHE_KEYS.join()
        || proof.schema_version !== SIDECAR_VERIFY_CACHE_SCHEMA
        || proof.policy !== SIDECAR_VERIFY_POLICY
        || proof.target !== "resources/sidecar"
        || !SHA256.test(proof.manifest_sha256 || "")
        || !SHA256.test(proof.content_set_sha256 || "")
        || !SHA256.test(proof.metadata_set_sha256 || "")
        || !Number.isSafeInteger(proof.files)
        || proof.files <= 0
        || !Number.isSafeInteger(proof.total_size)
        || proof.total_size < 0
        || !Number.isFinite(Date.parse(proof.generated_at))) {
      return { hit: false, reason: "proof_invalid" };
    }
    return { hit: true, reason: "proof_loaded", proof };
  } catch {
    return { hit: false, reason: "proof_invalid" };
  }
}

function sidecarProofMatches(proof, manifestSha256, sidecar) {
  return proof.manifest_sha256 === manifestSha256
    && proof.files === sidecar.files
    && proof.total_size === sidecar.total_size
    && proof.content_set_sha256 === sidecar.content_set_sha256
    && proof.metadata_set_sha256 === sidecar.metadata_set_sha256;
}

function refreshSidecarVerificationProof({
  sidecarRoot,
  sidecar,
  proofPath = SIDECAR_VERIFY_CACHE_PATH,
  manifestSha256 = sha256File(path.join(sidecarRoot, "sidecar-manifest.json")),
  io = fs,
  now = () => new Date(),
}) {
  if (!sidecar || !Number.isSafeInteger(sidecar.files) || !SHA256.test(sidecar.content_set_sha256 || "") || !SHA256.test(sidecar.metadata_set_sha256 || "")) {
    throw new Error("sidecar verification proof received an invalid verified identity");
  }
  const proof = {
    schema_version: SIDECAR_VERIFY_CACHE_SCHEMA,
    policy: SIDECAR_VERIFY_POLICY,
    target: "resources/sidecar",
    manifest_sha256: manifestSha256,
    files: sidecar.files,
    total_size: sidecar.total_size,
    content_set_sha256: sidecar.content_set_sha256,
    metadata_set_sha256: sidecar.metadata_set_sha256,
    generated_at: now().toISOString(),
  };
  io.mkdirSync(path.dirname(proofPath), { recursive: true });
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

function verifySidecarWithProofCache(sidecarRoot, {
  proofPath = SIDECAR_VERIFY_CACHE_PATH,
  io = fs,
} = {}) {
  const manifestSha256 = sha256File(path.join(sidecarRoot, "sidecar-manifest.json"), io);
  const metadata = verifySidecarMetadata(sidecarRoot);
  const loaded = readSidecarVerificationProof(proofPath, io);
  if (loaded.hit && sidecarProofMatches(loaded.proof, manifestSha256, metadata)) {
    return { ...metadata, verification: "cached-full-content-proof", cache: "hit" };
  }
  const verified = verifySidecar(sidecarRoot);
  refreshSidecarVerificationProof({ sidecarRoot, sidecar: verified, proofPath, manifestSha256, io });
  return { ...verified, cache: "refreshed", cache_miss: loaded.hit ? "metadata_changed" : loaded.reason };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  console.log("[verify-build] 检查打包产物...");

  // 1. 验证 frontend-dist 存在且包含 index.html
  const indexHtml = path.join(FRONTEND_DIST, "index.html");
  if (!fs.existsSync(indexHtml)) {
    throw new Error(`frontend-dist/index.html 不存在: ${indexHtml}`);
  }
  const assetReferences = verifyFrontendAssetPaths(fs.readFileSync(indexHtml, "utf8"));
  const localResourceFiles = verifyNoExternalFrontendResourceImports(FRONTEND_DIST);
  console.log(`[verify-build] frontend-dist/index.html ✓ (${assetReferences} relative assets; ${localResourceFiles} HTML/CSS files have no external imports)`);

  // 2. Bind verification to the requested build mode. Historical setup
  // executables must never stand in for the current unpacked candidate.
  const packageJson = JSON.parse(fs.readFileSync(PACKAGE_JSON, "utf8"));
  const artifact = verifyReleaseArtifacts({
    releaseDir: RELEASE_DIR,
    candidateDir: options.candidateDir || RELEASE_DIR,
    packageJson,
    mode: options.mode,
    installerNotOlderThanMs: options.installerNotOlderThanMs,
  });
  verifyPackageData(path.join(artifact.unpacked, "resources"));
  console.log(`[verify-build] 当前Windows解压候选: ${artifact.unpacked}`);
  if (artifact.installer) {
    console.log(`[verify-build] 本轮Windows安装器: ${artifact.installer}`);
  } else {
    console.log("[verify-build] dir模式不使用release目录中的历史安装器作为证据");
  }

  const identity = options.candidateId === null ? null : verifyCandidateIdentity(artifact.unpacked, options.candidateId);
  if (identity) console.log(`[verify-build] candidate identity: ${identity.build_id} source ${identity.source_commit.slice(0, 12)}`);

  const manual = verifyPackagedManual(
    REPOSITORY_MANUAL,
    path.join(artifact.unpacked, "resources", "manual", "readme.md"),
  );
  console.log(`[verify-build] packaged manual verified: ${manual.bytes} bytes, sha256 ${manual.sha256}`);

  const sidecarRoot = path.join(artifact.unpacked, "resources", "sidecar");
  const sidecar = options.sidecarTransferNonce === null
    ? verifySidecarWithProofCache(sidecarRoot)
    : verifyAtomicSidecarTransfer({
      sidecarRoot,
      proofPath: path.join(options.candidateDir || RELEASE_DIR, proofName(options.sidecarTransferNonce)),
      expectedNonce: options.sidecarTransferNonce,
      expectedManifestSha256: options.sidecarManifestSha256,
    });
  if (options.sidecarTransferNonce !== null) {
    refreshSidecarVerificationProof({
      sidecarRoot,
      sidecar,
      manifestSha256: options.sidecarManifestSha256,
    });
  }
  console.log(`[verify-build] sidecar verified: ${sidecar.files} files (${sidecar.verification})`);
  if (options.runPackagedSmoke) {
    const smoke = await smokePackaged(path.join(artifact.unpacked, "resources"));
    console.log(`[verify-build] packaged sidecar smoke: ${smoke.startupMs} ms`);
    console.log("[verify-build] 验证通过 ✓");
  } else {
    console.log("[verify-build] packaged sidecar smoke: skipped (fast local candidate; full Gate required)");
    console.log("[verify-build] 完整性验证通过；真实启动smoke尚未执行");
  }
}

function verifyPackageData(resourcesRoot, stageRoot = path.join(__dirname, "..", ".sidecar-stage")) {
  assertPackageClean(resourcesRoot, {
    runtimeRoots: [path.join(resourcesRoot, "sidecar", "runtime")],
    cleanupStageRoot: stageRoot,
  });
}

if (require.main === module) main().catch((error) => {
  console.error(`[verify-build] ${error.stack || error.message}`);
  process.exitCode = 1;
});

module.exports = {
  expectedInstallerName,
  main,
  parseArgs,
  readSidecarVerificationProof,
  refreshSidecarVerificationProof,
  SIDECAR_VERIFY_CACHE_PATH,
  SIDECAR_VERIFY_POLICY,
  sidecarProofMatches,
  verifySidecarWithProofCache,
  verifyFrontendAssetPaths,
  verifyNoExternalFrontendResourceImports,
  verifyAtomicSidecarTransfer,
  verifyCandidateIdentity,
  verifyPackagedManual,
  verifyPackageData,
  verifyReleaseArtifacts,
};
