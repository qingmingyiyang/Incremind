const MICROPHONE_PURPOSES = Object.freeze(["companion-voice", "workbench-transcription"]);
const MICROPHONE_PERMISSION_TTL_MS = 60_000;

class CompanionMicrophonePermission {
  constructor({ mainWindowProvider, now = Date.now, ttlMs = MICROPHONE_PERMISSION_TTL_MS }) {
    if (typeof mainWindowProvider !== "function" || typeof now !== "function" || !Number.isInteger(ttlMs) || ttlMs < 1_000 || ttlMs > MICROPHONE_PERMISSION_TTL_MS) throw new TypeError("microphone permission dependencies are invalid");
    this.mainWindowProvider = mainWindowProvider;
    this.now = now;
    this.ttlMs = ttlMs;
    this.armedSender = null;
    this.purpose = null;
    this.expiresAt = 0;
  }

  arm(sender, { purpose = "companion-voice", transientActivation = false, topFrame = false } = {}) {
    if (!MICROPHONE_PURPOSES.includes(purpose)) throw new Error("microphone_purpose_denied");
    if (!this.isMainSender(sender) || transientActivation !== true || topFrame !== true || !this.isMainWindowActive()) throw new Error("companion_microphone_sender_denied");
    this.armedSender = sender;
    this.purpose = purpose;
    this.expiresAt = this.now() + this.ttlMs;
    return Object.freeze({ status: "armed", purpose, expires_at_ms: this.expiresAt });
  }

  decideCheck(sender, permission, details = {}) {
    const allowedShape = details?.mediaType === "audio" && details?.isMainFrame === true && this.isTrustedOrigin(details?.requestingOrigin);
    return allowedShape && this.isArmed(sender, permission);
  }

  decideRequest(sender, permission, details = {}) {
    const mediaTypes = Array.isArray(details?.mediaTypes) ? details.mediaTypes : [];
    const allowed = mediaTypes.length > 0 && mediaTypes.every((value) => value === "audio") && this.isArmed(sender, permission);
    if (allowed) this.clear();
    return allowed;
  }

  clear() { this.armedSender = null; this.purpose = null; this.expiresAt = 0; }
  isMainSender(sender) { const window = this.mainWindowProvider(); return Boolean(window && !window.isDestroyed?.() && sender === window.webContents); }
  isMainWindowActive() { const window = this.mainWindowProvider(); return Boolean(window && !window.isDestroyed?.() && window.isVisible?.() && window.isFocused?.()); }
  isArmed(sender, permission) { return permission === "media" && this.isMainSender(sender) && this.isMainWindowActive() && sender === this.armedSender && this.expiresAt > this.now(); }
  isTrustedOrigin(value) {
    if (typeof value !== "string" || !value) return false;
    try {
      const url = new URL(this.mainWindowProvider()?.webContents?.getURL?.() || "");
      const expected = url.protocol === "file:" ? "file://" : url.origin;
      return value === expected;
    } catch { return false; }
  }
}

module.exports = { CompanionMicrophonePermission, MICROPHONE_PERMISSION_TTL_MS, MICROPHONE_PURPOSES };
