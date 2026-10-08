class CompanionMemoryAttentionController {
  constructor({ present, isQuiet }) {
    if (typeof present !== "function" || typeof isQuiet !== "function") throw new TypeError("memory attention handlers are required");
    this.present = present;
    this.isQuiet = isQuiet;
    this.pendingCount = 0;
    this.notifiedInEpoch = false;
  }

  update(payload = {}) {
    const raw = Number(payload.pending_memory_candidate_count);
    const nextCount = Number.isSafeInteger(raw) && raw > 0 ? Math.min(raw, 999) : 0;
    this.pendingCount = nextCount;
    if (nextCount === 0) {
      this.notifiedInEpoch = false;
      return Object.freeze({ status: "idle", pending_count: 0 });
    }
    if (this.notifiedInEpoch) return Object.freeze({ status: "already_notified", pending_count: nextCount });
    if (this.isQuiet()) return Object.freeze({ status: "suppressed", pending_count: nextCount });
    this.present(Object.freeze({
      event_id: `memory-review-${nextCount}`,
      kind: "memory_review_ready",
      visual_state: "attention",
      text: `有 ${nextCount} 条记忆候选等你确认。确认前，它们不会进入长期记忆。`,
      actions: Object.freeze([Object.freeze({ id: "memory.review", label: "去审阅" })]),
      requires_ack: false,
    }));
    this.notifiedInEpoch = true;
    return Object.freeze({ status: "presented", pending_count: nextCount });
  }

  hasPending() {
    return this.pendingCount > 0;
  }
}

module.exports = { CompanionMemoryAttentionController };
