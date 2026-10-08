const OUTFITS = new Set(["default", "red-scarf", "gold-star"]);
const BACKGROUNDS = new Set(["default", "night"]);
const STAGES = new Set(["new", "friend", "partner", "confidant", "bonded"]);
const IDLES = new Set(["default", "warm", "smile", "bright", "radiant"]);

function sanitizeAppearanceProjection(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (Object.keys(value).sort().join() !== "background_id,growth_stage,idle_variant,outfit_id,state_revision") return null;
  if (!OUTFITS.has(value.outfit_id) || !BACKGROUNDS.has(value.background_id) || !STAGES.has(value.growth_stage) || !IDLES.has(value.idle_variant)) return null;
  if (!Number.isSafeInteger(value.state_revision) || value.state_revision < 1) return null;
  return Object.freeze({ outfit_id: value.outfit_id, background_id: value.background_id, growth_stage: value.growth_stage, idle_variant: value.idle_variant, revision: value.state_revision });
}

class CompanionAppearanceRuntimeController {
  constructor({ readStatus, onProjection, onError = () => {}, intervalMs = 3000, setIntervalImpl = setInterval, clearIntervalImpl = clearInterval }) {
    this.readStatus = readStatus; this.onProjection = onProjection; this.intervalMs = intervalMs;
    this.setIntervalImpl = setIntervalImpl; this.clearIntervalImpl = clearIntervalImpl; this.onError = onError;
    this.timer = null; this.inFlight = false; this.current = Object.freeze({ outfit_id: "default", background_id: "default", growth_stage: "new", idle_variant: "default", revision: 1 });
  }
  async refresh() {
    if (this.inFlight) return false; this.inFlight = true;
    try {
      const next = sanitizeAppearanceProjection(await this.readStatus());
      if (!next) return false;
      const changed = JSON.stringify(next) !== JSON.stringify(this.current);
      this.current = next; if (changed) this.onProjection(next); return changed;
    } catch (error) { this.onError(error); return false; }
    finally { this.inFlight = false; }
  }
  deliverCurrent() { this.onProjection(this.current); }
  start() { if (this.timer !== null) return; void this.refresh(); this.timer = this.setIntervalImpl(() => void this.refresh(), this.intervalMs); }
  stop() { if (this.timer !== null) this.clearIntervalImpl(this.timer); this.timer = null; }
}

module.exports = { CompanionAppearanceRuntimeController, sanitizeAppearanceProjection };
