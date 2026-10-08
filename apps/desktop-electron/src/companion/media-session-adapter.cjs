const path = require("node:path");
const { spawn } = require("node:child_process");

const { terminateProcessTree } = require("../sidecar-supervisor.cjs");

const OUTPUT_LIMIT = 16 * 1024;
const PLAYBACK = new Set(["playing", "paused", "stopped", "closed", "unknown"]);

class WindowsMediaSessionAdapter {
  constructor({
    platform = process.platform,
    scriptPath = path.join(__dirname, "windows-media-session.ps1"),
    spawnProcess = spawn,
    terminateTree = terminateProcessTree,
    setTimer = setTimeout,
    clearTimer = clearTimeout,
    timeoutMs = 5000,
  } = {}) {
    if (typeof spawnProcess !== "function" || typeof terminateTree !== "function" || typeof setTimer !== "function" || typeof clearTimer !== "function") throw new TypeError("media adapter dependencies are invalid");
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 15000) throw new TypeError("media adapter timeout is invalid");
    Object.assign(this, { platform, scriptPath: path.resolve(scriptPath), spawnProcess, terminateTree, setTimer, clearTimer, timeoutMs });
    this.active = null;
    this.inFlight = null;
  }

  sample() {
    if (this.platform !== "win32") return Promise.resolve(empty("unavailable"));
    if (this.inFlight) return this.inFlight;
    this.inFlight = this.run().finally(() => { this.inFlight = null; });
    return this.inFlight;
  }

  run() {
    return new Promise((resolve) => {
      let child;
      try {
        child = this.spawnProcess("powershell.exe", ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", this.scriptPath], {
          windowsHide: true, shell: false, stdio: ["ignore", "pipe", "pipe"],
        });
      } catch {
        resolve(empty("unavailable"));
        return;
      }
      let stdout = Buffer.alloc(0), stderrBytes = 0, settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        this.clearTimer(timer);
        if (this.active?.child === child) this.active = null;
        resolve(value);
      };
      const abort = () => {
        if (settled) return;
        Promise.resolve(this.terminateTree(child)).catch(() => child.kill?.());
        finish(empty("unavailable"));
      };
      const timer = this.setTimer(abort, this.timeoutMs);
      this.active = { child, finish };
      child.stdout?.on("data", (chunk) => {
        const value = Buffer.from(chunk);
        if (stdout.length + value.length > OUTPUT_LIMIT) { abort(); return; }
        stdout = Buffer.concat([stdout, value]);
      });
      child.stderr?.on("data", (chunk) => {
        stderrBytes += Buffer.byteLength(chunk);
        if (stderrBytes > OUTPUT_LIMIT) abort();
      });
      child.once?.("error", abort);
      child.once?.("close", (code) => {
        if (settled) return;
        if (code !== 0) { finish(empty("unavailable")); return; }
        finish(parseOutput(stdout));
      });
    });
  }

  cancel() {
    const record = this.active;
    if (!record) return false;
    this.active = null;
    record.finish(empty("unavailable"));
    Promise.resolve(this.terminateTree(record.child)).catch(() => record.child.kill?.());
    return true;
  }
}

function parseOutput(buffer) {
  try {
    const text = Buffer.from(buffer).toString("utf8").trim();
    if (!text || text.split(/\r?\n/).length !== 1) return empty("error");
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().join() !== "artist,playback_status,source,status,title") return empty("error");
    if (!new Set(["ready", "empty", "unavailable"]).has(value.status) || !PLAYBACK.has(value.playback_status)) return empty("error");
    const title = safeText(value.title, 160), artist = safeText(value.artist, 160), source = safeText(value.source, 260);
    if (title === null || artist === null || source === null || (value.status === "ready" && !title)) return empty("error");
    if (value.status !== "ready") return empty(value.status);
    return Object.freeze({ status: "ready", source, title, artist, playback_status: value.playback_status });
  } catch {
    return empty("error");
  }
}

function safeText(value, maximum) {
  if (typeof value !== "string" || value.length > maximum || /[\x00-\x1f\x7f]/.test(value)) return null;
  return value.trim();
}

function empty(status) { return Object.freeze({ status, source: "", title: "", artist: "", playback_status: status === "empty" ? "closed" : "unknown" }); }

module.exports = { OUTPUT_LIMIT, WindowsMediaSessionAdapter, parseOutput };
