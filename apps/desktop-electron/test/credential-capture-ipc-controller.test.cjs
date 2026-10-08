const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CHANNEL,
  MAX_VALUE_LENGTH,
  CredentialCaptureIpcController,
  isCaptureRequest,
} = require("../src/credential-capture-ipc-controller.cjs");

const event = Object.freeze({ trusted: true });
const request = Object.freeze({
  credential_kind: "provider_api_key",
  credential_subject: "openai",
  value: "provider-key-not-returned",
  command_id: "cmd-credential-capture-001",
});

function harness({ result = { stored: true, secret_ref: "provider:api-key", generation: 2, authorization_revision: 3 }, captureError } = {}) {
  const calls = [];
  const handlers = new Map();
  const removed = [];
  const controller = new CredentialCaptureIpcController({
    ipcMain: {
      handle(channel, handler) { handlers.set(channel, handler); },
      removeHandler(channel) { removed.push(channel); handlers.delete(channel); },
    },
    requireMainRenderer(value) {
      calls.push(["guard", value]);
      if (value?.trusted !== true) throw new Error("ipc_main_sender_rejected");
    },
    async captureCredential(value) {
      calls.push(["capture", value]);
      if (captureError) throw captureError;
      return result;
    },
  });
  controller.install();
  return { calls, controller, handlers, removed };
}

test("capture request accepts only its exact bounded DTO", () => {
  assert.equal(isCaptureRequest(request), true);
  assert.equal(isCaptureRequest({ ...request, credential_kind: "tokenhub_asr_api_key", credential_subject: "tokenhub-hy-asr" }), true);
  assert.equal(isCaptureRequest({ ...request, credential_kind: "qwen_realtime_asr_api_key", credential_subject: "qwen-realtime" }), true);
  for (const payload of [
    null, [], {}, { ...request, extra: true }, { ...request, credential_kind: "anything" },
    { ...request, command_id: "bad" }, { ...request, value: "" },
    { ...request, value: "x".repeat(MAX_VALUE_LENGTH + 1) },
  ]) assert.equal(isCaptureRequest(payload), false, JSON.stringify(payload));
});

test("capture validates the main renderer before accepting a credential", async () => {
  const value = harness();
  await assert.rejects(value.handlers.get(CHANNEL)({ trusted: false }, request), /ipc_main_sender_rejected/);
  assert.deepEqual(value.calls, [["guard", { trusted: false }]]);
});

test("capture rejects field expansion, overlong values, and command replay", async () => {
  const value = harness();
  for (const payload of [{ ...request, extra: true }, { ...request, value: "x".repeat(MAX_VALUE_LENGTH + 1) }]) {
    await assert.rejects(value.handlers.get(CHANNEL)(event, payload), /credential_capture_payload_rejected/);
  }
  await value.handlers.get(CHANNEL)(event, request);
  await assert.rejects(value.handlers.get(CHANNEL)(event, request), /credential_capture_command_replayed/);
  assert.equal(value.calls.filter(([name]) => name === "capture").length, 1);
});

test("capture only returns the stable stored-secret projection", async () => {
  const value = harness({ result: { stored: true, secret_ref: "provider:api-key", generation: 2, authorization_revision: 3, value: request.value } });
  const result = await value.handlers.get(CHANNEL)(event, request);
  assert.deepEqual(result, { stored: true, secret_ref: "provider:api-key", generation: 2, authorization_revision: 3 });
  assert.equal(JSON.stringify(result).includes(request.value), false);
  assert.deepEqual(value.calls.at(-1), ["capture", request]);
});

test("capture rejects malformed backend responses and owns its channel", async () => {
  const value = harness({ result: { stored: true, secret_ref: "provider:api-key", generation: 0, authorization_revision: 3 } });
  await assert.rejects(value.handlers.get(CHANNEL)(event, request), /credential_capture_result_invalid/);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.deepEqual(value.removed, [CHANNEL]);
});
