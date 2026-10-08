class CompanionReplyPresentationController {
  constructor({
    overlayController,
    appearanceArbiter,
    isQuiet,
    isSleeping,
    speak,
    setTimeoutFn = setTimeout,
    clearTimeoutFn = clearTimeout,
    now = Date.now,
  }) {
    if (!overlayController || typeof overlayController.present !== "function"
      || !appearanceArbiter || typeof appearanceArbiter.set !== "function" || typeof appearanceArbiter.clear !== "function") {
      throw new TypeError("companion_reply_presentation_surface_invalid");
    }
    if ([isQuiet, isSleeping, speak, setTimeoutFn, clearTimeoutFn, now].some((value) => typeof value !== "function")) {
      throw new TypeError("companion_reply_presentation_boundary_invalid");
    }
    this.overlayController = overlayController;
    this.appearanceArbiter = appearanceArbiter;
    this.isQuiet = isQuiet;
    this.isSleeping = isSleeping;
    this.speak = speak;
    this.setTimeout = setTimeoutFn;
    this.clearTimeout = clearTimeoutFn;
    this.now = now;
    this.timer = null;
  }

  present(value, { speak = true } = {}) {
    if (typeof value !== "string" || !value.trim() || value.length > 4_000 || value.includes("\0")) {
      throw new Error("companion_reply_invalid");
    }
    const text = value.trim().slice(0, 400);
    this.overlayController.present({
      event_id: `chat-reply:${this.now().toString(36)}`,
      kind: "chat_reply",
      visual_state: "speaking",
      text,
      actions: [],
      requires_ack: false,
    }, { focus: false });
    const speechDuration = Math.min(8_000, Math.max(1_500, text.length * 80));
    if (!this.isQuiet()) {
      this.appearanceArbiter.set(
        "interactive",
        { state: "speaking", mood: "calm", animation_key: "talk" },
        { ttlMs: speechDuration },
      );
    }
    if (this.timer !== null) this.clearTimeout(this.timer);
    this.timer = this.setTimeout(() => {
      this.timer = null;
      this.appearanceArbiter.clear("interactive");
    }, speechDuration);
    if (speak && !this.isQuiet() && !this.isSleeping()) void this.speak(text);
    return Object.freeze({ status: "presented" });
  }

  cancel() {
    if (this.timer !== null) {
      this.clearTimeout(this.timer);
      this.timer = null;
    }
    this.appearanceArbiter.clear("interactive");
  }

  dispose() {
    this.cancel();
  }
}

module.exports = { CompanionReplyPresentationController };
