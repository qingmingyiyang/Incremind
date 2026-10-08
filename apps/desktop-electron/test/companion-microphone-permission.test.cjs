const assert = require("node:assert/strict");
const test = require("node:test");

const { CompanionMicrophonePermission, MICROPHONE_PERMISSION_TTL_MS, MICROPHONE_PURPOSES } = require("../src/companion/microphone-permission.cjs");

test("microphone permission is main-window, audio-only, armed, expiring, and single-use", () => {
  let now = 1_000;
  const mainSender = {};
  const otherSender = {};
  let visible = true; let focused = true;
  const permission = new CompanionMicrophonePermission({
    mainWindowProvider: () => ({ webContents: Object.assign(mainSender, { getURL: () => "file:///C:/app/index.html" }), isDestroyed: () => false, isVisible: () => visible, isFocused: () => focused }),
    now: () => now,
  });

  const check = { mediaType: "audio", isMainFrame: true, requestingOrigin: "file://" };
  assert.equal(permission.decideCheck(mainSender, "media", check), false);
  assert.throws(() => permission.arm(otherSender, { transientActivation: true, topFrame: true }), /sender_denied/);
  assert.throws(() => permission.arm(mainSender, { transientActivation: false, topFrame: true }), /sender_denied/);
  assert.throws(() => permission.arm(mainSender, { transientActivation: true, topFrame: false }), /sender_denied/);
  assert.equal(permission.arm(mainSender, { transientActivation: true, topFrame: true }).status, "armed");
  assert.equal(permission.decideCheck(otherSender, "media", check), false);
  assert.equal(permission.decideCheck(mainSender, "media", { mediaType: "video", isMainFrame: true }), false);
  assert.equal(permission.decideCheck(mainSender, "media", { mediaType: "unknown", isMainFrame: true }), false);
  assert.equal(permission.decideCheck(mainSender, "media", { mediaType: "audio", isMainFrame: false }), false);
  assert.equal(permission.decideCheck(mainSender, "media", { ...check, requestingOrigin: "https://evil.example" }), false);
  assert.equal(permission.decideCheck(mainSender, "media", check), true);
  assert.equal(permission.decideRequest(mainSender, "media", { mediaTypes: ["audio", "video"] }), false);
  assert.equal(permission.decideRequest(mainSender, "media", { mediaTypes: ["audio"] }), true);
  assert.equal(permission.decideCheck(mainSender, "media", check), false);

  permission.arm(mainSender, { transientActivation: true, topFrame: true });
  focused = false;
  assert.equal(permission.decideCheck(mainSender, "media", check), false);
  focused = true; visible = false;
  assert.throws(() => permission.arm(mainSender, { transientActivation: true, topFrame: true }), /sender_denied/);
  visible = true;
  permission.arm(mainSender, { transientActivation: true, topFrame: true });
  now += MICROPHONE_PERMISSION_TTL_MS - 1;
  assert.equal(permission.decideCheck(mainSender, "media", check), true);
  now += 2;
  assert.equal(permission.decideCheck(mainSender, "media", check), false);
});

test("workbench transcription is a fixed, separately labeled microphone purpose", () => {
  let now = 1_000;
  const mainSender = {};
  const permission = new CompanionMicrophonePermission({
    mainWindowProvider: () => ({ webContents: Object.assign(mainSender, { getURL: () => "file:///C:/app/index.html" }), isDestroyed: () => false, isVisible: () => true, isFocused: () => true }),
    now: () => now,
  });
  assert.deepEqual(MICROPHONE_PURPOSES, ["companion-voice", "workbench-transcription"]);
  assert.throws(() => permission.arm(mainSender, { purpose: "arbitrary", transientActivation: true, topFrame: true }), /microphone_purpose_denied/);
  assert.deepEqual(
    permission.arm(mainSender, { purpose: "workbench-transcription", transientActivation: true, topFrame: true }),
    { status: "armed", purpose: "workbench-transcription", expires_at_ms: 61_000 },
  );
  assert.equal(permission.decideRequest(mainSender, "media", { mediaTypes: ["audio"] }), true);
  assert.equal(permission.purpose, null);
  now += 1;
  assert.equal(permission.decideCheck(mainSender, "media", { mediaType: "audio", isMainFrame: true, requestingOrigin: "file://" }), false);
});

test("microphone permission cannot be extended beyond the fixed 60 second window", () => {
  assert.equal(MICROPHONE_PERMISSION_TTL_MS, 60_000);
  assert.throws(
    () => new CompanionMicrophonePermission({ mainWindowProvider: () => null, ttlMs: 60_001 }),
    /dependencies are invalid/,
  );
});

test("workbench bridge remains fixed to a real user activation and main-only IPC", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const root = path.join(__dirname, "..", "src");
  const preload = fs.readFileSync(path.join(root, "preload.cjs"), "utf8");
  const main = fs.readFileSync(path.join(root, "main.cjs"), "utf8");
  assert.match(preload, /armWorkbenchMicrophone: \(\) => \{/);
  assert.match(preload, /navigator\.userActivation\?\.isActive !== true/);
  assert.match(preload, /chriptmas:workbench-microphone-arm/);
  assert.match(main, /ipcMain\.handle\("chriptmas:workbench-microphone-arm"/);
  assert.match(main, /requireMainRenderer\(event\)/);
  assert.match(main, /purpose: "workbench-transcription"/);
  assert.match(main, /topFrame: event\.senderFrame === mainWindow\?\.webContents\?\.mainFrame/);
});
