const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { performance } = require("node:perf_hooks");

const { verifySidecar } = require("./verify-sidecar.cjs");

function median(values) {
  if (!Array.isArray(values) || values.length === 0) throw new Error("profile median requires samples");
  const ordered = [...values].sort((left, right) => left - right);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 === 0 ? (ordered[middle - 1] + ordered[middle]) / 2 : ordered[middle];
}

function timedSpawn(command, args, options = {}) {
  const started = performance.now();
  const result = spawnSync(command, args, { encoding: "utf8", timeout: 300_000, windowsHide: true, ...options });
  const elapsedMs = Math.round(performance.now() - started);
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${path.basename(command)} failed (${result.status}): ${(result.stderr || result.stdout || "").trim()}`);
  return { elapsed_ms: elapsedMs, stdout: (result.stdout || "").trim() };
}

function profileSidecar(root) {
  const resolved = path.resolve(root);
  const profilePath = path.join(resolved, "sidecar-profile.json");
  if (!fs.existsSync(profilePath)) throw new Error(`missing sidecar profile: ${profilePath}`);
  const verifyStarted = performance.now();
  const integrity = verifySidecar(resolved);
  const integrityMs = Math.round(performance.now() - verifyStarted);
  const python = path.join(resolved, "runtime", process.platform === "win32" ? "python.exe" : "python");
  const startupSamples = [];
  for (let index = 0; index < 5; index += 1) {
    startupSamples.push(timedSpawn(python, ["-c", "import sys; assert sys.version_info[:2] == (3, 11)"]).elapsed_ms);
  }
  const dependencies = timedSpawn(python, [
    path.join(resolved, "verify-runtime-dependencies.py"),
    "--requirements",
    path.join(resolved, "requirements.txt"),
  ]);
  return {
    schema_version: "1.0.0",
    measured_at: new Date().toISOString(),
    host: { platform: process.platform, arch: process.arch, node: process.version },
    inventory: JSON.parse(fs.readFileSync(profilePath, "utf8")),
    integrity: { ...integrity, verify_ms: integrityMs, root: undefined },
    python_startup: { samples_ms: startupSamples, median_ms: median(startupSamples) },
    dependency_verifier: { elapsed_ms: dependencies.elapsed_ms, result: dependencies.stdout },
  };
}

if (require.main === module) try {
  const root = process.argv[2] || path.join(__dirname, "..", ".sidecar-stage");
  console.log(JSON.stringify(profileSidecar(root), null, 2));
} catch (error) {
  console.error(`[profile-sidecar] ${error.stack || error.message}`);
  process.exitCode = 1;
}

module.exports = { median, profileSidecar, timedSpawn };
