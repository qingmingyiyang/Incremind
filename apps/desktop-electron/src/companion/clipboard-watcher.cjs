const crypto = require("node:crypto");

const MAX_CLIPBOARD_CHARS = 4000;
const UNDO_WINDOW_MS = 10_000;
const PRIVATE_MARKERS = [
  /-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----/i,
  /(?:password|passwd|pwd|token|secret|api[_-]?key)\s*[:=]\s*\S+/i,
  /\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b/,
  /\bBearer\s+[A-Za-z0-9._~+\/-]{16,}=*\b/i,
];

class CompanionClipboardWatcher {
  constructor({ clipboard, onEvent, isQuiet = () => false, now = () => Date.now(), setIntervalFn = setInterval, clearIntervalFn = clearInterval, setTimeoutFn = setTimeout, clearTimeoutFn = clearTimeout }) {
    if (!clipboard || typeof clipboard.readText !== "function" || typeof clipboard.clear !== "function" || typeof clipboard.writeText !== "function") {
      throw new TypeError("clipboard adapter is invalid");
    }
    if (typeof onEvent !== "function" || typeof isQuiet !== "function") throw new TypeError("clipboard watcher callbacks are invalid");
    Object.assign(this, { clipboard, onEvent, isQuiet, now, setIntervalFn, clearIntervalFn, setTimeoutFn, clearTimeoutFn });
    this.enabled = false;
    this.timer = null;
    this.undoTimer = null;
    this.currentHash = "";
    this.currentBucket = "empty";
    this.currentText = null;
    this.currentSensitive = false;
    this.currentEventId = null;
    this.undoText = null;
    this.suppressedHash = null;
    this.sequence = 0;
  }

  setEnabled(enabled) {
    const next = enabled === true;
    if (next === this.enabled) return this.status();
    this.enabled = next;
    if (next) {
      this._prime();
      this.timer = this.setIntervalFn(() => this.poll(), 1000);
    } else {
      this._stopTimer();
      this._forgetAll();
    }
    return this.status();
  }

  poll() {
    if (!this.enabled) return this.status();
    let value;
    try {
      value = this.clipboard.readText("clipboard");
    } catch {
      return { ...this.status(), state: "degraded" };
    }
    const text = typeof value === "string" ? value : "";
    const hash = digest(text);
    if (hash === this.currentHash || hash === this.suppressedHash) {
      if (hash === this.suppressedHash) this.suppressedHash = null;
      return this.status();
    }
    this.currentHash = hash;
    this.currentBucket = lengthBucket(text.length);
    this.currentText = text || null;
    this.currentSensitive = classifySensitive(text).sensitive;
    this.currentEventId = text ? this._eventId("changed") : null;
    this._dropUndo();
    if (!text || this.isQuiet()) return this.status();
    this.onEvent(Object.freeze({
      event_id: this.currentEventId,
      kind: "clipboard_changed",
      visual_state: "attention",
      text: this.currentSensitive ? "剪贴板里出现了可能敏感的内容，我不会展示或外发。" : "你复制了新文字，要让我看看吗？",
      actions: Object.freeze([
        Object.freeze({ id: "clipboard.inspect", label: "看看" }),
        Object.freeze({ id: "clipboard.eat", label: "吃掉" }),
      ]),
      requires_ack: false,
    }));
    return this.status();
  }

  inspect(eventId) {
    this._requireCurrent(eventId);
    if (!this.currentText) throw new Error("clipboard_content_unavailable");
    const analysis = analyseText(this.currentText);
    const message = analysis.sensitive
      ? "这段内容看起来可能包含密码、令牌或私钥。我不会显示、保存或发送它。"
      : localComment(analysis);
    const nextId = this._eventId("inspected");
    this.currentEventId = nextId;
    return Object.freeze({
      event_id: nextId,
      kind: "clipboard_comment",
      visual_state: analysis.sensitive ? "warning" : "happy",
      text: message,
      actions: Object.freeze([Object.freeze({ id: "clipboard.eat", label: "吃掉" })]),
      requires_ack: false,
    });
  }

  eat(eventId) {
    this._requireCurrent(eventId);
    if (!this.currentText) throw new Error("clipboard_content_unavailable");
    this._dropUndo();
    this.undoText = this.currentText;
    this.clipboard.clear("clipboard");
    this.suppressedHash = digest("");
    this.currentHash = this.suppressedHash;
    this.currentText = null;
    this.currentSensitive = false;
    this.currentBucket = "empty";
    const nextId = this._eventId("eaten");
    this.currentEventId = nextId;
    this.undoTimer = this.setTimeoutFn(() => this._dropUndo(), UNDO_WINDOW_MS);
    return Object.freeze({
      event_id: nextId,
      kind: "clipboard_eaten",
      visual_state: "happy",
      text: "已经吃掉啦。十秒内还可以撤销。",
      actions: Object.freeze([Object.freeze({ id: "clipboard.undo", label: "撤销" })]),
      requires_ack: false,
    });
  }

  undo(eventId) {
    this._requireCurrent(eventId);
    if (this.undoText === null) throw new Error("clipboard_undo_expired");
    const restored = this.undoText;
    this._dropUndo();
    this.clipboard.writeText(restored, "clipboard");
    this.suppressedHash = digest(restored);
    this.currentHash = this.suppressedHash;
    this.currentBucket = lengthBucket(restored.length);
    this.currentText = restored;
    this.currentSensitive = classifySensitive(restored).sensitive;
    const nextId = this._eventId("restored");
    this.currentEventId = nextId;
    return Object.freeze({
      event_id: nextId,
      kind: "clipboard_restored",
      visual_state: "idle",
      text: "已经把剪贴板内容恢复了。",
      actions: Object.freeze([]),
      requires_ack: false,
    });
  }

  stop() {
    this.enabled = false;
    this._stopTimer();
    this._forgetAll();
  }

  status() {
    return Object.freeze({
      enabled: this.enabled,
      state: this.enabled ? "ready" : "disabled",
      length_bucket: this.currentBucket,
      sensitive: this.currentSensitive,
      undo_available: this.undoText !== null,
    });
  }

  _prime() {
    let text = "";
    try { text = this.clipboard.readText("clipboard") || ""; } catch {}
    this.currentHash = digest(text);
    this.currentBucket = lengthBucket(text.length);
    this.currentText = null;
    this.currentSensitive = false;
    this.currentEventId = null;
  }

  _eventId(kind) {
    this.sequence += 1;
    return `clipboard:${kind}:${this.now().toString(36)}:${this.sequence.toString(36)}`;
  }

  _requireCurrent(eventId) {
    if (typeof eventId !== "string" || !eventId || eventId !== this.currentEventId) throw new Error("clipboard_event_not_current");
  }

  _stopTimer() {
    if (this.timer !== null) this.clearIntervalFn(this.timer);
    this.timer = null;
  }

  _dropUndo() {
    if (this.undoTimer !== null) this.clearTimeoutFn(this.undoTimer);
    this.undoTimer = null;
    this.undoText = null;
  }

  _forgetAll() {
    this._dropUndo();
    this.currentHash = "";
    this.currentBucket = "empty";
    this.currentText = null;
    this.currentSensitive = false;
    this.currentEventId = null;
    this.suppressedHash = null;
  }
}

function digest(value) {
  return crypto.createHash("sha256").update(value, "utf8").digest("hex");
}

function lengthBucket(length) {
  if (!Number.isSafeInteger(length) || length <= 0) return "empty";
  if (length <= 80) return "short";
  if (length <= 500) return "medium";
  if (length <= MAX_CLIPBOARD_CHARS) return "long";
  return "oversized";
}

function classifySensitive(text) {
  const marker = PRIVATE_MARKERS.some((pattern) => pattern.test(text));
  const oversized = text.length > MAX_CLIPBOARD_CHARS;
  const compact = text.replace(/\s/g, "");
  const highEntropy = compact.length >= 48 && compact.length <= 512 && shannonEntropy(compact) >= 4.5;
  return Object.freeze({ sensitive: marker || oversized || highEntropy, marker, oversized, high_entropy: highEntropy });
}

function analyseText(text) {
  const classification = classifySensitive(text);
  const lowered = text.toLocaleLowerCase();
  const keywords = [];
  if (/https:\/\//i.test(text)) keywords.push("link");
  if (/\b(?:todo|待办|提醒)\b/i.test(text)) keywords.push("todo");
  if (/```|\b(?:function|const|class|def)\b/.test(text)) keywords.push("code");
  if (/\b(?:error|exception|失败|报错)\b/i.test(lowered)) keywords.push("error");
  return Object.freeze({ ...classification, length_bucket: lengthBucket(text.length), line_count: text.split(/\r?\n/).length, keywords: Object.freeze(keywords) });
}

function localComment(analysis) {
  if (analysis.keywords.includes("code")) return "像是一段代码，我会安静地替你看住它。";
  if (analysis.keywords.includes("error")) return "看起来像报错信息，别急，我们可以一步步排查。";
  if (analysis.keywords.includes("todo")) return "像是一条待办，记得安排一下时间。";
  if (analysis.keywords.includes("link")) return "像是一条链接，打开前记得确认域名。";
  if (analysis.line_count > 8 || analysis.length_bucket === "long") return "复制了不少内容，辛苦啦，注意别让剪贴板放太久。";
  return "是一小段文字，我已经看过啦。";
}

function shannonEntropy(value) {
  if (!value) return 0;
  const counts = new Map();
  for (const char of value) counts.set(char, (counts.get(char) || 0) + 1);
  let entropy = 0;
  for (const count of counts.values()) {
    const probability = count / value.length;
    entropy -= probability * Math.log2(probability);
  }
  return entropy;
}

module.exports = {
  CompanionClipboardWatcher,
  MAX_CLIPBOARD_CHARS,
  UNDO_WINDOW_MS,
  analyseText,
  classifySensitive,
  digest,
  lengthBucket,
  localComment,
  shannonEntropy,
};
