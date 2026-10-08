const test = require("node:test");
const assert = require("node:assert/strict");

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  expectedInstallerName,
  parseArgs,
  verifyFrontendAssetPaths,
  verifyNoExternalFrontendResourceImports,
  verifyCandidateIdentity,
  verifyPackagedManual,
  verifyReleaseArtifacts,
} = require("../scripts/verify-build.cjs");

test("accepts Vite relative asset URLs for packaged file pages", () => {
  assert.equal(
    verifyFrontendAssetPaths('<script type="module" src="./assets/index.js"></script><link href="./assets/index.css" rel="stylesheet">'),
    2,
  );
});

test("rejects Vite absolute asset URLs for packaged file pages", () => {
  assert.throws(
    () => verifyFrontendAssetPaths('<script type="module" src="/assets/index.js"></script>'),
    /file-protocol-incompatible absolute assets/,
  );
});

test("rejects external HTML and CSS resource imports in the packaged frontend", () => {
  const frontendDist = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-verify-build-"));
  try {
    fs.writeFileSync(path.join(frontendDist, "index.html"), '<link href="./assets/index.css" rel="stylesheet">');
    fs.mkdirSync(path.join(frontendDist, "assets"));
    fs.writeFileSync(path.join(frontendDist, "assets", "index.css"), '@import url("https://fonts.googleapis.com/css2?family=Example");');
    assert.throws(() => verifyNoExternalFrontendResourceImports(frontendDist), /unapproved external HTML\/CSS resource imports/);
  } finally {
    fs.rmSync(frontendDist, { recursive: true, force: true });
  }
});

test("verify-build arguments default to directory mode and reject invalid provenance options", () => {
  assert.deepEqual(parseArgs([]), {
    mode: "dir",
    installerNotOlderThanMs: null,
    sidecarTransferNonce: null,
    sidecarManifestSha256: null,
    runPackagedSmoke: true,
    candidateDir: null,
    candidateId: null,
  });
  assert.deepEqual(
    parseArgs(["--mode", "installer", "--installer-not-older-than-ms", "123"]),
    {
      mode: "installer",
      installerNotOlderThanMs: 123,
      sidecarTransferNonce: null,
      sidecarManifestSha256: null,
      runPackagedSmoke: true,
      candidateDir: null,
      candidateId: null,
    },
  );
  assert.deepEqual(
    parseArgs([
      "--sidecar-transfer-nonce",
      "a".repeat(64),
      "--sidecar-manifest-sha256",
      "b".repeat(64),
    ]),
    {
      mode: "dir",
      installerNotOlderThanMs: null,
      sidecarTransferNonce: "a".repeat(64),
      sidecarManifestSha256: "b".repeat(64),
      runPackagedSmoke: true,
      candidateDir: null,
      candidateId: null,
    },
  );
  assert.equal(parseArgs(["--skip-packaged-smoke"]).runPackagedSmoke, false);
  assert.throws(
    () => parseArgs(["--mode", "installer", "--skip-packaged-smoke"]),
    /cannot skip the packaged startup smoke/,
  );
  assert.throws(() => parseArgs(["--mode", "archive"]), /must be dir or installer/);
  assert.throws(
    () => parseArgs(["--installer-not-older-than-ms", "123"]),
    /only valid in installer mode/,
  );
  assert.throws(
    () => parseArgs(["--mode", "installer", "--installer-not-older-than-ms", "NaN"]),
    /non-negative finite number/,
  );
  assert.throws(
    () => parseArgs(["--sidecar-transfer-nonce", "a".repeat(64)]),
    /must be supplied together/,
  );
  assert.throws(
    () => parseArgs([
      "--sidecar-transfer-nonce",
      "not-a-nonce",
      "--sidecar-manifest-sha256",
      "b".repeat(64),
    ]),
    /nonce must be a 64-character/,
  );
});

test("isolated candidate verification binds the requested directory and manifest identity", (t) => {
  const release = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-isolated-candidate-"));
  t.after(() => fs.rmSync(release, { recursive: true, force: true }));
  const candidateId = "windows-20260905T010203Z-0123456789ab";
  const candidateDir = path.join(release, "candidates", candidateId);
  const unpacked = path.join(candidateDir, "win-unpacked");
  fs.mkdirSync(path.join(unpacked, "resources"), { recursive: true });
  fs.writeFileSync(path.join(unpacked, "resources", "candidate-identity.json"), JSON.stringify({
    schema_version: 1,
    candidate_id: candidateId,
    build_id: candidateId,
    source_commit: "a".repeat(40),
    payload_revision: `source:${"a".repeat(40)}`,
    package_version: "0.2.0",
    created_at: "2026-09-05T01:02:03.000Z",
  }));
  assert.equal(verifyCandidateIdentity(unpacked, candidateId).source_commit, "a".repeat(40));
  assert.throws(() => verifyCandidateIdentity(unpacked, "windows-20260905T010204Z-0123456789ab"), /does not match/);
});

test("expectedInstallerName resolves only a safe versioned executable name", () => {
  assert.equal(
    expectedInstallerName({ version: "0.2.0", build: { win: { artifactName: "ChriptmasOS-${version}-setup.exe" } } }),
    "ChriptmasOS-0.2.0-setup.exe",
  );
  assert.throws(
    () => expectedInstallerName({ version: "0.2.0", build: { win: { artifactName: "../setup.exe" } } }),
    /unsupported installer artifactName/,
  );
  assert.throws(
    () => expectedInstallerName({ version: "0.2.0", build: { win: { artifactName: "${channel}-setup.exe" } } }),
    /unsupported installer artifactName/,
  );
});

test("directory verification requires win-unpacked and ignores historical setup executables", () => {
  const releaseDir = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-release-dir-"));
  try {
    const packageJson = { version: "0.2.0", build: { win: { artifactName: "ChriptmasOS-${version}-setup.exe" } } };
    fs.writeFileSync(path.join(releaseDir, "ChriptmasOS-0.1.0-setup.exe"), "historical", "utf8");
    assert.throws(
      () => verifyReleaseArtifacts({ releaseDir, packageJson, mode: "dir" }),
      /缺少当前Windows解压候选/,
    );
    fs.mkdirSync(path.join(releaseDir, "win-unpacked"));
    const result = verifyReleaseArtifacts({ releaseDir, packageJson, mode: "dir" });
    assert.equal(result.installer, null);
  } finally {
    fs.rmSync(releaseDir, { recursive: true, force: true });
  }
});

test("installer verification requires exact current artifact and current invocation timestamp", () => {
  const releaseDir = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-release-installer-"));
  try {
    const packageJson = { version: "0.2.0", build: { win: { artifactName: "ChriptmasOS-${version}-setup.exe" } } };
    fs.mkdirSync(path.join(releaseDir, "win-unpacked"));
    fs.writeFileSync(path.join(releaseDir, "ChriptmasOS-0.1.0-setup.exe"), "wrong-version", "utf8");
    assert.throws(
      () => verifyReleaseArtifacts({ releaseDir, packageJson, mode: "installer" }),
      /缺少当前版本安装器/,
    );

    const installer = path.join(releaseDir, "ChriptmasOS-0.2.0-setup.exe");
    fs.writeFileSync(installer, "current-version", "utf8");
    const modifiedMs = fs.statSync(installer).mtimeMs;
    assert.throws(
      () => verifyReleaseArtifacts({
        releaseDir,
        packageJson,
        mode: "installer",
        installerNotOlderThanMs: modifiedMs + 10_000,
      }),
      /安装器不是本轮构建产物/,
    );
    const result = verifyReleaseArtifacts({
      releaseDir,
      packageJson,
      mode: "installer",
      installerNotOlderThanMs: modifiedMs - 1,
    });
    assert.equal(result.installer, installer);
  } finally {
    fs.rmSync(releaseDir, { recursive: true, force: true });
  }
});

test("packaged manual must exactly match the current repository README", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-packaged-manual-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, "README.md");
  const packaged = path.join(root, "resources", "manual", "readme.md");
  fs.mkdirSync(path.dirname(packaged), { recursive: true });
  fs.writeFileSync(source, "# Current manual\n", "utf8");
  fs.writeFileSync(packaged, "# Stale manual\n", "utf8");

  assert.throws(
    () => verifyPackagedManual(source, packaged),
    /does not match the current repository README/,
  );

  fs.copyFileSync(source, packaged);
  assert.deepEqual(verifyPackagedManual(source, packaged), {
    bytes: 17,
    sha256: "232ce82d9249bbc27b0aa1e21e3b97cca0c826735ed4bc582d525008fde23436",
  });
});

test("packaged manual verification rejects missing, symlinked and oversized files", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-packaged-manual-boundary-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, "README.md");
  const packaged = path.join(root, "resources", "manual", "readme.md");
  fs.writeFileSync(source, "safe", "utf8");

  assert.throws(() => verifyPackagedManual(source, packaged), /packaged manual must be a regular/);
  fs.mkdirSync(path.dirname(packaged), { recursive: true });
  fs.writeFileSync(packaged, "safe", "utf8");
  assert.throws(
    () => verifyPackagedManual(source, packaged, {
      ...fs,
      lstatSync(filePath, options) {
        if (filePath === packaged) {
          return {
            size: 4,
            isFile: () => true,
            isSymbolicLink: () => true,
          };
        }
        return fs.lstatSync(filePath, options);
      },
    }),
    /packaged manual must be a regular/,
  );
  fs.writeFileSync(packaged, Buffer.alloc(2 * 1024 * 1024 + 1));
  assert.throws(() => verifyPackagedManual(source, packaged), /packaged manual exceeds the 2 MiB limit/);
});
