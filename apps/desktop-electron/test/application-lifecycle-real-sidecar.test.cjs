const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { ApplicationLifecycleCoordinator } = require("../src/application-lifecycle-coordinator.cjs");
const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");

const repositoryRoot = path.resolve(__dirname, "..", "..", "..");
const sourcePython = process.env.CHRIPTMAS_OS_PYTHON
  || path.join(repositoryRoot, "python-runtime", process.platform === "win32" ? "python.exe" : "python");

function processIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code !== "ESRCH";
  }
}

async function waitForProcessExit(pid, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!processIsAlive(pid)) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.fail(`sidecar_pid_still_alive:${pid}`);
}

test("source sidecar exits before the lifecycle coordinator replays Electron quit", {
  skip: !fs.existsSync(sourcePython),
  timeout: 30000,
}, async () => {
  const temporaryRoot = path.toNamespacedPath(fs.mkdtempSync(path.join(os.tmpdir(), "u4-l-")));
  fs.mkdirSync(path.join(temporaryRoot, "config"));
  fs.copyFileSync(
    path.join(repositoryRoot, "config", "settings.toml.example"),
    path.join(temporaryRoot, "config", "settings.toml"),
  );
  const supervisor = new SidecarSupervisor({
    rootDir: temporaryRoot,
    moduleRoot: path.join(repositoryRoot, "src"),
    workingDir: temporaryRoot,
    pythonPath: sourcePython,
    startupTimeoutMs: 15000,
  });
  const listeners = new Map();
  const events = [];
  const app = {
    isQuitting: false,
    on(name, listener) { listeners.set(name, listener); },
    removeListener(name, listener) {
      if (listeners.get(name) === listener) listeners.delete(name);
    },
    quit() {
      const event = { prevented: false, preventDefault() { this.prevented = true; } };
      const result = listeners.get("before-quit")(event);
      events.push({
        name: "replay-quit",
        prevented: event.prevented,
        result,
        childExited: observedChildExit,
        childPidAlive: processIsAlive(child.pid),
      });
    },
  };
  let child = null;
  let observedChildExit = false;
  const coordinator = new ApplicationLifecycleCoordinator({
    app,
    shutdownSteps: [{ name: "sidecar", run: () => supervisor.stop() }],
    shutdownDeadlineMs: 10000,
  });

  try {
    const session = await supervisor.start();
    child = supervisor.child;
    assert.ok(child);
    assert.equal(session.child_pid, child.pid);
    child.once("exit", () => {
      observedChildExit = true;
      events.push({ name: "sidecar-exit" });
    });

    coordinator.install();
    const initialEvent = { prevented: false, preventDefault() { this.prevented = true; } };
    const initialResult = listeners.get("before-quit")(initialEvent);

    assert.equal(initialResult, true);
    assert.equal(initialEvent.prevented, true);
    assert.deepEqual(events, []);

    await coordinator.shutdownPromise;
    await new Promise((resolve) => setImmediate(resolve));
    await waitForProcessExit(session.child_pid);

    assert.equal(observedChildExit, true);
    assert.equal(supervisor.child, null);
    assert.equal(supervisor.session, null);
    assert.deepEqual(events, [
      { name: "sidecar-exit" },
      {
        name: "replay-quit",
        prevented: false,
        result: true,
        childExited: true,
        childPidAlive: false,
      },
    ]);
    assert.equal(app.isQuitting, true);
  } finally {
    await supervisor.stop();
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  }
});
