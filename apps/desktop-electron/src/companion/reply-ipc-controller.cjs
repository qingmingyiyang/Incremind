const CHANNEL = "chriptmas:companion-reply-present";

class CompanionReplyIpcController {
  constructor({ ipcMain, requireMainRenderer, presentation }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("companion_reply_ipc_invalid");
    }
    if (typeof requireMainRenderer !== "function" || !presentation || typeof presentation.present !== "function") {
      throw new TypeError("companion_reply_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.requireMainRenderer = requireMainRenderer;
    this.presentation = presentation;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    this.ipcMain.handle(CHANNEL, (event, payload) => this.present(event, payload));
    this.installed = true;
    return true;
  }

  present(event, payload) {
    this.requireMainRenderer(event);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)
      || Object.keys(payload).join() !== "text" || typeof payload.text !== "string") {
      throw new Error("companion_reply_payload_rejected");
    }
    return this.presentation.present(payload.text);
  }

  dispose() {
    if (!this.installed) return false;
    this.ipcMain.removeHandler(CHANNEL);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNEL, CompanionReplyIpcController };
