const CONDITIONS = new Set(["clear", "cloudy", "rain", "snow", "extreme", "unknown"]);

class CompanionWeatherRuntimeController {
  constructor({ readStatus, onProjection, onExtreme, quiet = () => false, onError = () => {}, setIntervalFn = setInterval, clearIntervalFn = clearInterval, intervalMs = 60000 }) {
    if ([readStatus, onProjection, onExtreme, quiet, onError, setIntervalFn, clearIntervalFn].some((value) => typeof value !== "function")) throw new TypeError("weather runtime handlers are required");
    if (!Number.isInteger(intervalMs) || intervalMs < 1000) throw new TypeError("weather runtime interval is invalid");
    Object.assign(this, { readStatus, onProjection, onExtreme, quiet, onError, setIntervalFn, clearIntervalFn, intervalMs });
    this.timer = null;
    this.inFlight = false;
    this.lastExtreme = null;
    this.projection = Object.freeze({ condition: "unknown", is_day: null, stale: true, revision: 0 });
  }

  async refresh() {
    if (this.inFlight) return null;
    this.inFlight = true;
    try {
      const status = await this.readStatus();
      return this.apply(status);
    } catch (error) {
      this.onError(error);
      return null;
    } finally {
      this.inFlight = false;
    }
  }

  apply(status) {
    const enabled = status?.config?.enabled === true;
    const weather = status?.weather || {};
    const condition = enabled && CONDITIONS.has(weather.condition) ? weather.condition : "unknown";
    const revision = boundedRevision(weather.revision);
    if (enabled && revision < this.projection.revision) {
      this.start();
      return status;
    }
    const projection = Object.freeze({
      condition,
      is_day: enabled && typeof weather.is_day === "boolean" ? weather.is_day : null,
      stale: enabled && condition !== "unknown" ? weather.stale === true : true,
      revision,
    });
    this.projection = projection;
    this.onProjection(projection);
    if (enabled) this.start(); else this.stop();
    const extremeKey = condition === "extreme" && typeof weather.fetched_at === "string" && weather.fetched_at.length <= 40
      ? `${projection.revision}:${weather.fetched_at}`
      : null;
    if (extremeKey && extremeKey !== this.lastExtreme) {
      this.lastExtreme = extremeKey;
      if (!this.quiet()) this.onExtreme(Object.freeze({ condition: "extreme", revision: projection.revision }));
    }
    return status;
  }

  start() {
    if (this.timer !== null) return false;
    this.timer = this.setIntervalFn(() => void this.refresh(), this.intervalMs);
    return true;
  }

  stop() {
    if (this.timer === null) return false;
    this.clearIntervalFn(this.timer);
    this.timer = null;
    return true;
  }

  current() { return this.projection; }

  dispose() {
    this.stop();
    this.projection = Object.freeze({ condition: "unknown", is_day: null, stale: true, revision: 0 });
    this.onProjection(this.projection);
  }
}

function boundedRevision(value) {
  return Number.isSafeInteger(value) && value >= 0 ? Math.min(value, 2_147_483_647) : 0;
}

module.exports = { CONDITIONS, CompanionWeatherRuntimeController };
