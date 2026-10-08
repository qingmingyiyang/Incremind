const { contextBridge, ipcRenderer } = require("electron");

const PANELS = new Set([
  "chat", "character", "master_profile", "schedule", "focus", "inventory",
  "status", "launchers", "privacy", "voice_vision", "help_data",
]);

contextBridge.exposeInMainWorld("companionOverlay", Object.freeze({
  subscribeCompanionOverlay(listener) {
    if (typeof listener !== "function") return () => {};
    const handler = (_event, projection) => listener(projection);
    ipcRenderer.on("chriptmas:companion-overlay-event", handler);
    ipcRenderer.send("chriptmas:companion-overlay-ready");
    return () => ipcRenderer.removeListener("chriptmas:companion-overlay-event", handler);
  },
  submitCompanionText(payload) {
    return ipcRenderer.invoke("chriptmas:companion-overlay-submit", {
      request_id: typeof payload?.request_id === "string" ? payload.request_id : "",
      text: typeof payload?.text === "string" ? payload.text : "",
    });
  },
  performCompanionAction(payload) {
    return ipcRenderer.invoke("chriptmas:companion-overlay-action", {
      event_id: typeof payload?.event_id === "string" ? payload.event_id : "",
      action_id: typeof payload?.action_id === "string" ? payload.action_id : "",
    });
  },
  acknowledgeCompanionEvent(eventId) {
    return ipcRenderer.invoke("chriptmas:companion-overlay-ack", {
      event_id: typeof eventId === "string" ? eventId : "",
    });
  },
  closeCompanionOverlay() {
    return ipcRenderer.invoke("chriptmas:companion-overlay-close");
  },
  openCompanionCenter(panel) {
    const safePanel = typeof panel === "string" && PANELS.has(panel) ? panel : "chat";
    return ipcRenderer.invoke("chriptmas:companion-overlay-open-center", { panel: safePanel });
  },
}));
