"use strict";

function validateSteps(steps) {
  if (!Array.isArray(steps)) throw new Error("application_bootstrap_steps_invalid");
  const names = new Set();
  return steps.map((step) => {
    if (!step || typeof step.name !== "string" || !/^[a-z][a-z0-9-]{1,63}$/.test(step.name) || names.has(step.name) || typeof step.run !== "function") {
      throw new Error("application_bootstrap_step_invalid");
    }
    if (step.optional === true && typeof step.onError !== "function") throw new Error("application_bootstrap_optional_error_handler_required");
    names.add(step.name);
    return Object.freeze({ name: step.name, run: step.run, optional: step.optional === true, onError: step.onError });
  });
}

class ApplicationBootstrapCoordinator {
  #app;
  #enabledProvider;
  #beginStartupFeedback;
  #startRequiredRuntime;
  #onRequiredRuntimeFailure;
  #steps;
  #onReady;
  #onActivate;
  #startPromise = null;
  #installPromise = null;
  #activateHandler = null;
  #ready = false;

  constructor({ app, enabledProvider, beginStartupFeedback, startRequiredRuntime, onRequiredRuntimeFailure, steps, onReady, onActivate } = {}) {
    if (!app || typeof app.whenReady !== "function" || typeof app.on !== "function" || typeof app.removeListener !== "function" || typeof enabledProvider !== "function" || typeof beginStartupFeedback !== "function" || typeof startRequiredRuntime !== "function" || typeof onRequiredRuntimeFailure !== "function" || typeof onReady !== "function" || typeof onActivate !== "function") {
      throw new Error("application_bootstrap_options_invalid");
    }
    this.#app = app;
    this.#enabledProvider = enabledProvider;
    this.#beginStartupFeedback = beginStartupFeedback;
    this.#startRequiredRuntime = startRequiredRuntime;
    this.#onRequiredRuntimeFailure = onRequiredRuntimeFailure;
    this.#steps = validateSteps(steps);
    this.#onReady = onReady;
    this.#onActivate = onActivate;
  }

  get ready() { return this.#ready; }

  install() {
    if (this.#installPromise) return this.#installPromise;
    this.#installPromise = Promise.resolve(this.#app.whenReady()).then(() => this.start());
    return this.#installPromise;
  }

  start() {
    if (this.#startPromise) return this.#startPromise;
    this.#startPromise = this.#run();
    return this.#startPromise;
  }

  async #run() {
    if (!this.#enabledProvider()) return Object.freeze({ status: "ignored", reason: "single_instance_lock_unavailable" });
    this.#beginStartupFeedback();
    try {
      await this.#startRequiredRuntime();
    } catch (error) {
      await this.#onRequiredRuntimeFailure(error);
      return Object.freeze({ status: "failed", stage: "required-runtime" });
    }
    for (const step of this.#steps) {
      try {
        await step.run();
      } catch (error) {
        if (!step.optional) throw error;
        step.onError(error);
      }
    }
    this.#ready = true;
    this.#onReady();
    this.#activateHandler = () => this.#onActivate();
    this.#app.on("activate", this.#activateHandler);
    return Object.freeze({ status: "ready" });
  }

  dispose() {
    if (!this.#activateHandler) return false;
    this.#app.removeListener("activate", this.#activateHandler);
    this.#activateHandler = null;
    return true;
  }
}

module.exports = { ApplicationBootstrapCoordinator };
