"use strict";

const VAULT_CONFLICT_DIALOG = Object.freeze({
  type: "error",
  title: "检测到两份本地资料",
  message: "旧数据目录和正式 Vault 同时包含资料。为保护数据，应用没有启动本地后端。请先完成资料迁移。",
  buttons: ["退出"],
  defaultId: 0,
  noLink: true,
});

const UNEXPECTED_EXIT_DIALOG = Object.freeze({
  type: "error",
  title: "本地后端已停止",
  message: "Chriptmas OS 的本地后端意外停止。重启应用会创建新的安全会话。",
  buttons: ["重启应用", "退出"],
  defaultId: 0,
  cancelId: 1,
  noLink: true,
});
const RENEWAL_WARNING_DIALOG = Object.freeze({
  type: "warning",
  title: "本地会话续期失败",
  message: "暂时无法确认本地会话续期结果，部分新请求可能不可用。系统会自动重试，正在运行的任务不会被自动终止。",
  buttons: ["立即重试", "继续使用"],
  defaultId: 0,
  cancelId: 1,
  noLink: true,
});
const RENEWAL_EXPIRED_DIALOG = Object.freeze({
  type: "error",
  title: "本地会话已到期",
  message: "新请求已暂停，正在运行的任务不会被自动终止。重启应用会中断仍在运行的任务。",
  buttons: ["稍后处理", "重启应用"],
  defaultId: 0,
  cancelId: 0,
  noLink: true,
});

class SidecarFailurePresenter {
  #app;
  #dialog;
  #markBackendOffline;
  #unexpectedExitPresentation = null;
  #renewalWarning = null;
  #renewalExpired = null;

  constructor({ app, dialog, markBackendOffline } = {}) {
    if (!app || typeof app.relaunch !== "function" || typeof app.exit !== "function" || !dialog || typeof dialog.showMessageBox !== "function" || typeof markBackendOffline !== "function") {
      throw new Error("sidecar_failure_presenter_options_invalid");
    }
    this.#app = app;
    this.#dialog = dialog;
    this.#markBackendOffline = markBackendOffline;
  }

  presentVaultConflict() {
    return this.#dialog.showMessageBox(VAULT_CONFLICT_DIALOG);
  }

  presentUnexpectedExit() {
    if (this.#app.isQuitting) return Promise.resolve(Object.freeze({ status: "ignored", reason: "application_quitting" }));
    if (this.#unexpectedExitPresentation) return this.#unexpectedExitPresentation;
    this.#markBackendOffline();
    this.#unexpectedExitPresentation = Promise.resolve()
      .then(() => this.#dialog.showMessageBox(UNEXPECTED_EXIT_DIALOG))
      .then(({ response }) => {
        if (response === 0) this.#app.relaunch();
        this.#app.exit();
        return Object.freeze({ status: "handled", action: response === 0 ? "restart" : "exit" });
      });
    return this.#unexpectedExitPresentation;
  }

  presentSessionRenewalFailure({ expired, retry } = {}) {
    if (this.#app.isQuitting) return Promise.resolve(Object.freeze({ status: "ignored", reason: "application_quitting" }));
    if (expired) {
      if (this.#renewalExpired) return this.#renewalExpired;
      this.#markBackendOffline();
      this.#renewalExpired = Promise.resolve().then(() => this.#dialog.showMessageBox(RENEWAL_EXPIRED_DIALOG))
        .then(({ response }) => {
          if (response === 1) {
            this.#app.relaunch();
            this.#app.exit();
          }
          return Object.freeze({ status: "handled", action: response === 1 ? "restart" : "wait" });
        });
      return this.#renewalExpired;
    }
    if (this.#renewalWarning) return this.#renewalWarning;
    this.#renewalWarning = Promise.resolve().then(() => this.#dialog.showMessageBox(RENEWAL_WARNING_DIALOG))
      .then(async ({ response }) => {
        if (response === 0 && typeof retry === "function") await Promise.resolve(retry()).catch(() => {});
        return Object.freeze({ status: "handled", action: response === 0 ? "retry" : "continue" });
      })
      .finally(() => { this.#renewalWarning = null; });
    return this.#renewalWarning;
  }
}

module.exports = { SidecarFailurePresenter };
