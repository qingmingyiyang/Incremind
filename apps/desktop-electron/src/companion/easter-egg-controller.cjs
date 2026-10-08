const fs = require("node:fs");
const path = require("node:path");

const SAFE_ID = /^[a-z][a-z0-9_.-]{0,63}$/;
const VISUAL_STATES = new Set(["happy", "surprised", "sleepy"]);

class CompanionEasterEggController {
  constructor({ catalogPath, statePath, now = () => new Date() }) {
    this.catalogPath = path.resolve(catalogPath);
    this.statePath = path.resolve(statePath);
    this.now = now;
    this.catalog = loadCatalog(this.catalogPath);
    const loaded = loadState(this.statePath, new Set(this.catalog.events.map((event) => event.id)));
    this.state = loaded.state;
    this.stateStatus = loaded.status;
  }

  status() {
    return Object.freeze({
      enabled: this.state.enabled,
      state: this.stateStatus,
      available_events: this.catalog.events.filter((event) => event.enabled).length,
    });
  }

  setEnabled(enabled) {
    if (typeof enabled !== "boolean") throw new Error("easter_egg_enabled_invalid");
    if (this.stateStatus === "invalid") throw new Error("easter_egg_state_invalid");
    this.state.enabled = enabled;
    persistState(this.statePath, this.state);
    return this.status();
  }

  record(counter) {
    if (typeof counter !== "string" || !SAFE_ID.test(counter)) throw new Error("easter_egg_counter_invalid");
    if (this.stateStatus === "invalid" || !this.state.enabled) return Object.freeze({ status: "disabled", events: Object.freeze([]) });
    const candidates = this.catalog.events.filter((event) => event.enabled && event.counter === counter);
    if (!candidates.length) throw new Error("easter_egg_counter_unknown");
    const now = requireDate(this.now());
    const periods = new Map();
    for (const event of candidates) {
      const period = periodKey(event.reset, now);
      if (periods.has(event.counter)) continue;
      const current = this.state.counters[event.counter];
      const count = current?.period === period ? current.count + 1 : 1;
      this.state.counters[event.counter] = { period, count };
      periods.set(event.counter, { period, count });
    }
    const triggered = [];
    for (const event of candidates) {
      const current = periods.get(event.counter);
      const previous = this.state.triggers[event.id];
      const previousCount = previous?.period === current.period ? previous.count : 0;
      const lastAt = previous ? Date.parse(previous.at) : Number.NEGATIVE_INFINITY;
      if (current.count < event.threshold || current.count - previousCount < event.threshold) continue;
      if (Number.isFinite(lastAt) && now.getTime() - lastAt < event.cooldown_seconds * 1000) continue;
      this.state.triggers[event.id] = { period: current.period, count: current.count, at: now.toISOString() };
      triggered.push(projectEvent(event));
    }
    persistState(this.statePath, this.state);
    return Object.freeze({ status: "recorded", events: Object.freeze(triggered) });
  }
}

function loadCatalog(catalogPath) {
  const stat = fs.lstatSync(catalogPath);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 64 * 1024) throw new Error("easter_egg_catalog_invalid");
  let payload;
  try { payload = JSON.parse(fs.readFileSync(catalogPath, "utf8")); } catch { throw new Error("easter_egg_catalog_invalid"); }
  if (!payload || typeof payload !== "object" || Array.isArray(payload) || payload.version !== 1 || !Array.isArray(payload.events)) throw new Error("easter_egg_catalog_invalid");
  const ids = new Set();
  const counters = new Set();
  const events = payload.events.map((event) => validateEvent(event, ids, counters));
  return Object.freeze({ version: 1, events: Object.freeze(events) });
}

function validateEvent(event, ids, counters) {
  const allowed = new Set(["id", "enabled", "counter", "threshold", "reset", "cooldown_seconds", "actions"]);
  if (!event || typeof event !== "object" || Array.isArray(event) || Object.keys(event).some((key) => !allowed.has(key))) throw new Error("easter_egg_event_invalid");
  if (typeof event.id !== "string" || !SAFE_ID.test(event.id) || ids.has(event.id)) throw new Error("easter_egg_event_invalid");
  ids.add(event.id);
  if (typeof event.enabled !== "boolean" || typeof event.counter !== "string" || !SAFE_ID.test(event.counter)) throw new Error("easter_egg_event_invalid");
  if (counters.has(event.counter)) throw new Error("easter_egg_event_invalid");
  counters.add(event.counter);
  if (!Number.isSafeInteger(event.threshold) || event.threshold < 1 || event.threshold > 10_000) throw new Error("easter_egg_event_invalid");
  if (!new Set(["daily", "weekly", "never"]).has(event.reset)) throw new Error("easter_egg_event_invalid");
  if (!Number.isSafeInteger(event.cooldown_seconds) || event.cooldown_seconds < 0 || event.cooldown_seconds > 604_800) throw new Error("easter_egg_event_invalid");
  if (!Array.isArray(event.actions) || event.actions.length < 1 || event.actions.length > 2) throw new Error("easter_egg_event_invalid");
  let text = null;
  let visualState = null;
  for (const action of event.actions) {
    if (!action || typeof action !== "object" || Array.isArray(action)) throw new Error("easter_egg_action_invalid");
    if (action.type === "show_text" && Object.keys(action).every((key) => ["type", "text"].includes(key)) && typeof action.text === "string" && action.text.trim() && action.text.length <= 160 && text === null) text = action.text.trim();
    else if (action.type === "visual_state" && Object.keys(action).every((key) => ["type", "state"].includes(key)) && VISUAL_STATES.has(action.state) && visualState === null) visualState = action.state;
    else throw new Error("easter_egg_action_invalid");
  }
  if (text === null) throw new Error("easter_egg_action_invalid");
  return Object.freeze({ id: event.id, enabled: event.enabled, counter: event.counter, threshold: event.threshold, reset: event.reset, cooldown_seconds: event.cooldown_seconds, text, visual_state: visualState || "surprised" });
}

function loadState(statePath, eventIds) {
  if (!fs.existsSync(statePath)) return { status: "ready", state: { version: 1, enabled: true, counters: {}, triggers: {} } };
  try {
    const stat = fs.lstatSync(statePath);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 64 * 1024) throw new Error();
    const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
    validateState(state, eventIds);
    return { status: "ready", state };
  } catch {
    return { status: "invalid", state: { version: 1, enabled: false, counters: {}, triggers: {} } };
  }
}

function validateState(state, eventIds) {
  if (!state || typeof state !== "object" || Array.isArray(state) || Object.keys(state).sort().join() !== "counters,enabled,triggers,version" || state.version !== 1 || typeof state.enabled !== "boolean") throw new Error();
  if (!state.counters || typeof state.counters !== "object" || Array.isArray(state.counters) || !state.triggers || typeof state.triggers !== "object" || Array.isArray(state.triggers)) throw new Error();
  for (const [counter, value] of Object.entries(state.counters)) {
    if (!SAFE_ID.test(counter) || !value || typeof value.period !== "string" || value.period.length > 16 || !Number.isSafeInteger(value.count) || value.count < 0 || value.count > 1_000_000) throw new Error();
  }
  for (const [id, value] of Object.entries(state.triggers)) {
    if (!eventIds.has(id) || !value || typeof value.period !== "string" || !Number.isSafeInteger(value.count) || !Number.isFinite(Date.parse(value.at))) throw new Error();
  }
}

function persistState(statePath, state) {
  const directory = path.dirname(statePath);
  fs.mkdirSync(directory, { recursive: true });
  if (fs.existsSync(statePath) && fs.lstatSync(statePath).isSymbolicLink()) throw new Error("easter_egg_state_invalid");
  const temporary = path.join(directory, `.${path.basename(statePath)}.${process.pid}.${Date.now()}.tmp`);
  fs.writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, { encoding: "utf8", flag: "wx" });
  try { fs.renameSync(temporary, statePath); } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
}

function periodKey(reset, date) {
  if (reset === "never") return "all";
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  if (reset === "daily") return `${year}-${month}-${day}`;
  const local = new Date(year, date.getMonth(), date.getDate());
  const weekday = (local.getDay() + 6) % 7;
  local.setDate(local.getDate() - weekday);
  return `${local.getFullYear()}-${String(local.getMonth() + 1).padStart(2, "0")}-${String(local.getDate()).padStart(2, "0")}`;
}

function projectEvent(event) {
  return Object.freeze({ id: event.id, text: event.text, visual_state: event.visual_state });
}

function requireDate(value) {
  if (!(value instanceof Date) || !Number.isFinite(value.getTime())) throw new Error("easter_egg_clock_invalid");
  return value;
}

module.exports = { CompanionEasterEggController, loadCatalog, periodKey };
