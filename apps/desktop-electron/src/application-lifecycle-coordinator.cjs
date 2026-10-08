const STEP_NAME = /^[a-z][a-z0-9_.-]{0,63}$/;

class ApplicationLifecycleCoordinator {
  constructor({
    app,
    shutdownSteps,
    log = () => {},
    shutdownDeadlineMs = 7500,
    setTimer = setTimeout,
    clearTimer = clearTimeout,
  }) {
    if (!app || typeof app.on !== "function" || typeof app.removeListener !== "function" || typeof app.quit !== "function") {
      throw new TypeError("application_lifecycle_app_invalid");
    }
    if (!Array.isArray(shutdownSteps) || shutdownSteps.length === 0) {
      throw new TypeError("application_lifecycle_steps_invalid");
    }
    if (!Number.isInteger(shutdownDeadlineMs) || shutdownDeadlineMs < 1000 || shutdownDeadlineMs > 30000
      || typeof setTimer !== "function" || typeof clearTimer !== "function") {
      throw new TypeError("application_lifecycle_deadline_invalid");
    }
    const names = new Set();
    this.shutdownSteps = shutdownSteps.map((step) => {
      if (!step || !STEP_NAME.test(step.name) || typeof step.run !== "function" || names.has(step.name)) {
        throw new TypeError("application_lifecycle_step_invalid");
      }
      names.add(step.name);
      return Object.freeze({ name: step.name, run: step.run });
    });
    this.app = app;
    this.log = log;
    this.shutdownDeadlineMs = shutdownDeadlineMs;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.installed = false;
    this.shuttingDown = false;
    this.shutdownComplete = false;
    this.shutdownPromise = null;
    this.quitReplayScheduled = false;
    this.quitReplayIssued = false;
    this.failures = [];
    this.beforeQuitListener = (event) => this.handleBeforeQuit(event);
  }

  install() {
    if (this.installed) return false;
    this.app.on("before-quit", this.beforeQuitListener);
    this.installed = true;
    return true;
  }

  quit() {
    this.markQuitting();
    this.app.quit();
    return { status: "quitting" };
  }

  shutdown() {
    this.markQuitting();
    if (this.shutdownPromise) return false;
    this.shuttingDown = true;
    this.shutdownPromise = this.awaitShutdownDeadline(this.shutdownSteps.map((step) => this.runStep(step)))
      .then(() => { this.shutdownComplete = true; });
    return true;
  }

  handleBeforeQuit(event) {
    if (this.shutdownComplete) return true;
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    const started = this.shutdown();
    this.scheduleQuitReplay();
    return started;
  }

  scheduleQuitReplay() {
    if (this.quitReplayScheduled) return false;
    this.quitReplayScheduled = true;
    this.shutdownPromise.then(() => this.replayQuit());
    return true;
  }

  awaitShutdownDeadline(stepPromises) {
    let timeout;
    const deadline = new Promise((resolve) => {
      timeout = this.setTimer(() => {
        this.recordFailure("shutdown-deadline", `application_shutdown_timeout_${this.shutdownDeadlineMs}ms`);
        resolve();
      }, this.shutdownDeadlineMs);
    });
    return Promise.race([Promise.allSettled(stepPromises), deadline])
      .finally(() => this.clearTimer(timeout));
  }

  replayQuit() {
    if (this.quitReplayIssued) return false;
    this.quitReplayIssued = true;
    this.app.quit();
    return true;
  }

  runStep(step) {
    try {
      const result = step.run();
      return Promise.resolve(result).catch((error) => this.recordFailure(step.name, error));
    } catch (error) {
      this.recordFailure(step.name, error);
      return Promise.resolve();
    }
  }

  recordFailure(name, error) {
    const message = String(error?.message || error || "unknown").replace(/[\r\n\0]/g, " ").slice(0, 240);
    const failure = Object.freeze({ name, message });
    this.failures.push(failure);
    this.log(`[lifecycle] shutdown step ${name} failed: ${message}`);
  }

  markQuitting() {
    this.app.isQuitting = true;
  }

  dispose() {
    if (!this.installed) return false;
    this.app.removeListener("before-quit", this.beforeQuitListener);
    this.installed = false;
    return true;
  }
}

module.exports = { ApplicationLifecycleCoordinator };
