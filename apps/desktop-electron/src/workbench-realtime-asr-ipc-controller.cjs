"use strict";

const CHANNEL = "chriptmas:workbench-realtime-asr-ticket";
const TICKET_PATH = "/api/rebuild/workbench/realtime-asr/ticket";
const TICKET_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;
const SAFE_ERROR_PATTERN = /^[a-z][a-z0-9_]{2,80}$/;

class WorkbenchRealtimeAsrIpcController {
  constructor({ ipcMain, requireMainRenderer, gateway } = {}) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("workbench_realtime_asr_ipc_invalid");
    }
    if (typeof requireMainRenderer !== "function" || !gateway || typeof gateway.requestJson !== "function") {
      throw new TypeError("workbench_realtime_asr_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.requireMainRenderer = requireMainRenderer;
    this.gateway = gateway;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    this.ipcMain.handle(CHANNEL, (event) => this.issueTicket(event));
    this.installed = true;
    return true;
  }

  async issueTicket(event) {
    this.requireMainRenderer(event);
    const result = await this.gateway.requestJson({
      method: "POST",
      pathname: TICKET_PATH,
      timeoutMs: 5000,
      unavailableError: "realtime_asr_sidecar_unavailable",
      nullableJson: true,
      parseErrorJson: true,
    });
    if (!result.ok) {
      const detail = String(result.payload?.detail || "");
      throw new Error(SAFE_ERROR_PATTERN.test(detail) ? detail : "realtime_asr_ticket_request_failed");
    }
    const ticket = String(result.payload?.ticket || "");
    if (!TICKET_PATTERN.test(ticket)) throw new Error("realtime_asr_ticket_invalid");
    return Object.freeze({
      ticket,
      expires_in_seconds: Number(result.payload?.expires_in_seconds) || 0,
      single_use: result.payload?.single_use === true,
    });
  }

  dispose() {
    if (!this.installed) return false;
    this.ipcMain.removeHandler(CHANNEL);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNEL, TICKET_PATH, TICKET_PATTERN, WorkbenchRealtimeAsrIpcController };
