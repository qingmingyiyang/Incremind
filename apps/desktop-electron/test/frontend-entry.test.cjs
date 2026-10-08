const test = require("node:test");
const assert = require("node:assert/strict");

const { resolveFrontendEntry } = require("../src/frontend-entry.cjs");
const { checkDesktopWorkspaceEntry } = require("../src/doctor.cjs");

test("development pet entry replaces a Developer Studio hash with the pet hash", () => {
  const entry = resolveFrontendEntry({
    devUrl: "http://127.0.0.1:4173/?view=rebuild-developer-studio#view=rebuild-developer-studio",
    petView: true,
    frontendIndex: "unused",
  });
  const url = new URL(entry.value);
  assert.equal(url.searchParams.has("view"), false);
  assert.equal(url.hash, "#view=rebuild-pet");
});

test("development main entry preserves an explicit Developer Studio hash", () => {
  const entry = resolveFrontendEntry({
    devUrl: "http://127.0.0.1:4173/#view=rebuild-developer-studio",
    petView: false,
    frontendIndex: "unused",
  });
  assert.equal(new URL(entry.value).hash, "#view=rebuild-developer-studio");
});

test("development query view is normalized to the hash used by React", () => {
  const entry = resolveFrontendEntry({
    devUrl: "http://127.0.0.1:4173/?view=rebuild-settings",
    petView: false,
    frontendIndex: "unused",
  });
  const url = new URL(entry.value);
  assert.equal(url.searchParams.has("view"), false);
  assert.equal(url.hash, "#view=rebuild-settings");
});

test("packaged entries retain separate home and pet hashes", () => {
  assert.deepEqual(resolveFrontendEntry({ frontendIndex: "index.html" }), {
    type: "file", value: "index.html", hash: "view=home",
  });
  assert.deepEqual(resolveFrontendEntry({ frontendIndex: "index.html", petView: true }), {
    type: "file", value: "index.html", hash: "view=rebuild-pet",
  });
});

test("doctor accepts the delegated route resolver without weakening security checks", () => {
  const result = checkDesktopWorkspaceEntry();
  const mainCheck = result.checks.find((item) => item.name === "main_security_and_route");
  const platformCheck = result.checks.find((item) => item.name === "platform_adapter_main");
  assert.equal(mainCheck?.status, "passed");
  assert.match(mainCheck?.evidence || "", /desktop-window-factory\.cjs/);
  assert.equal(platformCheck?.status, "passed");
  assert.match(platformCheck?.evidence || "", /desktop-system-ipc\.cjs/);
});
