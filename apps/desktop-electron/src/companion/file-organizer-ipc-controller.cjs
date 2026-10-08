const CHANNELS = Object.freeze([
  "chriptmas:companion-file-organizer-preview",
  "chriptmas:companion-file-organizer-execute",
  "chriptmas:companion-file-organizer-history",
  "chriptmas:companion-file-organizer-undo",
]);

function strictIdPayload(payload, key) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return "";
  if (Object.keys(payload).join() !== key) return "";
  return typeof payload[key] === "string" ? payload[key] : "";
}

class CompanionFileOrganizerIpcController {
  constructor({ ipcMain, dialog, requireMainRenderer, mainWindowProvider, organizerProvider, onExecuted }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("companion_file_organizer_ipc_invalid");
    }
    if (!dialog || typeof dialog.showOpenDialog !== "function" || typeof dialog.showMessageBox !== "function") {
      throw new TypeError("companion_file_organizer_dialog_invalid");
    }
    if (typeof requireMainRenderer !== "function" || typeof mainWindowProvider !== "function"
      || typeof organizerProvider !== "function" || typeof onExecuted !== "function") {
      throw new TypeError("companion_file_organizer_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.dialog = dialog;
    this.requireMainRenderer = requireMainRenderer;
    this.mainWindowProvider = mainWindowProvider;
    this.organizerProvider = organizerProvider;
    this.onExecuted = onExecuted;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      [CHANNELS[0], (event) => this.preview(event)],
      [CHANNELS[1], (event, payload) => this.execute(event, payload)],
      [CHANNELS[2], (event) => this.history(event)],
      [CHANNELS[3], (event, payload) => this.undo(event, payload)],
    ]);
    const registered = [];
    try {
      for (const [channel, handler] of handlers) {
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

  async preview(event) {
    this.requireMainRenderer(event);
    const { mainWindow, organizer } = this.available({ requireFocus: true });
    const source = await this.dialog.showOpenDialog(mainWindow, {
      title: "选择需要整理的目录",
      properties: ["openDirectory", "dontAddToRecent"],
    });
    if (source.canceled || source.filePaths.length !== 1) return { status: "cancelled" };
    const target = await this.dialog.showOpenDialog(mainWindow, {
      title: "选择分类文件夹的存放目录",
      properties: ["openDirectory", "createDirectory", "dontAddToRecent"],
    });
    if (target.canceled || target.filePaths.length !== 1) return { status: "cancelled" };
    return { status: "preview", preview: organizer.preview(source.filePaths[0], target.filePaths[0]) };
  }

  async execute(event, payload) {
    this.requireMainRenderer(event);
    const planId = strictIdPayload(payload, "plan_id");
    if (!planId) throw new Error("companion_file_organizer_payload_rejected");
    const { mainWindow, organizer } = this.available({ requireFocus: true });
    const confirmation = await this.dialog.showMessageBox(mainWindow, {
      type: "warning",
      title: "确认移动本地文件",
      message: "是否执行刚才预览的文件整理计划？",
      detail: "文件将移动到所选目标目录的分类文件夹。程序不会覆盖同名文件，并会保留可撤销记录。",
      buttons: ["取消", "确认移动"],
      defaultId: 0,
      cancelId: 0,
      noLink: true,
    });
    if (confirmation.response !== 1) return { status: "cancelled" };
    const result = organizer.execute(planId);
    this.onExecuted(result);
    return result;
  }

  history(event) {
    this.requireMainRenderer(event);
    return { status: "ready", operations: this.organizerProvider()?.history() || [] };
  }

  undo(event, payload) {
    this.requireMainRenderer(event);
    const operationId = strictIdPayload(payload, "operation_id");
    if (!operationId) throw new Error("companion_file_organizer_payload_rejected");
    const organizer = this.organizerProvider();
    if (!organizer) throw new Error("companion_file_organizer_unavailable");
    return organizer.undo(operationId);
  }

  available({ requireFocus }) {
    const mainWindow = this.mainWindowProvider();
    const organizer = this.organizerProvider();
    if (!organizer || !mainWindow || (requireFocus && (!mainWindow.isVisible() || !mainWindow.isFocused()))) {
      throw new Error("companion_file_organizer_unavailable");
    }
    return { mainWindow, organizer };
  }

  dispose() {
    if (!this.installed) return false;
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, CompanionFileOrganizerIpcController, strictIdPayload };
