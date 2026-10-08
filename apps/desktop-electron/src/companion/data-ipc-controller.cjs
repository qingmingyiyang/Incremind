const CHANNELS = Object.freeze([
  "chriptmas:companion-manual-open",
  "chriptmas:companion-data-backup",
  "chriptmas:companion-data-restore-preflight",
  "chriptmas:companion-data-restore",
]);

const RESTORE_GRANT_TTL_MS = 10 * 60 * 1000;

class CompanionDataIpcController {
  constructor({
    ipcMain,
    dialog,
    shell,
    crypto,
    fetchImpl,
    sessionHeader,
    sessionProvider,
    mainWindowProvider,
    manualPathProvider,
    requireMainRenderer,
    createTimeoutSignal,
    now = () => Date.now(),
  }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("companion_data_ipc_invalid");
    }
    if (!dialog || typeof dialog.showSaveDialog !== "function" || typeof dialog.showOpenDialog !== "function"
      || typeof dialog.showMessageBox !== "function" || !shell || typeof shell.openPath !== "function"
      || !crypto || typeof crypto.createHmac !== "function" || typeof crypto.randomUUID !== "function"
      || typeof fetchImpl !== "function" || typeof sessionHeader !== "string" || !sessionHeader
      || typeof sessionProvider !== "function" || typeof mainWindowProvider !== "function"
      || typeof manualPathProvider !== "function" || typeof requireMainRenderer !== "function"
      || typeof createTimeoutSignal !== "function" || typeof now !== "function") {
      throw new TypeError("companion_data_boundary_invalid");
    }
    Object.assign(this, {
      ipcMain,
      dialog,
      shell,
      crypto,
      fetchImpl,
      sessionHeader,
      sessionProvider,
      mainWindowProvider,
      manualPathProvider,
      requireMainRenderer,
      createTimeoutSignal,
      now,
    });
    this.restoreGrants = new Map();
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = [
      (event) => this.openManual(event),
      (event) => this.backup(event),
      (event) => this.restorePreflight(event),
      (event, payload) => this.restore(event, payload),
    ];
    const registered = [];
    try {
      CHANNELS.forEach((channel, index) => {
        this.ipcMain.handle(channel, handlers[index]);
        registered.push(channel);
      });
    } catch (error) {
      for (const channel of registered) this.ipcMain.removeHandler(channel);
      throw error;
    }
    this.installed = true;
    return true;
  }

  async openManual(event) {
    this.requireMainRenderer(event);
    try {
      const error = await this.shell.openPath(this.manualPathProvider());
      return error ? { status: "failed", error: "manual_editor_unavailable" } : { status: "opened" };
    } catch {
      return { status: "failed", error: "manual_authority_unavailable" };
    }
  }

  async backup(event) {
    this.requireMainRenderer(event);
    const mainWindow = this.requireWindow();
    const selection = await this.dialog.showSaveDialog(mainWindow, {
      title: "导出桌宠数据备份",
      defaultPath: `chriptmas-companion-${new Date(this.now()).toISOString().slice(0, 10)}.sqlite3`,
      filters: [{ name: "Chriptmas Companion backup", extensions: ["sqlite3"] }],
      properties: ["dontAddToRecent", "showOverwriteConfirmation"],
    });
    if (selection.canceled || !selection.filePath) return { status: "cancelled" };
    return this.requestData("backup", "/api/rebuild/companion/data/backup", { path: selection.filePath });
  }

  async restorePreflight(event) {
    this.requireMainRenderer(event);
    const mainWindow = this.requireWindow();
    const selection = await this.dialog.showOpenDialog(mainWindow, {
      title: "选择桌宠数据备份",
      properties: ["openFile", "dontAddToRecent"],
      filters: [{ name: "Chriptmas Companion backup", extensions: ["sqlite3"] }],
    });
    if (selection.canceled || selection.filePaths.length !== 1) return { status: "cancelled" };
    const selectedPath = selection.filePaths[0];
    const payload = await this.requestData(
      "restore-preflight",
      "/api/rebuild/companion/data/restore/preflight",
      { path: selectedPath },
    );
    if (typeof payload.fingerprint !== "string" || !payload.fingerprint) {
      throw new Error("companion_restore_preflight_invalid");
    }
    const grantId = this.crypto.randomUUID();
    this.restoreGrants.clear();
    this.restoreGrants.set(grantId, {
      path: selectedPath,
      fingerprint: payload.fingerprint,
      senderId: event.sender.id,
      expiresAt: this.now() + RESTORE_GRANT_TTL_MS,
    });
    return { ...payload, grant_id: grantId };
  }

  async restore(event, payload) {
    this.requireMainRenderer(event);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)
      || Object.keys(payload).sort().join() !== "expected_fingerprint,grant_id"
      || typeof payload.grant_id !== "string" || typeof payload.expected_fingerprint !== "string") {
      throw new Error("companion_restore_payload_rejected");
    }
    const grant = this.restoreGrants.get(payload.grant_id);
    if (!grant || grant.senderId !== event.sender.id || grant.expiresAt <= this.now()
      || grant.fingerprint !== payload.expected_fingerprint) {
      this.restoreGrants.delete(payload.grant_id);
      throw new Error("companion_restore_grant_rejected");
    }
    const mainWindow = this.requireWindow();
    const confirmation = await this.dialog.showMessageBox(mainWindow, {
      type: "warning",
      title: "确认恢复桌宠数据",
      message: "恢复将用所选备份替换当前桌宠数据。",
      detail: "执行前会自动创建当前数据库的回滚备份。关闭对话框或选择取消都不会写入。",
      buttons: ["取消", "确认恢复"],
      defaultId: 0,
      cancelId: 0,
      noLink: true,
    });
    if (confirmation.response !== 1) return { status: "cancelled" };
    this.restoreGrants.delete(payload.grant_id);
    return this.requestData("restore", "/api/rebuild/companion/data/restore", {
      path: grant.path,
      expected_fingerprint: grant.fingerprint,
    });
  }

  requireWindow() {
    const mainWindow = this.mainWindowProvider();
    if (!mainWindow?.isVisible() || !mainWindow?.isFocused()) {
      throw new Error("companion_data_window_required");
    }
    return mainWindow;
  }

  async requestData(action, pathname, body) {
    const active = this.sessionProvider();
    if (!active) throw new Error("companion_data_sidecar_unavailable");
    const fingerprint = typeof body?.expected_fingerprint === "string" ? body.expected_fingerprint : "";
    const signature = this.crypto.createHmac("sha256", active.secret)
      .update(`companion-data:${action}:${body.path}:${fingerprint}`)
      .digest("hex");
    const response = await this.fetchImpl(`${active.origin}${pathname}`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        [this.sessionHeader]: active.secret,
        "X-Chriptmas-Main-Signature": signature,
      },
      body: JSON.stringify(body),
      signal: this.createTimeoutSignal(30000),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload?.error?.message || `companion_data_http_${response.status}`);
    return payload;
  }

  dispose() {
    this.restoreGrants.clear();
    if (!this.installed) return false;
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, CompanionDataIpcController, RESTORE_GRANT_TTL_MS };
