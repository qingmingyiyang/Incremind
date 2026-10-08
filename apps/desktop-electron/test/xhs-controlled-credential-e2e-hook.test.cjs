const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const {
  HARNESS_PID_ENV,
  REAL_OCR_TOKEN_ENV,
  TOKEN_ENV,
  resolveXhsControlledCredentialE2E,
  resolveXhsControlledCredentialRealOcrE2E,
} = require("../src/xhs-controlled-credential-e2e-hook.cjs");

const desktopRoot = path.resolve(__dirname, "..");

test("XHS controlled credential fixture requires a matching bounded token and direct harness parent", () => {
  const token = "x".repeat(32);
  const env = { [TOKEN_ENV]: token, [HARNESS_PID_ENV]: "8123" };
  assert.equal(resolveXhsControlledCredentialE2E({ commandLineToken: token, env, parentPid: 8123 }), true);
  assert.equal(resolveXhsControlledCredentialE2E({ commandLineToken: `${token}x`, env, parentPid: 8123 }), false);
  assert.equal(resolveXhsControlledCredentialE2E({ commandLineToken: token, env, parentPid: 8124 }), false);
  assert.equal(resolveXhsControlledCredentialE2E({ commandLineToken: "short", env: { [TOKEN_ENV]: "short", [HARNESS_PID_ENV]: "8123" }, parentPid: 8123 }), false);
});

test("XHS real OCR fixture mode requires the separately bound run token", () => {
  const token = "x".repeat(32);
  const base = { [TOKEN_ENV]: token, [HARNESS_PID_ENV]: "8123" };
  assert.equal(resolveXhsControlledCredentialRealOcrE2E({
    commandLineToken: token,
    env: { ...base, [REAL_OCR_TOKEN_ENV]: token },
    parentPid: 8123,
  }), true);
  assert.equal(resolveXhsControlledCredentialRealOcrE2E({
    commandLineToken: token,
    env: { ...base, [REAL_OCR_TOKEN_ENV]: `${token}x` },
    parentPid: 8123,
  }), false);
});

test("XHS controlled binary revocation Gate uses the packaged fixture and restart fence", () => {
  const packageJson = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
  const script = fs.readFileSync(
    path.join(desktopRoot, "scripts", "e2e-electron.cjs"),
    "utf8",
  );

  assert.equal(
    packageJson.scripts["test:e2e:xhs-controlled-binary-revocation"],
    "node scripts/e2e-electron.cjs --xhs-controlled-binary-revocation-only",
  );
  assert.match(script, /XHS_CONTROLLED_BINARY_REVOCATION_ONLY/);
  assert.match(script, /runXhsControlledBinaryRevocationGate/);
  assert.match(script, /binary-boundary credential revoke did not persist across restart/);
  assert.match(script, /binary revocation Job changed or replayed after restart/);
  assert.match(script, /assertSecretIsolation\(temporaryRoot\)/);
});

test("XHS controlled bundled OCR Gate persists settings before the production success path", () => {
  const packageJson = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
  const script = fs.readFileSync(
    path.join(desktopRoot, "scripts", "e2e-electron.cjs"),
    "utf8",
  );

  assert.equal(
    packageJson.scripts["test:e2e:xhs-controlled-bundled-ocr"],
    "node scripts/e2e-electron.cjs --xhs-controlled-bundled-ocr-only",
  );
  assert.match(script, /command:\['builtin:windows-ocr'\]/);
  assert.match(script, /runXhsControlledBundledOcrGate/);
  assert.match(script, /controlled OCR Job, Receipt or Document changed after restart/);
  assert.match(script, /xhs_controlled_bundled_windows_ocr/);
});
