const CHANNELS = Object.freeze([
  "chriptmas:companion-multicharacter-status",
  "chriptmas:companion-multicharacter-configure",
  "chriptmas:companion-multicharacter-send",
]);

class CompanionMultiCharacterIpcController {
  constructor({ ipcMain, requireMainRenderer, runtimeController }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("companion_multicharacter_ipc_invalid");
    }
    if (typeof requireMainRenderer !== "function" || !runtimeController
      || typeof runtimeController.projection !== "function" || typeof runtimeController.configure !== "function"
      || typeof runtimeController.sendAction !== "function") {
      throw new TypeError("companion_multicharacter_boundary_invalid");
    }
    Object.assign(this, { ipcMain, requireMainRenderer, runtimeController });
    this.handlers = new Map([
      [CHANNELS[0], (event) => this.status(event)],
      [CHANNELS[1], (event, payload) => this.configure(event, payload)],
      [CHANNELS[2], (event, payload) => this.send(event, payload)],
    ]);
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const registered = [];
    try {
      for (const [channel, handler] of this.handlers) {
        this.ipcMain.handle(channel, handler);
        registered.push(channel);
      }
    } catch (error) {
      for (const channel of registered) this.ipcMain.removeHandler(channel);
      throw error;
    }
    this.installed = true;
    return true;
  }

  status(event) {
    this.requireMainRenderer(event);
    return this.runtimeController.projection();
  }

  configure(event, payload) {
    this.requireMainRenderer(event);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)
      || Object.keys(payload).sort().join() !== "allowed_character_ids,character_id,consented,enabled,expected_revision"
      || !Array.isArray(payload.allowed_character_ids)) {
      throw new Error("companion_multicharacter_payload_rejected");
    }
    return this.runtimeController.configure(payload);
  }

  send(event, payload) {
    this.requireMainRenderer(event);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)
      || Object.keys(payload).sort().join() !== "action,instance_id"
      || typeof payload.instance_id !== "string" || !["wave", "greeting", "cheer"].includes(payload.action)) {
      throw new Error("companion_multicharacter_payload_rejected");
    }
    return this.runtimeController.sendAction(payload.instance_id, payload.action);
  }

  dispose() {
    if (!this.installed) return false;
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, CompanionMultiCharacterIpcController };
