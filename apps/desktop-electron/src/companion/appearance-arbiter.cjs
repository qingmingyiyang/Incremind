const PRIORITY = Object.freeze({ idle: 100, schedule: 200, interactive: 300, motion: 400, system: 450, critical: 500 });
const CHANNELS = new Set(Object.keys(PRIORITY));
const STATES = new Set(["booting", "ready", "working", "attention", "offline", "speaking", "sleeping", "warning"]);
const MOODS = new Set(["calm", "focused", "curious", "analyzing", "idle"]);
const ANIMATIONS = new Set(["idle", "talk", "listen", "sleep", "warn", "offline", "drag", "falling", "hang_left", "hang_right"]);

class CompanionAppearanceArbiter {
  constructor({ emit, now = Date.now, setTimeoutImpl = setTimeout, clearTimeoutImpl = clearTimeout }) {
    if (typeof emit !== "function") throw new TypeError("appearance arbiter emit is required");
    this.emit = emit;
    this.now = now;
    this.setTimeoutImpl = setTimeoutImpl;
    this.clearTimeoutImpl = clearTimeoutImpl;
    this.lanes = new Map();
    this.timers = new Map();
    this.revision = 0;
    this.current = null;
  }

  set(channel, projection, { ttlMs = null } = {}) {
    requireChannel(channel);
    const safe = safeProjection(projection);
    if (ttlMs !== null && (!Number.isSafeInteger(ttlMs) || ttlMs < 100 || ttlMs > 86_400_000)) throw new TypeError("appearance ttl is invalid");
    this._clearTimer(channel);
    this.lanes.set(channel, { projection: safe, expiresAt: ttlMs === null ? null : this.now() + ttlMs });
    if (ttlMs !== null) this.timers.set(channel, this.setTimeoutImpl(() => this.clear(channel), ttlMs));
    return this._emitResolved();
  }

  clear(channel) {
    requireChannel(channel);
    this._clearTimer(channel);
    const changed = this.lanes.delete(channel);
    if (changed) this._emitResolved();
    return changed;
  }

  resolve() {
    const now = this.now();
    for (const [channel, lane] of this.lanes) if (lane.expiresAt !== null && lane.expiresAt <= now) this.lanes.delete(channel);
    const selected = [...this.lanes.entries()].sort((left, right) => PRIORITY[right[0]] - PRIORITY[left[0]])[0];
    return selected ? { channel: selected[0], ...selected[1].projection } : null;
  }

  deliverCurrent() {
    if (this.current?.projection) this.emit(this.current.projection);
  }

  dispose() {
    for (const channel of [...this.timers.keys()]) this._clearTimer(channel);
    this.lanes.clear();
    this.current = null;
  }

  _emitResolved() {
    const resolved = this.resolve();
    const signature = resolved ? `${resolved.channel}\0${resolved.state}\0${resolved.mood}\0${resolved.animation_key || ""}` : "";
    if (signature === this.current?.signature) return false;
    this.revision += 1;
    this.current = resolved ? { signature, projection: Object.freeze({
      state: resolved.state,
      mood: resolved.mood,
      ...(resolved.animation_key ? { animation_key: resolved.animation_key } : {}),
      revision: this.revision,
    }) } : { signature, projection: null };
    if (this.current.projection) this.emit(this.current.projection);
    return true;
  }

  _clearTimer(channel) {
    if (this.timers.has(channel)) this.clearTimeoutImpl(this.timers.get(channel));
    this.timers.delete(channel);
  }
}

function safeProjection(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError("appearance projection is invalid");
  const allowed = new Set(["state", "mood", "animation_key", "revision"]);
  if (Object.keys(value).some((key) => !allowed.has(key))) throw new TypeError("appearance projection contains unsupported data");
  if (!STATES.has(value.state) || !MOODS.has(value.mood)) throw new TypeError("appearance projection state is invalid");
  if (value.animation_key !== undefined && !ANIMATIONS.has(value.animation_key)) throw new TypeError("appearance animation is invalid");
  return Object.freeze({ state: value.state, mood: value.mood, ...(value.animation_key ? { animation_key: value.animation_key } : {}) });
}

function requireChannel(value) { if (!CHANNELS.has(value)) throw new TypeError("appearance channel is invalid"); }

module.exports = { CompanionAppearanceArbiter, PRIORITY, safeProjection };
