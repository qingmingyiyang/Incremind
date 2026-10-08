class CompanionSystemSensorRuntimeController {
  constructor({ readStatus, sample, probeNetwork, onSnapshot, onGameModeChanged, onAlert, onError = () => {}, setIntervalFn = setInterval, clearIntervalFn = clearInterval, intervalMs = 5000 }) {
    if ([readStatus, sample, probeNetwork, onSnapshot, onGameModeChanged, onAlert, onError, setIntervalFn, clearIntervalFn].some((value) => typeof value !== "function")) throw new TypeError("system sensor handlers are required");
    if (!Number.isInteger(intervalMs) || intervalMs < 1000) throw new TypeError("system sensor interval is invalid");
    Object.assign(this, { readStatus, sample, probeNetwork, onSnapshot, onGameModeChanged, onAlert, onError, setIntervalFn, clearIntervalFn, intervalMs });
    this.timer = null;
    this.inFlight = false;
    this.status = null;
    this.game = Object.freeze({ active: false, behavior: "quiet" });
  }

  async refresh() {
    try {
      const status = await this.readStatus();
      this.status = status;
      if (status?.config?.enabled === true) this.start(); else this.stop();
      return status;
    } catch (error) {
      this.onError(error);
      return null;
    }
  }

  start() {
    if (this.timer !== null) return false;
    this.timer = this.setIntervalFn(() => void this.tick(), this.intervalMs);
    void this.tick();
    return true;
  }

  stop() {
    if (this.timer === null) return false;
    this.clearIntervalFn(this.timer);
    this.timer = null;
    return true;
  }

  async tick() {
    if (this.inFlight) return null;
    this.inFlight = true;
    try {
      const config = this.status?.config || (await this.readStatus())?.config;
      if (config?.enabled !== true) { this.stop(); return null; }
      const network = config.network_enabled === true
        ? await this.probeNetwork(config.health_origin || null)
        : { network_state: "unknown", latency_ms: null };
      const snapshot = await this.sample(network);
      this.status = snapshot;
      this.onSnapshot(snapshot);
      const nextGame = Object.freeze({ active: snapshot?.sample?.game_active === true, behavior: snapshot?.config?.game_behavior || "quiet" });
      if (nextGame.active !== this.game.active || nextGame.behavior !== this.game.behavior) {
        const previous = this.game;
        this.game = nextGame;
        this.onGameModeChanged(nextGame, previous);
      }
      if (snapshot?.sample?.should_alert === true) this.onAlert(snapshot.sample);
      return snapshot;
    } catch (error) {
      this.onError(error);
      return null;
    } finally {
      this.inFlight = false;
    }
  }

  isGameQuiet() { return this.game.active; }
  dispose() { this.stop(); }
}

module.exports = { CompanionSystemSensorRuntimeController };
