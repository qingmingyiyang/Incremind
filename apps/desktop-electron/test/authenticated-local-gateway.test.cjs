"use strict";

const assert = require("node:assert/strict");
const http = require("node:http");
const test = require("node:test");

const { AuthenticatedLocalGateway } = require("../src/authenticated-local-gateway.cjs");

function fixture({ session = { origin: "http://127.0.0.1:3210", secret: "instance-secret" }, response = { ok: true, status: 200, json: async () => ({ value: 1 }) } } = {}) {
  const requests = [];
  const signals = [];
  const gateway = new AuthenticatedLocalGateway({
    sessionProvider: () => session,
    sessionHeader: "X-Chriptmas-Session",
    fetchImpl: async (...args) => { requests.push(args); return response; },
    createTimeoutSignal: (milliseconds) => { const signal = { milliseconds }; signals.push(signal); return signal; },
  });
  return { gateway, requests, signals };
}

test("GET uses the current loopback session and returns a bounded transport projection", async () => {
  const value = fixture();
  const result = await value.gateway.requestJson({ pathname: "/api/rebuild/companion/settings", unavailableError: "companion_unavailable" });
  assert.deepEqual(result, { ok: true, status: 200, payload: { value: 1 } });
  assert.equal(Object.isFrozen(result), true);
  assert.deepEqual(value.requests, [["http://127.0.0.1:3210/api/rebuild/companion/settings", {
    method: "GET",
    headers: { Accept: "application/json", "X-Chriptmas-Session": "instance-secret" },
    body: undefined,
    signal: { milliseconds: 5000 },
  }]]);
});

test("POST owns JSON encoding content type and the requested timeout", async () => {
  const value = fixture();
  await value.gateway.requestJson({ method: "post", pathname: "/api/rebuild/companion/chat", body: { text: "你好" }, timeoutMs: 65_000, unavailableError: "companion_chat_unavailable" });
  assert.deepEqual(value.requests[0][1], {
    method: "POST",
    headers: { Accept: "application/json", "X-Chriptmas-Session": "instance-secret", "Content-Type": "application/json" },
    body: JSON.stringify({ text: "你好" }),
    signal: { milliseconds: 65_000 },
  });
});

test("missing malformed or non-loopback sessions fail before network access without exposing the secret", async () => {
  for (const session of [null, {}, { origin: "https://example.com", secret: "do-not-leak" }, { origin: "http://127.0.0.1:3210/path", secret: "do-not-leak" }]) {
    const value = fixture({ session });
    await assert.rejects(value.gateway.requestJson({ pathname: "/api/rebuild/pet/mood", unavailableError: "companion_state_unavailable" }), /^Error: companion_state_unavailable$/);
    assert.equal(value.gateway.isAvailable(), false);
    assert.equal(value.requests.length, 0);
  }
});

test("request authority rejects absolute non-API malformed methods timeouts and error codes", async () => {
  const value = fixture();
  const cases = [
    [{ pathname: "https://example.com/api/x", unavailableError: "safe_error" }, /local_gateway_path_invalid/],
    [{ pathname: "/other/path", unavailableError: "safe_error" }, /local_gateway_path_invalid/],
    [{ pathname: "/api/rebuild\\escape", unavailableError: "safe_error" }, /local_gateway_path_invalid/],
    [{ pathname: "/api/rebuild/%2e%2e/private", unavailableError: "safe_error" }, /local_gateway_path_invalid/],
    [{ pathname: "/api/rebuild/%ZZ", unavailableError: "safe_error" }, /local_gateway_path_invalid/],
    [{ pathname: "/api/rebuild/x?target=https://example.com", unavailableError: "safe_error" }, /local_gateway_path_invalid/],
    [{ method: "DELETE", pathname: "/api/rebuild/x", unavailableError: "safe_error" }, /local_gateway_method_invalid/],
    [{ pathname: "/api/rebuild/x", timeoutMs: 99, unavailableError: "safe_error" }, /local_gateway_timeout_invalid/],
    [{ pathname: "/api/rebuild/x", timeoutMs: 120_001, unavailableError: "safe_error" }, /local_gateway_timeout_invalid/],
    [{ pathname: "/api/rebuild/x", unavailableError: "secret: value" }, /local_gateway_error_code_invalid/],
  ];
  for (const [request, error] of cases) await assert.rejects(value.gateway.requestJson(request), error);
  assert.equal(value.requests.length, 0);
});

test("HTTP errors skip parsing by default so domain status errors remain authoritative", async () => {
  const invalidJson = { ok: false, status: 503, json: async () => { throw new SyntaxError("invalid json"); } };
  const value = fixture({ response: invalidJson });
  assert.deepEqual(await value.gateway.requestJson({ pathname: "/api/rebuild/x", unavailableError: "safe_error" }), { ok: false, status: 503, payload: null });
});

test("tolerant callers can explicitly parse nullable JSON on HTTP errors", async () => {
  const invalidJson = { ok: false, status: 503, json: async () => { throw new SyntaxError("invalid json"); } };
  const value = fixture({ response: invalidJson });
  assert.deepEqual(await value.gateway.requestJson({ pathname: "/api/rebuild/x", unavailableError: "safe_error", nullableJson: true, parseErrorJson: true }), { ok: false, status: 503, payload: null });
});

test("constructor rejects incomplete dependencies", () => {
  assert.throws(() => new AuthenticatedLocalGateway(), /authenticated_local_gateway_options_invalid/);
});

test("a real random loopback server receives only the fixed authenticated JSON contract", async () => {
  let received;
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      received = {
        method: request.method,
        url: request.url,
        accept: request.headers.accept,
        contentType: request.headers["content-type"],
        session: request.headers["x-chriptmas-session"],
        body: Buffer.concat(chunks).toString("utf8"),
      };
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ accepted: true }));
    });
  });
  await new Promise((resolve, reject) => server.listen(0, "127.0.0.1", resolve).once("error", reject));
  try {
    const address = server.address();
    const gateway = new AuthenticatedLocalGateway({
      sessionProvider: () => ({ origin: `http://127.0.0.1:${address.port}`, secret: "ephemeral-secret" }),
      sessionHeader: "X-Chriptmas-Session",
    });
    const result = await gateway.requestJson({
      method: "POST",
      pathname: "/api/rebuild/companion/focus/observe",
      body: { foreground: "productive" },
      unavailableError: "companion_focus_sidecar_unavailable",
    });
    assert.deepEqual(result, { ok: true, status: 200, payload: { accepted: true } });
    assert.deepEqual(received, {
      method: "POST",
      url: "/api/rebuild/companion/focus/observe",
      accept: "application/json",
      contentType: "application/json",
      session: "ephemeral-secret",
      body: JSON.stringify({ foreground: "productive" }),
    });
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
