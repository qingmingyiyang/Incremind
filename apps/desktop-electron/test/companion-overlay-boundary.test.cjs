const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const root = path.join(__dirname, "..", "src");

test("overlay bridge is fixed and cannot access backend, paths, shell or generic IPC", () => {
  const preload = fs.readFileSync(path.join(root, "companion-overlay-preload.cjs"), "utf8");
  for (const api of ["subscribeCompanionOverlay", "submitCompanionText", "performCompanionAction", "acknowledgeCompanionEvent", "closeCompanionOverlay", "openCompanionCenter"]) assert.match(preload, new RegExp(api));
  for (const forbidden of ["backendBaseUrl", "openPath", "shell", "webUtils", "send:", "invoke: payload", "selectLocalFile"]) assert.doesNotMatch(preload, new RegExp(forbidden));
});

test("overlay window uses a nonpersistent restricted session and local CSP", () => {
  const main = fs.readFileSync(path.join(root, "main.cjs"), "utf8");
  const windowFactory = fs.readFileSync(path.join(root, "desktop-window-factory.cjs"), "utf8");
  const surfaceCoordinator = fs.readFileSync(path.join(root, "desktop-surface-coordinator.cjs"), "utf8");
  const block = main.slice(main.indexOf("function createCompanionOverlayWindow"), main.indexOf("function showMainWindow"));
  assert.match(block, /desktopWindowFactory\.prepareOverlay\(\)/);
  assert.match(block, /createdOverlayWindow\.once\("ready-to-show"[\s\S]*?preparedOverlayWindow\.load\(\)/);
  assert.match(windowFactory, /OVERLAY_SESSION_PARTITION = "chriptmas-companion-overlay"/);
  assert.doesNotMatch(windowFactory, /OVERLAY_SESSION_PARTITION = "persist:/);
  for (const expected of [/sandbox:\s*true/, /nodeIntegration:\s*false/, /contextIsolation:\s*true/, /devTools:\s*false/, /this\.#installNavigationPolicy/]) assert.match(windowFactory, expected);
  assert.match(block, /desktopSurfaceCoordinator\.onOverlayReady\(createdOverlayWindow\)/);
  assert.match(surfaceCoordinator, /this\.overlayFocus = options\.focus !== false/);
  assert.match(surfaceCoordinator, /if \(!this\.overlayFocus\)[\s\S]*?window\.showInactive\(\)/);
  const html = fs.readFileSync(path.join(root, "companion", "overlay.html"), "utf8");
  assert.match(html, /default-src 'none'/);
  assert.match(html, /connect-src 'none'/);
  assert.doesNotMatch(html, /<script(?![^>]*src=)/);
});

test("overlay renders one action and offers acknowledgement only without one", () => {
  const renderer = fs.readFileSync(path.join(root, "companion", "overlay.js"), "utf8");
  assert.match(renderer, /event\.actions\.slice\(0, 1\)/);
  assert.match(renderer, /event\?\.requires_ack === true && projectedActions\.length === 0/);
  const displayedActionHandler = renderer.slice(renderer.indexOf("for (const action of projectedActions)"), renderer.indexOf("if (event?.requires_ack"));
  assert.doesNotMatch(displayedActionHandler, /acknowledgeCompanionEvent/);
});

test("clipboard access stays in main and its settings IPC is main-renderer-bound", () => {
  const main = fs.readFileSync(path.join(root, "main.cjs"), "utf8");
  const controller = fs.readFileSync(path.join(root, "companion", "clipboard-ipc-controller.cjs"), "utf8");
  const pet = fs.readFileSync(path.join(root, "pet-preload.cjs"), "utf8");
  const overlay = fs.readFileSync(path.join(root, "companion-overlay-preload.cjs"), "utf8");
  assert.match(main, /CompanionClipboardWatcher/);
  assert.match(main, /new CompanionClipboardIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?watcher: companionClipboardWatcher/);
  assert.match(main, /showInactive\(\)/);
  for (const channel of ["companion-clipboard-status", "companion-clipboard-enabled"]) {
    assert.doesNotMatch(main, new RegExp(`ipcMain\\.handle\\("chriptmas:${channel}"`));
    assert.match(controller, new RegExp(`"chriptmas:${channel}"`));
  }
  assert.match(controller, /this\.requireMainRenderer\(event\)/);
  assert.doesNotMatch(controller, /readText|writeText|clipboard\.clear/);
  assert.doesNotMatch(pet, /clipboard/i);
  assert.doesNotMatch(overlay, /clipboard/i);
});

test("clipboard bubbles respect focus sleep game and blocking reminder quiet states", () => {
  const main = fs.readFileSync(path.join(root, "main.cjs"), "utf8");
  const start = main.indexOf("const companionClipboardWatcher = new CompanionClipboardWatcher");
  const block = main.slice(start, main.indexOf("const companionGestureController", start));

  assert.match(block, /companionFocusRuntimeController\.isActive\(\)/);
  assert.match(block, /companionSystemSensorRuntimeController\.isGameQuiet\(\)/);
  assert.match(block, /companionRoutineController\.status\(\)\.sleeping/);
  assert.match(block, /companionReminderPresenter\.isBlocking\(\)/);
});

test("chat transport stays in the authenticated main gateway and only bounded projections cross renderer bridges", () => {
  const main = fs.readFileSync(path.join(root, "main.cjs"), "utf8");
  const gateway = fs.readFileSync(path.join(root, "authenticated-local-gateway.cjs"), "utf8");
  const replyIpc = fs.readFileSync(path.join(root, "companion", "reply-ipc-controller.cjs"), "utf8");
  const replyPresentation = fs.readFileSync(path.join(root, "companion", "reply-presentation-controller.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(root, "preload.cjs"), "utf8");
  const pet = fs.readFileSync(path.join(root, "pet-preload.cjs"), "utf8");
  const overlay = fs.readFileSync(path.join(root, "companion-overlay-preload.cjs"), "utf8");

  const requestStart = main.indexOf("async function requestCompanionChat");
  const requestBlock = main.slice(requestStart, main.indexOf("async function speakCompanionReply", requestStart));
  assert.match(requestBlock, /authenticatedLocalGateway\.requestJson/);
  assert.match(requestBlock, /pathname: "\/api\/rebuild\/companion\/chat"/);
  assert.match(requestBlock, /timeoutMs: 65_000/);
  assert.match(gateway, /\[this\.#sessionHeader\]: active\.secret/);
  assert.match(gateway, /this\.#createTimeoutSignal\(timeoutMs\)/);
  assert.doesNotMatch(overlay, /backendBaseUrl|SESSION_HEADER|secret|\/api\/rebuild\/companion\/chat/);
  assert.doesNotMatch(pet, /presentCompanionReply|companion-reply-present/);

  assert.match(replyIpc, /this\.requireMainRenderer\(event\)/);
  assert.match(replyIpc, /Object\.keys\(payload\)\.join\(\) !== "text"/);
  assert.match(preload, /presentCompanionReply/);
  assert.match(replyPresentation, /value\.length > 4_000/);
  assert.match(replyPresentation, /value\.trim\(\)\.slice\(0, 400\)/);
  assert.match(replyPresentation, /this\.appearanceArbiter\.set\([\s\S]*"interactive"[\s\S]*state: "speaking"[\s\S]*animation_key: "talk"/);
  assert.match(replyPresentation, /Math\.min\(8_000, Math\.max\(1_500, text\.length \* 80\)\)/);
  assert.doesNotMatch(main, /ipcMain\.handle\("chriptmas:companion-reply-present"/);
  for (const restricted of ["deleteMessage", "companion-message-delete", "/messages/"]) {
    assert.doesNotMatch(pet, new RegExp(restricted));
    assert.doesNotMatch(overlay, new RegExp(restricted));
  }
});
