const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  MAX_PACKAGE_BYTES,
  writeMemoryAssetPackage,
} = require("../src/memory-asset-export.cjs");

function packagePayload(overrides = {}) {
  return {
    package_id: "pkg-0123456789ab",
    created_at: "2026-07-24T00:00:00+00:00",
    files: [{
      logical_path: "manifest.json",
      description: "资产包总览 🐻",
      size_bytes: 12,
      content: { title: "中文记忆", secret: "[redacted]" },
    }],
    total_size_display: "12 B",
    rebuild_index_hint: "重建索引",
    ...overrides,
  };
}

test("writes a reopenable Unicode package with matching integrity", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-memory-export-"));
  try {
    const target = path.join(root, "记忆资产.json");
    const payload = packagePayload();
    const result = writeMemoryAssetPackage({
      targetPath: target,
      packagePayload: payload,
      randomId: () => "fixed",
    });
    const envelope = JSON.parse(fs.readFileSync(target, "utf8"));
    const expected = crypto.createHash("sha256")
      .update(JSON.stringify(payload), "utf8")
      .digest("hex");
    assert.equal(envelope.schema_version, "1.0.0");
    assert.equal(envelope.package_kind, "chriptmas_memory_asset_package");
    assert.equal(envelope.integrity.package_sha256, expected);
    assert.equal(envelope.package.files[0].content.title, "中文记忆");
    assert.equal(result.status, "saved");
    assert.equal(result.package_sha256, expected);
    assert.equal("targetPath" in result, false);
    assert.equal(fs.existsSync(path.join(root, ".记忆资产.json.fixed.tmp")), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("rejects invalid packages targets and oversized content without a file", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-memory-export-"));
  try {
    const target = path.join(root, "asset.json");
    assert.throws(
      () => writeMemoryAssetPackage({
        targetPath: "relative.json",
        packagePayload: packagePayload(),
      }),
      /target_invalid/,
    );
    assert.throws(
      () => writeMemoryAssetPackage({
        targetPath: target,
        packagePayload: packagePayload({ package_id: "bad" }),
      }),
      /payload_invalid/,
    );
    assert.throws(
      () => writeMemoryAssetPackage({
        targetPath: target,
        packagePayload: packagePayload({
          files: [{
            logical_path: "manifest.json",
            description: "large",
            size_bytes: MAX_PACKAGE_BYTES,
            content: { text: "x".repeat(MAX_PACKAGE_BYTES) },
          }],
        }),
      }),
      /too_large/,
    );
    assert.equal(fs.existsSync(target), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("cleans the partial file when atomic rename fails", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-memory-export-"));
  try {
    const target = path.join(root, "asset.json");
    const failing = {
      ...fs,
      renameSync() {
        throw new Error("rename failed");
      },
    };
    assert.throws(
      () => writeMemoryAssetPackage({
        targetPath: target,
        packagePayload: packagePayload(),
        fileSystem: failing,
        randomId: () => "failed",
      }),
      /rename failed/,
    );
    assert.equal(fs.existsSync(target), false);
    assert.equal(fs.existsSync(path.join(root, ".asset.json.failed.tmp")), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
