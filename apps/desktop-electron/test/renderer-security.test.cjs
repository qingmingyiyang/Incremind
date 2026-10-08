const test = require("node:test");
const assert = require("node:assert/strict");
const {
  allowsNavigation,
  installPetRendererSessionPolicy,
  installRendererNavigationPolicy,
  installRendererSessionPolicy,
  petRendererCsp,
  rendererCsp,
} = require("../src/renderer-security.cjs");

const backendOrigin = "http://127.0.0.1:49321";

test("production CSP permits only the exact authenticated loopback origin", () => {
  const csp = rendererCsp({ backendOrigin });
  assert.match(csp, /default-src 'self'/);
  assert.match(csp, new RegExp(`connect-src 'self' ${backendOrigin}`));
  assert.match(csp, new RegExp(`connect-src 'self' ${backendOrigin} ws://127\\.0\\.0\\.1:49321`));
  assert.match(csp, /style-src 'self'; style-src-attr 'unsafe-inline'/);
  assert.doesNotMatch(csp, /style-src 'self' 'unsafe-inline'/);
  assert.doesNotMatch(csp, /unsafe-eval/);
  assert.doesNotMatch(csp, /http:\/\/127\.0\.0\.1:\*/);
  assert.doesNotMatch(csp, /https:\/\//);
});

test("development-only CSP is explicit and does not alter production policy", () => {
  const csp = rendererCsp({ backendOrigin, developmentOrigin: "http://127.0.0.1:5173/vite" });
  assert.match(csp, /script-src 'self' 'unsafe-eval'/);
  assert.match(csp, /ws:\/\/127\.0\.0\.1:5173/);
  assert.match(rendererCsp({ backendOrigin }), /script-src 'self';/);
});

test("invalid backend origin fails closed", () => {
  assert.throws(() => rendererCsp({ backendOrigin: "http://localhost:49321" }), /exact loopback origin/);
});

test("window opening and navigation default to deny", () => {
  const handlers = {};
  const webContents = {
    setWindowOpenHandler: (handler) => { handlers.windowOpen = handler; },
    on: (event, handler) => { handlers[event] = handler; },
  };
  installRendererNavigationPolicy(webContents, { rendererOrigin: "file://", backendOrigin });
  assert.deepEqual(handlers.windowOpen(), { action: "deny" });
  const allowed = { prevented: false, preventDefault() { this.prevented = true; } };
  handlers["will-navigate"](allowed, "file:///C:/app/index.html");
  assert.equal(allowed.prevented, false);
  const blocked = { prevented: false, preventDefault() { this.prevented = true; } };
  handlers["will-navigate"](blocked, "https://example.com");
  assert.equal(blocked.prevented, true);
  assert.equal(allowsNavigation(`${backendOrigin}/api/health`, new Set(["file://", backendOrigin])), true);
});

test("permission and main-frame CSP handlers deny by default", () => {
  const handlers = {};
  const electronSession = {
    setPermissionRequestHandler: (handler) => { handlers.permission = handler; },
    webRequest: { onHeadersReceived: (handler) => { handlers.headers = handler; } },
  };
  const csp = installRendererSessionPolicy(electronSession, { backendOrigin });
  let decision = null;
  handlers.permission(null, "notifications", (value) => { decision = value; });
  assert.equal(decision, false);
  let response;
  handlers.headers({ resourceType: "mainFrame", responseHeaders: { Server: ["test"] } }, (value) => { response = value; });
  assert.equal(response.responseHeaders["Content-Security-Policy"][0], csp);
});

test("main renderer policy delegates a narrowly scoped permission check and request", () => {
  const handlers = {};
  const electronSession = {
    setPermissionRequestHandler: (handler) => { handlers.request = handler; },
    setPermissionCheckHandler: (handler) => { handlers.check = handler; },
    webRequest: { onHeadersReceived: () => {} },
  };
  const sender = {};
  installRendererSessionPolicy(electronSession, {
    backendOrigin,
    permissionCheck: (candidate, permission, details) => candidate === sender && permission === "media" && details.mediaType === "audio" && details.isMainFrame === true,
    permissionRequest: (candidate, permission, details) => candidate === sender && permission === "media" && details.mediaTypes[0] === "audio",
  });
  assert.equal(handlers.check(sender, "media", "file://", { mediaType: "audio", isMainFrame: true }), true);
  assert.equal(handlers.check(sender, "media", "file://", { mediaType: "video", isMainFrame: true }), false);
  let decision = null;
  handlers.request(sender, "media", (value) => { decision = value; }, { mediaTypes: ["audio"] });
  assert.equal(decision, true);
});

test("pet renderer CSP and session policy never grant backend connectivity", () => {
  const developmentOrigin = "http://127.0.0.1:4173";
  const csp = petRendererCsp({ developmentOrigin });
  assert.match(csp, /connect-src 'self' http:\/\/127\.0\.0\.1:4173 ws:\/\/127\.0\.0\.1:4173/);
  assert.doesNotMatch(csp, new RegExp(backendOrigin.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));

  const handlers = {};
  const petSession = {
    setPermissionRequestHandler: (handler) => { handlers.permission = handler; },
    webRequest: {
      onHeadersReceived: (handler) => { handlers.headers = handler; },
      onBeforeRequest: (handler) => { handlers.request = handler; },
    },
  };
  assert.equal(installPetRendererSessionPolicy(petSession, { developmentOrigin }), csp);
  let decision = null;
  handlers.permission(null, "notifications", (value) => { decision = value; });
  assert.equal(decision, false);
  let apiDecision = null;
  handlers.request({ url: `${developmentOrigin}/api/rebuild/pet/mood` }, (value) => { apiDecision = value; });
  assert.deepEqual(apiDecision, { cancel: true });
  let assetDecision = null;
  handlers.request({ url: `${developmentOrigin}/assets/DesktopPet.js` }, (value) => { assetDecision = value; });
  assert.deepEqual(assetDecision, { cancel: false });
});
