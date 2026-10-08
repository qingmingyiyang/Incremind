const CHANNEL = "chriptmas:vault-restore";
const SNAPSHOT_ID = /^snap-[a-z0-9][a-z0-9._-]{0,122}$/;
const ROLLBACK_ID = /^rb-[0-9a-f]{12}$/;

function isRestoreRequest(payload) {
  return Boolean(
    payload
      && typeof payload === "object"
      && !Array.isArray(payload)
      && Object.keys(payload).sort().join() === "rollback_id,snapshot_id"
      && SNAPSHOT_ID.test(payload.snapshot_id)
      && ROLLBACK_ID.test(payload.rollback_id),
  );
}

class VaultRestoreIpcController {
  constructor({
    ipcMain,
    dialog,
    requireMainRenderer,
    mainWindowProvider,
    sessionProvider,
    recoveryProvider,
    sidecarStop,
    scheduleRelaunch,
    sessionHeader,
    fetchImpl = fetch,
    timeoutSignal = (milliseconds) => AbortSignal.timeout(milliseconds),
  }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("vault_restore_ipc_invalid");
    }
    if (!dialog || typeof dialog.showMessageBox !== "function") {
      throw new TypeError("vault_restore_dialog_invalid");
    }
    if ([requireMainRenderer, mainWindowProvider, sessionProvider, recoveryProvider, sidecarStop,
      scheduleRelaunch, fetchImpl, timeoutSignal].some((value) => typeof value !== "function")) {
      throw new TypeError("vault_restore_boundary_invalid");
    }
    if (typeof sessionHeader !== "string" || !sessionHeader) {
      throw new TypeError("vault_restore_session_header_invalid");
    }
    this.ipcMain = ipcMain;
    this.dialog = dialog;
    this.requireMainRenderer = requireMainRenderer;
    this.mainWindowProvider = mainWindowProvider;
    this.sessionProvider = sessionProvider;
    this.recoveryProvider = recoveryProvider;
    this.sidecarStop = sidecarStop;
    this.scheduleRelaunch = scheduleRelaunch;
    this.sessionHeader = sessionHeader;
    this.fetch = fetchImpl;
    this.timeoutSignal = timeoutSignal;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    this.ipcMain.handle(CHANNEL, (event, payload) => this.restore(event, payload));
    this.installed = true;
    return true;
  }

  async restore(event, payload) {
    this.requireMainRenderer(event);
    if (!isRestoreRequest(payload)) throw new Error("vault_restore_payload_rejected");

    const mainWindow = this.mainWindowProvider();
    if (!mainWindow?.isVisible() || !mainWindow?.isFocused()) {
      throw new Error("vault_restore_window_required");
    }
    const confirmation = await this.dialog.showMessageBox(mainWindow, {
      type: "warning",
      title: "确认恢复本地记忆保险箱",
      message: "应用将恢复到所选恢复点并自动重启。",
      detail: "恢复内容会先在隔离目录完整校验。当前 Vault 会保留为回滚副本；关闭对话框或选择取消不会修改数据。",
      buttons: ["取消", "确认恢复并重启"],
      defaultId: 0,
      cancelId: 0,
      noLink: true,
    });
    if (confirmation.response !== 1) return { status: "cancelled" };

    const active = this.sessionProvider();
    const recovery = this.recoveryProvider();
    if (!active || !recovery) throw new Error("vault_restore_runtime_unavailable");
    const endpoint = `${active.origin}/api/rebuild/memory-snapshots/${encodeURIComponent(payload.snapshot_id)}/rollback`;
    const response = await this.fetch(endpoint, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        [this.sessionHeader]: active.secret,
      },
      body: JSON.stringify({ rollback_id: payload.rollback_id, confirm: true }),
      signal: this.timeoutSignal(120000),
    });
    const prepared = await response.json().catch(() => ({}));
    if (!response.ok || prepared.status !== "prepared_restart_required"
      || typeof prepared.operation_id !== "string") {
      throw new Error(prepared.detail || `vault_restore_prepare_http_${response.status}`);
    }

    await this.sidecarStop();
    try {
      const adopted = await recovery.adopt(prepared.operation_id);
      this.scheduleRelaunch();
      return { status: "restored_relaunching", operation_id: adopted.operation_id };
    } catch (error) {
      await this.dialog.showMessageBox(mainWindow, {
        type: "error",
        title: "恢复切换尚未完成",
        message: "应用将重新启动并继续核对恢复状态。",
        detail: "当前 Vault 不会被静默删除。若恢复记录存在矛盾，启动会停止并保留现场。",
        buttons: ["重新启动"],
        defaultId: 0,
        noLink: true,
      });
      this.scheduleRelaunch();
      return { status: "recovery_pending_relaunch", error: String(error?.message || error) };
    }
  }

  dispose() {
    if (!this.installed) return false;
    this.ipcMain.removeHandler(CHANNEL);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNEL, VaultRestoreIpcController, isRestoreRequest };
