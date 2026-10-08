const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { sidecarTransferEnvironment } = require("../scripts/build.cjs");
const {
  beginSidecarReplacement,
  commitSidecarReplacement,
  MANIFEST_SHA_ENV,
  NONCE_ENV,
  proofName,
  rollbackSidecarReplacement,
  sha256File,
  transferSidecar,
} = require("../scripts/sidecar-transfer.cjs");
const {
  refreshSidecarVerificationProof,
  verifyAtomicSidecarTransfer,
  verifySidecarWithProofCache,
} = require("../scripts/verify-build.cjs");
const { contentSetSha256, verifySidecar } = require("../scripts/verify-sidecar.cjs");

const CAPABILITIES = [
  "authenticated_sidecar",
  "local_intake",
  "sqlite_authority",
  "document_memory_project_skill",
  "file_grant_streaming",
  "ffmpeg_media_probe",
  "provider_policy",
];

function stageFixture(root) {
  const stageRoot = path.join(root, ".sidecar-stage");
  fs.mkdirSync(path.join(stageRoot, "runtime"), { recursive: true });
  fs.mkdirSync(path.join(stageRoot, "backend"), { recursive: true });
  fs.mkdirSync(path.join(stageRoot, "config"), { recursive: true });
  fs.writeFileSync(path.join(stageRoot, "runtime", "python.exe"), "runtime");
  fs.writeFileSync(path.join(stageRoot, "backend", "app.py"), "application");
  fs.writeFileSync(path.join(stageRoot, "config", "codex-hooks.toml"), "[hooks]\nenabled = true\n");
  const files = [
    ["backend/app.py", "application"],
    ["config/codex-hooks.toml", "[hooks]\nenabled = true\n"],
    ["runtime/python.exe", "runtime"],
  ].map(([filePath, body]) => ({
    path: filePath,
    size: Buffer.byteLength(body),
    sha256: crypto.createHash("sha256").update(body).digest("hex"),
  }));
  const manifest = {
    schema_version: "2.0.0",
    build_kind: "windows-cpu-sidecar",
    pack: {
      pack_id: "chriptmas-windows-cpu-base",
      role: "required",
      platform: "win32",
      arch: "x64",
      capabilities: CAPABILITIES,
    },
    generated_at: "2026-07-30T00:00:00.000Z",
    inputs: ["runtime", "src/backend", "config/codex-hooks.toml"],
    files,
    content_set_sha256: contentSetSha256(files),
    total_size: files.reduce((total, file) => total + file.size, 0),
  };
  fs.writeFileSync(
    path.join(stageRoot, "sidecar-manifest.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
  return {
    stageRoot,
    manifest,
    manifestSha256: sha256File(path.join(stageRoot, "sidecar-manifest.json")),
  };
}

test("atomically transfers a staged sidecar and verifies a nonce-bound proof", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-transfer-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fixture = stageFixture(root);
  const appOutDir = path.join(root, "release", "win-unpacked");
  const resources = path.join(appOutDir, "resources");
  const targetRoot = path.join(resources, "sidecar");
  const nonce = "a".repeat(64);
  const proofPath = path.join(root, "release", proofName(nonce));
  fs.mkdirSync(resources, { recursive: true });

  const proof = transferSidecar({
    stageRoot: fixture.stageRoot,
    targetRoot,
    proofPath,
    nonce,
    expectedManifestSha256: fixture.manifestSha256,
    now: () => new Date("2026-07-30T01:02:03.000Z"),
  });

  assert.equal(fs.existsSync(fixture.stageRoot), false);
  assert.equal(fs.existsSync(targetRoot), true);
  assert.equal(proof.method, "same-volume-atomic-directory-rename");
  assert.equal(proof.files, 3);
  assert.equal(proof.created_at, "2026-07-30T01:02:03.000Z");
  const verified = verifyAtomicSidecarTransfer({
    sidecarRoot: targetRoot,
    proofPath,
    expectedNonce: nonce,
    expectedManifestSha256: fixture.manifestSha256,
  });
  assert.equal(verified.verification, "nonce-bound-atomic-transfer");
  assert.equal(verified.content_set_sha256, fixture.manifest.content_set_sha256);
  assert.deepEqual(verifySidecar(targetRoot).verification, "full-content-hash");
  const cachePath = path.join(root, "cache", "sidecar-verification.json");
  refreshSidecarVerificationProof({ sidecarRoot: targetRoot, sidecar: verified, proofPath: cachePath });
  assert.equal(verifySidecarWithProofCache(targetRoot, { proofPath: cachePath }).verification, "cached-full-content-proof");
});

test("sidecar verification proof reuses unchanged content and rejects same-size mutation", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-verify-cache-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fixture = stageFixture(root);
  const targetRoot = path.join(root, "candidate", "resources", "sidecar");
  fs.mkdirSync(path.dirname(targetRoot), { recursive: true });
  fs.renameSync(fixture.stageRoot, targetRoot);
  const proofPath = path.join(root, "cache", "proof.json");

  const first = verifySidecarWithProofCache(targetRoot, { proofPath });
  assert.equal(first.verification, "full-content-hash");
  assert.equal(first.cache, "refreshed");
  const second = verifySidecarWithProofCache(targetRoot, { proofPath });
  assert.equal(second.verification, "cached-full-content-proof");
  assert.equal(second.cache, "hit");

  const changed = path.join(targetRoot, "backend", "app.py");
  const touchedAt = new Date(Date.now() + 5_000);
  fs.utimesSync(changed, touchedAt, touchedAt);
  const touched = verifySidecarWithProofCache(targetRoot, { proofPath });
  assert.equal(touched.verification, "full-content-hash");
  assert.equal(touched.cache, "refreshed");
  assert.equal(touched.cache_miss, "metadata_changed");
  assert.equal(verifySidecarWithProofCache(targetRoot, { proofPath }).cache, "hit");

  fs.writeFileSync(changed, "mutat1on!!!");
  assert.throws(
    () => verifySidecarWithProofCache(targetRoot, { proofPath }),
    /sidecar file hash mismatch: backend\/app.py/,
  );
});

test("incremental replacement commits or rolls back as one bounded transaction", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-replacement-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fixture = stageFixture(root);
  const releaseRoot = path.join(root, "release");
  const targetRoot = path.join(releaseRoot, "win-unpacked", "resources", "sidecar");
  const backupRoot = path.join(releaseRoot, ".sidecar-backup");
  const proofPath = path.join(releaseRoot, proofName("9".repeat(64)));
  fs.mkdirSync(targetRoot, { recursive: true });
  fs.writeFileSync(path.join(targetRoot, "old.txt"), "old-sidecar");

  const first = beginSidecarReplacement({
    stageRoot: fixture.stageRoot,
    targetRoot,
    backupRoot,
    proofPath,
    nonce: "9".repeat(64),
    expectedManifestSha256: fixture.manifestSha256,
  });
  assert.equal(fs.existsSync(path.join(targetRoot, "runtime", "python.exe")), true);
  assert.equal(fs.existsSync(path.join(backupRoot, "old.txt")), true);
  rollbackSidecarReplacement(first);
  assert.equal(fs.readFileSync(path.join(targetRoot, "old.txt"), "utf8"), "old-sidecar");
  assert.equal(fs.existsSync(fixture.stageRoot), true);
  assert.equal(fs.existsSync(backupRoot), false);
  assert.equal(fs.existsSync(proofPath), false);

  const second = beginSidecarReplacement({
    stageRoot: fixture.stageRoot,
    targetRoot,
    backupRoot,
    proofPath,
    nonce: "9".repeat(64),
    expectedManifestSha256: fixture.manifestSha256,
  });
  commitSidecarReplacement(second, { retainBackupAsStage: true });
  assert.equal(fs.existsSync(path.join(targetRoot, "runtime", "python.exe")), true);
  assert.equal(fs.existsSync(backupRoot), false);
  assert.equal(fs.readFileSync(path.join(fixture.stageRoot, "old.txt"), "utf8"), "old-sidecar");
  assert.equal(fs.existsSync(proofPath), true);
});

test("failed incremental transfer restores the existing target", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-replacement-fail-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fixture = stageFixture(root);
  const releaseRoot = path.join(root, "release");
  const targetRoot = path.join(releaseRoot, "win-unpacked", "resources", "sidecar");
  const backupRoot = path.join(releaseRoot, ".sidecar-backup");
  const proofPath = path.join(releaseRoot, proofName("8".repeat(64)));
  fs.mkdirSync(targetRoot, { recursive: true });
  fs.writeFileSync(path.join(targetRoot, "old.txt"), "old-sidecar");
  fs.writeFileSync(proofPath, "stale-proof");

  assert.throws(
    () => beginSidecarReplacement({
      stageRoot: fixture.stageRoot,
      targetRoot,
      backupRoot,
      proofPath,
      nonce: "8".repeat(64),
      expectedManifestSha256: fixture.manifestSha256,
    }),
    /proof already exists/,
  );
  assert.equal(fs.readFileSync(path.join(targetRoot, "old.txt"), "utf8"), "old-sidecar");
  assert.equal(fs.existsSync(backupRoot), false);
  assert.equal(fs.existsSync(fixture.stageRoot), true);
});

test("atomic proof rejects another invocation and packaged metadata drift", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-proof-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fixture = stageFixture(root);
  const appOutDir = path.join(root, "release", "win-unpacked");
  const targetRoot = path.join(appOutDir, "resources", "sidecar");
  const proofPath = path.join(root, "release", proofName("b".repeat(64)));
  fs.mkdirSync(path.dirname(targetRoot), { recursive: true });
  transferSidecar({
    stageRoot: fixture.stageRoot,
    targetRoot,
    proofPath,
    nonce: "b".repeat(64),
    expectedManifestSha256: fixture.manifestSha256,
  });

  assert.throws(
    () => verifyAtomicSidecarTransfer({
      sidecarRoot: targetRoot,
      proofPath,
      expectedNonce: "c".repeat(64),
      expectedManifestSha256: fixture.manifestSha256,
    }),
    /does not match this build invocation/,
  );
  fs.writeFileSync(path.join(targetRoot, "unexpected.txt"), "drift");
  assert.throws(
    () => verifyAtomicSidecarTransfer({
      sidecarRoot: targetRoot,
      proofPath,
      expectedNonce: "b".repeat(64),
      expectedManifestSha256: fixture.manifestSha256,
    }),
    /file set does not match manifest/,
  );
});

test("transfer fails closed for stale targets proofs manifest drift and cross-volume metadata", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-transfer-boundary-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fixture = stageFixture(root);
  const appOutDir = path.join(root, "release", "win-unpacked");
  const targetRoot = path.join(appOutDir, "resources", "sidecar");
  const proofPath = path.join(root, "release", proofName("d".repeat(64)));
  fs.mkdirSync(path.dirname(targetRoot), { recursive: true });
  fs.mkdirSync(targetRoot);
  assert.throws(
    () => transferSidecar({
      stageRoot: fixture.stageRoot,
      targetRoot,
      proofPath,
      nonce: "d".repeat(64),
      expectedManifestSha256: fixture.manifestSha256,
    }),
    /target already exists/,
  );
  fs.rmSync(targetRoot, { recursive: true, force: true });
  fs.writeFileSync(proofPath, "{}");
  assert.throws(
    () => transferSidecar({
      stageRoot: fixture.stageRoot,
      targetRoot,
      proofPath,
      nonce: "d".repeat(64),
      expectedManifestSha256: fixture.manifestSha256,
    }),
    /proof already exists/,
  );
  fs.rmSync(proofPath);
  assert.throws(
    () => transferSidecar({
      stageRoot: fixture.stageRoot,
      targetRoot,
      proofPath,
      nonce: "d".repeat(64),
      expectedManifestSha256: "e".repeat(64),
    }),
    /manifest changed after staging/,
  );

  const actualTargetParent = path.dirname(targetRoot);
  assert.throws(
    () => transferSidecar({
      stageRoot: fixture.stageRoot,
      targetRoot,
      proofPath,
      nonce: "d".repeat(64),
      expectedManifestSha256: fixture.manifestSha256,
      io: {
        ...fs,
        lstatSync(filePath, options) {
          const stat = fs.lstatSync(filePath, options);
          if (filePath !== actualTargetParent || !stat) return stat;
          return {
            dev: stat.dev + 1,
            isDirectory: () => true,
            isSymbolicLink: () => false,
          };
        },
      },
    }),
    /same volume/,
  );
});

test("build wrapper creates or preserves a strict transfer environment", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-build-env-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const manifestPath = path.join(root, "sidecar-manifest.json");
  fs.writeFileSync(manifestPath, "manifest");
  const generated = sidecarTransferEnvironment(
    { KEEP: "value" },
    manifestPath,
    () => Buffer.alloc(32, 0xab),
  );
  assert.equal(generated.KEEP, "value");
  assert.equal(generated[NONCE_ENV], "ab".repeat(32));
  assert.equal(generated[MANIFEST_SHA_ENV], sha256File(manifestPath));

  const preserved = sidecarTransferEnvironment({
    [NONCE_ENV]: "1".repeat(64),
    [MANIFEST_SHA_ENV]: "2".repeat(64),
  });
  assert.equal(preserved[NONCE_ENV], "1".repeat(64));
  assert.throws(
    () => sidecarTransferEnvironment({ [NONCE_ENV]: "1".repeat(64) }),
    /must be supplied together/,
  );
});
