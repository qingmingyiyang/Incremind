const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const [repoRoot, root, readyPath, stopPath, resultPath] = process.argv.slice(2).map((value) => path.resolve(value));
const { SidecarSupervisor } = require(path.join(repoRoot, "apps", "desktop-electron", "src", "sidecar-supervisor.cjs"));
const pythonPath = path.join(repoRoot, "runtime", "python.exe");
const crashWorker = path.join(repoRoot, "tests", "fixtures", "plugin_hands_sidecar_crash_worker.py");
const pidPath = path.join(root, "plugin-child.json");
const launchCountPath = path.join(root, "launch.count");
let requested = null;
let supervisor = null;

function atomicJson(target, value) {
  const pending = `${target}.pending`;
  fs.writeFileSync(pending, JSON.stringify(value), "utf8");
  fs.renameSync(pending, target);
}

async function waitForFile(target, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fs.statSync(target, { throwIfNoEntry: false })?.isFile()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`fixture_timeout:${path.basename(target)}`);
}

async function main() {
  fs.mkdirSync(root, { recursive: true });
  supervisor = new SidecarSupervisor({
    rootDir: root,
    moduleRoot: path.join(repoRoot, "src"),
    workingDir: root,
    pythonPath,
    startupTimeoutMs: 15000,
    spawnChild: (executable, args, options) => {
      requested = { executable, args, cwd: options.cwd, windowsHide: options.windowsHide };
      return spawn(executable, [crashWorker, root, pidPath, launchCountPath], options);
    },
  });
  supervisor.waitForHealth = async () => waitForFile(pidPath, 15000);
  const session = await supervisor.start();
  const plugin = JSON.parse(fs.readFileSync(pidPath, "ascii"));
  atomicJson(readyPath, {
    session: {
      protocol_version: session.protocol_version,
      origin: session.origin,
      child_pid: session.child_pid,
    },
    plugin,
    requested,
  });
  await waitForFile(stopPath, 15000);
  await supervisor.stop();
  supervisor = null;
  atomicJson(resultPath, { status: "stopped", launch_count: fs.readFileSync(launchCountPath, "ascii") });
}

main().catch(async (error) => {
  if (supervisor) await supervisor.stop().catch(() => {});
  atomicJson(resultPath, { status: "failed", error: String(error?.stack || error) });
  process.exitCode = 1;
});
