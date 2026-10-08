const assert = require("node:assert/strict");
const test = require("node:test");

const { CHANNEL, OriginalAssetIpcController } = require("../src/original-asset-ipc-controller.cjs");

const event = Object.freeze({ trusted: true });

function response({ ok = true, status = 200, payload = { resolved_path: "C:\\Vault\\asset.pdf" } } = {}) {
  return { ok, status, async json() { return payload; } };
}

function harness({ session = { origin: "http://127.0.0.1:11495", secret: "secret" }, fetchResponse = response(), fetchError = null, shellResult = "" } = {}) {
  const calls = [];
  const handlers = new Map();
  const removed = [];
  const controller = new OriginalAssetIpcController({
    ipcMain: {
      handle(channel, handler) {
        assert.equal(handlers.has(channel), false, `duplicate ${channel}`);
        handlers.set(channel, handler);
      },
      removeHandler(channel) {
        removed.push(channel);
        handlers.delete(channel);
      },
    },
    shell: {
      async openPath(targetPath) {
        calls.push(["openPath", targetPath]);
        return shellResult;
      },
    },
    requireMainRenderer(value) {
      calls.push(["guard", value]);
      if (value?.trusted !== true) throw new Error("ipc_main_sender_rejected");
    },
    sessionProvider() {
      calls.push(["session"]);
      return session;
    },
    async fetchImpl(url, options) {
      calls.push(["fetch", url, options]);
      if (fetchError) throw fetchError;
      return fetchResponse;
    },
    cryptoApi: {
      createHmac(algorithm, secret) {
        calls.push(["hmac", algorithm, secret]);
        return {
          update(value) {
            calls.push(["update", value]);
            return this;
          },
          digest(encoding) {
            calls.push(["digest", encoding]);
            return "signed-asset";
          },
        };
      },
    },
    sessionHeader: "X-Chriptmas-Session",
    timeoutSignal(milliseconds) {
      const signal = { milliseconds };
      calls.push(["timeout", milliseconds]);
      return signal;
    },
  });
  controller.install();
  return { calls, controller, handlers, removed };
}

test("controller owns one idempotent channel and disposes it", () => {
  const value = harness();
  assert.deepEqual([...value.handlers.keys()], [CHANNEL]);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.deepEqual(value.removed, [CHANNEL]);
});

test("sender and bounded asset identity are checked before session or Shell", async () => {
  const untrusted = harness();
  await assert.rejects(untrusted.handlers.get(CHANNEL)({ trusted: false }, { assetId: "asset-1" }), /ipc_main_sender_rejected/);
  assert.deepEqual(untrusted.calls.map(([name]) => name), ["guard"]);

  for (const assetId of ["", "../escape", "x".repeat(241), "asset id"]) {
    const value = harness();
    assert.deepEqual(await value.handlers.get(CHANNEL)(event, { assetId }), {
      status: "rejected",
      reason: "asset_id_invalid",
    });
    assert.deepEqual(value.calls.map(([name]) => name), ["guard"]);
  }
});

test("missing sidecar is explicit and performs no auth resolution or open", async () => {
  const value = harness({ session: null });
  assert.deepEqual(await value.handlers.get(CHANNEL)(event, { assetId: "asset-1" }), {
    status: "unavailable",
    reason: "sidecar_not_ready",
  });
  assert.deepEqual(value.calls.map(([name]) => name), ["guard", "session"]);
});

test("main signs the exact asset identity and opens only the sidecar-resolved path", async () => {
  const value = harness();
  assert.deepEqual(await value.handlers.get(CHANNEL)(event, { assetId: " asset:1 " }), { status: "opened" });
  assert.deepEqual(value.calls.find(([name]) => name === "hmac"), ["hmac", "sha256", "secret"]);
  assert.deepEqual(value.calls.find(([name]) => name === "update"), ["update", "open-original-asset:asset:1"]);
  const fetchCall = value.calls.find(([name]) => name === "fetch");
  assert.equal(fetchCall[1], "http://127.0.0.1:11495/api/rebuild/desktop/original-assets/asset%3A1/resolve");
  assert.equal(fetchCall[2].headers["X-Chriptmas-Session"], "secret");
  assert.equal(fetchCall[2].headers["X-Chriptmas-Main-Signature"], "signed-asset");
  assert.equal(fetchCall[2].signal.milliseconds, 5000);
  assert.deepEqual(value.calls.find(([name]) => name === "openPath"), ["openPath", "C:\\Vault\\asset.pdf"]);
});

test("HTTP resolution transport and Shell failures stay bounded and never project a path", async () => {
  const denied = harness({ fetchResponse: response({ ok: false, status: 404, payload: { status: "missing", reason: "asset_not_found" } }) });
  assert.deepEqual(await denied.handlers.get(CHANNEL)(event, { assetId: "asset-1" }), {
    status: "missing",
    reason: "asset_not_found",
  });
  assert.equal(denied.calls.some(([name]) => name === "openPath"), false);

  const transport = harness({ fetchError: new Error("connect_failed") });
  assert.deepEqual(await transport.handlers.get(CHANNEL)(event, { assetId: "asset-1" }), {
    status: "failed",
    reason: "connect_failed",
  });

  const shell = harness({ shellResult: "no association" });
  const shellResult = await shell.handlers.get(CHANNEL)(event, { assetId: "asset-1" });
  assert.deepEqual(shellResult, { status: "failed", reason: "no association" });
  assert.equal("resolved_path" in shellResult, false);
});
