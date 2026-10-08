const fs = require("node:fs");
const path = require("node:path");

const [repoRoot, root, readyPath, stopPath, resultPath] = process.argv.slice(2).map((value) => path.resolve(value));
const { SidecarSupervisor } = require(path.join(repoRoot, "apps", "desktop-electron", "src", "sidecar-supervisor.cjs"));
const pythonPath = path.join(repoRoot, "runtime", "python.exe");

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
  const supervisor = new SidecarSupervisor({
    rootDir: root,
    moduleRoot: path.join(repoRoot, "src"),
    workingDir: root,
    pythonPath,
    startupTimeoutMs: 15000,
  });
  try {
    const session = await supervisor.start();
    atomicJson(readyPath, {
      session: {
        protocol_version: session.protocol_version,
        instance_id: session.instance_id,
        child_pid: session.child_pid,
      },
    });
    await waitForFile(stopPath, 30000);
    await supervisor.stop();
    atomicJson(resultPath, { status: "stopped" });
  } finally {
    await supervisor.stop();
  }
}

main().catch((error) => {
  atomicJson(resultPath, { status: "failed", error: String(error?.stack || error) });
  process.exitCode = 1;
});
