const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const NAME_MAX = 48;
const ENTRY_ID = /^(program|bookmark):[0-9a-f-]{36}$/;
const PROGRAM_EXTENSIONS = new Set([".exe", ".com"]);

class CompanionLauncherController {
  constructor({ statePath, openExternal, spawnProcess = spawn, idFactory = () => crypto.randomUUID(), launchArgs = () => [] }) {
    this.statePath = path.resolve(statePath);
    this.openExternal = openExternal;
    this.spawnProcess = spawnProcess;
    this.idFactory = idFactory;
    this.launchArgs = launchArgs;
    const loaded = loadState(this.statePath);
    this.state = loaded.state;
    this.stateStatus = loaded.status;
  }

  list() {
    return Object.freeze({
      state: this.stateStatus,
      entries: Object.freeze(this.state.entries.map(projectEntry)),
    });
  }

  addProgram({ name, selectedPath }) {
    this.requireWritable();
    this.requireCapacity();
    const safeName = requireName(name);
    const authority = inspectProgram(selectedPath);
    const entry = {
      id: `program:${this.idFactory()}`,
      kind: "program",
      name: safeName,
      path: authority.path,
      fingerprint: authority.fingerprint,
    };
    requireEntryId(entry.id, "program");
    if (this.state.entries.some((candidate) => candidate.id === entry.id)) throw new Error("companion_launcher_id_invalid");
    this.commitEntries([...this.state.entries, entry]);
    return projectEntry(entry);
  }

  ensureProgram({ name, selectedPath }) {
    this.requireWritable();
    const authority = inspectProgram(selectedPath);
    const existing = this.state.entries.find((entry) => entry.kind === "program" && entry.path === authority.path && entry.fingerprint === authority.fingerprint);
    return existing ? projectEntry(existing) : this.addProgram({ name, selectedPath: authority.path });
  }

  addBookmark({ name, url }) {
    this.requireWritable();
    this.requireCapacity();
    const entry = {
      id: `bookmark:${this.idFactory()}`,
      kind: "bookmark",
      name: requireName(name),
      url: normalizeBookmark(url),
    };
    requireEntryId(entry.id, "bookmark");
    if (this.state.entries.some((candidate) => candidate.id === entry.id)) throw new Error("companion_launcher_id_invalid");
    this.commitEntries([...this.state.entries, entry]);
    return projectEntry(entry);
  }

  rename({ id, name }) {
    this.requireWritable();
    const entry = this.requireEntry(id);
    const renamed = { ...entry, name: requireName(name) };
    this.commitEntries(this.state.entries.map((candidate) => candidate.id === id ? renamed : candidate));
    return projectEntry(renamed);
  }

  remove({ id }) {
    this.requireWritable();
    const index = this.state.entries.findIndex((entry) => entry.id === id);
    if (index < 0) throw new Error("companion_launcher_unknown");
    this.commitEntries(this.state.entries.filter((_entry, entryIndex) => entryIndex !== index));
    return Object.freeze({ status: "deleted", id });
  }

  async launch({ id }) {
    const entry = this.requireEntry(id);
    if (entry.kind === "bookmark") {
      await this.openExternal(entry.url);
      return Object.freeze({ status: "launched", id: entry.id, kind: entry.kind, name: entry.name });
    }
    const current = inspectProgram(entry.path);
    if (current.fingerprint !== entry.fingerprint) throw new Error("companion_launcher_authority_changed");
    const args = requireLaunchArgs(this.launchArgs(entry));
    const child = this.spawnProcess(entry.path, args, { shell: false, detached: true, windowsHide: false, stdio: "ignore" });
    if (typeof child.once === "function") {
      await new Promise((resolve, reject) => {
        child.once("spawn", resolve);
        child.once("error", reject);
      });
    }
    child.unref();
    return Object.freeze({ status: "launched", id: entry.id, kind: entry.kind, name: entry.name });
  }

  requireEntry(id) {
    if (typeof id !== "string" || !ENTRY_ID.test(id)) throw new Error("companion_launcher_id_invalid");
    const entry = this.state.entries.find((candidate) => candidate.id === id);
    if (!entry) throw new Error("companion_launcher_unknown");
    return entry;
  }

  requireWritable() {
    if (this.stateStatus !== "ready") throw new Error("companion_launcher_state_invalid");
  }

  requireCapacity() {
    if (this.state.entries.length >= 100) throw new Error("companion_launcher_capacity_reached");
  }

  commitEntries(entries) {
    const next = { version: 1, entries };
    persistState(this.statePath, next);
    this.state = next;
  }
}

function inspectProgram(value) {
  if (typeof value !== "string" || !path.isAbsolute(value) || !PROGRAM_EXTENSIONS.has(path.extname(value).toLowerCase())) throw new Error("companion_program_invalid");
  const resolved = fs.realpathSync.native(value);
  const stat = fs.statSync(resolved);
  if (!stat.isFile() || stat.size < 1) throw new Error("companion_program_invalid");
  const identity = `${resolved.toLowerCase()}\0${stat.dev}\0${stat.ino}\0${stat.size}\0${stat.mtimeMs}`;
  return Object.freeze({ path: resolved, fingerprint: crypto.createHash("sha256").update(identity).digest("hex") });
}

function normalizeBookmark(value) {
  if (typeof value !== "string" || value.length > 2048 || value !== value.trim() || value.includes("#")) throw new Error("companion_bookmark_invalid");
  let parsed;
  try { parsed = new URL(value); } catch { throw new Error("companion_bookmark_invalid"); }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.hash || !parsed.hostname) throw new Error("companion_bookmark_invalid");
  parsed.hostname = parsed.hostname.toLowerCase();
  return parsed.toString();
}

function projectEntry(entry) {
  if (entry.kind === "program") return Object.freeze({ id: entry.id, kind: entry.kind, name: entry.name, target: path.basename(entry.path) });
  const parsed = new URL(entry.url);
  return Object.freeze({ id: entry.id, kind: entry.kind, name: entry.name, target: parsed.host });
}

function requireName(value) {
  if (typeof value !== "string") throw new Error("companion_launcher_name_invalid");
  const name = value.replace(/\s+/g, " ").trim();
  if (!name || name.length > NAME_MAX || /[\u0000-\u001f\u007f]/.test(name)) throw new Error("companion_launcher_name_invalid");
  return name;
}

function requireEntryId(id, kind) {
  if (!ENTRY_ID.test(id) || !id.startsWith(`${kind}:`)) throw new Error("companion_launcher_id_invalid");
}

function requireLaunchArgs(value) {
  if (!Array.isArray(value) || value.length > 1 || value.some((item) => typeof item !== "string" || !item || item.length > 4096 || /[\u0000\r\n]/.test(item))) {
    throw new Error("companion_launcher_arguments_invalid");
  }
  return Object.freeze([...value]);
}

function loadState(statePath) {
  if (!fs.existsSync(statePath)) return { status: "ready", state: { version: 1, entries: [] } };
  try {
    const stat = fs.lstatSync(statePath);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 256 * 1024) throw new Error();
    const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
    validateState(state);
    return { status: "ready", state };
  } catch {
    return { status: "invalid", state: { version: 1, entries: [] } };
  }
}

function validateState(state) {
  if (!state || typeof state !== "object" || Array.isArray(state) || Object.keys(state).sort().join() !== "entries,version" || state.version !== 1 || !Array.isArray(state.entries) || state.entries.length > 100) throw new Error();
  const ids = new Set();
  for (const entry of state.entries) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry) || !["program", "bookmark"].includes(entry.kind)) throw new Error();
    const keys = entry.kind === "program" ? "fingerprint,id,kind,name,path" : "id,kind,name,url";
    if (Object.keys(entry).sort().join() !== keys) throw new Error();
    requireEntryId(entry.id, entry.kind);
    if (ids.has(entry.id)) throw new Error();
    ids.add(entry.id);
    requireName(entry.name);
    if (entry.kind === "program") {
      if (typeof entry.path !== "string" || !path.isAbsolute(entry.path) || !/^[a-f0-9]{64}$/.test(entry.fingerprint)) throw new Error();
    } else if (normalizeBookmark(entry.url) !== entry.url) throw new Error();
  }
}

function persistState(statePath, state) {
  const directory = path.dirname(statePath);
  fs.mkdirSync(directory, { recursive: true });
  if (fs.existsSync(statePath) && fs.lstatSync(statePath).isSymbolicLink()) throw new Error("companion_launcher_state_invalid");
  const temporary = path.join(directory, `.${path.basename(statePath)}.${process.pid}.${Date.now()}.tmp`);
  fs.writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
  try { fs.renameSync(temporary, statePath); } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
}

module.exports = { CompanionLauncherController, inspectProgram, normalizeBookmark, projectEntry };
