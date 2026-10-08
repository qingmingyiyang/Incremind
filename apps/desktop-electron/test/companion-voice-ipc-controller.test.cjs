const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CHANNELS,
  TEST_PHRASE,
  CompanionVoiceIpcController,
} = require("../src/companion/voice-ipc-controller.cjs");

function fixture(overrides = {}) {
  const handlers = new Map();
  const removed = [];
  const calls = { guarded: 0, configured: [], references: [], spoken: [], audio: [] };
  const status = { state: "ready", enabled: false };
  const voice = {
    status: () => status,
    configure: (payload) => { calls.configured.push(payload); return { ...status, ...payload }; },
    setReference: (selectedPath) => { calls.references.push(selectedPath); return { ...status, has_reference: true }; },
    speak: async (text) => { calls.spoken.push(text); return { audio: Buffer.from("wav") }; },
  };
  const options = {
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => { removed.push(channel); handlers.delete(channel); },
    },
    dialog: {
      showOpenDialog: async () => ({ canceled: false, filePaths: ["C:\\fixtures\\reference.wav"] }),
    },
    requireMainRenderer: () => { calls.guarded += 1; },
    mainWindowProvider: () => ({ id: "main" }),
    voiceProvider: () => voice,
    isQuiet: () => false,
    dispatchAudio: (audio) => { calls.audio.push(audio); return true; },
    ...overrides,
  };
  return { controller: new CompanionVoiceIpcController(options), handlers, removed, calls, voice, status };
}

test("installs and disposes the fixed local voice IPC allowlist", () => {
  const value = fixture();
  assert.equal(value.controller.install(), true);
  assert.deepEqual([...value.handlers.keys()], CHANNELS);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.deepEqual(value.removed, CHANNELS);
  assert.equal(value.controller.dispose(), false);
});

test("status and configuration require the main renderer and exact payload", () => {
  const value = fixture();
  assert.equal(value.controller.status({ sender: 1 }), value.status);
  const payload = { enabled: true, origin: "http://127.0.0.1:9880", prompt_lang: "zh", prompt_text: "参考", text_lang: "zh" };
  assert.equal(value.controller.configure({ sender: 1 }, payload).enabled, true);
  assert.equal(value.calls.configured[0], payload);
  assert.throws(() => value.controller.configure({}, { ...payload, reference_path: "C:\\secret.wav" }), /payload_rejected/);
  assert.throws(() => value.controller.configure({}, null), /payload_rejected/);
  assert.equal(value.calls.guarded, 4);
});

test("unavailable voice runtime fails explicitly after the sender guard", () => {
  const value = fixture({ voiceProvider: () => null });
  assert.throws(() => value.controller.status({}), /companion_voice_unavailable/);
  const payload = { enabled: true, origin: "http://127.0.0.1:9880", prompt_lang: "zh", prompt_text: "", text_lang: "zh" };
  assert.throws(() => value.controller.configure({}, payload), /companion_voice_payload_rejected/);
  assert.equal(value.calls.guarded, 2);
});

test("reference selection is native WAV-only and never accepts a renderer path", async () => {
  let owner;
  let options;
  const value = fixture({
    dialog: {
      showOpenDialog: async (window, dialogOptions) => {
        owner = window;
        options = dialogOptions;
        return { canceled: false, filePaths: ["C:\\fixtures\\voice.wav"] };
      },
    },
  });
  const result = await value.controller.selectReference({ sender: 1 }, { path: "C:\\renderer.wav" });
  assert.deepEqual(owner, { id: "main" });
  assert.deepEqual(options.properties, ["openFile"]);
  assert.deepEqual(options.filters, [{ name: "WAV audio", extensions: ["wav"] }]);
  assert.deepEqual(value.calls.references, ["C:\\fixtures\\voice.wav"]);
  assert.equal(result.status, "selected");
});

test("cancelled reference selection returns only the bounded voice projection", async () => {
  const value = fixture({ dialog: { showOpenDialog: async () => ({ canceled: true, filePaths: [] }) } });
  assert.deepEqual(await value.controller.selectReference({}), { status: "cancelled", voice: value.status });
  assert.deepEqual(value.calls.references, []);
});

test("test playback honors quiet state and requires pet audio delivery", async () => {
  const quiet = fixture({ isQuiet: () => true });
  await assert.rejects(quiet.controller.testPlayback({}), /companion_voice_quiet/);
  assert.deepEqual(quiet.calls.spoken, []);

  const missingPet = fixture({ dispatchAudio: () => false });
  await assert.rejects(missingPet.controller.testPlayback({}), /companion_voice_pet_unavailable/);
  assert.deepEqual(missingPet.calls.spoken, [TEST_PHRASE]);

  const value = fixture();
  assert.deepEqual(await value.controller.testPlayback({}), { status: "playing" });
  assert.deepEqual(value.calls.spoken, [TEST_PHRASE]);
  assert.equal(value.calls.audio[0].toString(), "wav");
});
