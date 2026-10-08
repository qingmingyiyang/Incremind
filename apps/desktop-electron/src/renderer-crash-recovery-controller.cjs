"use strict";

const NORMAL_RENDERER_EXIT_REASON = "clean-exit";
const RECOVERABLE_RENDERER_EXIT_REASONS = new Set([
  "abnormal-exit",
  "killed",
  "crashed",
  "oom",
  "launch-failed",
  "integrity-failure",
]);

function rendererExitReason(details) {
  if (!details || typeof details.reason !== "string") return "unknown";
  return details.reason.replace(/[\r\n\0]/g, " ").slice(0, 64) || "unknown";
}

class RendererCrashRecoveryController {
  #isApplicationQuitting;
  #log;
  #bindings = new WeakMap();

  constructor({ isApplicationQuitting, log = () => {} } = {}) {
    if (typeof isApplicationQuitting !== "function" || typeof log !== "function") {
      throw new Error("renderer_crash_recovery_options_invalid");
    }
    this.#isApplicationQuitting = isApplicationQuitting;
    this.#log = log;
  }

  watch(webContents) {
    if (!webContents || typeof webContents.on !== "function" || typeof webContents.removeListener !== "function"
      || typeof webContents.isDestroyed !== "function" || typeof webContents.reload !== "function") {
      throw new Error("renderer_crash_recovery_web_contents_invalid");
    }
    if (this.#bindings.has(webContents)) return false;

    const binding = { reloadUsed: false, destroyed: false, onGone: null, onDestroyed: null };
    binding.onGone = (_event, details) => this.handleRenderProcessGone(webContents, details, binding);
    binding.onDestroyed = () => { binding.destroyed = true; };
    webContents.on("render-process-gone", binding.onGone);
    webContents.on("destroyed", binding.onDestroyed);
    this.#bindings.set(webContents, binding);
    return true;
  }

  unwatch(webContents) {
    const binding = this.#bindings.get(webContents);
    if (!binding) return false;
    webContents.removeListener("render-process-gone", binding.onGone);
    webContents.removeListener("destroyed", binding.onDestroyed);
    this.#bindings.delete(webContents);
    return true;
  }

  handleRenderProcessGone(webContents, details, binding = this.#bindings.get(webContents)) {
    if (!binding) return Object.freeze({ status: "ignored", reason: "unwatched" });
    const reason = rendererExitReason(details);
    if (binding.destroyed || webContents.isDestroyed()) return Object.freeze({ status: "ignored", reason: "web_contents_destroyed" });
    if (reason === NORMAL_RENDERER_EXIT_REASON) return Object.freeze({ status: "ignored", reason: "clean_exit" });
    if (!RECOVERABLE_RENDERER_EXIT_REASONS.has(reason)) {
      this.#log(`[renderer] process gone (${reason}); recovery suppressed: unknown reason`);
      return Object.freeze({ status: "ignored", reason: "unknown_exit_reason" });
    }
    if (this.#isApplicationQuitting()) {
      this.#log(`[renderer] process gone (${reason}); recovery suppressed: application quitting`);
      return Object.freeze({ status: "ignored", reason: "application_quitting" });
    }
    if (binding.reloadUsed) {
      this.#log(`[renderer] process gone (${reason}); recovery suppressed: reload already used`);
      return Object.freeze({ status: "ignored", reason: "reload_already_used" });
    }

    binding.reloadUsed = true;
    try {
      webContents.reload();
      this.#log(`[renderer] process gone (${reason}); reloading renderer once`);
      return Object.freeze({ status: "reloading", reason });
    } catch (error) {
      this.#log(`[renderer] process gone (${reason}); reload failed: ${String(error?.message || error || "unknown").replace(/[\r\n\0]/g, " ").slice(0, 160)}`);
      return Object.freeze({ status: "failed", reason: "reload_failed" });
    }
  }
}

module.exports = {
  NORMAL_RENDERER_EXIT_REASON,
  RECOVERABLE_RENDERER_EXIT_REASONS,
  RendererCrashRecoveryController,
};
