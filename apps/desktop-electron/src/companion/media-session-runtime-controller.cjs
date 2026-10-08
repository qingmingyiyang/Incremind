const crypto = require("node:crypto");

const DELAYS = Object.freeze([30000, 60000, 120000, 300000]);

class CompanionMediaSessionRuntimeController {
  constructor({ readStatus, observe, adapter, onProjection, onCommentary, quiet = () => false, onError = () => {}, setTimer = setTimeout, clearTimer = clearTimeout, now = () => new Date(), intervalMs = 10000 }) {
    if (![readStatus, observe, onProjection, onCommentary, quiet, onError, setTimer, clearTimer, now].every((item) => typeof item === "function") || !adapter?.sample || !adapter?.cancel) throw new TypeError("media runtime dependencies are invalid");
    if (!Number.isInteger(intervalMs) || intervalMs < 1000) throw new TypeError("media runtime interval is invalid");
    Object.assign(this, { readStatus, observe, adapter, onProjection, onCommentary, quiet, onError, setTimer, clearTimer, now, intervalMs });
    this.timer = null; this.inFlight = false; this.enabled = false; this.failures = 0;
    this.generation = 0;
    this.projection = frozenProjection("disabled");
  }

  async refreshConfig() {
    const generation = ++this.generation;
    try {
      const status = await this.readStatus();
      if (generation !== this.generation) return null;
      this.enabled = status?.config?.enabled === true;
      if (!this.enabled) { this.stop(); this.adapter.cancel(); this.project(frozenProjection("disabled")); return status; }
      this.schedule(0);
      return status;
    } catch (error) { if (generation !== this.generation) return null; this.onError(error); this.enabled = false; this.stop(); this.project(frozenProjection("unavailable")); return null; }
  }

  async poll() {
    if (!this.enabled || this.inFlight) return null;
    const generation = this.generation;
    this.inFlight = true;
    this.stop();
    try {
      const sample = await this.adapter.sample();
      if (!this.enabled || generation !== this.generation) return null;
      if (!["ready", "empty"].includes(sample?.status)) {
        this.failures = Math.min(this.failures + 1, DELAYS.length);
        this.project(frozenProjection(sample?.status === "error" ? "error" : "unavailable"));
        return sample;
      }
      const response = await this.observe({
        observation_id: `media:${crypto.randomUUID()}`,
        title: sample.title,
        artist: sample.artist,
        playback_status: sample.playback_status,
        quiet: this.quiet() === true,
      });
      if (!this.enabled || generation !== this.generation) return null;
      const result = response?.result;
      this.failures = 0;
      const projection = normalizeProjection(result, this.now);
      this.project(projection);
      if (projection.commentary) this.onCommentary(projection);
      return response;
    } catch (error) {
      this.failures = Math.min(this.failures + 1, DELAYS.length);
      if (this.enabled && generation === this.generation) {
        this.project(frozenProjection("error"));
        this.onError(error);
      }
      return null;
    } finally {
      this.inFlight = false;
      if (this.enabled && generation === this.generation) this.schedule(this.failures ? DELAYS[this.failures - 1] : this.intervalMs);
    }
  }

  schedule(delay) { if (!this.enabled || this.timer !== null) return false; this.timer = this.setTimer(() => { this.timer = null; void this.poll(); }, delay); return true; }
  stop() { if (this.timer === null) return false; this.clearTimer(this.timer); this.timer = null; return true; }
  current() { return this.projection; }
  project(value) { this.projection = value; this.onProjection(value); }
  dispose() { this.generation += 1; this.enabled = false; this.stop(); this.adapter.cancel(); this.project(frozenProjection("disabled")); }
}

function normalizeProjection(value, now = () => new Date()) {
  if (!value || typeof value !== "object") return frozenProjection("error");
  const status = new Set(["empty", "playing", "paused"]).has(value.status) ? value.status : "error";
  const title = safe(value.title, 160), artist = safe(value.artist, 160), commentary = value.commentary == null ? "" : safe(value.commentary, 160);
  if (title === null || artist === null || commentary === null || (status === "playing" && !title)) return frozenProjection("error");
  const timestamp = now();
  if (!(timestamp instanceof Date) || Number.isNaN(timestamp.valueOf())) return frozenProjection("error");
  return Object.freeze({ status, title, artist, playback_status: new Set(["playing", "paused", "stopped", "closed", "unknown"]).has(value.playback_status) ? value.playback_status : "unknown", commentary, commentary_source: new Set(["local", "model"]).has(value.commentary_source) ? value.commentary_source : null, updated_at: timestamp.toISOString() });
}
function safe(value, max) { return typeof value === "string" && value.length <= max && !/[\x00-\x1f\x7f]/.test(value) ? value.trim() : null; }
function frozenProjection(status) { return Object.freeze({ status, title: "", artist: "", playback_status: "unknown", commentary: "", commentary_source: null, updated_at: null }); }

module.exports = { CompanionMediaSessionRuntimeController, DELAYS, normalizeProjection };
