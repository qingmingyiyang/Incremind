const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const MAX_EXPORT_BYTES = 64 * 1024 * 1024;
const EXPORT_PRESETS = Object.freeze({
  full_asset_package: Object.freeze({ extension: ".zip", format: "zip", label: "ZIP 资产包" }),
  generic_markdown_knowledge_base: Object.freeze({ extension: ".md", format: "markdown", label: "Markdown" }),
  compact_persona_prompt: Object.freeze({ extension: ".md", format: "prompt_text", label: "Markdown" }),
  full_memory_brief: Object.freeze({ extension: ".md", format: "markdown", label: "Markdown" }),
  generic_llm_project_knowledge: Object.freeze({ extension: ".md", format: "markdown", label: "Markdown" }),
  generic_custom_instructions: Object.freeze({ extension: ".md", format: "markdown", label: "Markdown" }),
  generic_rag_corpus: Object.freeze({ extension: ".ndjson", format: "ndjson", label: "NDJSON" }),
});

function validateMemoryExportRequest(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.keys(value).some((key) => !["preset", "scope"].includes(key))) {
    throw new Error("memory_export_request_invalid");
  }
  const preset = typeof value.preset === "string" ? value.preset : "";
  const config = EXPORT_PRESETS[preset];
  if (!config) throw new Error("memory_export_preset_invalid");
  const inputScope = value.scope == null ? {} : value.scope;
  if (!inputScope || typeof inputScope !== "object" || Array.isArray(inputScope)) {
    throw new Error("memory_export_scope_invalid");
  }
  const allowedScope = new Set([
    "redact_secrets", "only_confirmed", "skip_raw_sources", "skip_av",
    "skip_evidence_text", "skip_low_trust", "skip_conflicts",
    "skip_provider_audit", "include_paths",
  ]);
  if (Object.keys(inputScope).some((key) => !allowedScope.has(key))
    || Object.values(inputScope).some((item) => typeof item !== "boolean")) {
    throw new Error("memory_export_scope_invalid");
  }
  return Object.freeze({
    preset,
    scope: Object.freeze({ ...inputScope, redact_secrets: true }),
    config,
  });
}

function writeMemoryPresetExport({
  targetPath,
  bytes,
  preset,
  responseFormat,
  fileSystem = fs,
  randomId = () => crypto.randomUUID(),
}) {
  const config = EXPORT_PRESETS[preset];
  if (!config || responseFormat !== config.format
    || typeof targetPath !== "string" || !path.isAbsolute(targetPath)
    || path.extname(targetPath).toLowerCase() !== config.extension) {
    throw new Error("memory_export_target_invalid");
  }
  const payload = Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes || []);
  if (payload.byteLength > MAX_EXPORT_BYTES) throw new Error("memory_export_too_large");
  const parent = path.dirname(targetPath);
  const parentStat = fileSystem.lstatSync(parent);
  if (!parentStat.isDirectory() || parentStat.isSymbolicLink()) {
    throw new Error("memory_export_parent_invalid");
  }
  if (fileSystem.existsSync(targetPath)) {
    const targetStat = fileSystem.lstatSync(targetPath);
    if (!targetStat.isFile() || targetStat.isSymbolicLink()) {
      throw new Error("memory_export_existing_target_invalid");
    }
  }
  const temporary = path.join(parent, `.${path.basename(targetPath)}.${randomId()}.tmp`);
  let descriptor = null;
  try {
    descriptor = fileSystem.openSync(temporary, "wx", 0o600);
    fileSystem.writeFileSync(descriptor, payload);
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
    preset,
    format: responseFormat,
    size_bytes: payload.byteLength,
    sha256: crypto.createHash("sha256").update(payload).digest("hex"),
  });
}

module.exports = {
  EXPORT_PRESETS,
  MAX_EXPORT_BYTES,
  validateMemoryExportRequest,
  writeMemoryPresetExport,
};
