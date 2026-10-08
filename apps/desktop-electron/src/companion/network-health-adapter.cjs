const { performance } = require("node:perf_hooks");

class CompanionNetworkHealthAdapter {
  constructor({ net, isOnline, timeoutMs = 3000, now = () => performance.now(), setTimeoutFn = setTimeout, clearTimeoutFn = clearTimeout }) {
    if (!net || typeof net.request !== "function" || typeof isOnline !== "function") throw new TypeError("network health dependencies are invalid");
    if (!Number.isInteger(timeoutMs) || timeoutMs < 250 || timeoutMs > 10000) throw new TypeError("network health timeout is invalid");
    Object.assign(this, { net, isOnline, timeoutMs, now, setTimeoutFn, clearTimeoutFn });
  }

  async probe(healthOrigin) {
    if (this.isOnline() !== true) return Object.freeze({ network_state: "offline", latency_ms: null });
    if (!healthOrigin) return Object.freeze({ network_state: "normal", latency_ms: 0 });
    const origin = new URL(healthOrigin);
    if (origin.protocol !== "https:" || origin.origin !== healthOrigin) throw new TypeError("health origin must be an HTTPS origin");
    const started = this.now();
    try {
      await this.#head(origin.href);
      const latency = Math.max(0, Math.min(30000, Math.round(this.now() - started)));
      return Object.freeze({ network_state: latency >= 300 ? "slow" : "normal", latency_ms: latency });
    } catch {
      return Object.freeze({ network_state: "offline", latency_ms: null });
    }
  }

  #head(url) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const request = this.net.request({ method: "HEAD", url, redirect: "error" });
      const finish = (error) => {
        if (settled) return;
        settled = true;
        this.clearTimeoutFn(timer);
        if (error) reject(error); else resolve();
      };
      const timer = this.setTimeoutFn(() => {
        request.abort?.();
        finish(new Error("network_health_timeout"));
      }, this.timeoutMs);
      request.on("response", (response) => {
        response.resume?.();
        finish();
      });
      request.on("error", (error) => finish(error));
      request.end();
    });
  }
}

module.exports = { CompanionNetworkHealthAdapter };
