const ACTION_LABELS = Object.freeze({ acknowledge: "知道了", snooze_5m: "5 分钟后提醒", complete: "已完成" });

class CompanionEventConsumer {
  constructor({ readEvent, onEvent, onError = () => {}, canRead = () => true, setIntervalFn = setInterval, clearIntervalFn = clearInterval, intervalMs = 1000 }) {
    if (typeof readEvent !== "function" || typeof onEvent !== "function" || typeof canRead !== "function") throw new TypeError("companion event consumer handlers are required");
    if (!Number.isInteger(intervalMs) || intervalMs < 250 || intervalMs > 10000) throw new TypeError("companion event consumer interval is invalid");
    this.readEvent = readEvent;
    this.onEvent = onEvent;
    this.onError = onError;
    this.canRead = canRead;
    this.setIntervalFn = setIntervalFn;
    this.clearIntervalFn = clearIntervalFn;
    this.intervalMs = intervalMs;
    this.timer = null;
    this.inFlight = false;
  }

  start() {
    if (this.timer !== null) return false;
    this.timer = this.setIntervalFn(() => { void this.tick(); }, this.intervalMs);
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
    if (this.inFlight || !this.canRead()) return null;
    this.inFlight = true;
    try {
      const event = await this.readEvent();
      if (event) await this.onEvent(event);
      return event || null;
    } catch (error) {
      this.onError(error);
      return null;
    } finally {
      this.inFlight = false;
    }
  }
}

class CompanionReminderPresenter {
  constructor({ windowProvider, overlayController, beep, setTimeoutFn = setTimeout, clearTimeoutFn = clearTimeout }) {
    if (typeof windowProvider !== "function" || !overlayController || typeof beep !== "function") throw new TypeError("reminder presenter dependencies are required");
    this.windowProvider = windowProvider;
    this.overlayController = overlayController;
    this.beep = beep;
    this.setTimeoutFn = setTimeoutFn;
    this.clearTimeoutFn = clearTimeoutFn;
    this.activeEvent = null;
    this.previousAlwaysOnTop = null;
    this.beepTimers = [];
  }

  isBlocking() { return this.activeEvent?.requires_ack === true; }

  present(event) {
    const actions = event.actions.map((id) => ({ id, label: ACTION_LABELS[id] })).filter((item) => item.label);
    const projection = {
      event_id: event.event_id, kind: event.kind, visual_state: event.visual_state,
      text: event.text, actions, requires_ack: event.requires_ack === true,
    };
    this.activeEvent = event;
    if (projection.requires_ack) {
      const window = this.windowProvider();
      if (window && !window.isDestroyed()) {
        this.previousAlwaysOnTop = window.isAlwaysOnTop();
        window.setAlwaysOnTop(true);
        if (typeof window.show === "function") window.show();
        if (typeof window.focus === "function") window.focus();
      }
      this._startFiniteBeep();
    }
    this.overlayController.present(projection, { focus: projection.requires_ack });
    return projection;
  }

  settle(eventId) {
    if (!this.activeEvent || this.activeEvent.event_id !== eventId) return false;
    this.overlayController.close({ force: true });
    this._clearBeeps();
    this._restoreWindow();
    this.activeEvent = null;
    return true;
  }

  shutdown() {
    this._clearBeeps();
    this._restoreWindow();
    this.activeEvent = null;
  }

  _startFiniteBeep() {
    this._clearBeeps();
    const invoke = () => { try { this.beep(); } catch {} };
    invoke();
    this.beepTimers = [2000, 4000].map((delay) => this.setTimeoutFn(invoke, delay));
  }

  _clearBeeps() {
    for (const timer of this.beepTimers) this.clearTimeoutFn(timer);
    this.beepTimers = [];
  }

  _restoreWindow() {
    const window = this.windowProvider();
    if (this.previousAlwaysOnTop !== null && window && !window.isDestroyed()) window.setAlwaysOnTop(this.previousAlwaysOnTop);
    this.previousAlwaysOnTop = null;
  }
}

module.exports = { ACTION_LABELS, CompanionEventConsumer, CompanionReminderPresenter };
