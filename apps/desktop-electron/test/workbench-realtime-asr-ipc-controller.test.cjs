const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CHANNEL,
  TICKET_PATH,
  WorkbenchRealtimeAsrIpcController,
} = require("../src/workbench-realtime-asr-ipc-controller.cjs");

const validTicket = "A".repeat(43);

function harness({ response, requestError } = {}) {
  const handlers = new Map();
  const removed = [];
  const calls = [];
  const controller = new WorkbenchRealtimeAsrIpcController({
    ipcMain: {
      handle(channel, handler) { handlers.set(channel, handler); },
      removeHandler(channel) { removed.push(channel); handlers.delete(channel); },
    },
    requireMainRenderer(event) {
      calls.push(["guard", event]);
      if (event?.trusted !== true) throw new Error("ipc_main_sender_rejected");
    },
    gateway: {
      async requestJson(request) {
        calls.push(["request", request]);
        if (requestError) throw requestError;
        return response || {
          ok: true,
          status: 201,
          payload: { ticket: validTicket, expires_in_seconds: 15, single_use: true },
        };
      },
    },
  });
  controller.install();
  return { calls, controller, handlers, removed };
}

test("ticket broker is main-renderer-only and uses the authenticated gateway", async () => {
  const value = harness();
  await assert.rejects(value.handlers.get(CHANNEL)({ trusted: false }), /ipc_main_sender_rejected/);
  assert.deepEqual(value.calls.map(([name]) => name), ["guard"]);

  const result = await value.handlers.get(CHANNEL)({ trusted: true });
  assert.deepEqual(result, { ticket: validTicket, expires_in_seconds: 15, single_use: true });
  assert.deepEqual(value.calls.at(-1), ["request", {
    method: "POST",
    pathname: TICKET_PATH,
    timeoutMs: 5000,
    unavailableError: "realtime_asr_sidecar_unavailable",
    nullableJson: true,
    parseErrorJson: true,
  }]);
});

test("ticket broker rejects malformed and unsafe sidecar responses", async () => {
  const malformed = harness({ response: { ok: true, status: 201, payload: { ticket: "short" } } });
  await assert.rejects(malformed.handlers.get(CHANNEL)({ trusted: true }), /realtime_asr_ticket_invalid/);

  const safe = harness({ response: { ok: false, status: 403, payload: { detail: "desktop_session_unauthorized" } } });
  await assert.rejects(safe.handlers.get(CHANNEL)({ trusted: true }), /desktop_session_unauthorized/);

  const unsafe = harness({ response: { ok: false, status: 500, payload: { detail: "secret detail!" } } });
  await assert.rejects(unsafe.handlers.get(CHANNEL)({ trusted: true }), /realtime_asr_ticket_request_failed/);
});

test("ticket broker installs and disposes one channel", () => {
  const value = harness();
  assert.deepEqual([...value.handlers], [[CHANNEL, value.handlers.get(CHANNEL)]]);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.deepEqual(value.removed, [CHANNEL]);
});
