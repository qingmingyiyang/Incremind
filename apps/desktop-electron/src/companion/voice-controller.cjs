const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const MAX_AUDIO_BYTES = 8 * 1024 * 1024;
const MAX_REFERENCE_BYTES = 50 * 1024 * 1024;
const LANGUAGES = new Set(["zh", "en", "ja", "yue", "ko", "auto"]);

class CompanionVoiceController {
  constructor({ statePath, fetchFn = fetch, timeoutMs = 45000, setTimeoutFn = setTimeout, clearTimeoutFn = clearTimeout }) {
    if (typeof fetchFn !== "function" || typeof setTimeoutFn !== "function" || typeof clearTimeoutFn !== "function" || !Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 120000) throw new TypeError("companion voice dependencies are invalid");
    this.statePath = path.resolve(statePath);
    this.fetchFn = fetchFn;
    this.timeoutMs = timeoutMs;
    this.setTimeoutFn = setTimeoutFn;
    this.clearTimeoutFn = clearTimeoutFn;
    const loaded = loadState(this.statePath);
    this.state = loaded.state;
    this.stateStatus = loaded.status;
    this.active = null;
  }

  status() {
    const reference = inspectStoredReference(this.state);
    return Object.freeze({
      state: this.stateStatus,
      enabled: this.state.enabled,
      origin: this.state.origin,
      text_lang: this.state.text_lang,
      prompt_lang: this.state.prompt_lang,
      prompt_text: this.state.prompt_text,
      has_reference: reference.valid,
      reference_name: reference.valid ? path.basename(this.state.ref_audio_path) : null,
      reference_state: reference.state,
      speaking: this.active !== null,
    });
  }

  configure(payload) {
    this.requireWritable();
    const next = {
      ...this.state,
      enabled: requireBoolean(payload?.enabled),
      origin: normalizeLoopbackOrigin(payload?.origin),
      text_lang: requireLanguage(payload?.text_lang),
      prompt_lang: requireLanguage(payload?.prompt_lang),
      prompt_text: requirePrompt(payload?.prompt_text),
    };
    validateState(next);
    persistState(this.statePath, next);
    this.state = next;
    if (!next.enabled) this.cancel();
    return this.status();
  }

  setReference(selectedPath) {
    this.requireWritable();
    const reference = inspectReference(selectedPath);
    const next = { ...this.state, ref_audio_path: reference.path, ref_fingerprint: reference.fingerprint };
    persistState(this.statePath, next);
    this.state = next;
    return this.status();
  }

  async speak(value) {
    this.requireWritable();
    const text = requireText(value);
    if (!this.state.enabled) throw new Error("companion_voice_disabled");
    const reference = inspectStoredReference(this.state);
    if (!reference.valid) throw new Error(`companion_voice_reference_${reference.state}`);
    this.cancel();
    const controller = new AbortController();
    const timeout = this.setTimeoutFn(() => controller.abort(new Error("companion_voice_timeout")), this.timeoutMs);
    this.active = controller;
    try {
      const response = await this.fetchFn(`${this.state.origin}/tts`, {
        method: "POST",
        headers: { Accept: "audio/wav", "Content-Type": "application/json" },
        body: JSON.stringify({
          text,
          text_lang: this.state.text_lang,
          ref_audio_path: this.state.ref_audio_path,
          prompt_lang: this.state.prompt_lang,
          prompt_text: this.state.prompt_text,
          text_split_method: "cut5",
          batch_size: 1,
          media_type: "wav",
          streaming_mode: false,
        }),
        signal: controller.signal,
      });
      if (!response?.ok) throw new Error(`companion_voice_http_${response?.status || 0}`);
      const contentType = String(response.headers?.get?.("content-type") || "").split(";", 1)[0].trim().toLowerCase();
      if (!["audio/wav", "audio/x-wav", "audio/wave"].includes(contentType)) throw new Error("companion_voice_media_invalid");
      const audio = await readBoundedAudio(response, MAX_AUDIO_BYTES);
      requireWav(audio);
      return Object.freeze({ audio, media_type: "audio/wav" });
    } finally {
      this.clearTimeoutFn(timeout);
      if (this.active === controller) this.active = null;
    }
  }

  cancel() {
    if (!this.active) return false;
    this.active.abort(new Error("companion_voice_superseded"));
    this.active = null;
    return true;
  }

  requireWritable() { if (this.stateStatus !== "ready") throw new Error("companion_voice_state_invalid"); }
}

function defaultState() {
  return { version: 1, enabled: false, origin: "http://127.0.0.1:9880", text_lang: "zh", prompt_lang: "zh", prompt_text: "", ref_audio_path: null, ref_fingerprint: null };
}

function loadState(statePath) {
  if (!fs.existsSync(statePath)) return { status: "ready", state: defaultState() };
  try {
    const stat = fs.lstatSync(statePath);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 64 * 1024) throw new Error();
    const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
    validateState(state);
    return { status: "ready", state };
  } catch { return { status: "invalid", state: defaultState() }; }
}

function validateState(state) {
  const keys = "enabled,origin,prompt_lang,prompt_text,ref_audio_path,ref_fingerprint,text_lang,version";
  if (!state || typeof state !== "object" || Array.isArray(state) || Object.keys(state).sort().join() !== keys || state.version !== 1) throw new Error("companion_voice_state_invalid");
  requireBoolean(state.enabled); normalizeLoopbackOrigin(state.origin); requireLanguage(state.text_lang); requireLanguage(state.prompt_lang); requirePrompt(state.prompt_text);
  const bothNull = state.ref_audio_path === null && state.ref_fingerprint === null;
  const bothPresent = typeof state.ref_audio_path === "string" && path.isAbsolute(state.ref_audio_path) && typeof state.ref_fingerprint === "string" && /^[a-f0-9]{64}$/.test(state.ref_fingerprint);
  if (!bothNull && !bothPresent) throw new Error("companion_voice_state_invalid");
}

function normalizeLoopbackOrigin(value) {
  if (typeof value !== "string" || value !== value.trim() || value.length > 80) throw new Error("companion_voice_origin_invalid");
  let parsed;
  try { parsed = new URL(value); } catch { throw new Error("companion_voice_origin_invalid"); }
  if (parsed.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(parsed.hostname.toLowerCase()) || parsed.username || parsed.password || parsed.pathname !== "/" || parsed.search || parsed.hash || !parsed.port) throw new Error("companion_voice_origin_invalid");
  return `http://${parsed.hostname.toLowerCase()}:${parsed.port}`;
}

function inspectReference(value) {
  if (typeof value !== "string" || !path.isAbsolute(value) || path.extname(value).toLowerCase() !== ".wav") throw new Error("companion_voice_reference_invalid");
  const resolved = fs.realpathSync.native(value);
  const stat = fs.statSync(resolved);
  if (!stat.isFile() || stat.size < 44 || stat.size > MAX_REFERENCE_BYTES) throw new Error("companion_voice_reference_invalid");
  const header = Buffer.alloc(12);
  const descriptor = fs.openSync(resolved, "r");
  try { if (fs.readSync(descriptor, header, 0, 12, 0) !== 12) throw new Error("companion_voice_reference_invalid"); }
  finally { fs.closeSync(descriptor); }
  requireWav(header);
  const fingerprint = crypto.createHash("sha256").update(`${resolved.toLowerCase()}\0${stat.dev}\0${stat.ino}\0${stat.size}\0${stat.mtimeMs}`).digest("hex");
  return { path: resolved, fingerprint };
}

function inspectStoredReference(state) {
  if (!state.ref_audio_path || !state.ref_fingerprint) return { valid: false, state: "missing" };
  try {
    const current = inspectReference(state.ref_audio_path);
    return current.fingerprint === state.ref_fingerprint ? { valid: true, state: "ready" } : { valid: false, state: "changed" };
  } catch { return { valid: false, state: "unavailable" }; }
}

async function readBoundedAudio(response, maximum) {
  const length = Number(response.headers?.get?.("content-length"));
  if (Number.isFinite(length) && length > maximum) throw new Error("companion_voice_audio_too_large");
  if (!response.body?.getReader) {
    const fallback = Buffer.from(await response.arrayBuffer());
    if (fallback.length > maximum) throw new Error("companion_voice_audio_too_large");
    return fallback;
  }
  const reader = response.body.getReader();
  const chunks = []; let total = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maximum) { await reader.cancel(); throw new Error("companion_voice_audio_too_large"); }
    chunks.push(Buffer.from(value));
  }
  return Buffer.concat(chunks, total);
}

function requireWav(value) {
  const bytes = Buffer.from(value);
  if (bytes.length < 12 || bytes.toString("ascii", 0, 4) !== "RIFF" || bytes.toString("ascii", 8, 12) !== "WAVE") throw new Error("companion_voice_wav_invalid");
}
function requireBoolean(value) { if (typeof value !== "boolean") throw new Error("companion_voice_setting_invalid"); return value; }
function requireLanguage(value) { if (typeof value !== "string" || !LANGUAGES.has(value.toLowerCase())) throw new Error("companion_voice_language_invalid"); return value.toLowerCase(); }
function requirePrompt(value) { if (typeof value !== "string" || value.length > 500 || value.includes("\0")) throw new Error("companion_voice_prompt_invalid"); return value.trim(); }
function requireText(value) { if (typeof value !== "string" || !value.trim() || value.length > 400 || value.includes("\0")) throw new Error("companion_voice_text_invalid"); return value.trim(); }

function persistState(statePath, state) {
  const directory = path.dirname(statePath); fs.mkdirSync(directory, { recursive: true });
  if (fs.existsSync(statePath) && fs.lstatSync(statePath).isSymbolicLink()) throw new Error("companion_voice_state_invalid");
  const temporary = path.join(directory, `.${path.basename(statePath)}.${process.pid}.${Date.now()}.tmp`);
  fs.writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
  try { fs.renameSync(temporary, statePath); } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
}

module.exports = { CompanionVoiceController, MAX_AUDIO_BYTES, inspectReference, normalizeLoopbackOrigin, readBoundedAudio, requireWav };
