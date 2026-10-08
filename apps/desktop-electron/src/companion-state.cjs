const COMPANION_STATES = Object.freeze(["booting", "ready", "working", "attention", "offline", "speaking", "sleeping", "warning"]);
const SAFE_MOODS = new Set(["calm", "focused", "curious", "analyzing", "idle"]);
const SAFE_ANIMATIONS = new Set(["idle", "talk", "listen", "sleep", "warn", "offline"]);
const PROJECTION_KEYS = new Set(["state", "mood", "revision", "animation_key"]);
const EXECUTION_SIGNALS = new Set(["active:valid", "active:unavailable", "inactive:none", "unavailable:unavailable"]);

function projectionFromMood(payload = {}) {
  const mood = SAFE_MOODS.has(payload.mood) ? payload.mood : "calm";
  const execution = sanitiseExecutionSignal(payload.execution);
  if (execution?.effect === "active" && execution.lease === "valid") {
    return { state: "working", mood: "analyzing" };
  }
  if (execution?.effect === "active" && execution.lease === "unavailable") {
    return { state: "attention", mood: "focused" };
  }
  if (execution?.effect === "unavailable" && execution.lease === "unavailable") {
    return { state: "offline", mood: "idle" };
  }
  if (mood === "focused" || mood === "curious") {
    return { state: "attention", mood };
  }
  return { state: "ready", mood: mood === "idle" ? "idle" : "calm" };
}

function sanitiseExecutionSignal(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (Object.keys(value).some((key) => !["effect", "lease"].includes(key))) return null;
  if (!EXECUTION_SIGNALS.has(`${value.effect}:${value.lease}`)) return null;
  return Object.freeze({ effect: value.effect, lease: value.lease });
}

function sanitiseProjection(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  if (Object.keys(payload).some((key) => !PROJECTION_KEYS.has(key))) return null;
  if (!COMPANION_STATES.includes(payload.state) || !SAFE_MOODS.has(payload.mood)) return null;
  if (payload.revision !== undefined && (!Number.isSafeInteger(payload.revision) || payload.revision < 1)) return null;
  if (payload.animation_key !== undefined && !SAFE_ANIMATIONS.has(payload.animation_key)) return null;
  return Object.freeze({
    state: payload.state,
    mood: payload.mood,
    ...(payload.animation_key === undefined ? {} : { animation_key: payload.animation_key }),
    ...(payload.revision === undefined ? {} : { revision: payload.revision }),
  });
}

class CompanionStateController {
  constructor({ readProjection, emitProjection, intervalMs = 15000, setIntervalImpl = setInterval, clearIntervalImpl = clearInterval }) {
    this.readProjection = readProjection;
    this.emitProjection = emitProjection;
    this.intervalMs = intervalMs;
    this.setIntervalImpl = setIntervalImpl;
    this.clearIntervalImpl = clearIntervalImpl;
    this.timer = null;
    this.inFlight = false;
    this.revision = 0;
    this.current = Object.freeze({ state: "booting", mood: "idle", revision: 0 });
  }

  publish(next, { force = false } = {}) {
    const safe = sanitiseProjection(next);
    if (!safe) return false;
    const nextAnimation = safe.animation_key ?? null;
    const currentAnimation = this.current.animation_key ?? null;
    const changed = safe.state !== this.current.state || safe.mood !== this.current.mood || nextAnimation !== currentAnimation;
    if (safe.revision !== undefined) {
      if (safe.revision < this.revision) return false;
      if (safe.revision === this.revision) {
        if (changed) return false;
        if (force) this.emitProjection(this.current);
        return false;
      }
      this.revision = safe.revision;
      this.current = Object.freeze({
        state: safe.state,
        mood: safe.mood,
        ...(safe.animation_key === undefined ? {} : { animation_key: safe.animation_key }),
        revision: this.revision,
      });
      this.emitProjection(this.current);
      return true;
    }
    if (changed) {
      this.revision += 1;
      this.current = Object.freeze({
        state: safe.state,
        mood: safe.mood,
        ...(safe.animation_key === undefined ? {} : { animation_key: safe.animation_key }),
        revision: this.revision,
      });
    }
    if (changed || force) this.emitProjection(this.current);
    return changed;
  }

  deliverCurrent() {
    this.publish(this.current, { force: true });
  }

  async poll() {
    if (this.inFlight) return;
    this.inFlight = true;
    try {
      this.publish(await this.readProjection());
    } catch {
      this.publish({ state: "offline", mood: "idle" });
    } finally {
      this.inFlight = false;
    }
  }

  start() {
    if (this.timer !== null) return;
    void this.poll();
    this.timer = this.setIntervalImpl(() => void this.poll(), this.intervalMs);
  }

  markOffline() {
    this.publish({ state: "offline", mood: "idle" });
  }

  stop() {
    if (this.timer !== null) this.clearIntervalImpl(this.timer);
    this.timer = null;
  }
}

module.exports = { COMPANION_STATES, CompanionStateController, projectionFromMood, sanitiseExecutionSignal, sanitiseProjection };
