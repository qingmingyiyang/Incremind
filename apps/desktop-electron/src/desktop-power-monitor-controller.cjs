"use strict";

const EVENT_BINDINGS = Object.freeze([
  ["lock-screen", "onLock"],
  ["unlock-screen", "onUnlock"],
  ["suspend", "onSuspend"],
  ["resume", "onResume"],
]);

class DesktopPowerMonitorController {
  constructor({ powerMonitor, onLock, onUnlock, onSuspend, onResume } = {}) {
    if (!powerMonitor || typeof powerMonitor.on !== "function" || typeof powerMonitor.removeListener !== "function" || typeof powerMonitor.getSystemIdleTime !== "function"
      || typeof onLock !== "function" || typeof onUnlock !== "function" || typeof onSuspend !== "function" || typeof onResume !== "function") {
      throw new TypeError("desktop_power_monitor_options_invalid");
    }
    this.powerMonitor = powerMonitor;
    this.handlers = Object.freeze({
      onLock: () => onLock(),
      onUnlock: () => onUnlock(),
      onSuspend: () => onSuspend(),
      onResume: () => onResume(),
    });
    this.installedEvents = [];
  }

  install() {
    if (this.installedEvents.length > 0) return false;
    try {
      for (const [eventName, handlerName] of EVENT_BINDINGS) {
        this.powerMonitor.on(eventName, this.handlers[handlerName]);
        this.installedEvents.push(eventName);
      }
    } catch (error) {
      this.dispose();
      throw error;
    }
    return true;
  }

  getSystemIdleTime() {
    return this.powerMonitor.getSystemIdleTime();
  }

  dispose() {
    if (this.installedEvents.length === 0) return false;
    for (const eventName of this.installedEvents.splice(0).reverse()) {
      const binding = EVENT_BINDINGS.find(([name]) => name === eventName);
      this.powerMonitor.removeListener(eventName, this.handlers[binding[1]]);
    }
    return true;
  }
}

module.exports = { DesktopPowerMonitorController };
