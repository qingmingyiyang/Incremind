const fs = require("node:fs");
const { randomUUID } = require("node:crypto");
const path = require("node:path");

const DATA_MARKERS = Object.freeze([".rebuild-data", "library", "data"]);
const DATA_ROOT_POINTER_FILE = "data-root.json";
const DATA_ROOT_PREVIOUS_POINTER_FILE = "data-root.previous.json";
const ROOT_MIGRATION_JOURNAL_FILE = "root-migration-journal.json";
const ROOT_MIGRATION_PREVIOUS_JOURNAL_FILE = "root-migration-journal.previous.json";
const ROOT_CONFIG_VERSION = 1;
const ROOT_ROLES = Object.freeze(["vault", "model", "media"]);
const ROOT_MIGRATION_RECEIPT_FILE = ".chriptmas-root-migration-receipt.json";

function resolvePackagedVaultRoot(options) {
  const { userDataDir, exists = fs.existsSync, readFile = (candidate) => fs.readFileSync(candidate, "utf8") } = options;
  const resolved = resolveRootConfig({ userDataDir, exists, readFile });
  if (resolved.mode === "conflict") return { mode: "conflict", workingDir: null };
  return resolved.pointerPath
    ? { mode: resolved.mode, workingDir: resolved.config.vaultRoot, pointerPath: resolved.pointerPath }
    : { mode: resolved.mode, workingDir: resolved.config.vaultRoot };
}

function resolveRootConfig({ userDataDir, exists = fs.existsSync, readFile = (candidate) => fs.readFileSync(candidate, "utf8") }) {
  const userDataRoot = resolveUserDataRoot(userDataDir);
  const pointerPath = path.join(userDataRoot, DATA_ROOT_POINTER_FILE);
  if (exists(pointerPath)) return { mode: "configured", config: parseRootConfig(readPointer(pointerPath, readFile)), pointerPath };
  const vaultRoot = path.join(userDataRoot, "vault");
  const legacyHasData = hasDataMarkers(userDataRoot, exists);
  const vaultHasData = hasDataMarkers(vaultRoot, exists);
  if (legacyHasData && vaultHasData) return { mode: "conflict", config: null, pointerPath: null };
  const effectiveVault = legacyHasData ? userDataRoot : vaultRoot;
  return { mode: legacyHasData ? "legacy" : (vaultHasData ? "vault" : "fresh_vault"), config: defaultRootConfig(effectiveVault, "implicit"), pointerPath: null };
}

function parseRootConfig(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("vault_root_pointer_invalid");
  if (payload.version !== undefined && payload.version !== ROOT_CONFIG_VERSION) throw new Error("vault_root_pointer_version_unsupported");
  const vaultRoot = absolutePath(payload.root);
  if (!vaultRoot) throw new Error("vault_root_pointer_invalid");
  const roots = payload.roots;
  if (roots !== undefined && (!roots || typeof roots !== "object" || Array.isArray(roots))) throw new Error("vault_root_pointer_invalid");
  const modelRoot = roots?.model === undefined ? vaultRoot : absolutePath(roots.model);
  const mediaRoot = roots?.media === undefined ? vaultRoot : absolutePath(roots.media);
  if (!modelRoot || !mediaRoot) throw new Error("vault_root_pointer_invalid");
  return Object.freeze({ version: payload.version === undefined ? 0 : ROOT_CONFIG_VERSION, vaultRoot, modelRoot, mediaRoot, source: payload.version === undefined ? "legacy_pointer" : "versioned_pointer" });
}

function defaultRootConfig(vaultRoot, source) {
  const resolvedVault = path.resolve(vaultRoot);
  return Object.freeze({ version: ROOT_CONFIG_VERSION, vaultRoot: resolvedVault, modelRoot: resolvedVault, mediaRoot: resolvedVault, source });
}

function rootConfigPayload(config) {
  const normalized = normalizeRootConfig(config);
  return { version: ROOT_CONFIG_VERSION, root: normalized.vaultRoot, roots: { model: normalized.modelRoot, media: normalized.mediaRoot } };
}

function normalizeRootConfig(config) {
  if (!config || typeof config !== "object") throw new TypeError("root_config_invalid");
  const vaultRoot = absolutePath(config.vaultRoot ?? config.root);
  const modelRoot = absolutePath(config.modelRoot ?? config.roots?.model ?? vaultRoot);
  const mediaRoot = absolutePath(config.mediaRoot ?? config.roots?.media ?? vaultRoot);
  if (!vaultRoot || !modelRoot || !mediaRoot) throw new TypeError("root_config_invalid");
  return Object.freeze({ version: ROOT_CONFIG_VERSION, vaultRoot, modelRoot, mediaRoot, source: "migration_target" });
}

function diagnoseRootConfig({ userDataDir, exists = fs.existsSync, readFile, probePath = (candidate) => ({ exists: exists(candidate) }) }) {
  const resolved = resolveRootConfig({ userDataDir, exists, readFile });
  const configured = resolved.config && rootConfigPayload(resolved.config);
  const actual = resolved.config && Object.fromEntries(ROOT_ROLES.map((role) => {
    const configuredPath = rolePath(resolved.config, role);
    return [role, Object.freeze({ path: configuredPath, ...probePath(configuredPath, role) })];
  }));
  return Object.freeze({ mode: resolved.mode, pointerPath: resolved.pointerPath, configured: configured && Object.freeze(configured), actual: actual && Object.freeze(actual) });
}

class RootConfigMigrationController {
  constructor({ userDataDir, exists = fs.existsSync, readFile = (candidate) => fs.readFileSync(candidate, "utf8"), writeFile = (candidate, value) => fs.writeFileSync(candidate, value, "utf8"), mkdir = (candidate) => fs.mkdirSync(candidate, { recursive: true }), rename = fs.renameSync, removeFile = fs.unlinkSync, flushFile = defaultFlushFile, flushDirectory = defaultFlushDirectory, copyTree = defaultCopyTree, targetAvailable = defaultTargetAvailable, targetWritable = defaultTargetWritable, availableBytes = defaultAvailableBytes, estimateBytes = estimateTreeBytes, assertSourceQuiescent: assertSourceQuiescentFn = assertNoUnmanagedSqlite, validateRootPath = assertNoReparsePoint, snapshotTree: snapshotTreeFn = snapshotTree, verifyCopiedTree: verifyCopiedTreeFn = verifyCopiedTree }) {
    this.userDataRoot = resolveUserDataRoot(userDataDir);
    this.exists = exists; this.readFile = readFile; this.writeFile = writeFile; this.mkdir = mkdir; this.rename = rename; this.removeFile = removeFile; this.flushFile = flushFile; this.flushDirectory = flushDirectory; this.copyTree = copyTree;
    this.targetAvailable = targetAvailable; this.targetWritable = targetWritable; this.availableBytes = availableBytes; this.estimateBytes = estimateBytes; this.assertSourceQuiescent = assertSourceQuiescentFn; this.validateRootPath = validateRootPath; this.snapshotTree = snapshotTreeFn; this.verifyCopiedTree = verifyCopiedTreeFn;
    this.hasCustomQuiescence = assertSourceQuiescentFn !== assertNoUnmanagedSqlite; this.hasCustomCopyVerifier = verifyCopiedTreeFn !== verifyCopiedTree;
    this.writeSequence = 0;
  }

  plan({ target }) {
    const source = resolveRootConfig({ userDataDir: this.userDataRoot, exists: this.exists, readFile: this.readFile });
    if (source.mode === "conflict") throw new Error("root_migration_source_conflict");
    return this.#newJournal(source.config, normalizeRootConfig(target), source.pointerPath ? parsePointerPayload(source.pointerPath, this.readFile) : null);
  }

  preflight({ target }) {
    const journal = this.plan({ target });
    if (journal.status === "noop") return migrationPlanSummary(journal);
    this.#preflight(journal);
    return migrationPlanSummary(journal);
  }

  execute({ target }) { return this.migrate({ target }); }

  migrate({ target }) {
    this.#assertNoUnfinishedJournal();
    const journal = this.plan({ target });
    if (journal.status === "noop") return Object.freeze({ status: "noop", operationId: journal.operationId, journalPath: this.#journalPath() });
    this.#preflight(journal); this.#writeJournal(journal);
    return this.#continue(journal);
  }

  recover() { return this.recoverPending(); }

  diagnose({ probePath } = {}) { return diagnoseRootConfig({ userDataDir: this.userDataRoot, exists: this.exists, readFile: this.readFile, ...(probePath ? { probePath } : {}) }); }

  recoverPending() {
    const journalPath = this.#journalPath();
    if (!this.exists(journalPath) && !this.exists(this.#previousJournalPath())) return Object.freeze({ status: "none", journalPath });
    const recovered = this.#readRecoverableJournal();
    const journal = recovered.journal;
    if (recovered.fromPrevious) this.#atomicWrite(journalPath, JSON.stringify(journal, null, 2));
    if (journal.status === "switched" || journal.status === "completed") {
      this.#cleanupReceipts(journal);
      return Object.freeze({ status: journal.status, journalPath, operationId: journal.operationId });
    }
    this.#reconcilePublishedOperations(journal);
    this.#preflight(journal);
    return this.#continue(journal);
  }

  #newJournal(source, target, previousPointer) {
    const operationId = `root-${Date.now()}-${randomUUID()}`;
    const operations = uniqueOperations(source, target).map((operation, index) => ({ ...operation, index, attempts: 0, status: operation.source === operation.target ? "unchanged" : "pending" }));
    return { version: ROOT_CONFIG_VERSION, operationId, status: operations.every((operation) => operation.status === "unchanged") ? "noop" : "copying", source, target, previousPointer, operations };
  }

  #preflight(journal) {
    validateMigrationPlan(journal.operations);
    if (journal.operations.some((operation) => operation.status !== "unchanged" && pathContains(operation.source, this.userDataRoot))) throw new Error("root_migration_control_path_overlap");
    const requiredByVolume = new Map();
    for (const operation of journal.operations) {
      if (operation.status === "unchanged") continue;
      this.validateRootPath(operation.source); this.validateRootPath(operation.target);
      if (!this.targetAvailable(operation.target, operation.roles)) throw new Error("root_migration_target_unavailable");
      if (!this.targetWritable(operation.target, operation.roles)) throw new Error("root_migration_target_unwritable");
      if (operation.status !== "copied" && operation.source !== operation.target && this.exists(operation.target)) throw new Error("root_migration_target_exists");
      this.assertSourceQuiescent(operation.source, operation.roles);
      if (containsSqlite(operation.source) && (!this.hasCustomQuiescence || !this.hasCustomCopyVerifier)) throw new Error("root_migration_database_verifier_required");
      if (operation.status === "copied") {
        this.#verifyPublishedTree(journal, operation);
        continue;
      }
      operation.manifest ||= this.snapshotTree(operation.source, operation.roles);
      const required = this.estimateBytes(operation.source, operation.roles);
      const volume = volumeIdentity(operation.target);
      if (!Number.isFinite(required) || required < 0) throw new Error("root_migration_insufficient_space");
      const current = requiredByVolume.get(volume) || { required: 0, target: operation.target, roles: [] };
      current.required += required; current.roles.push(...operation.roles); requiredByVolume.set(volume, current);
    }
    for (const { required, target, roles } of requiredByVolume.values()) {
      if (this.availableBytes(target, roles) < required) throw new Error("root_migration_insufficient_space");
    }
  }

  #continue(journal) {
    for (const operation of journal.operations) {
      if (operation.status === "copied" || operation.status === "unchanged") continue;
      operation.status = "copying"; operation.attempts += 1; operation.stagingPath = this.#stagingPath(journal, operation);
      while (this.exists(operation.stagingPath)) { operation.attempts += 1; operation.stagingPath = this.#stagingPath(journal, operation); }
      this.#writeJournal(journal);
      this.mkdir(path.dirname(operation.stagingPath)); this.copyTree(operation.source, operation.stagingPath, operation.roles);
      this.#writeReceipt(journal, operation);
      this.rename(operation.stagingPath, operation.target);
      this.#assertPublishedReceipt(journal, operation);
      this.#verifyPublishedTree(journal, operation);
      operation.status = "copied"; this.#writeJournal(journal);
      this.#removeReceipt(operation);
    }
    this.#writePreviousPointer(journal);
    this.#writePointer(journal.target);
    journal.status = "switched"; this.#writeJournal(journal);
    this.#cleanupReceipts(journal);
    return Object.freeze({ status: "switched", operationId: journal.operationId, journalPath: this.#journalPath() });
  }

  #journalPath() { return path.join(this.userDataRoot, ROOT_MIGRATION_JOURNAL_FILE); }
  #previousJournalPath() { return path.join(this.userDataRoot, ROOT_MIGRATION_PREVIOUS_JOURNAL_FILE); }
  #stagingPath(journal, operation) { return path.join(path.dirname(operation.target), `.chriptmas-root-migration-${journal.operationId}-${operation.index}-${operation.attempts}`); }
  #receiptPath(operation) { return path.join(operation.stagingPath || operation.target, ROOT_MIGRATION_RECEIPT_FILE); }
  #writeReceipt(journal, operation) {
    const receiptPath = this.#receiptPath(operation);
    if (this.exists(receiptPath)) throw new Error("root_migration_receipt_conflict");
    this.writeFile(receiptPath, JSON.stringify({ version: ROOT_CONFIG_VERSION, operationId: journal.operationId, index: operation.index, source: operation.source, target: operation.target }));
  }
  #assertPublishedReceipt(journal, operation) {
    const receipt = readJson(path.join(operation.target, ROOT_MIGRATION_RECEIPT_FILE), this.readFile);
    if (receipt.operationId !== journal.operationId || receipt.index !== operation.index || receipt.source !== operation.source || receipt.target !== operation.target) throw new Error("root_migration_target_receipt_invalid");
  }
  #reconcilePublishedOperations(journal) {
    let changed = false;
    for (const operation of journal.operations) {
      if (operation.status !== "copying" || !this.exists(operation.target)) continue;
      this.#assertPublishedReceipt(journal, operation);
      this.#verifyPublishedTree(journal, operation);
      operation.status = "copied"; changed = true;
    }
    if (changed) this.#writeJournal(journal);
    this.#cleanupReceipts(journal);
  }
  #removeReceipt(operation) { try { this.removeFile(path.join(operation.target, ROOT_MIGRATION_RECEIPT_FILE)); } catch {} }
  #verifyPublishedTree(journal, operation) {
    if (!operation.manifest || !this.verifyCopiedTree(operation.source, operation.target, operation.manifest, operation.roles)) throw new Error("root_migration_target_verification_failed");
  }
  #cleanupReceipts(journal) { for (const operation of journal.operations) if (operation.status === "copied") this.#removeReceipt(operation); }
  #writePreviousPointer(journal) {
    if (journal.previousPointer === null || journal.previousPointer === undefined) return;
    this.#atomicWrite(path.join(this.userDataRoot, DATA_ROOT_PREVIOUS_POINTER_FILE), JSON.stringify(journal.previousPointer, null, 2));
  }
  #writePointer(target) { this.#atomicWrite(path.join(this.userDataRoot, DATA_ROOT_POINTER_FILE), JSON.stringify(rootConfigPayload(target), null, 2)); }
  #writeJournal(journal) {
    const journalPath = this.#journalPath();
    if (this.exists(journalPath)) this.#atomicWrite(this.#previousJournalPath(), this.readFile(journalPath));
    this.#atomicWrite(journalPath, JSON.stringify(journal, null, 2));
  }
  #assertNoUnfinishedJournal() {
    if (!this.exists(this.#journalPath()) && !this.exists(this.#previousJournalPath())) return;
    const journal = this.#readRecoverableJournal().journal;
    if (journal.status !== "switched" && journal.status !== "completed") throw new Error("root_migration_recovery_required");
  }
  #readRecoverableJournal() {
    if (this.exists(this.#journalPath())) {
      try { return { journal: parseJournal(readJson(this.#journalPath(), this.readFile)), fromPrevious: false }; } catch (error) {
        if (!this.exists(this.#previousJournalPath())) throw error;
      }
    }
    return { journal: parseJournal(readJson(this.#previousJournalPath(), this.readFile)), fromPrevious: true };
  }
  #atomicWrite(candidate, value) {
    this.mkdir(path.dirname(candidate));
    const temporary = `${candidate}.tmp-${process.pid}-${Date.now()}-${++this.writeSequence}`;
    this.writeFile(temporary, value);
    this.flushFile(temporary);
    this.rename(temporary, candidate);
    this.flushDirectory(path.dirname(candidate));
  }
}

function uniqueOperations(source, target) {
  const grouped = new Map();
  for (const role of ROOT_ROLES) {
    const sourcePath = rolePath(source, role); const targetPath = rolePath(target, role); const key = `${sourcePath}\u0000${targetPath}`;
    const operation = grouped.get(key) || { source: sourcePath, target: targetPath, roles: [] };
    operation.roles.push(role); grouped.set(key, operation);
  }
  return [...grouped.values()];
}

function validateMigrationPlan(operations) {
  for (let left = 0; left < operations.length; left += 1) {
    const current = operations[left];
    for (let right = left + 1; right < operations.length; right += 1) {
      const other = operations[right];
      if (pathsOverlap(current.source, other.source) && current.source !== other.source) throw new Error("root_migration_source_roots_overlap");
      if (pathsOverlap(current.target, other.target)) throw new Error("root_migration_target_roots_overlap");
      if (pathsOverlap(current.target, other.source) || pathsOverlap(other.target, current.source)) throw new Error("root_migration_source_target_overlap");
    }
    if (current.source !== current.target && pathsOverlap(current.source, current.target)) throw new Error("root_migration_source_target_overlap");
  }
}

function migrationPlanSummary(journal) {
  return Object.freeze({
    operationId: journal.operationId,
    status: journal.status,
    source: rootConfigPayload(journal.source),
    target: rootConfigPayload(journal.target),
    operations: Object.freeze(journal.operations.map((operation) => Object.freeze({ source: operation.source, target: operation.target, roles: Object.freeze([...operation.roles]), status: operation.status }))),
  });
}

function pathsOverlap(left, right) {
  return pathContains(left, right) || pathContains(right, left);
}

function pathContains(ancestor, candidate) {
  const relative = path.relative(normalizePathForComparison(ancestor), normalizePathForComparison(candidate));
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

function normalizePathForComparison(candidate) {
  const resolved = path.resolve(candidate);
  return process.platform === "win32" ? resolved.toLocaleLowerCase("en-US") : resolved;
}

function parseJournal(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || value.version !== ROOT_CONFIG_VERSION || !["copying", "switched", "completed"].includes(value.status) || !Array.isArray(value.operations)) throw new Error("root_migration_journal_invalid");
  value.source = normalizeRootConfig(value.source); value.target = normalizeRootConfig(value.target);
  for (const operation of value.operations) {
    if (!operation || !absolutePath(operation.source) || !absolutePath(operation.target) || !Number.isInteger(operation.index) || !Number.isInteger(operation.attempts) || operation.attempts < 0 || !Array.isArray(operation.roles) || !operation.roles.every((role) => ROOT_ROLES.includes(role)) || !["pending", "copying", "copied", "unchanged"].includes(operation.status) || (operation.manifest !== undefined && !validManifest(operation.manifest)) || (operation.status === "copied" && !validManifest(operation.manifest))) throw new Error("root_migration_journal_invalid");
  }
  return value;
}

function validManifest(manifest) {
  if (!Array.isArray(manifest)) return false;
  const paths = new Set();
  return manifest.every((entry) => {
    if (!entry || typeof entry.path !== "string" || !entry.path || path.isAbsolute(entry.path) || entry.path.split(/[\\/]/).includes("..") || !["file", "directory"].includes(entry.type) || !Number.isInteger(entry.size) || entry.size < 0 || paths.has(entry.path)) return false;
    paths.add(entry.path); return true;
  });
}

function defaultCopyTree(source, target) { fs.cpSync(source, target, { recursive: true, force: false, errorOnExist: true }); }
function defaultFlushFile(candidate) {
  const descriptor = fs.openSync(candidate, "r+");
  try { fs.fsyncSync(descriptor); } finally { fs.closeSync(descriptor); }
}
function defaultFlushDirectory(candidate) {
  try {
    const descriptor = fs.openSync(candidate, "r");
    try { fs.fsyncSync(descriptor); } finally { fs.closeSync(descriptor); }
  } catch (error) {
    if (!["EISDIR", "EPERM", "EINVAL", "ENOTSUP"].includes(error?.code)) throw error;
  }
}
function defaultTargetAvailable(candidate) { try { return fs.statSync(nearestExistingDirectory(candidate)).isDirectory(); } catch { return false; } }
function defaultTargetWritable(candidate) {
  try {
    const directory = nearestExistingDirectory(candidate);
    fs.accessSync(directory, fs.constants.W_OK);
    const probe = path.join(directory, `.chriptmas-root-write-probe-${process.pid}-${Date.now()}`);
    const descriptor = fs.openSync(probe, "wx"); fs.closeSync(descriptor); fs.unlinkSync(probe);
    return true;
  } catch { return false; }
}
function defaultAvailableBytes(candidate) {
  try { const stats = fs.statfsSync(nearestExistingDirectory(candidate)); return Number(stats.bavail) * Number(stats.bsize); } catch { return -1; }
}
function estimateTreeBytes(candidate) {
  let total = 0;
  const visit = (entry) => {
    const stats = fs.lstatSync(entry);
    if (stats.isSymbolicLink()) throw new Error("root_migration_reparse_point");
    if (stats.isDirectory()) for (const child of fs.readdirSync(entry)) visit(path.join(entry, child));
    else if (stats.isFile()) total += stats.size;
  };
  visit(candidate); return total;
}
function snapshotTree(candidate) { return Object.freeze(scanTree(candidate)); }
function verifyCopiedTree(_source, target, expectedManifest) {
  try {
    const actual = scanTree(target, new Set([ROOT_MIGRATION_RECEIPT_FILE]));
    if (!Array.isArray(expectedManifest) || expectedManifest.length !== actual.length) return false;
    for (let index = 0; index < expectedManifest.length; index += 1) {
      const expected = expectedManifest[index]; const observed = actual[index];
      if (!observed || expected.path !== observed.path || expected.type !== observed.type || expected.size !== observed.size) return false;
      if (observed.type === "file") readFileProbe(path.join(target, observed.path));
    }
    return true;
  } catch { return false; }
}
function scanTree(root, excludedNames = new Set()) {
  const entries = [];
  const visit = (candidate, relative) => {
    const stats = fs.lstatSync(candidate);
    if (stats.isSymbolicLink()) throw new Error("root_migration_reparse_point");
    if (stats.isDirectory()) {
      if (relative) entries.push({ path: relative, type: "directory", size: 0 });
      for (const child of fs.readdirSync(candidate).sort()) {
        if (!relative && excludedNames.has(child)) continue;
        visit(path.join(candidate, child), relative ? path.join(relative, child) : child);
      }
    } else if (stats.isFile()) entries.push({ path: relative, type: "file", size: stats.size });
    else throw new Error("root_migration_unsupported_entry");
  };
  visit(root, ""); return entries;
}
function readFileProbe(candidate) {
  const descriptor = fs.openSync(candidate, "r");
  try { if (fs.fstatSync(descriptor).size > 0) fs.readSync(descriptor, Buffer.alloc(1), 0, 1, 0); } finally { fs.closeSync(descriptor); }
}
function assertNoUnmanagedSqlite(candidate) {
  const visit = (entry) => {
    const stats = fs.lstatSync(entry);
    if (stats.isSymbolicLink()) throw new Error("root_migration_reparse_point");
    if (stats.isDirectory()) for (const child of fs.readdirSync(entry)) visit(path.join(entry, child));
    else if (stats.isFile() && /(?:\.sqlite|\.sqlite3|\.db)(?:-(?:wal|shm))?$/i.test(entry)) throw new Error("root_migration_database_snapshot_required");
  };
  visit(candidate);
}
function containsSqlite(candidate) {
  const visit = (entry) => {
    const stats = fs.lstatSync(entry);
    if (stats.isSymbolicLink()) throw new Error("root_migration_reparse_point");
    if (stats.isDirectory()) return fs.readdirSync(entry).some((child) => visit(path.join(entry, child)));
    return stats.isFile() && /(?:\.sqlite|\.sqlite3|\.db)(?:-(?:wal|shm))?$/i.test(entry);
  };
  return visit(candidate);
}
function assertNoReparsePoint(candidate) {
  let current = path.resolve(candidate);
  while (true) {
    if (fs.existsSync(current) && fs.lstatSync(current).isSymbolicLink()) throw new Error("root_migration_reparse_point");
    const parent = path.dirname(current); if (parent === current) return;
    current = parent;
  }
}
function nearestExistingDirectory(candidate) {
  let current = path.resolve(candidate);
  while (!fs.existsSync(current)) {
    const parent = path.dirname(current); if (parent === current) throw new Error("root_migration_target_unavailable");
    current = parent;
  }
  return current;
}
function volumeIdentity(candidate) {
  try { return `device:${fs.statSync(nearestExistingDirectory(candidate)).dev}`; } catch { return `path:${path.parse(path.resolve(candidate)).root.toLowerCase()}`; }
}
function readPointer(pointerPath, readFile) { try { return JSON.parse(readFile(pointerPath)); } catch { throw new Error("vault_root_pointer_invalid"); } }
function parsePointerPayload(pointerPath, readFile) { return readPointer(pointerPath, readFile); }
function readJson(candidate, readFile) { try { return JSON.parse(readFile(candidate)); } catch { throw new Error("root_migration_journal_invalid"); } }
function resolveUserDataRoot(userDataDir) { if (typeof userDataDir !== "string" || !userDataDir.trim()) throw new TypeError("vault_root_requires_user_data_dir"); return path.resolve(userDataDir); }
function absolutePath(value) { return typeof value === "string" && value.trim() && path.isAbsolute(value) ? path.resolve(value) : null; }
function rolePath(config, role) { return config[`${role}Root`]; }
function hasDataMarkers(root, exists) { return DATA_MARKERS.some((marker) => exists(path.join(root, marker))); }

module.exports = { DATA_MARKERS, DATA_ROOT_POINTER_FILE, DATA_ROOT_PREVIOUS_POINTER_FILE, ROOT_CONFIG_VERSION, ROOT_MIGRATION_JOURNAL_FILE, ROOT_MIGRATION_PREVIOUS_JOURNAL_FILE, RootConfigMigrationController, diagnoseRootConfig, resolvePackagedVaultRoot, resolveRootConfig, rootConfigPayload, verifyCopiedTree };
