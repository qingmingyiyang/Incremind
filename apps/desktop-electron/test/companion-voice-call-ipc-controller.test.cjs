const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CHANNELS,
  PLAYBACK_CHANNEL,
  CompanionVoiceCallIpcController,
} = require("../src/companion/voice-call-ipc-controller.cjs");

function fixture(overrides = {}) {
  const handlers = new Map();
  const listeners = new Map();
  const removedHandlers = [];
  const removedListeners = [];
  const calls = [];
  const mainWindow = { webContents: { mainFrame: { id: "main-frame" } } };
  const petWindow = { isDestroyed: () => false, webContents: { id: 42 } };
  const microphonePermission = {
    arm: (sender, options) => { calls.push(["arm", sender, options]); return { status: "armed" }; },
    clear: () => calls.push(["permission-clear"]),
  };
  const voice = { cancel: () => calls.push(["voice-cancel"]) };
  const voiceCall = {
    transcribe: async (payload) => { calls.push(["transcribe", payload]); return { text: "你好" }; },
    cancel: () => { calls.push(["voice-call-cancel"]); return true; },
  };
  const presentation = {
    present: async (payload) => { calls.push(["presentation-present", payload]); return { status: "completed" }; },
    cancel: () => calls.push(["presentation-cancel"]),
    acknowledge: (requestId, status) => { calls.push(["ack", requestId, status]); return true; },
  };
  const options = {
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => { removedHandlers.push(channel); handlers.delete(channel); },
      on: (channel, listener) => listeners.set(channel, listener),
      removeListener: (channel, listener) => {
        removedListeners.push([channel, listener]);
        if (listeners.get(channel) === listener) listeners.delete(channel);
      },
    },
    requireMainRenderer: () => calls.push(["guard"]),
    mainWindowProvider: () => mainWindow,
    petWindowProvider: () => petWindow,
    microphonePermission,
    voiceProvider: () => voice,
    voiceCallProvider: () => voiceCall,
    presentationProvider: () => presentation,
    cancelAudio: () => calls.push(["audio-cancel"]),
    presentReply: (text, settings) => calls.push(["present-reply", text, settings]),
    ...overrides,
  };
  return {
    controller: new CompanionVoiceCallIpcController(options), handlers, listeners,
    removedHandlers, removedListeners, calls, mainWindow, petWindow,
    microphonePermission, voice, voiceCall, presentation,
  };
}

test("installs and disposes the fixed half-duplex voice transaction", () => {
  const value = fixture();
  assert.equal(value.controller.install(), true);
  assert.deepEqual([...value.handlers.keys()], CHANNELS);
  assert.deepEqual([...value.listeners.keys()], [PLAYBACK_CHANNEL]);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.deepEqual(value.removedHandlers, CHANNELS);
  assert.equal(value.removedListeners.length, 1);
  assert.equal(value.removedListeners[0][0], PLAYBACK_CHANNEL);
  assert.equal(value.controller.dispose(), false);
});

test("all main invokes validate the sender before providers or side effects", async () => {
  const calls = [];
  const value = fixture({
    requireMainRenderer: () => { calls.push("guard"); throw new Error("ipc_main_sender_rejected"); },
    mainWindowProvider: () => { calls.push("main-window"); return null; },
    voiceProvider: () => { calls.push("voice"); return null; },
    voiceCallProvider: () => { calls.push("voice-call"); return null; },
    presentationProvider: () => { calls.push("presentation"); return null; },
    cancelAudio: () => calls.push("audio"),
    presentReply: () => calls.push("reply"),
  });
  value.controller.install();
  for (const channel of CHANNELS) {
    await assert.rejects(Promise.resolve().then(() => value.handlers.get(channel)({}, {})), /ipc_main_sender_rejected/);
  }
  assert.deepEqual(calls, ["guard", "guard", "guard", "guard"]);
});

test("microphone arm requires the exact transient activation and preserves the main-frame grant", () => {
  const value = fixture();
  value.controller.install();
  const event = { sender: { id: 7 }, senderFrame: value.mainWindow.webContents.mainFrame };
  assert.deepEqual(value.handlers.get(CHANNELS[0])(event, { transient_activation: true }), { status: "armed" });
  assert.deepEqual(value.calls, [
    ["guard"],
    ["voice-cancel"],
    ["audio-cancel"],
    ["arm", event.sender, { purpose: "companion-voice", transientActivation: true, topFrame: true }],
  ]);

  const rejected = fixture();
  rejected.controller.install();
  assert.throws(() => rejected.handlers.get(CHANNELS[0])(event, { transient_activation: true, extra: true }), /companion_microphone_activation_required/);
  assert.deepEqual(rejected.calls, [["guard"]]);
});

test("transcription checks availability before consuming the microphone grant", async () => {
  const unavailable = fixture({ voiceCallProvider: () => null });
  unavailable.controller.install();
  await assert.rejects(
    Promise.resolve().then(() => unavailable.handlers.get(CHANNELS[1])({}, { request_id: "voice:12345678" })),
    /companion_voice_call_unavailable/,
  );
  assert.deepEqual(unavailable.calls, [["guard"]]);

  const value = fixture();
  value.controller.install();
  const payload = { request_id: "voice:12345678", bytes: new Uint8Array([1]) };
  assert.deepEqual(await value.handlers.get(CHANNELS[1])({}, payload), { text: "你好" });
  assert.deepEqual(value.calls, [["guard"], ["permission-clear"], ["transcribe", payload]]);
});

test("presentation projects text without regular speech before awaiting playback", async () => {
  const value = fixture();
  value.controller.install();
  const payload = { request_id: "voice:12345678", text: "回答" };
  assert.deepEqual(await value.handlers.get(CHANNELS[2])({}, payload), { status: "completed" });
  assert.deepEqual(value.calls, [
    ["guard"],
    ["present-reply", "回答", { speak: false }],
    ["presentation-present", payload],
  ]);

  for (const rejectedPayload of [null, [], { request_id: payload.request_id }, { ...payload, extra: true }]) {
    const rejected = fixture();
    rejected.controller.install();
    await assert.rejects(Promise.resolve().then(() => rejected.handlers.get(CHANNELS[2])({}, rejectedPayload)), /companion_voice_call_present_rejected/);
    assert.deepEqual(rejected.calls, [["guard"]]);
  }
});

test("cancel clears every half-duplex surface and projects only transcription cancellation", () => {
  const value = fixture();
  value.controller.install();
  assert.deepEqual(value.handlers.get(CHANNELS[3])({}), { cancelled: true });
  assert.deepEqual(value.calls, [
    ["guard"],
    ["permission-clear"],
    ["voice-cancel"],
    ["audio-cancel"],
    ["presentation-cancel"],
    ["voice-call-cancel"],
  ]);

  const absent = fixture({ voiceProvider: () => null, voiceCallProvider: () => null, presentationProvider: () => null });
  absent.controller.install();
  assert.deepEqual(absent.handlers.get(CHANNELS[3])({}), { cancelled: false });
  assert.deepEqual(absent.calls, [["guard"], ["permission-clear"], ["audio-cancel"]]);
});

test("playback acknowledgement accepts only the current pet and bounded statuses", () => {
  const value = fixture();
  value.controller.install();
  const listener = value.listeners.get(PLAYBACK_CHANNEL);
  assert.equal(listener({ sender: { id: 41 } }, { request_id: "voice:12345678", status: "ended" }), false);
  assert.equal(listener({ sender: { id: 42 } }, { request_id: "voice:12345678", status: "unknown" }), false);
  assert.deepEqual(value.calls, []);
  assert.equal(listener({ sender: { id: 42 } }, { request_id: "voice:12345678", status: "playing" }), true);
  assert.deepEqual(value.calls, [["ack", "voice:12345678", "playing"]]);

  const absent = fixture({ presentationProvider: () => null });
  absent.controller.install();
  assert.equal(absent.listeners.get(PLAYBACK_CHANNEL)({ sender: { id: 42 } }, { request_id: "voice:12345678", status: "ended" }), false);
});

test("failed registration rolls back both the event and registered invoke channels", () => {
  const removedHandlers = [];
  const removedListeners = [];
  const value = fixture({
    ipcMain: {
      handle: () => {},
      removeHandler: (channel) => removedHandlers.push(channel),
      on: () => { throw new Error("registration_failed"); },
      removeListener: (channel) => removedListeners.push(channel),
    },
  });
  assert.throws(() => value.controller.install(), /registration_failed/);
  assert.deepEqual(removedHandlers, CHANNELS);
  assert.deepEqual(removedListeners, [PLAYBACK_CHANNEL]);
  assert.equal(value.controller.installed, false);
});
