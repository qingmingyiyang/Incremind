const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  MAX_EXPORT_BYTES,
  validateMemoryExportRequest,
  writeMemoryPresetExport,
} = require("../src/memory-preset-export.cjs");

test("validates a closed preset and boolean-only scope", () => {
  const result = validateMemoryExportRequest({
    preset: "generic_markdown_knowledge_base",
    scope: { redact_secrets: true, only_confirmed: false },
  });
  assert.equal(result.config.extension, ".md");
  assert.equal(validateMemoryExportRequest({
    preset: "generic_markdown_knowledge_base",
    scope: { redact_secrets: false },
  }).scope.redact_secrets, true);
  assert.throws(() => validateMemoryExportRequest({ preset: "unknown", scope: {} }), /preset_invalid/);
  assert.throws(() => validateMemoryExportRequest({
    preset: "full_asset_package",
    scope: { redact_secrets: "yes" },
  }), /scope_invalid/);
  assert.throws(() => validateMemoryExportRequest({
    preset: "full_asset_package",
    scope: { unexpected: true },
  }), /scope_invalid/);
});

test("writes real Unicode preset bytes atomically and returns a bounded receipt", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-preset-export-"));
  try {
    const target = path.join(root, "知识库.md");
    const bytes = Buffer.from("# 真实知识库\n\n中文内容 🐻\n", "utf8");
    const receipt = writeMemoryPresetExport({
      targetPath: target,
      bytes,
      preset: "generic_markdown_knowledge_base",
      responseFormat: "markdown",
      randomId: () => "fixed",
    });
    assert.deepEqual(fs.readFileSync(target), bytes);
    assert.equal(receipt.status, "saved");
    assert.equal(receipt.sha256, crypto.createHash("sha256").update(bytes).digest("hex"));
    assert.equal("targetPath" in receipt, false);
    assert.equal(fs.existsSync(path.join(root, ".知识库.md.fixed.tmp")), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("rejects extension format oversized and linked targets without partial output", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-preset-export-"));
  try {
    assert.throws(() => writeMemoryPresetExport({
      targetPath: path.join(root, "wrong.zip"),
      bytes: Buffer.from("x"),
      preset: "generic_markdown_knowledge_base",
      responseFormat: "markdown",
    }), /target_invalid/);
    assert.throws(() => writeMemoryPresetExport({
      targetPath: path.join(root, "wrong.md"),
      bytes: Buffer.from("x"),
      preset: "generic_markdown_knowledge_base",
      responseFormat: "zip",
    }), /target_invalid/);
    assert.throws(() => writeMemoryPresetExport({
      targetPath: path.join(root, "large.md"),
      bytes: Buffer.alloc(MAX_EXPORT_BYTES + 1),
      preset: "generic_markdown_knowledge_base",
      responseFormat: "markdown",
    }), /too_large/);
    const real = path.join(root, "real.md");
    fs.writeFileSync(real, "existing");
    const link = path.join(root, "linked.md");
    try {
      fs.symlinkSync(real, link, "file");
      assert.throws(() => writeMemoryPresetExport({
        targetPath: link,
        bytes: Buffer.from("x"),
        preset: "generic_markdown_knowledge_base",
        responseFormat: "markdown",
      }), /existing_target_invalid/);
    } catch (error) {
      if (error?.code !== "EPERM") throw error;
    }
    assert.equal(fs.existsSync(path.join(root, "large.md")), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("cleans the temporary file when atomic replacement fails", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-preset-export-"));
  try {
    const target = path.join(root, "export.ndjson");
    const failing = {
      ...fs,
      renameSync() { throw new Error("rename failed"); },
    };
    assert.throws(() => writeMemoryPresetExport({
      targetPath: target,
      bytes: Buffer.from('{"id":"one"}\n'),
      preset: "generic_rag_corpus",
      responseFormat: "ndjson",
      fileSystem: failing,
      randomId: () => "failed",
    }), /rename failed/);
    assert.equal(fs.existsSync(target), false);
    assert.equal(fs.existsSync(path.join(root, ".export.ndjson.failed.tmp")), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
