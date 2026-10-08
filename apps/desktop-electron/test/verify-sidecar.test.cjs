const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { REQUIRED_BASE_FILES, contentSetSha256, verifySidecar } = require("../scripts/verify-sidecar.cjs");

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-integrity-"));
  fs.mkdirSync(path.join(root, "runtime"));
  fs.mkdirSync(path.join(root, "config"));
  fs.writeFileSync(path.join(root, "runtime", "python.exe"), "python-fixture");
  fs.writeFileSync(path.join(root, "config", "codex-hooks.toml"), "[hooks]\nenabled = true\n");
  fs.writeFileSync(path.join(root, "requirements.txt"), "fastapi==fixture\n");
  const files = ["config/codex-hooks.toml", "requirements.txt", "runtime/python.exe"].map((relative) => {
    const payload = fs.readFileSync(path.join(root, ...relative.split("/")));
    return {
      path: relative,
      size: payload.length,
      sha256: crypto.createHash("sha256").update(payload).digest("hex"),
    };
  });
  const manifest = {
    schema_version: "2.0.0",
    build_kind: "windows-cpu-sidecar",
    pack: {
      pack_id: "chriptmas-windows-cpu-base",
      role: "required",
      platform: "win32",
      arch: "x64",
      capabilities: [
        "authenticated_sidecar",
        "local_intake",
        "sqlite_authority",
        "document_memory_project_skill",
        "file_grant_streaming",
        "ffmpeg_media_probe",
        "provider_policy",
      ],
    },
    files,
    total_size: files.reduce((total, file) => total + file.size, 0),
    content_set_sha256: contentSetSha256(files),
  };
  fs.writeFileSync(path.join(root, "sidecar-manifest.json"), JSON.stringify(manifest));
  return root;
}

function withFixture(run) {
  const root = fixture();
  try {
    run(root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test("verifies every file and the complete sidecar set", () => {
  withFixture((root) => {
    const result = verifySidecar(root);
    assert.equal(result.files, 3);
    assert.match(result.content_set_sha256, /^[a-f0-9]{64}$/);
  });
});

test("requires the packaged Codex Hook baseline", () => {
  assert.ok(REQUIRED_BASE_FILES.has("config/codex-hooks.toml"));
  withFixture((root) => {
    const manifestPath = path.join(root, "sidecar-manifest.json");
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    manifest.files = manifest.files.filter((file) => file.path !== "config/codex-hooks.toml");
    manifest.total_size = manifest.files.reduce((total, file) => total + file.size, 0);
    manifest.content_set_sha256 = contentSetSha256(manifest.files);
    fs.writeFileSync(manifestPath, JSON.stringify(manifest));
    assert.throws(() => verifySidecar(root), /missing required file: config\/codex-hooks\.toml/);
  });
});

test("rejects a changed non-critical file", () => {
  withFixture((root) => {
    fs.writeFileSync(path.join(root, "requirements.txt"), "changed");
    assert.throws(() => verifySidecar(root), /file hash mismatch/);
  });
});

test("rejects missing and unexpected files", () => {
  withFixture((root) => {
    fs.unlinkSync(path.join(root, "requirements.txt"));
    assert.throws(() => verifySidecar(root), /missing sidecar file/);
  });
  withFixture((root) => {
    fs.writeFileSync(path.join(root, "unexpected.txt"), "unexpected");
    assert.throws(() => verifySidecar(root), /file set does not match manifest/);
  });
});

test("rejects traversal and duplicate manifest identities", () => {
  withFixture((root) => {
    const manifestPath = path.join(root, "sidecar-manifest.json");
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    manifest.files[0].path = "../outside";
    fs.writeFileSync(manifestPath, JSON.stringify(manifest));
    assert.throws(() => verifySidecar(root), /invalid or duplicate path/);
  });
  withFixture((root) => {
    const manifestPath = path.join(root, "sidecar-manifest.json");
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    manifest.files[1].path = manifest.files[0].path;
    fs.writeFileSync(manifestPath, JSON.stringify(manifest));
    assert.throws(() => verifySidecar(root), /invalid or duplicate path/);
  });
});

test("rejects Windows absolute and alternate-stream paths", () => {
  for (const invalidPath of ["C:/outside", "runtime/python.exe:payload"]) {
    withFixture((root) => {
      const manifestPath = path.join(root, "sidecar-manifest.json");
      const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
      manifest.files[0].path = invalidPath;
      fs.writeFileSync(manifestPath, JSON.stringify(manifest));
      assert.throws(() => verifySidecar(root), /invalid or duplicate path/);
    });
  }
});

test("rejects missing or ambiguous base-pack capabilities", () => {
  withFixture((root) => {
    const manifestPath = path.join(root, "sidecar-manifest.json");
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    manifest.pack.capabilities = manifest.pack.capabilities.filter((item) => item !== "ffmpeg_media_probe");
    fs.writeFileSync(manifestPath, JSON.stringify(manifest));
    assert.throws(() => verifySidecar(root), /missing capability: ffmpeg_media_probe/);
  });
  withFixture((root) => {
    const manifestPath = path.join(root, "sidecar-manifest.json");
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    manifest.pack.capabilities.push(manifest.pack.capabilities[0]);
    fs.writeFileSync(manifestPath, JSON.stringify(manifest));
    assert.throws(() => verifySidecar(root), /invalid pack capabilities/);
  });
});
