"use strict";

const CHANNEL = "chriptmas:credential-capture";
const MAX_VALUE_LENGTH = 16 * 1024;
const MAX_COMMANDS = 256;
const COMMAND_ID = /^cmd-[a-z0-9][a-z0-9._-]{7,122}$/;
const SUBJECT_ID = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const SECRET_REF = /^[a-z][a-z0-9_.-]{0,63}:[a-z0-9][a-z0-9_.-]{0,127}$/;
const CREDENTIAL_KINDS = new Set(["provider_api_key", "tokenhub_asr_api_key", "qwen_realtime_asr_api_key", "xiaohongshu_cookie"]);

function isCaptureRequest(payload) {
  return Boolean(
    payload && typeof payload === "object" && !Array.isArray(payload)
      && Object.keys(payload).sort().join() === "command_id,credential_kind,credential_subject,value"
      && CREDENTIAL_KINDS.has(payload.credential_kind)
      && SUBJECT_ID.test(payload.credential_subject)
      && typeof payload.value === "string" && payload.value.length > 0 && payload.value.length <= MAX_VALUE_LENGTH
      && COMMAND_ID.test(payload.command_id),
  );
}

function projectCaptureResult(result) {
  if (!result || typeof result !== "object" || Array.isArray(result)
    || result.stored !== true || !SECRET_REF.test(result.secret_ref)
    || !Number.isSafeInteger(result.generation) || result.generation < 1
    || !Number.isSafeInteger(result.authorization_revision) || result.authorization_revision < 1) {
    throw new Error("credential_capture_result_invalid");
  }
  return Object.freeze({
    stored: true,
    secret_ref: result.secret_ref,
    generation: result.generation,
    authorization_revision: result.authorization_revision,
  });
}

class CredentialCaptureIpcController {
  constructor({ ipcMain, requireMainRenderer, captureCredential } = {}) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("credential_capture_ipc_invalid");
    }
    if (typeof requireMainRenderer !== "function" || typeof captureCredential !== "function") {
      throw new TypeError("credential_capture_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.requireMainRenderer = requireMainRenderer;
    this.captureCredential = captureCredential;
    this.completedCommands = new Set();
    this.inFlightCommands = new Set();
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    this.ipcMain.handle(CHANNEL, (event, payload) => this.capture(event, payload));
    this.installed = true;
    return true;
  }

  async capture(event, payload) {
    this.requireMainRenderer(event);
    if (!isCaptureRequest(payload)) throw new Error("credential_capture_payload_rejected");
    if (this.completedCommands.has(payload.command_id) || this.inFlightCommands.has(payload.command_id)) {
      throw new Error("credential_capture_command_replayed");
    }
    this.inFlightCommands.add(payload.command_id);
    try {
      const projected = projectCaptureResult(await this.captureCredential({
        credential_kind: payload.credential_kind,
        credential_subject: payload.credential_subject,
        value: payload.value,
        command_id: payload.command_id,
      }));
      this.completedCommands.add(payload.command_id);
      while (this.completedCommands.size > MAX_COMMANDS) this.completedCommands.delete(this.completedCommands.values().next().value);
      return projected;
    } finally {
      this.inFlightCommands.delete(payload.command_id);
    }
  }

  dispose() {
    if (!this.installed) return false;
    this.ipcMain.removeHandler(CHANNEL);
    this.installed = false;
    this.completedCommands.clear();
    this.inFlightCommands.clear();
    return true;
  }
}

module.exports = {
  CHANNEL,
  CREDENTIAL_KINDS,
  MAX_VALUE_LENGTH,
  CredentialCaptureIpcController,
  isCaptureRequest,
  projectCaptureResult,
};
