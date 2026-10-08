const { createFileGrant, grantHeaders, uploadFileGrant } = require("./file-grant.cjs");
const { currentSessionForRequest } = require("./desktop-session.cjs");
const { SESSION_HEADER } = require("./sidecar-supervisor.cjs");

const CHANNEL = "chriptmas:stage-context-graph-import";
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SOURCE_TYPE = /^[a-z][a-z0-9_-]{0,63}$/;
const DISPLAY_NAME = /^[^\0\r\n]{1,255}$/;
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
const FILE_FILTERS = Object.freeze([{ name: "LineMap graph files", extensions: ["*"] }]);

class ContextGraphImportIpcController {
  constructor({
    ipcMain,
    dialog,
    requireMainRenderer,
    mainWindowProvider,
    sessionProvider,
    createFileGrant: createGrant = createFileGrant,
    grantHeaders: authorizeGrant = grantHeaders,
    uploadFileGrant: uploadGrant = uploadFileGrant,
    fetchImpl = fetch,
    sessionHeader = SESSION_HEADER,
    timeoutSignal = (milliseconds) => AbortSignal.timeout(milliseconds),
  }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("context_graph_import_ipc_invalid");
    }
    if (!dialog || typeof dialog.showOpenDialog !== "function") throw new TypeError("context_graph_import_dialog_invalid");
    if (typeof requireMainRenderer !== "function" || typeof mainWindowProvider !== "function" || typeof sessionProvider !== "function") {
      throw new TypeError("context_graph_import_boundary_invalid");
    }
    if (typeof createGrant !== "function" || typeof authorizeGrant !== "function"
      || typeof uploadGrant !== "function" || typeof fetchImpl !== "function"
      || typeof timeoutSignal !== "function" || typeof sessionHeader !== "string" || !sessionHeader) {
      throw new TypeError("context_graph_import_runtime_invalid");
    }
    this.ipcMain = ipcMain;
    this.dialog = dialog;
    this.requireMainRenderer = requireMainRenderer;
    this.mainWindowProvider = mainWindowProvider;
    this.sessionProvider = sessionProvider;
    this.createFileGrant = createGrant;
    this.grantHeaders = authorizeGrant;
    this.uploadFileGrant = uploadGrant;
    this.fetch = fetchImpl;
    this.sessionHeader = sessionHeader;
    this.timeoutSignal = timeoutSignal;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    this.ipcMain.handle(CHANNEL, (event, request) => this.stage(event, request));
    this.installed = true;
    return true;
  }

  async stage(event, request = {}) {
    this.requireMainRenderer(event);
    const command = parseRequest(request);
    const initialSession = this.sessionProvider();
    const active = initialSession ? { ...initialSession } : null;
    if (!active?.origin || !active?.secret || !active?.instance_id) {
      return { status: "unavailable", reason: "sidecar_not_ready" };
    }
    const selected = await this.dialog.showOpenDialog(this.mainWindowProvider(), {
      title: "选择 LineMap 导入文件",
      properties: ["openFile"],
      filters: FILE_FILTERS,
    });
    if (selected?.canceled === true || !Array.isArray(selected?.filePaths) || selected.filePaths.length !== 1) return null;
    const filePath = selected.filePaths[0];
    if (typeof filePath !== "string" || !filePath || filePath.includes("\0")) throw new Error("context_graph_import_selection_invalid");

    const grant = await this.createFileGrant({
      filePath,
      session: active,
      mediaType: "application/octet-stream",
      sourceKind: "file",
    });
    const uploaded = await this.uploadFileGrant(grant, currentSessionForRequest(this.sessionProvider, active));
    const assetId = typeof uploaded?.asset_id === "string" ? uploaded.asset_id : "";
    if (!ID.test(assetId)) throw new Error("context_graph_import_asset_invalid");

    const requestSession = currentSessionForRequest(this.sessionProvider, active);
    const authorizationHeaders = this.grantHeaders(grant, requestSession.secret);
    delete authorizationHeaders["Content-Length"];
    const response = await this.fetch(new URL("/api/rebuild/context-graph-import-selections", requestSession.origin), {
      method: "POST",
      headers: {
        ...authorizationHeaders,
        Accept: "application/json",
        "Content-Type": "application/json",
        [this.sessionHeader]: requestSession.secret,
      },
      body: JSON.stringify({
        project_id: command.projectId,
        source_type: command.sourceType,
        command_id: command.commandId,
        asset_id: assetId,
      }),
      signal: this.timeoutSignal(5000),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(`context_graph_import_selection_failed:${response.status}`);
    return safeSelection(payload, command);
  }

  dispose() {
    if (!this.installed) return false;
    this.ipcMain.removeHandler(CHANNEL);
    this.installed = false;
    return true;
  }
}

function parseRequest(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.keys(value).length !== 3
    || !Object.hasOwn(value, "projectId") || !Object.hasOwn(value, "sourceType") || !Object.hasOwn(value, "commandId")) {
    throw new Error("context_graph_import_request_invalid");
  }
  const projectId = typeof value.projectId === "string" ? value.projectId.trim() : "";
  const sourceType = typeof value.sourceType === "string" ? value.sourceType.trim() : "";
  const commandId = typeof value.commandId === "string" ? value.commandId.trim() : "";
  if (!ID.test(projectId) || !SOURCE_TYPE.test(sourceType) || !ID.test(commandId)) {
    throw new Error("context_graph_import_request_invalid");
  }
  return Object.freeze({ projectId, sourceType, commandId });
}

function safeSelection(value, request) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("context_graph_import_selection_response_invalid");
  const selectionId = typeof value.selection_id === "string" ? value.selection_id : "";
  const projectId = typeof value.project_id === "string" ? value.project_id : "";
  const sourceType = typeof value.source_type === "string" ? value.source_type : "";
  const displayName = typeof value.display_name === "string" ? value.display_name : "";
  const expiresAt = typeof value.expires_at === "string" ? value.expires_at : "";
  if (!ID.test(selectionId) || projectId !== request.projectId || sourceType !== request.sourceType
    || !DISPLAY_NAME.test(displayName) || !ISO_TIMESTAMP.test(expiresAt)) {
    throw new Error("context_graph_import_selection_response_invalid");
  }
  return Object.freeze({
    selection_id: selectionId,
    project_id: projectId,
    source_type: sourceType,
    display_name: displayName,
    expires_at: expiresAt,
  });
}

module.exports = { CHANNEL, ContextGraphImportIpcController, FILE_FILTERS, SOURCE_TYPE, parseRequest, safeSelection };
