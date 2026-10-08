"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { SidecarFailurePresenter } = require("../src/sidecar-failure-presenter.cjs");

function fixture({ response = 1, pending = false, isQuitting = false } = {}) {
  const events = [];
  let resolveDialog;
  const dialogResult = pending ? new Promise((resolve) => { resolveDialog = resolve; }) : Promise.resolve({ response });
  const app = {
    isQuitting,
    relaunch: () => events.push("relaunch"),
    exit: () => events.push("exit"),
  };
  const presenter = new SidecarFailurePresenter({
    app,
    dialog: { showMessageBox: (options) => { events.push(["dialog", options]); return dialogResult; } },
    markBackendOffline: () => events.push("offline"),
  });
  return { presenter, events, resolveDialog };
}

test("Vault conflict uses the fixed native fail-closed message", async () => {
  const value = fixture();
  await value.presenter.presentVaultConflict();
  assert.deepEqual(value.events, [["dialog", {
    type: "error",
    title: "检测到两份本地资料",
    message: "旧数据目录和正式 Vault 同时包含资料。为保护数据，应用没有启动本地后端。请先完成资料迁移。",
    buttons: ["退出"],
    defaultId: 0,
    noLink: true,
  }]]);
});

test("unexpected exit marks offline then exits without relaunch for the exit action", async () => {
  const value = fixture({ response: 1 });
  assert.deepEqual(await value.presenter.presentUnexpectedExit(), { status: "handled", action: "exit" });
  assert.equal(value.events[0], "offline");
  assert.deepEqual(value.events.at(-1), "exit");
  assert.equal(value.events.includes("relaunch"), false);
});

test("restart action preserves relaunch before exit ordering", async () => {
  const value = fixture({ response: 0 });
  assert.deepEqual(await value.presenter.presentUnexpectedExit(), { status: "handled", action: "restart" });
  assert.deepEqual(value.events.slice(-2), ["relaunch", "exit"]);
});

test("application shutdown suppresses the unexpected-exit projection and prompt", async () => {
  const value = fixture({ isQuitting: true });
  assert.deepEqual(await value.presenter.presentUnexpectedExit(), { status: "ignored", reason: "application_quitting" });
  assert.deepEqual(value.events, []);
});

test("concurrent unexpected exits share one native decision", async () => {
  const value = fixture({ pending: true });
  const first = value.presenter.presentUnexpectedExit();
  const second = value.presenter.presentUnexpectedExit();
  assert.equal(second, first);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(value.events.filter((entry) => entry === "offline").length, 1);
  assert.equal(value.events.filter((entry) => Array.isArray(entry) && entry[0] === "dialog").length, 1);
  value.resolveDialog({ response: 1 });
  await first;
  assert.equal(value.events.filter((entry) => entry === "exit").length, 1);
});

test("constructor rejects incomplete native dependencies", () => {
  assert.throws(() => new SidecarFailurePresenter(), /sidecar_failure_presenter_options_invalid/);
});

test("renewal warning is deduplicated and retries without exiting", async () => {
  const value = fixture({ response: 0 });
  let retries = 0;
  const first = value.presenter.presentSessionRenewalFailure({ expired: false, retry: () => { retries += 1; } });
  const second = value.presenter.presentSessionRenewalFailure({ expired: false, retry: () => { retries += 1; } });
  assert.equal(first, second);
  assert.deepEqual(await first, { status: "handled", action: "retry" });
  assert.equal(retries, 1);
  assert.equal(value.events.includes("offline"), false);
  assert.equal(value.events.includes("exit"), false);
});

test("expired renewal waits by default and only an explicit restart exits", async () => {
  const waiting = fixture({ response: 0 });
  assert.deepEqual(await waiting.presenter.presentSessionRenewalFailure({ expired: true }), { status: "handled", action: "wait" });
  assert.equal(waiting.events[0], "offline");
  assert.equal(waiting.events.includes("exit"), false);
  const restarting = fixture({ response: 1 });
  assert.deepEqual(await restarting.presenter.presentSessionRenewalFailure({ expired: true }), { status: "handled", action: "restart" });
  assert.deepEqual(restarting.events.slice(-2), ["relaunch", "exit"]);
});
