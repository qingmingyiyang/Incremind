const crypto = require("node:crypto");
const { SESSION_HEADER } = require("./sidecar-supervisor.cjs");

const CHANNEL = "chriptmas:open-original-asset";
const ASSET_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,239}$/;

class OriginalAssetIpcController {
  constructor({
    ipcMain,
    shell,
    requireMainRenderer,
    sessionProvider,
    fetchImpl = fetch,
    cryptoApi = crypto,
    sessionHeader = SESSION_HEADER,
    timeoutSignal = (milliseconds) => AbortSignal.timeout(milliseconds),
  }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("original_asset_ipc_invalid");
    }
    if (!shell || typeof shell.openPath !== "function") throw new TypeError("original_asset_shell_invalid");
    if (typeof requireMainRenderer !== "function" || typeof sessionProvider !== "function"
      || typeof fetchImpl !== "function" || typeof timeoutSignal !== "function") {
      throw new TypeError("original_asset_boundary_invalid");
    }
    if (!cryptoApi || typeof cryptoApi.createHmac !== "function"
      || typeof sessionHeader !== "string" || !sessionHeader) {
      throw new TypeError("original_asset_auth_invalid");
    }
    this.ipcMain = ipcMain;
    this.shell = shell;
    this.requireMainRenderer = requireMainRenderer;
    this.sessionProvider = sessionProvider;
    this.fetch = fetchImpl;
    this.crypto = cryptoApi;
    this.sessionHeader = sessionHeader;
    this.timeoutSignal = timeoutSignal;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    this.ipcMain.handle(CHANNEL, (event, options) => this.open(event, options));
    this.installed = true;
    return true;
  }

  async open(event, options = {}) {
    this.requireMainRenderer(event);
    const assetId = typeof options?.assetId === "string" ? options.assetId.trim() : "";
    if (!ASSET_ID.test(assetId)) return { status: "rejected", reason: "asset_id_invalid" };

    const active = this.sessionProvider();
    if (!active) return { status: "unavailable", reason: "sidecar_not_ready" };
    const signature = this.crypto
      .createHmac("sha256", active.secret)
      .update(`open-original-asset:${assetId}`)
      .digest("hex");
    try {
      const response = await this.fetch(
        `${active.origin}/api/rebuild/desktop/original-assets/${encodeURIComponent(assetId)}/resolve`,
        {
          headers: {
            Accept: "application/json",
            [this.sessionHeader]: active.secret,
            "X-Chriptmas-Main-Signature": signature,
          },
          signal: this.timeoutSignal(5000),
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || typeof payload.resolved_path !== "string") {
        return { status: payload.status || "unavailable", reason: payload.reason || `http_${response.status}` };
      }
      const result = await this.shell.openPath(payload.resolved_path);
      return result ? { status: "failed", reason: result } : { status: "opened" };
    } catch (error) {
      return { status: "failed", reason: error?.message || "original_asset_open_failed" };
    }
  }

  dispose() {
    if (!this.installed) return false;
    this.ipcMain.removeHandler(CHANNEL);
    this.installed = false;
    return true;
  }
}

module.exports = { ASSET_ID, CHANNEL, OriginalAssetIpcController };
