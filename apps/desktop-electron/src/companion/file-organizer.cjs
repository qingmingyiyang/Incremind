const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const SCHEMA_VERSION = 1;
const PLAN_TTL_MS = 5 * 60_000;
const MAX_FILES = 5000;
const MAX_HISTORY = 20;
const CATEGORY_EXTENSIONS = Object.freeze({
  images: new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"]),
  documents: new Set([".txt", ".md", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"]),
  archives: new Set([".zip", ".7z", ".rar", ".tar", ".gz"]),
  audio: new Set([".mp3", ".wav", ".flac", ".m4a", ".ogg"]),
  video: new Set([".mp4", ".mkv", ".mov", ".avi", ".webm"]),
  code: new Set([".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx", ".py", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".css", ".html", ".json", ".toml", ".yaml", ".yml"]),
});
const CATEGORY_NAMES = Object.freeze({ images: "图片", documents: "文档", archives: "压缩包", audio: "音频", video: "视频", code: "代码", other: "其他" });

class CompanionFileOrganizerError extends Error {
  constructor(code) { super(code); this.code = code; }
}

class CompanionFileOrganizer {
  constructor({ journalPath, now = () => Date.now(), randomId = () => crypto.randomUUID() }) {
    if (!journalPath || typeof journalPath !== "string") throw new TypeError("organizer journal path is required");
    this.journalPath = path.resolve(journalPath);
    this.now = now;
    this.randomId = randomId;
    this.plans = new Map();
  }

  preview(sourceRoot, targetRoot) {
    const source = realDirectory(sourceRoot);
    const target = realDirectory(targetRoot);
    if (samePath(source, target) || isWithin(source, target) || path.parse(source).root.toLowerCase() !== path.parse(target).root.toLowerCase()) {
      throw new CompanionFileOrganizerError("organizer_roots_invalid");
    }
    const entries = fs.readdirSync(source, { withFileTypes: true });
    if (entries.length > MAX_FILES) throw new CompanionFileOrganizerError("organizer_too_many_entries");
    const files = [];
    let skipped = 0;
    let conflicts = 0;
    let totalBytes = 0;
    const counts = {};
    for (const entry of entries) {
      const sourcePath = path.join(source, entry.name);
      const info = fs.lstatSync(sourcePath, { bigint: true });
      if (!entry.isFile() || !info.isFile() || info.isSymbolicLink()) { skipped += 1; continue; }
      const category = categoryFor(entry.name);
      const destination = path.join(target, CATEGORY_NAMES[category], entry.name);
      const conflict = fs.existsSync(destination);
      conflicts += Number(conflict);
      counts[category] = (counts[category] || 0) + 1;
      totalBytes += Number(info.size);
      files.push(Object.freeze({ name: entry.name, category, size: info.size.toString(), mtimeNs: info.mtimeNs.toString(), conflict }));
    }
    const planId = this.randomId();
    this.plans.clear();
    this.plans.set(planId, Object.freeze({ planId, source, target, createdAt: this.now(), files: Object.freeze(files) }));
    return freezeProjection({ plan_id: planId, file_count: files.length, total_bytes: totalBytes, conflicts, skipped, categories: counts, expires_in_seconds: PLAN_TTL_MS / 1000 });
  }

  execute(planId) {
    const plan = this._consumePlan(planId);
    const moved = [];
    let failure = null;
    for (const file of plan.files) {
      try {
        if (file.conflict) throw new CompanionFileOrganizerError("organizer_destination_exists");
        const sourcePath = safeChild(plan.source, file.name);
        const destinationDir = safeChild(plan.target, CATEGORY_NAMES[file.category]);
        const destinationPath = safeChild(destinationDir, file.name);
        verifyIdentity(sourcePath, file);
        if (fs.existsSync(destinationPath)) throw new CompanionFileOrganizerError("organizer_destination_exists");
        fs.mkdirSync(destinationDir, { recursive: true });
        fs.renameSync(sourcePath, destinationPath);
        moved.push({ name: file.name, category: file.category, size: file.size, mtimeNs: file.mtimeNs });
        this._appendJournal(plan, moved, "running");
      } catch (error) {
        failure = safeCode(error);
        break;
      }
    }
    const status = failure ? "partial" : "completed";
    const record = this._appendJournal(plan, moved, status, failure);
    return freezeProjection({ operation_id: record.id, status, moved: moved.length, remaining: plan.files.length - moved.length, error: failure });
  }

  undo(operationId) {
    const state = this._readJournal();
    const record = state.operations.find((item) => item.id === operationId);
    if (!record || !["completed", "partial"].includes(record.status)) throw new CompanionFileOrganizerError("organizer_undo_unavailable");
    let restored = 0;
    let skipped = 0;
    for (const file of [...record.files].reverse()) {
      const current = safeChild(safeChild(record.target, CATEGORY_NAMES[file.category]), file.name);
      const original = safeChild(record.source, file.name);
      try {
        verifyIdentity(current, file);
        if (fs.existsSync(original)) throw new CompanionFileOrganizerError("organizer_source_exists");
        fs.renameSync(current, original);
        restored += 1;
      } catch { skipped += 1; }
    }
    record.status = skipped ? "undo_partial" : "undone";
    record.undone_at = this.now();
    this._writeJournal(state);
    return freezeProjection({ operation_id: record.id, status: record.status, restored, skipped });
  }

  history() {
    return Object.freeze(this._readJournal().operations.map((item) => freezeProjection({ operation_id: item.id, status: item.status, moved: item.files.length, created_at: item.created_at })));
  }

  _consumePlan(planId) {
    const plan = this.plans.get(planId);
    this.plans.delete(planId);
    if (!plan || this.now() - plan.createdAt > PLAN_TTL_MS) throw new CompanionFileOrganizerError("organizer_plan_expired");
    if (realDirectory(plan.source) !== plan.source || realDirectory(plan.target) !== plan.target) throw new CompanionFileOrganizerError("organizer_root_changed");
    return plan;
  }

  _appendJournal(plan, files, status, error = null) {
    const state = this._readJournal();
    let record = state.operations.find((item) => item.id === plan.planId);
    if (!record) {
      record = { id: plan.planId, source: plan.source, target: plan.target, created_at: this.now(), status, error, files: [] };
      state.operations.unshift(record);
    }
    record.files = files.map((item) => ({ ...item }));
    record.status = status;
    record.error = error;
    state.operations = state.operations.slice(0, MAX_HISTORY);
    this._writeJournal(state);
    return record;
  }

  _readJournal() {
    if (!fs.existsSync(this.journalPath)) return { schema_version: SCHEMA_VERSION, operations: [] };
    try {
      const value = JSON.parse(fs.readFileSync(this.journalPath, "utf8"));
      if (value?.schema_version !== SCHEMA_VERSION || !Array.isArray(value.operations) || value.operations.length > MAX_HISTORY) throw new Error();
      return value;
    } catch { throw new CompanionFileOrganizerError("organizer_journal_invalid"); }
  }

  _writeJournal(value) {
    fs.mkdirSync(path.dirname(this.journalPath), { recursive: true });
    const temporary = `${this.journalPath}.${process.pid}.tmp`;
    fs.writeFileSync(temporary, `${JSON.stringify(value)}\n`, { encoding: "utf8", flag: "wx" });
    fs.renameSync(temporary, this.journalPath);
  }
}

function categoryFor(name) {
  const extension = path.extname(name).toLowerCase();
  return Object.entries(CATEGORY_EXTENSIONS).find(([, values]) => values.has(extension))?.[0] || "other";
}
function realDirectory(value) {
  if (typeof value !== "string" || !path.isAbsolute(value)) throw new CompanionFileOrganizerError("organizer_root_invalid");
  const authority = fs.lstatSync(path.resolve(value));
  if (!authority.isDirectory() || authority.isSymbolicLink()) throw new CompanionFileOrganizerError("organizer_root_invalid");
  const resolved = fs.realpathSync.native(value);
  const info = fs.lstatSync(resolved);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new CompanionFileOrganizerError("organizer_root_invalid");
  return resolved;
}
function safeChild(root, name) {
  if (typeof name !== "string" || !name || name === "." || name === ".." || path.basename(name) !== name) throw new CompanionFileOrganizerError("organizer_path_invalid");
  const candidate = path.resolve(root, name);
  if (!isWithin(root, candidate)) throw new CompanionFileOrganizerError("organizer_path_invalid");
  return candidate;
}
function isWithin(root, candidate) { const relative = path.relative(root, candidate); return Boolean(relative) && !relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative); }
function samePath(left, right) { return left.toLowerCase() === right.toLowerCase(); }
function verifyIdentity(target, expected) {
  const info = fs.lstatSync(target, { bigint: true });
  if (!info.isFile() || info.isSymbolicLink() || info.size.toString() !== expected.size || info.mtimeNs.toString() !== expected.mtimeNs) throw new CompanionFileOrganizerError("organizer_file_changed");
}
function safeCode(error) { return error instanceof CompanionFileOrganizerError ? error.code : "organizer_move_failed"; }
function freezeProjection(value) { return Object.freeze(JSON.parse(JSON.stringify(value))); }

module.exports = { CATEGORY_NAMES, CompanionFileOrganizer, CompanionFileOrganizerError, categoryFor };
