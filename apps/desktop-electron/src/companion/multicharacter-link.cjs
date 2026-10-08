const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const PROTOCOL_VERSION = 1;
const RECORD_TTL_MS = 45_000;
const MESSAGE_TYPES = new Set(["hello", "state_changed", "emote", "short_line", "bye"]);
const STATES = new Set(["idle", "busy", "sleeping", "playing", "away"]);
const EMOTES = new Set(["wave", "smile", "surprised", "cheer", "sleepy"]);
const ID_PATTERN = /^[a-z0-9][a-z0-9._-]{2,63}$/;
const FORBIDDEN_TEXT = /[\x00-\x1f\x7f]|<[^>]*>|https?:\/\/|(?:[a-zA-Z]:\\|\\\\)|\b(?:system|assistant|prompt|tool|command|powershell|cmd\.exe|bash)\b/i;

class CompanionLinkError extends Error { constructor(code) { super(code); this.code = code; } }

class CompanionMultiCharacterSettings {
  constructor({ statePath }) { this.statePath = path.resolve(statePath); }
  read() {
    if (!fs.existsSync(this.statePath)) return frozenSettings();
    try {
      const value = readRegularJson(this.statePath, 8192);
      if (!value || Object.keys(value).sort().join() !== "allowed_character_ids,character_id,consented,enabled,revision,schema_version" || value.schema_version !== 1 || typeof value.enabled !== "boolean" || typeof value.consented !== "boolean" || !Number.isInteger(value.revision) || value.revision < 0 || value.revision > Number.MAX_SAFE_INTEGER) throw new Error();
      const characterId = requireId(value.character_id);
      if (!Array.isArray(value.allowed_character_ids) || value.allowed_character_ids.length > 12) throw new Error();
      const allowed = value.allowed_character_ids.map(requireId);
      if (new Set(allowed).size !== allowed.length || allowed.includes(characterId) || (value.enabled && !value.consented)) throw new Error();
      return frozenSettings({ ...value, character_id: characterId, allowed_character_ids: allowed });
    } catch { throw new CompanionLinkError("companion_link_settings_invalid"); }
  }
  save({ enabled, consented, characterId, allowedCharacterIds, expectedRevision }) {
    const current = this.read();
    if (expectedRevision !== current.revision || typeof enabled !== "boolean" || typeof consented !== "boolean" || (enabled && !consented)) throw new CompanionLinkError("companion_link_settings_conflict");
    const identity = requireId(characterId);
    if (!Array.isArray(allowedCharacterIds) || allowedCharacterIds.length > 12) throw new CompanionLinkError("companion_link_settings_invalid");
    const allowed = allowedCharacterIds.map(requireId);
    if (new Set(allowed).size !== allowed.length || allowed.includes(identity)) throw new CompanionLinkError("companion_link_settings_invalid");
    const next = frozenSettings({ schema_version: 1, enabled, consented, character_id: identity, allowed_character_ids: allowed, revision: current.revision + 1 });
    fs.mkdirSync(path.dirname(this.statePath), { recursive: true, mode: 0o700 }); atomicWrite(this.statePath, `${JSON.stringify(next)}\n`, 0o600); return next;
  }
}

class CompanionMultiCharacterLink {
  constructor({ root, characterId, allowedCharacterIds = [], onEvent = () => {}, quiet = () => false, stateProvider = null, now = () => Date.now(), randomBytes = crypto.randomBytes, createServer = http.createServer, request = http.request, setTimer = setTimeout, clearTimer = clearTimeout }) {
    this.root = path.resolve(root); this.characterId = requireId(characterId); this.allowed = new Set(allowedCharacterIds.map(requireId));
    stateProvider = stateProvider || (() => this.state);
    if (typeof onEvent !== "function" || typeof quiet !== "function" || typeof stateProvider !== "function") throw new TypeError("companion link callbacks are invalid");
    Object.assign(this, { onEvent, quiet, stateProvider, now, randomBytes, createServer, request, setTimer, clearTimer });
    this.instanceId = randomBytes(18).toString("hex"); this.token = randomBytes(32).toString("base64url"); this.startedAt = now();
    this.server = null; this.port = null; this.timer = null; this.replays = new Map(); this.rates = new Map(); this.state = "idle";
  }

  async start() {
    if (this.server) return this.status();
    fs.mkdirSync(this.root, { recursive: true, mode: 0o700 }); this.cleanup();
    const server = this.createServer((request, response) => this._receive(request, response));
    server.requestTimeout = 5000; server.headersTimeout = 5000; server.keepAliveTimeout = 1000; server.maxConnections = 8;
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
    const address = server.address();
    if (!address || address.address !== "127.0.0.1" || !Number.isInteger(address.port)) { server.close(); throw new CompanionLinkError("companion_link_bind_invalid"); }
    this.server = server; this.port = address.port; this._publish(); this._schedule();
    return this.status();
  }

  async stop() {
    if (this.timer) { this.clearTimer(this.timer); this.timer = null; }
    this._removeOwnedFiles();
    const server = this.server; this.server = null; this.port = null;
    if (server) await new Promise((resolve) => server.close(resolve));
    this.replays.clear(); this.rates.clear();
    return this.status();
  }

  status() { return Object.freeze({ enabled: Boolean(this.server), character_id: this.characterId, state: this.state, peer_count: this.peers().length, protocol_version: PROTOCOL_VERSION }); }
  setAllowed(values) { this.allowed = new Set(values.map(requireId)); return this.status(); }
  setState(value) { if (!STATES.has(value)) throw new CompanionLinkError("companion_link_state_invalid"); this.state = value; if (this.server) this._publish(); return this.status(); }

  peers() {
    if (!fs.existsSync(this.root)) return [];
    this.cleanup();
    const peers = [];
    for (const name of fs.readdirSync(this.root)) {
      if (!/^instance-[a-f0-9]{36}\.json$/.test(name) || name === this._recordName()) continue;
      try {
        const record = readRegularJson(path.join(this.root, name), 4096);
        validateRecord(record, this.now());
        if (!this.allowed.has(record.character_id)) continue;
        const token = readRegularText(path.join(this.root, `instance-${record.instance_id}.token`), 128);
        if (sha256(token) !== record.token_hash) continue;
        peers.push(Object.freeze({ instance_id: record.instance_id, character_id: record.character_id, state: record.state, protocol_version: record.protocol_version, port: record.port, token }));
      } catch {}
    }
    return peers;
  }

  send(instanceId, message) {
    const peer = this.peers().find((item) => item.instance_id === instanceId);
    if (!peer) return Promise.reject(new CompanionLinkError("companion_link_peer_unavailable"));
    const payload = normalizeMessage({ ...message, protocol_version: PROTOCOL_VERSION, instance_id: this.instanceId, character_id: this.characterId, message_id: crypto.randomUUID() });
    const body = Buffer.from(JSON.stringify(payload));
    return new Promise((resolve, reject) => {
      const req = this.request({ hostname: "127.0.0.1", port: peer.port, path: "/v1/events", method: "POST", headers: { Host: `127.0.0.1:${peer.port}`, Authorization: `Bearer ${peer.token}`, "Content-Type": "application/json", "Content-Length": body.length } }, (res) => {
        const chunks = []; let size = 0;
        res.on("data", (chunk) => { size += chunk.length; if (size <= 4096) chunks.push(chunk); });
        res.on("end", () => { if (res.statusCode !== 200 || size > 4096) return reject(new CompanionLinkError("companion_link_peer_rejected")); try { const value = JSON.parse(Buffer.concat(chunks).toString("utf8")); if (!value || Object.keys(value).sort().join() !== "protocol_version,status" || value.status !== "accepted" || !Number.isInteger(value.protocol_version)) throw new Error(); resolve(value); } catch { reject(new CompanionLinkError("companion_link_peer_invalid")); } });
      });
      req.setTimeout(3000, () => req.destroy(new CompanionLinkError("companion_link_timeout"))); req.on("error", reject); req.end(body);
    });
  }

  cleanup() {
    if (!fs.existsSync(this.root)) return;
    const instances = new Set(fs.readdirSync(this.root).map((name) => /^instance-([a-f0-9]{36})\.(?:json|token)$/.exec(name)?.[1]).filter(Boolean));
    for (const instance of instances) {
      const recordPath = path.join(this.root, `instance-${instance}.json`);
      const tokenPath = path.join(this.root, `instance-${instance}.token`);
      try {
        const recordInfo = fs.lstatSync(recordPath); const tokenInfo = fs.lstatSync(tokenPath);
        if (!recordInfo.isFile() || recordInfo.isSymbolicLink() || !tokenInfo.isFile() || tokenInfo.isSymbolicLink()) continue;
        let expired = false;
        try { const value = readRegularJson(recordPath, 4096); expired = !Number.isFinite(value.expires_at) || value.expires_at <= this.now(); } catch { expired = true; }
        if (expired) { fs.rmSync(recordPath, { force: true }); fs.rmSync(tokenPath, { force: true }); }
      } catch {}
    }
  }

  _receive(request, response) {
    const reject = (status, code) => { response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" }); response.end(JSON.stringify({ status: "rejected", reason: code })); };
    if (request.socket.remoteAddress !== "127.0.0.1" || request.method !== "POST" || request.url !== "/v1/events" || request.headers.host !== `127.0.0.1:${this.port}` || request.headers.authorization !== `Bearer ${this.token}` || request.headers["content-type"] !== "application/json") return reject(403, "boundary");
    const declared = Number(request.headers["content-length"]); if (!Number.isInteger(declared) || declared < 2 || declared > 4096) return reject(413, "size");
    const chunks = []; let size = 0;
    request.on("data", (chunk) => { size += chunk.length; if (size <= 4096) chunks.push(chunk); else request.destroy(); });
    request.on("end", () => {
      try {
        if (size !== declared) throw new CompanionLinkError("companion_link_size_invalid");
        const message = normalizeMessage(JSON.parse(Buffer.concat(chunks).toString("utf8")));
        if (!this.allowed.has(message.character_id)) return reject(403, "character");
        if (message.protocol_version !== PROTOCOL_VERSION && !["hello", "bye"].includes(message.type)) return reject(409, "version");
        if (this._isReplay(message) || !this._rateAllowed(message.instance_id)) return reject(429, "rate_or_replay");
        if (!(this.quiet() && message.type === "short_line")) this.onEvent(publicEvent(message));
        response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" }); response.end(JSON.stringify({ status: "accepted", protocol_version: PROTOCOL_VERSION }));
      } catch (error) { reject(400, error instanceof CompanionLinkError ? error.code : "payload"); }
    });
  }

  _isReplay(message) { const key = `${message.instance_id}:${message.message_id}`; if (this.replays.has(key)) return true; this.replays.set(key, this.now()); while (this.replays.size > 256) this.replays.delete(this.replays.keys().next().value); return false; }
  _rateAllowed(instance) { const cutoff = this.now() - 60_000; const values = (this.rates.get(instance) || []).filter((value) => value > cutoff); if (values.length >= 6) return false; values.push(this.now()); this.rates.set(instance, values); return true; }
  _schedule() { this.timer = this.setTimer(() => { this.timer = null; if (this.server) { this._publish(); this.cleanup(); this._schedule(); } }, 15_000); }
  _publish() {
    const observedState = this.stateProvider(); if (!STATES.has(observedState)) throw new CompanionLinkError("companion_link_state_invalid"); this.state = observedState;
    const record = { protocol_version: PROTOCOL_VERSION, instance_id: this.instanceId, character_id: this.characterId, port: this.port, token_hash: sha256(this.token), state: this.state, started_at: this.startedAt, updated_at: this.now(), expires_at: this.now() + RECORD_TTL_MS };
    atomicWrite(path.join(this.root, this._recordName()), `${JSON.stringify(record)}\n`, 0o600); atomicWrite(path.join(this.root, `instance-${this.instanceId}.token`), `${this.token}\n`, 0o600);
  }
  _recordName() { return `instance-${this.instanceId}.json`; }
  _removeOwnedFiles() { for (const suffix of ["json", "token"]) { try { fs.rmSync(path.join(this.root, `instance-${this.instanceId}.${suffix}`), { force: true }); } catch {} } }
}

function normalizeMessage(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new CompanionLinkError("companion_link_message_invalid");
  const common = ["protocol_version", "instance_id", "character_id", "message_id", "type"];
  if (!Number.isInteger(value.protocol_version) || !/^[a-f0-9-]{36}$/.test(value.message_id || "") || !/^[a-f0-9]{36}$/.test(value.instance_id || "") || !ID_PATTERN.test(value.character_id || "") || !MESSAGE_TYPES.has(value.type)) throw new CompanionLinkError("companion_link_message_invalid");
  const extra = value.type === "state_changed" ? ["state"] : value.type === "emote" ? ["emote"] : value.type === "short_line" ? ["text"] : [];
  if (Object.keys(value).sort().join() !== [...common, ...extra].sort().join()) throw new CompanionLinkError("companion_link_message_invalid");
  if (extra[0] === "state" && !STATES.has(value.state)) throw new CompanionLinkError("companion_link_message_invalid");
  if (extra[0] === "emote" && !EMOTES.has(value.emote)) throw new CompanionLinkError("companion_link_message_invalid");
  if (extra[0] === "text" && (typeof value.text !== "string" || !value.text.trim() || [...value.text].length > 200 || FORBIDDEN_TEXT.test(value.text))) throw new CompanionLinkError("companion_link_text_rejected");
  return Object.freeze({ ...value, ...(value.text ? { text: value.text.trim() } : {}) });
}
function publicEvent(value) { return Object.freeze({ type: value.type, character_id: value.character_id, ...(value.state ? { state: value.state } : {}), ...(value.emote ? { emote: value.emote } : {}), ...(value.text ? { text: value.text } : {}) }); }
function validateRecord(value, now) { if (!value || Object.keys(value).sort().join() !== "character_id,expires_at,instance_id,port,protocol_version,started_at,state,token_hash,updated_at" || !Number.isInteger(value.protocol_version) || !/^[a-f0-9]{36}$/.test(value.instance_id || "") || !ID_PATTERN.test(value.character_id || "") || !Number.isInteger(value.port) || value.port < 1024 || value.port > 65535 || !/^[a-f0-9]{64}$/.test(value.token_hash || "") || !STATES.has(value.state) || !Number.isFinite(value.started_at) || !Number.isFinite(value.updated_at) || value.started_at > value.updated_at || !Number.isFinite(value.expires_at) || value.expires_at <= now) throw new CompanionLinkError("companion_link_record_invalid"); }
function requireId(value) { if (typeof value !== "string" || !ID_PATTERN.test(value)) throw new CompanionLinkError("companion_link_character_invalid"); return value; }
function sha256(value) { return crypto.createHash("sha256").update(value.trim(), "utf8").digest("hex"); }
function readRegularJson(target, max) { return JSON.parse(readRegularText(target, max)); }
function readRegularText(target, max) { const info = fs.lstatSync(target); if (!info.isFile() || info.isSymbolicLink() || info.size > max) throw new Error(); return fs.readFileSync(target, "utf8").trim(); }
function atomicWrite(target, content, mode) { const temporary = `${target}.${process.pid}.tmp`; try { fs.writeFileSync(temporary, content, { encoding: "utf8", mode }); fs.renameSync(temporary, target); } finally { try { fs.rmSync(temporary, { force: true }); } catch {} } }
function frozenSettings(value = {}) { return Object.freeze({ schema_version: 1, enabled: false, consented: false, character_id: "chriptmas.bear", allowed_character_ids: Object.freeze([]), revision: 0, ...value, allowed_character_ids: Object.freeze([...(value.allowed_character_ids || [])]) }); }

module.exports = { CompanionLinkError, CompanionMultiCharacterLink, CompanionMultiCharacterSettings, EMOTES, MESSAGE_TYPES, PROTOCOL_VERSION, STATES, normalizeMessage };
