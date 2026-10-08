// electron-builder 包装脚本
// 自动设置 CSC_IDENTITY_AUTO_DISCOVERY=false 跳过代码签名（自用应用不需要签名）
// 并解决 winCodeSign 在非管理员 Windows 下的符号链接问题
const crypto = require("node:crypto");
const fs = require("node:fs");
const { spawnSync } = require("node:child_process");
const path = require("node:path");
const {
  MANIFEST_SHA_ENV,
  NONCE_ENV,
  proofName,
  sha256File,
  validateTransferInputs,
} = require("./sidecar-transfer.cjs");

process.env.CSC_IDENTITY_AUTO_DISCOVERY = "false";

function sidecarTransferEnvironment(
  baseEnv = process.env,
  manifestPath = path.join(__dirname, "..", ".sidecar-stage", "sidecar-manifest.json"),
  randomBytes = crypto.randomBytes,
) {
  const next = { ...baseEnv };
  const nonce = next[NONCE_ENV] || "";
  const manifestSha256 = next[MANIFEST_SHA_ENV] || "";
  if ((nonce && !manifestSha256) || (!nonce && manifestSha256)) {
    throw new Error("sidecar transfer nonce and manifest SHA-256 must be supplied together");
  }
  if (nonce) {
    validateTransferInputs(nonce, manifestSha256);
    return next;
  }
  const stat = fs.lstatSync(manifestPath, { throwIfNoEntry: false });
  if (!stat?.isFile() || stat.isSymbolicLink()) {
    throw new Error(`sidecar stage manifest is missing or unsafe: ${manifestPath}`);
  }
  next[NONCE_ENV] = randomBytes(32).toString("hex");
  next[MANIFEST_SHA_ENV] = sha256File(manifestPath);
  return next;
}

function main() {
  const args = process.argv.slice(2);
  const builderCli = path.join(__dirname, "..", "node_modules", "electron-builder", "cli.js");
  const inheritedTransfer = Boolean(process.env[NONCE_ENV] || process.env[MANIFEST_SHA_ENV]);
  let result;
  let builderEnv;
  try {
    builderEnv = sidecarTransferEnvironment();
    result = spawnSync(process.execPath, [builderCli, ...args], {
      stdio: "inherit",
      cwd: process.cwd(),
      env: builderEnv,
    });
  } catch (error) {
    console.error(`[build] ${error.message}`);
    process.exitCode = 1;
    return;
  }

  if (result.error) {
    console.error(`[build] unable to start electron-builder: ${result.error.message}`);
    process.exitCode = 1;
  } else if (result.status !== 0) {
    console.error(`[build] electron-builder exited with status ${result.status ?? "unknown"}${result.signal ? ` (${result.signal})` : ""}`);
    process.exitCode = result.status || 1;
  }
  if (!inheritedTransfer && builderEnv?.[NONCE_ENV]) {
    const proofPath = path.join(__dirname, "..", "release", proofName(builderEnv[NONCE_ENV]));
    fs.rmSync(proofPath, { force: true });
  }
}

if (require.main === module) main();

module.exports = { sidecarTransferEnvironment };
