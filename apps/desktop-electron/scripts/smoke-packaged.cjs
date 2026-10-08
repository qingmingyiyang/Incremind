const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ELECTRON_ROOT = path.resolve(__dirname, "..");
const RESOURCES_ROOT = path.join(ELECTRON_ROOT, "release", "win-unpacked", "resources");

function resolvePackagedAppLayout(resourcesRoot) {
  const appAsar = path.join(resourcesRoot, "app.asar");
  const appRoot = path.join(resourcesRoot, "app");
  const supervisorPath = path.join(appRoot, "src", "sidecar-supervisor.cjs");
  if (fs.existsSync(appAsar)) {
    throw new Error("packaged app.asar is not allowed by the no-asar packaging contract");
  }
  if (!fs.statSync(appRoot, { throwIfNoEntry: false })?.isDirectory()) {
    throw new Error("packaged app directory is missing");
  }
  if (!fs.statSync(supervisorPath, { throwIfNoEntry: false })?.isFile()) {
    throw new Error("packaged sidecar supervisor is missing");
  }
  return { appRoot, supervisorPath };
}

function requirePackagedSupervisor(supervisorPath) {
  return require(supervisorPath);
}

async function smokePackaged(resourcesRoot = RESOURCES_ROOT) {
  const { supervisorPath } = resolvePackagedAppLayout(resourcesRoot);
  const sidecarRoot = path.join(resourcesRoot, "sidecar");
  const pythonPath = path.join(sidecarRoot, "runtime", process.platform === "win32" ? "python.exe" : "python");
  if (!fs.existsSync(pythonPath)) throw new Error("packaged sidecar runtime is missing");

  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-packaged-smoke-"));
  const workRoot = path.join(tempRoot, "appData");
  const settingsPath = path.join(workRoot, "config", "settings.toml");
  const startedAt = Date.now();
  let supervisor = null;
  try {
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
    fs.copyFileSync(path.join(sidecarRoot, "config", "settings.toml"), settingsPath);
    const { PACKAGED_STARTUP_TIMEOUT_MS, SidecarSupervisor, SESSION_HEADER } = requirePackagedSupervisor(supervisorPath);
    supervisor = new SidecarSupervisor({
      rootDir: workRoot,
      moduleRoot: sidecarRoot,
      workingDir: workRoot,
      pythonPath,
      startupTimeoutMs: PACKAGED_STARTUP_TIMEOUT_MS,
      log: (message) => console.log(`[smoke:packaged] ${message}`),
    });
    const session = await supervisor.start();
    if (session.origin.endsWith(":8001")) throw new Error("packaged sidecar used forbidden fixed port 8001");
    const response = await fetch(`${session.origin}/api/health`, { headers: { [SESSION_HEADER]: session.secret } });
    const payload = await response.json();
    if (!response.ok || payload?.desktop_session?.instance_id !== session.instance_id) throw new Error("packaged sidecar health contract mismatch");
    const intakeResponse = await fetch(`${session.origin}/api/rebuild/workbench/auto-intake`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        [SESSION_HEADER]: session.secret,
      },
      body: JSON.stringify({
        content: "今天完成了本地工作台烟雾测试。",
        media_type: "",
        file_name: "",
        urls: [],
        add_to_knowledge_base: true,
        title: "",
      }),
    });
    const intake = await intakeResponse.json();
    if (!intakeResponse.ok || !intake?.job_id || !intake?.items?.[0]?.source_id) {
      throw new Error("packaged sidecar local-only auto-intake contract mismatch");
    }
    await supervisor.stop();
    supervisor = null;
    return { startupMs: Date.now() - startedAt, origin: session.origin, sourceId: intake.items[0].source_id, jobId: intake.job_id };
  } finally {
    if (supervisor) await supervisor.stop();
    fs.rmSync(tempRoot, { recursive: true, force: true, maxRetries: 3, retryDelay: 150 });
  }
}

if (require.main === module) smokePackaged()
  .then((result) => console.log(JSON.stringify(result)))
  .catch((error) => { console.error(`[smoke:packaged] ${error.stack || error.message}`); process.exitCode = 1; });

module.exports = { resolvePackagedAppLayout, smokePackaged };
