const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  APPLICATION_STAGE_POLICY,
  RUNTIME_HASH_POLICY,
  INPUTS,
  HASH_CONCURRENCY,
  ROBOCOPY_THREADS,
  assertNoLinkedEntries,
  buildProfile,
  compileApplicationBytecode,
  collectApplicationSourceEntries,
  contentSetSha256,
  copyRuntimeBytecodeWithRobocopy,
  copyRuntimeWithRobocopy,
  hashFileEntries,
  parseArgs,
  pruneUnstagedArtifacts,
  readRuntimeHashCache,
  readApplicationStageProof,
  refreshRotatedStageProofs,
  sha256,
  shouldStage,
  shouldStageRuntimeBytecode,
  snapshotRuntimeSource,
  snapshotApplicationSources,
  readStageReuseProof,
  writeStageReuseProof,
  validateRuntimeBytecodeCache,
  writeRuntimeHashCache,
  writeApplicationStageProof,
} = require("../scripts/stage-sidecar.cjs");
const { verifySidecar } = require("../scripts/verify-sidecar.cjs");
const { refreshSidecarVerificationProof } = require("../scripts/verify-build.cjs");
const {
  beginFullCandidateReplacement,
  commitFullCandidateReplacement,
  inspectVerifiedStandbyCandidate,
  parseArgs: parseBuildArgs,
  rollbackFullCandidateReplacement,
} = require("../scripts/build-windows-candidate.cjs");
const packageManifest = require("../package.json");

test("stages the rebuild storage configuration required by packaged routes", () => {
  assert.ok(
    INPUTS.some(([source, target]) => source.endsWith("config\\rebuild.toml.example") && target.endsWith("config\\rebuild.toml.example")),
  );
});

test("stages the migrated core package for the Electron backend", () => {
  const projectRoot = path.resolve(__dirname, "..", "..", "..");
  const electronRoot = path.resolve(__dirname, "..");
  const coreSource = path.join(projectRoot, "src", "core");
  const coreTarget = path.join(electronRoot, ".sidecar-stage", "core");
  const coreInput = INPUTS.find(([source, target]) => source === coreSource && target === coreTarget);
  assert.ok(coreInput, "Electron must copy src/core into the importable sidecar root");
  assert.ok(collectApplicationSourceEntries([coreInput]).some((entry) =>
    entry.path === "core/ai_kernel/runtime.py" && entry.absolute === path.join(coreSource, "ai_kernel", "runtime.py")));
});

test("stages the canonical Codex Hook baseline required by packaged AI runtime", () => {
  assert.ok(
    INPUTS.some(([source, target]) => source.endsWith("config\\codex-hooks.toml") && target.endsWith("config\\codex-hooks.toml")),
  );
});

test("formal package builds recreate the stage while retaining the runtime hash cache", () => {
  assert.match(packageManifest.scripts.prebuild, /stage:sidecar -- --clean$/);
  assert.match(packageManifest.scripts["build:windows:refresh-sidecar-cache"], /--refresh-sidecar-cache$/);
  assert.match(packageManifest.scripts["build:windows:refresh-candidate-cache"], /--refresh-candidate-cache$/);
  assert.match(packageManifest.scripts["build:windows:fast"], /--dir --fast$/);
  assert.match(packageManifest.scripts["build:windows:gate"], /--dir --gate$/);
  assert.match(packageManifest.scripts["build:windows:verify"], /--verify-only --gate$/);
});

test("exposes separate development, candidate, and release gate entrypoints", () => {
  const developmentBuild = packageManifest.scripts["build:dev"];
  assert.match(developmentBuild, /verify:companion-pack/);
  assert.match(developmentBuild, /build:frontend/);
  assert.doesNotMatch(developmentBuild, /build-windows-candidate|stage:sidecar|copy:frontend|release/);
  assert.equal(packageManifest.scripts["build:candidate"], "npm run build:windows");
  assert.equal(packageManifest.scripts["gate:release"], "npm run build:windows:gate");
});

test("production sidecar staging excludes only audited development runtime entries", () => {
  const excluded = [
    "runtime/Lib/site-packages/_pytest",
    "runtime/Lib/site-packages/pytest",
    "runtime/Lib/site-packages/pytest-8.4.2.dist-info",
    "runtime/Lib/site-packages/pytest_asyncio",
    "runtime/Lib/site-packages/importlinter",
    "runtime/Lib/site-packages/import_linter-2.11.dist-info",
    "runtime/Lib/site-packages/grimp",
    "runtime/Lib/site-packages/pip",
    "runtime/Lib/site-packages/pip-26.1.2.dist-info",
    "runtime/Lib/site-packages/wheel",
    "runtime/Lib/site-packages/wheel-0.47.0.dist-info",
    "runtime/Library/share/doc",
    "runtime/Library/share/man",
    "runtime/Library/share/info",
    "runtime/share/doc",
    "runtime/share/man",
    "runtime/Lib/idlelib",
    "runtime/Lib/lib2to3",
    "runtime/Lib/test",
    "runtime/Lib/turtledemo",
    "runtime/Lib/ensurepip",
    "runtime/Scripts/pytest.exe",
    "runtime/Scripts/pip.exe",
    "runtime/Scripts/wheel.exe",
  ];
  for (const relative of excluded) assert.equal(shouldStage(relative), false, relative);

  const retained = [
    "runtime/Lib/site-packages/setuptools",
    "runtime/Lib/site-packages/numpy",
    "runtime/Lib/site-packages/onnxruntime",
    "runtime/Lib/site-packages/av",
    "runtime/Lib/site-packages/lance",
    "runtime/Lib/site-packages/lancedb",
    "runtime/Lib/site-packages/fastembed",
    "runtime/Lib/site-packages/faster_whisper",
    "runtime/Lib/site-packages/llama_index",
    "runtime/Lib/site-packages/pygments",
    "runtime/Lib/site-packages/networkx",
    "runtime/Lib/site-packages/nltk",
    "runtime/Lib/site-packages/sqlalchemy",
    "runtime/Lib/site-packages/tantivy",
    "runtime/Library/share/locale",
    "runtime/Lib/unittest",
  ];
  for (const relative of retained) assert.equal(shouldStage(relative), true, relative);
  assert.equal(shouldStage("runtime/Lib/site-packages/example/__pycache__/module.cpython-311.pyc"), false);
  assert.equal(shouldStageRuntimeBytecode("runtime/Lib/site-packages/example/__pycache__/module.cpython-311.pyc"), true);
  assert.equal(shouldStageRuntimeBytecode("runtime/Lib/site-packages/_pytest/__pycache__/main.cpython-311.pyc"), false);
  assert.equal(shouldStageRuntimeBytecode("runtime/Lib/site-packages/pip/__pycache__/main.cpython-311.pyc"), false);
});

test("stage and candidate cache arguments are explicit and mutually exclusive", () => {
  assert.deepEqual(parseArgs(["--clean", "--refresh-runtime-cache"]), {
    clean: true,
    refreshRuntimeCache: true,
    useRuntimeCache: true,
    reuseVerifiedStage: false,
  });
  assert.deepEqual(parseArgs(["--clean", "--no-runtime-cache"]), {
    clean: true,
    refreshRuntimeCache: false,
    useRuntimeCache: false,
    reuseVerifiedStage: false,
  });
  assert.throws(() => parseArgs(["--refresh-runtime-cache", "--no-runtime-cache"]), /cannot be combined/);
  assert.equal(parseArgs(["--reuse-verified-stage"]).reuseVerifiedStage, true);
  assert.throws(() => parseArgs(["--clean", "--reuse-verified-stage"]), /cannot be combined/);
  assert.throws(() => parseArgs(["--unknown"]), /unknown stage-sidecar argument/);
  assert.equal(parseBuildArgs(["--dir", "--refresh-sidecar-cache"]).refreshSidecarCache, true);
  assert.equal(parseBuildArgs(["--dir", "--no-sidecar-cache"]).useSidecarCache, false);
  assert.equal(parseBuildArgs(["--dir", "--refresh-candidate-cache"]).refreshCandidateCache, true);
  assert.equal(parseBuildArgs(["--dir", "--no-candidate-cache"]).useCandidateCache, false);
  assert.equal(parseBuildArgs(["--dir"]).runE2e, false);
  assert.equal(parseBuildArgs(["--dir", "--gate"]).runE2e, true);
  assert.equal(parseBuildArgs(["--verify-only", "--gate", "--candidate-id", "windows-20261008T000000Z-0123456789ab"]).runE2e, true);
  assert.throws(() => parseBuildArgs(["--verify-only", "--gate"]), /verify-only必须指定--candidate-id/);
  assert.equal(parseBuildArgs(["--installer"]).runE2e, true);
  assert.deepEqual(
    { fastLocalCandidate: parseBuildArgs(["--dir", "--fast"]).fastLocalCandidate, runE2e: parseBuildArgs(["--dir", "--fast"]).runE2e },
    { fastLocalCandidate: true, runE2e: false },
  );
  assert.throws(() => parseBuildArgs(["--installer", "--fast"]), /不能跳过真实启动smoke/);
  assert.throws(() => parseBuildArgs(["--installer", "--skip-e2e"]), /安装包构建必须运行完整Electron E2E/);
  assert.throws(() => parseBuildArgs(["--gate", "--skip-e2e"]), /不能同时/);
  assert.throws(() => parseBuildArgs(["--refresh-sidecar-cache", "--no-sidecar-cache"]), /不能与/);
  assert.throws(() => parseBuildArgs(["--refresh-candidate-cache", "--no-candidate-cache"]), /不能与/);
});

test("verified standby stage proof round-trips and detects same-size runtime changes", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-verified-stage-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const sourceRoot = path.join(root, "source-runtime");
  const stageRoot = path.join(root, "stage");
  const stageRuntime = path.join(stageRoot, "runtime");
  const proofRoot = path.join(root, "proofs");
  fs.mkdirSync(path.join(sourceRoot, "Lib"), { recursive: true });
  fs.mkdirSync(path.join(stageRuntime, "Lib", "__pycache__"), { recursive: true });
  fs.writeFileSync(path.join(sourceRoot, "python.exe"), "runtime");
  fs.writeFileSync(path.join(sourceRoot, "Lib", "module.py"), "source");
  fs.copyFileSync(path.join(sourceRoot, "python.exe"), path.join(stageRuntime, "python.exe"));
  fs.copyFileSync(path.join(sourceRoot, "Lib", "module.py"), path.join(stageRuntime, "Lib", "module.py"));
  fs.writeFileSync(path.join(stageRuntime, "Lib", "__pycache__", "module.pyc"), "bytecode");
  const sourceSnapshot = snapshotRuntimeSource(sourceRoot);
  const runtimeFiles = [
    ["runtime/Lib/__pycache__/module.pyc", "bytecode"],
    ["runtime/Lib/module.py", "source"],
    ["runtime/python.exe", "runtime"],
  ].map(([filePath, body]) => ({
    path: filePath,
    size: Buffer.byteLength(body),
    sha256: require("node:crypto").createHash("sha256").update(body).digest("hex"),
  }));
  writeStageReuseProof(sourceSnapshot, runtimeFiles, { stageRoot, proofRoot });
  assert.equal(readStageReuseProof(sourceSnapshot, runtimeFiles, { stageRoot, proofRoot }).hit, true);
  fs.writeFileSync(path.join(stageRuntime, "python.exe"), "changed");
  assert.equal(
    readStageReuseProof(sourceSnapshot, runtimeFiles, { stageRoot, proofRoot }).reason,
    "stage_proof_missing_or_unsafe",
  );
});

test("verified candidate rotation reissues metadata-bound standby proofs after history eviction", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-rotated-standby-proof-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const sourceRuntime = path.join(root, "source-runtime");
  const sourceBackend = path.join(root, "source-backend");
  const sourceConfig = path.join(root, "source-config", "codex-hooks.toml");
  const targetRoot = path.join(root, "candidate-sidecar");
  const stageRoot = path.join(root, "stage");
  const runtimeProofRoot = path.join(root, "runtime-proofs");
  const applicationProofRoot = path.join(root, "application-proofs");
  fs.mkdirSync(path.join(sourceRuntime, "Lib"), { recursive: true });
  fs.mkdirSync(sourceBackend, { recursive: true });
  fs.mkdirSync(path.dirname(sourceConfig), { recursive: true });
  fs.mkdirSync(path.join(targetRoot, "runtime", "Lib", "__pycache__"), { recursive: true });
  fs.mkdirSync(path.join(targetRoot, "backend", "__pycache__"), { recursive: true });
  fs.mkdirSync(path.join(targetRoot, "config"), { recursive: true });
  fs.writeFileSync(path.join(sourceRuntime, "python.exe"), "runtime");
  fs.writeFileSync(path.join(sourceRuntime, "Lib", "module.py"), "source");
  fs.writeFileSync(path.join(sourceBackend, "app.py"), "print('source')\n");
  fs.writeFileSync(sourceConfig, "[hooks]\nenabled = true\n");
  fs.copyFileSync(path.join(sourceRuntime, "python.exe"), path.join(targetRoot, "runtime", "python.exe"));
  fs.copyFileSync(path.join(sourceRuntime, "Lib", "module.py"), path.join(targetRoot, "runtime", "Lib", "module.py"));
  fs.copyFileSync(path.join(sourceBackend, "app.py"), path.join(targetRoot, "backend", "app.py"));
  fs.copyFileSync(sourceConfig, path.join(targetRoot, "config", "codex-hooks.toml"));
  fs.writeFileSync(path.join(targetRoot, "runtime", "Lib", "__pycache__", "module.pyc"), "runtime-bytecode");
  fs.writeFileSync(path.join(targetRoot, "backend", "__pycache__", "app.pyc"), "application-bytecode");
  fs.writeFileSync(path.join(targetRoot, "sidecar-profile.json"), "{}\n");
  const manifestFiles = [
    "backend/__pycache__/app.pyc",
    "backend/app.py",
    "config/codex-hooks.toml",
    "runtime/Lib/__pycache__/module.pyc",
    "runtime/Lib/module.py",
    "runtime/python.exe",
    "sidecar-profile.json",
  ].map((relative) => {
    const absolute = path.join(targetRoot, ...relative.split("/"));
    return { path: relative, size: fs.statSync(absolute).size, sha256: sha256(absolute) };
  });
  const manifest = {
    schema_version: "2.0.0",
    build_kind: "windows-cpu-sidecar",
    pack: require("../scripts/stage-sidecar.cjs").BASE_PACK,
    generated_at: new Date().toISOString(),
    inputs: ["runtime", "src/backend", "config/codex-hooks.toml"],
    files: manifestFiles,
    content_set_sha256: contentSetSha256(manifestFiles),
    total_size: manifestFiles.reduce((total, file) => total + file.size, 0),
  };
  fs.writeFileSync(path.join(targetRoot, "sidecar-manifest.json"), `${JSON.stringify(manifest)}\n`);
  const verified = verifySidecar(targetRoot);
  const manifestSha256 = sha256(path.join(targetRoot, "sidecar-manifest.json"));
  const candidateProofPath = path.join(root, "candidate-proof.json");
  refreshSidecarVerificationProof({
    sidecarRoot: targetRoot,
    sidecar: verified,
    proofPath: candidateProofPath,
    manifestSha256,
  });
  const inspected = inspectVerifiedStandbyCandidate(targetRoot, { proofPath: candidateProofPath });
  assert.equal(inspected.hit, true);
  assert.equal(inspected.manifestSha256, manifestSha256);

  fs.renameSync(targetRoot, stageRoot);
  const runtimeSourceSnapshot = snapshotRuntimeSource(sourceRuntime);
  const runtimeFiles = manifestFiles.filter((file) => file.path.startsWith("runtime/"));
  assert.equal(
    readStageReuseProof(runtimeSourceSnapshot, runtimeFiles, { stageRoot, proofRoot: runtimeProofRoot }).reason,
    "stage_proof_missing_or_unsafe",
  );

  const refreshed = await refreshRotatedStageProofs({
    stageRoot,
    verifiedSidecar: verified,
    expectedManifestSha256: manifestSha256,
    runtimeSourceRoot: sourceRuntime,
    applicationInputs: [
      [sourceBackend, path.join(stageRoot, "backend"), true],
      [sourceConfig, path.join(stageRoot, "config", "codex-hooks.toml"), true],
    ],
    runtimeProofRoot,
    applicationProofRoot,
    compiledModules: 123,
  });
  assert.equal(refreshed.runtime.hit, true);
  assert.equal(refreshed.application.hit, true);
  assert.equal(
    readStageReuseProof(runtimeSourceSnapshot, runtimeFiles, { stageRoot, proofRoot: runtimeProofRoot }).hit,
    true,
  );
  const applicationSourceSnapshot = await snapshotApplicationSources(
    [
      [sourceBackend, path.join(stageRoot, "backend"), true],
      [sourceConfig, path.join(stageRoot, "config", "codex-hooks.toml"), true],
    ],
    { stageRoot },
  );
  assert.equal(
    readApplicationStageProof(applicationSourceSnapshot, { stageRoot, proofRoot: applicationProofRoot }).hit,
    true,
  );

  fs.writeFileSync(path.join(stageRoot, "runtime", "python.exe"), "mutat1n");
  assert.equal(
    readStageReuseProof(runtimeSourceSnapshot, runtimeFiles, { stageRoot, proofRoot: runtimeProofRoot }).hit,
    false,
  );
});

test("an older candidate without the Codex Hook baseline is an ineligible standby cache", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-legacy-hook-standby-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const oldCandidate = path.join(root, "old-candidate", "resources", "sidecar");
  const nextStage = path.join(root, "next-stage");
  const backup = path.join(root, "candidate-backup");
  fs.mkdirSync(path.join(oldCandidate, "runtime"), { recursive: true });
  fs.mkdirSync(nextStage, { recursive: true });
  fs.writeFileSync(path.join(oldCandidate, "runtime", "python.exe"), "legacy-runtime");
  fs.writeFileSync(path.join(nextStage, "identity.txt"), "current-stage");
  const legacyFiles = ["runtime/python.exe"].map((relative) => {
    const absolute = path.join(oldCandidate, ...relative.split("/"));
    return { path: relative, size: fs.statSync(absolute).size, sha256: sha256(absolute) };
  });
  fs.writeFileSync(path.join(oldCandidate, "sidecar-manifest.json"), `${JSON.stringify({
    schema_version: "2.0.0",
    build_kind: "windows-cpu-sidecar",
    pack: require("../scripts/stage-sidecar.cjs").BASE_PACK,
    files: legacyFiles,
    total_size: legacyFiles[0].size,
    content_set_sha256: contentSetSha256(legacyFiles),
  })}\n`);

  const standby = inspectVerifiedStandbyCandidate(oldCandidate, {
    proofPath: path.join(root, "legacy-proof.json"),
  });
  assert.deepEqual(standby, { hit: false, reason: "candidate_metadata_invalid" });

  const transaction = beginFullCandidateReplacement({
    stageRoot: nextStage,
    targetRoot: path.join(root, "old-candidate"),
    backupRoot: backup,
  });
  assert.ok(transaction);
  assert.equal(fs.readFileSync(path.join(backup, "resources", "sidecar", "runtime", "python.exe"), "utf8"), "legacy-runtime");
  assert.equal(fs.readFileSync(path.join(nextStage, "identity.txt"), "utf8"), "current-stage");
});

test("full shell rebuild keeps the previous candidate intact until the new candidate commits", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-full-shell-standby-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const stageRoot = path.join(root, "stage");
  const targetRoot = path.join(root, "release", "win-unpacked");
  const backupRoot = path.join(root, "cache", "backup");
  fs.mkdirSync(stageRoot, { recursive: true });
  fs.mkdirSync(path.join(targetRoot, "resources", "sidecar"), { recursive: true });
  fs.writeFileSync(path.join(stageRoot, "identity.txt"), "new");
  fs.writeFileSync(path.join(targetRoot, "Chriptmas OS.exe"), "old-exe");
  fs.writeFileSync(path.join(targetRoot, "resources", "sidecar", "identity.txt"), "old-sidecar");
  const transaction = beginFullCandidateReplacement({ stageRoot, targetRoot, backupRoot });
  assert.equal(fs.existsSync(targetRoot), false);
  assert.equal(fs.readFileSync(path.join(backupRoot, "Chriptmas OS.exe"), "utf8"), "old-exe");
  fs.mkdirSync(path.join(targetRoot, "resources"), { recursive: true });
  fs.writeFileSync(path.join(targetRoot, "Chriptmas OS.exe"), "new-exe");
  fs.renameSync(stageRoot, path.join(targetRoot, "resources", "sidecar"));
  commitFullCandidateReplacement(transaction, { retainBackupSidecarAsStage: true });
  assert.equal(fs.readFileSync(path.join(targetRoot, "Chriptmas OS.exe"), "utf8"), "new-exe");
  assert.equal(fs.readFileSync(path.join(stageRoot, "identity.txt"), "utf8"), "old-sidecar");
  assert.equal(fs.existsSync(backupRoot), false);
});

test("failed full shell rebuild restores the complete previous candidate and retains the new stage", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-full-shell-rollback-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const stageRoot = path.join(root, "stage");
  const targetRoot = path.join(root, "release", "win-unpacked");
  const backupRoot = path.join(root, "cache", "backup");
  fs.mkdirSync(stageRoot, { recursive: true });
  fs.mkdirSync(path.join(targetRoot, "resources", "sidecar"), { recursive: true });
  fs.writeFileSync(path.join(stageRoot, "identity.txt"), "new");
  fs.writeFileSync(path.join(targetRoot, "Chriptmas OS.exe"), "old-exe");
  fs.writeFileSync(path.join(targetRoot, "resources", "sidecar", "identity.txt"), "old-sidecar");
  const transaction = beginFullCandidateReplacement({ stageRoot, targetRoot, backupRoot });
  fs.mkdirSync(path.join(targetRoot, "resources"), { recursive: true });
  fs.writeFileSync(path.join(targetRoot, "Chriptmas OS.exe"), "");
  fs.renameSync(stageRoot, path.join(targetRoot, "resources", "sidecar"));
  rollbackFullCandidateReplacement(transaction);
  assert.equal(fs.readFileSync(path.join(targetRoot, "Chriptmas OS.exe"), "utf8"), "old-exe");
  assert.equal(fs.readFileSync(path.join(targetRoot, "resources", "sidecar", "identity.txt"), "utf8"), "old-sidecar");
  assert.equal(fs.readFileSync(path.join(stageRoot, "identity.txt"), "utf8"), "new");
  assert.equal(fs.existsSync(backupRoot), false);
});

test("runtime and application cache policies are independently scoped", () => {
  const scriptDigest = crypto.createHash("sha256")
    .update(fs.readFileSync(path.join(__dirname, "..", "scripts", "stage-sidecar.cjs")))
    .digest("hex");
  assert.match(RUNTIME_HASH_POLICY, /^stage-sidecar-runtime:[a-f0-9]{64}$/);
  assert.match(APPLICATION_STAGE_POLICY, /^stage-sidecar-application:[a-f0-9]{64}$/);
  assert.notEqual(RUNTIME_HASH_POLICY, `stage-sidecar:${scriptDigest}`);
  assert.notEqual(RUNTIME_HASH_POLICY, APPLICATION_STAGE_POLICY);
});

test("application source identity maps canonical inputs to stage paths", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-application-source-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, "source");
  const stage = path.join(root, "stage");
  fs.mkdirSync(path.join(source, "nested"), { recursive: true });
  fs.writeFileSync(path.join(source, "nested", "module.py"), "print('ok')\n");
  fs.writeFileSync(path.join(source, "nested", "ignored.pyc"), "ignored");
  const inputs = [[source, path.join(stage, "backend"), true]];
  assert.deepEqual(
    collectApplicationSourceEntries(inputs, fs, stage).map((file) => file.path),
    ["backend/nested/module.py"],
  );
  const before = await snapshotApplicationSources(inputs, { stageRoot: stage });
  fs.writeFileSync(path.join(source, "nested", "module.py"), "print('changed')\n");
  const after = await snapshotApplicationSources(inputs, { stageRoot: stage });
  assert.notEqual(before.content_set_sha256, after.content_set_sha256);
});

test("verified application proof round-trips and fails closed on source or stage changes", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-application-proof-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, "source");
  const stage = path.join(root, "stage");
  const proofRoot = path.join(root, "proofs");
  fs.mkdirSync(path.join(source, "backend"), { recursive: true });
  fs.mkdirSync(path.join(stage, "backend", "__pycache__"), { recursive: true });
  fs.writeFileSync(path.join(source, "backend", "module.py"), "source");
  fs.writeFileSync(path.join(stage, "backend", "module.py"), "source");
  fs.writeFileSync(path.join(stage, "backend", "__pycache__", "module.pyc"), "bytecode");
  fs.mkdirSync(path.join(stage, "runtime"), { recursive: true });
  fs.writeFileSync(path.join(stage, "runtime", "python.exe"), "runtime");
  fs.writeFileSync(path.join(stage, "sidecar-profile.json"), "profile");
  fs.writeFileSync(path.join(stage, "sidecar-manifest.json"), "manifest");
  const inputs = [[path.join(source, "backend"), path.join(stage, "backend"), true]];
  const sourceSnapshot = await snapshotApplicationSources(inputs, { stageRoot: stage });
  const applicationFiles = [
    ["backend/__pycache__/module.pyc", "bytecode"],
    ["backend/module.py", "source"],
  ].map(([filePath, body]) => ({
    path: filePath,
    size: Buffer.byteLength(body),
    sha256: crypto.createHash("sha256").update(body).digest("hex"),
  }));
  writeApplicationStageProof(sourceSnapshot, applicationFiles, 123, { stageRoot: stage, proofRoot });
  const hit = readApplicationStageProof(sourceSnapshot, { stageRoot: stage, proofRoot });
  assert.equal(hit.hit, true);
  assert.equal(hit.compiled, 123);
  assert.deepEqual(hit.files, applicationFiles);

  const changedSource = { ...sourceSnapshot, content_set_sha256: "0".repeat(64) };
  assert.equal(
    readApplicationStageProof(changedSource, { stageRoot: stage, proofRoot }).reason,
    "application_proof_mismatch",
  );

  fs.writeFileSync(path.join(stage, "backend", "module.py"), "change");
  assert.equal(
    readApplicationStageProof(sourceSnapshot, { stageRoot: stage, proofRoot }).reason,
    "application_proof_missing_or_unsafe",
  );
});

test("Electron Builder receives the staged sidecar through the fail-closed after-pack hook", () => {
  assert.equal(packageManifest.build.afterPack, "./scripts/sidecar-transfer.cjs");
  assert.equal(
    packageManifest.build.extraResources.some((entry) => entry.to === "sidecar"),
    false,
  );
});

test("prunes stale development artifacts from an incremental stage", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-prune-"));
  try {
    fs.mkdirSync(path.join(root, "include", "nested"), { recursive: true });
    fs.writeFileSync(path.join(root, "include", "nested", "header.h"), "header");
    fs.writeFileSync(path.join(root, "runtime.dll"), "runtime");
    pruneUnstagedArtifacts(root);
    assert.equal(fs.existsSync(path.join(root, "include", "nested", "header.h")), false);
    assert.equal(fs.existsSync(path.join(root, "runtime.dll")), true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("verified standby pruning retains bytecode but default pruning removes it", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-prune-bytecode-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const cache = path.join(root, "runtime", "Lib", "__pycache__");
  fs.mkdirSync(cache, { recursive: true });
  fs.writeFileSync(path.join(cache, "module.pyc"), "bytecode");
  pruneUnstagedArtifacts(root, { preserveBytecode: true });
  assert.equal(fs.existsSync(path.join(cache, "module.pyc")), true);
  pruneUnstagedArtifacts(root);
  assert.equal(fs.existsSync(cache), false);
});

test("excludes only cache and development artifacts from the CPU runtime candidate", () => {
  assert.equal(shouldStage("runtime/Lib/site-packages/example.py"), true);
  assert.equal(shouldStage("runtime/Library/bin/ffmpeg.exe"), true);
  assert.equal(shouldStage("runtime/Library/bin/codec.dll"), true);
  for (const relative of ["backend/empty/.gitkeep", "runtime/include/header.h", "runtime/lib/example.lib", "runtime/python311.pdb", "runtime/cache.pyc", "runtime/__pycache__"]) {
    assert.equal(shouldStage(relative), false, relative);
  }
});

test("Windows runtime staging uses bounded native copy with the existing exclusions", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-native-copy-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, "runtime");
  const target = path.join(root, "stage", "runtime");
  fs.mkdirSync(path.join(source, "Lib", "site-packages"), { recursive: true });
  fs.writeFileSync(path.join(source, "python.exe"), "python");
  let invocation = null;
  const result = copyRuntimeWithRobocopy(source, target, false, {
    platform: "win32",
    run(command, args, options) {
      invocation = { command, args, options };
      fs.cpSync(source, target, { recursive: true });
      return { status: 7, stdout: "", stderr: "" };
    },
  });

  assert.deepEqual(result, { method: "robocopy", status: 7 });
  assert.equal(invocation.command, "robocopy.exe");
  assert.ok(invocation.args.includes(`/MT:${ROBOCOPY_THREADS}`));
  assert.ok(invocation.args.includes("/XJ"));
  assert.ok(invocation.args.includes("*.pyc"));
  assert.ok(invocation.args.includes("__pycache__"));
  assert.equal(invocation.options.windowsHide, true);
  assert.equal(fs.readFileSync(path.join(target, "python.exe"), "utf8"), "python");
});

test("native runtime staging fails closed on links partial output and robocopy errors", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-native-boundary-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, "runtime");
  const target = path.join(root, "stage", "runtime");
  fs.mkdirSync(source);
  fs.writeFileSync(path.join(source, "python.exe"), "python");

  assert.throws(
    () => assertNoLinkedEntries(source, {
      ...fs,
      readdirSync(directory) {
        if (directory === source) {
          return [{
            name: "linked-runtime",
            isDirectory: () => false,
            isFile: () => false,
            isSymbolicLink: () => true,
          }];
        }
        return fs.readdirSync(directory, { withFileTypes: true });
      },
    }),
    /unsupported linked entry/,
  );
  assert.throws(
    () => copyRuntimeWithRobocopy(source, target, false, {
      platform: "win32",
      run: () => ({ status: 8, stdout: "copy failed", stderr: "" }),
    }),
    /robocopy failed \(8\)/,
  );
  assert.throws(
    () => copyRuntimeWithRobocopy(source, target, false, {
      platform: "win32",
      run: () => ({ status: null, error: new Error("spawn failed") }),
    }),
    /spawn failed/,
  );
  assert.throws(
    () => copyRuntimeWithRobocopy(source, target, false, {
      platform: "win32",
      run: () => ({ status: 1, stdout: "", stderr: "" }),
    }),
    /did not create a safe target directory/,
  );
});

test("precompiles application modules with portable unchecked-hash caches", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-bytecode-"));
  let invocation = null;
  const run = (command, args, options) => {
    invocation = { command, args, options };
    for (let index = 0; index < 100; index += 1) {
      const cache = path.join(root, "backend", `module-${index}`, "__pycache__");
      fs.mkdirSync(cache, { recursive: true });
      fs.writeFileSync(path.join(cache, `module-${index}.cpython-311.pyc`), "compiled");
    }
    return { status: 0, stdout: "100\n", stderr: "" };
  };
  try {
    assert.equal(compileApplicationBytecode(root, run), 100);
    assert.equal(invocation.command, path.join(root, "runtime", "python.exe"));
    assert.equal(invocation.args[0], "-c");
    assert.match(invocation.args[1], /import backend\.api\.app/);
    assert.match(invocation.args[1], /PycInvalidationMode\.UNCHECKED_HASH/);
    assert.match(invocation.args[1], /cache_from_source/);
    assert.match(invocation.args[1], /preexisting/);
    assert.equal(invocation.options.cwd, root);
    assert.equal(invocation.options.env.PYTHONPATH, root);
    assert.notEqual(invocation.options.env.CHRIPTMAS_APP_ROOT, root);
    assert.equal(invocation.options.timeout, 300000);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("runtime source snapshots detect ordinary same-size mutations through change time", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-snapshot-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, "runtime");
  fs.mkdirSync(path.join(source, "Lib"), { recursive: true });
  const target = path.join(source, "Lib", "module.py");
  fs.writeFileSync(target, "first");
  const before = snapshotRuntimeSource(source);
  const originalTimes = fs.statSync(target);
  fs.writeFileSync(target, "other");
  fs.utimesSync(target, originalTimes.atime, originalTimes.mtime);
  const after = snapshotRuntimeSource(source);
  assert.equal(before.files.length, 1);
  assert.notEqual(after.fingerprint, before.fingerprint);
});

test("runtime hash cache round-trips content identity and fails closed on mismatch", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-hash-cache-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const cachePath = path.join(root, "cache", "runtime.json");
  const snapshot = { fingerprint: "a".repeat(64), files: [] };
  const files = [
    { path: "runtime/python.exe", size: 6, sha256: "b".repeat(64) },
    { path: "runtime/Lib/module.py", size: 5, sha256: "c".repeat(64) },
  ];
  writeRuntimeHashCache(snapshot, files, { cachePath, now: () => new Date("2026-08-01T00:00:00.000Z") });
  const hit = readRuntimeHashCache(snapshot, { cachePath });
  assert.equal(hit.hit, true);
  assert.deepEqual(hit.files, files);
  assert.equal(readRuntimeHashCache({ fingerprint: "d".repeat(64) }, { cachePath }).reason, "cache_mismatch");
  const traversal = JSON.parse(fs.readFileSync(cachePath, "utf8"));
  traversal.files[0].path = "runtime/../escape.bin";
  traversal.content_set_sha256 = contentSetSha256(traversal.files);
  fs.writeFileSync(cachePath, JSON.stringify(traversal), "utf8");
  assert.equal(readRuntimeHashCache(snapshot, { cachePath }).reason, "cache_mismatch");
  fs.writeFileSync(cachePath, "{broken", "utf8");
  assert.equal(readRuntimeHashCache(snapshot, { cachePath }).reason, "cache_invalid");
});

test("parallel hashing reuses only matching runtime entries", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-parallel-hash-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const first = path.join(root, "first.bin");
  const second = path.join(root, "second.bin");
  fs.writeFileSync(first, "first");
  fs.writeFileSync(second, "second");
  const calls = [];
  const result = await hashFileEntries([
    { path: "runtime/first.bin", size: 5, absolute: first },
    { path: "runtime/second.bin", size: 6, absolute: second },
  ], {
    concurrency: HASH_CONCURRENCY,
    cached: new Map([
      ["runtime/first.bin", { path: "runtime/first.bin", size: 5, sha256: "a".repeat(64) }],
      ["runtime/second.bin", { path: "runtime/second.bin", size: 99, sha256: "b".repeat(64) }],
    ]),
    hash: async (file) => { calls.push(file); return "c".repeat(64); },
  });
  assert.equal(result.reused, 1);
  assert.equal(result.hashed, 1);
  assert.deepEqual(calls, [second]);
  assert.deepEqual(result.files.map((file) => file.sha256), ["a".repeat(64), "c".repeat(64)]);
});

test("runtime bytecode cache is content-verified before reuse", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-bytecode-cache-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const cacheRoot = path.join(root, "cache");
  const cached = path.join(cacheRoot, "Lib", "example", "__pycache__", "module.cpython-311.pyc");
  fs.mkdirSync(path.dirname(cached), { recursive: true });
  fs.writeFileSync(cached, "compiled");
  const expected = [{
    path: "runtime/Lib/example/__pycache__/module.cpython-311.pyc",
    size: 8,
    sha256: sha256(cached),
  }];
  const hit = await validateRuntimeBytecodeCache(expected, { cacheRoot });
  assert.deepEqual(hit, { hit: true, reason: "bytecode_cache_hit", files: 1 });
  fs.writeFileSync(cached, "tampered");
  assert.equal((await validateRuntimeBytecodeCache(expected, { cacheRoot })).reason, "bytecode_cache_corrupt");
});

test("runtime bytecode native copy includes only pyc files", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-bytecode-copy-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, "source");
  const target = path.join(root, "target");
  fs.mkdirSync(source);
  fs.writeFileSync(path.join(source, "module.pyc"), "compiled");
  fs.writeFileSync(path.join(source, "module.py"), "source");
  let invocation;
  const result = copyRuntimeBytecodeWithRobocopy(source, target, true, {
    platform: "win32",
    run(command, args, options) {
      invocation = { command, args, options };
      fs.mkdirSync(target, { recursive: true });
      fs.copyFileSync(path.join(source, "module.pyc"), path.join(target, "module.pyc"));
      return { status: 1, stdout: "", stderr: "" };
    },
  });
  assert.deepEqual(result, { method: "robocopy", status: 1 });
  assert.equal(invocation.command, "robocopy.exe");
  assert.ok(invocation.args.includes("*.pyc"));
  assert.ok(invocation.args.includes("/S"));
  assert.equal(fs.existsSync(path.join(target, "module.py")), false);
});

test("builds a deterministic size profile and complete set identity", () => {
  const files = [
    { path: "runtime/Lib/site-packages/example/__init__.py", size: 4, sha256: "a".repeat(64) },
    { path: "runtime/example.dll", size: 10, sha256: "b".repeat(64) },
    { path: "backend/api/app.py", size: 6, sha256: "c".repeat(64) },
  ];
  const profile = buildProfile(files);

  assert.deepEqual(profile.top_level.map(({ name, bytes }) => [name, bytes]), [["runtime", 14], ["backend", 6]]);
  assert.equal(profile.python_packages[0].name, "example");
  assert.equal(profile.native_binaries[0].name, ".dll");
  assert.deepEqual(Object.keys(profile.largest_files[0]).sort(), ["path", "size"]);
  assert.equal(contentSetSha256(files), contentSetSha256([...files]));
  assert.match(contentSetSha256(files), /^[a-f0-9]{64}$/);
});
