const { createFileGrant, uploadFileGrant } = require("./file-grant.cjs");
const { currentSessionForRequest } = require("./desktop-session.cjs");

const CHANNELS = Object.freeze([
  "chriptmas:select-local-file",
  "chriptmas:upload-local-file",
  "chriptmas:cancel-file-upload",
]);

function localFileFilters(mediaKind) {
  if (mediaKind === "image") {
    return [{ name: "Images", extensions: ["png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"] }];
  }
  if (mediaKind === "audio") {
    return [{ name: "Audio", extensions: ["wav", "mp3", "m4a", "aac", "flac", "ogg"] }];
  }
  if (mediaKind === "video") {
    return [{ name: "Video", extensions: ["mp4", "mov", "mkv", "avi", "webm", "m4v"] }];
  }
  if (mediaKind === "document") {
    return [{ name: "Documents", extensions: ["pdf", "doc", "docx"] }];
  }
  return [{ name: "All Files", extensions: ["*"] }];
}

class FileGrantIpcController {
  constructor({
    ipcMain,
    dialog,
    requireMainRenderer,
    sessionProvider,
    createFileGrant: createGrant = createFileGrant,
    uploadFileGrant: uploadGrant = uploadFileGrant,
    createAbortController = () => new AbortController(),
  }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("file_grant_ipc_invalid");
    }
    if (!dialog || typeof dialog.showOpenDialog !== "function") throw new TypeError("file_grant_dialog_invalid");
    if (typeof requireMainRenderer !== "function" || typeof sessionProvider !== "function") {
      throw new TypeError("file_grant_boundary_invalid");
    }
    if (typeof createGrant !== "function" || typeof uploadGrant !== "function" || typeof createAbortController !== "function") {
      throw new TypeError("file_grant_runtime_invalid");
    }
    this.ipcMain = ipcMain;
    this.dialog = dialog;
    this.requireMainRenderer = requireMainRenderer;
    this.sessionProvider = sessionProvider;
    this.createFileGrant = createGrant;
    this.uploadFileGrant = uploadGrant;
    this.createAbortController = createAbortController;
    this.activeUploads = new Map();
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      ["chriptmas:select-local-file", (event, options) => this.selectLocalFile(event, options)],
      ["chriptmas:upload-local-file", (event, options) => this.uploadLocalFile(event, options)],
      ["chriptmas:cancel-file-upload", (event, options) => this.cancelFileUpload(event, options)],
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

  async selectLocalFile(event, options = {}) {
    this.requireMainRenderer(event);
    const mediaKind = typeof options?.mediaKind === "string" ? options.mediaKind : "file";
    const result = await this.dialog.showOpenDialog({
      title: "选择本地文件",
      properties: ["openFile"],
      filters: localFileFilters(mediaKind),
    });
    if (result.canceled || result.filePaths.length === 0) return null;
    return result.filePaths[0];
  }

  async uploadLocalFile(event, options = {}) {
    this.requireMainRenderer(event);
    const filePath = typeof options?.filePath === "string" ? options.filePath : "";
    if (!filePath || filePath.includes("\0")) throw new Error("file_grant_path_invalid");
    const initialSession = this.sessionProvider();
    const session = initialSession ? { ...initialSession } : null;
    if (!session) throw new Error("file_grant_sidecar_unavailable");
    const requestId = typeof options?.requestId === "string" && /^[A-Za-z0-9_-]{16,128}$/.test(options.requestId)
      ? options.requestId : "";
    if (!requestId || this.activeUploads.has(requestId)) throw new Error("file_grant_request_invalid");

    const active = Object.freeze({ controller: this.createAbortController(), senderId: event.sender.id });
    this.activeUploads.set(requestId, active);
    try {
      const grant = await this.createFileGrant({
        filePath,
        session,
        mediaType: options.mediaType,
        sourceKind: options.sourceKind,
      });
      const requestSession = currentSessionForRequest(this.sessionProvider, session);
      return await this.uploadFileGrant(grant, requestSession, { signal: active.controller.signal });
    } finally {
      if (this.activeUploads.get(requestId) === active) this.activeUploads.delete(requestId);
    }
  }

  cancelFileUpload(event, options = {}) {
    this.requireMainRenderer(event);
    const requestId = typeof options?.requestId === "string" ? options.requestId : "";
    const active = this.activeUploads.get(requestId);
    if (!active || active.senderId !== event.sender.id) return { status: "not_found" };
    active.controller.abort(new Error("file_grant_upload_cancelled"));
    return { status: "cancelled" };
  }

  dispose() {
    if (!this.installed) return false;
    for (const upload of this.activeUploads.values()) upload.controller.abort(new Error("app_quit"));
    this.activeUploads.clear();
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, FileGrantIpcController, localFileFilters };
