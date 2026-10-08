class CompanionAmbientRuntimeController {
  constructor({ offer, choose, idle, present, flags = () => ({}), onError = () => {}, setIntervalFn = setInterval, clearIntervalFn = clearInterval, intervalMs = 60000 }) {
    if ([offer, choose, idle, present, flags, onError].some((value) => typeof value !== "function")) throw new TypeError("ambient runtime handlers are required");
    if (!Number.isInteger(intervalMs) || intervalMs < 1000) throw new TypeError("ambient runtime interval is invalid");
    Object.assign(this, { offer, choose, idle, present, flags, onError, setIntervalFn, clearIntervalFn, intervalMs });
    this.timer = null; this.inFlight = false; this.active = null; this.idleAnnounced = false;
  }
  start() { if (this.timer !== null) return false; this.timer = this.setIntervalFn(() => void this.tick(), this.intervalMs); void this.tick(); return true; }
  stop() { if (this.timer === null) return false; this.clearIntervalFn(this.timer); this.timer = null; return true; }
  async tick() {
    if (this.inFlight || this.active) return null;
    this.inFlight = true;
    try {
      const state = this.flags() || {};
      const idleResult = await this.idle({ idle_seconds: Number.isSafeInteger(state.idleSeconds) ? state.idleSeconds : 0, quiet: state.quiet === true, game: state.game === true, sleeping: state.sleeping === true });
      if (idleResult?.active !== true) this.idleAnnounced = false;
      if (idleResult?.message && !this.idleAnnounced) {
        this.idleAnnounced = true;
        this.present({ event_id: `idle_${Date.now()}`, kind: "ambient_idle", visual_state: "attention", text: idleResult.message, actions: [], requires_ack: false });
        return idleResult;
      }
      const result = await this.offer({ require_due: true, quiet: state.quiet === true, game: state.game === true, sleeping: state.sleeping === true });
      if (!result?.event || result.event.state !== "offered") return null;
      this.active = result.event;
      this.present({ event_id: result.event.event_id, kind: "random_event", visual_state: "attention", text: result.event.scene, actions: result.event.options.map((option) => ({ id: option.id, label: option.label })), requires_ack: false });
      return result.event;
    } catch (error) { this.onError(error); return null; }
    finally { this.inFlight = false; }
  }
  owns(eventId, actionId) { return this.active?.event_id === eventId && this.active.options.some((option) => option.id === actionId); }
  async act(eventId, actionId) {
    if (!this.owns(eventId, actionId)) throw new Error("ambient_action_not_current");
    const result = await this.choose(eventId, actionId, this.active.revision);
    this.active = null;
    const event = result.event;
    this.present({ event_id: `${event.event_id}_result`, kind: "random_event_result", visual_state: "happy", text: event.result.text, actions: [], requires_ack: false });
    return result;
  }
}

module.exports = { CompanionAmbientRuntimeController };
