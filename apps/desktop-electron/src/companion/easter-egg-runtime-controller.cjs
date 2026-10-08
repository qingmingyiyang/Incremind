const UNAVAILABLE_RESULT = Object.freeze({ status: "unavailable", events: Object.freeze([]) });
const UNAVAILABLE_STATUS = Object.freeze({ enabled: false, state: "unavailable", available_events: 0 });

class CompanionEasterEggRuntimeController {
  constructor({ controllerProvider, overlayController, now = Date.now }) {
    if (typeof controllerProvider !== "function" || !overlayController
      || typeof overlayController.present !== "function" || typeof now !== "function") {
      throw new TypeError("companion_easter_egg_runtime_invalid");
    }
    this.controllerProvider = controllerProvider;
    this.overlayController = overlayController;
    this.now = now;
  }

  status() {
    return this.controllerProvider()?.status() || UNAVAILABLE_STATUS;
  }

  setEnabled(enabled) {
    const controller = this.controllerProvider();
    if (!controller) throw new Error("companion_easter_egg_unavailable");
    return controller.setEnabled(enabled);
  }

  record(counter) {
    let result;
    try {
      const controller = this.controllerProvider();
      result = controller ? controller.record(counter) : UNAVAILABLE_RESULT;
    } catch {
      result = UNAVAILABLE_RESULT;
    }
    for (const event of result?.events || []) {
      this.overlayController.present({
        event_id: `easter-egg:${event.id}:${this.now().toString(36)}`,
        kind: "easter_egg",
        visual_state: event.visual_state,
        text: event.text,
        actions: [],
        requires_ack: false,
      }, { focus: false });
    }
    return result;
  }
}

module.exports = { CompanionEasterEggRuntimeController, UNAVAILABLE_RESULT, UNAVAILABLE_STATUS };
