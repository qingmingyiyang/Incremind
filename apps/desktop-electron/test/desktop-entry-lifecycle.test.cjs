const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const main = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
const surfaceCoordinator = fs.readFileSync(path.join(__dirname, "..", "src", "desktop-surface-coordinator.cjs"), "utf8");
const windowFactory = fs.readFileSync(path.join(__dirname, "..", "src", "desktop-window-factory.cjs"), "utf8");
const bootstrapCoordinator = fs.readFileSync(path.join(__dirname, "..", "src", "application-bootstrap-coordinator.cjs"), "utf8");

function applicationBootstrapBlock() {
  return main.slice(main.indexOf("const applicationBootstrapCoordinator"), main.indexOf("applicationBootstrapCoordinator.install();") + "applicationBootstrapCoordinator.install();".length);
}

test("ready path shows the main workspace and keeps the pet hidden", () => {
  const readyBlock = applicationBootstrapBlock();
  assert.match(readyBlock, /name: "main-window", run: createMainWindow/);
  assert.match(readyBlock, /name: "pet-window", run: createPetWindow/);
  assert.match(readyBlock, /onReady: \(\) => \{[\s\S]*?showMainWindow\(\)/);
  assert.ok(readyBlock.indexOf('name: "pet-window"') < readyBlock.indexOf("onReady:"));
  const petBlock = main.slice(main.indexOf("function createPetWindow"), main.indexOf("function showMainWindow"));
  assert.match(petBlock, /desktopWindowFactory\.preparePet\(\{ entry, savedPosition \}\)/);
  assert.match(petBlock, /createdPetWindow\.once\("ready-to-show"[\s\S]*?preparedPetWindow\.load\(\)/);
  assert.match(windowFactory, /preparePet\([\s\S]*?show:\s*false/);
});

test("main close transitions to a visible pet without quitting", () => {
  const mainBlock = main.slice(main.indexOf("function createMainWindow"), main.indexOf("function createPetWindow"));
  assert.match(mainBlock, /event\.preventDefault\(\)/);
  assert.match(mainBlock, /showPetWindow\(\)/);
  assert.match(main, /function showPetWindow\(\) \{\s*return desktopSurfaceCoordinator\.showPetWindow\(\)/s);
  assert.match(surfaceCoordinator, /showPetWindow\(\)[\s\S]*?mainWindow\.isVisible\(\)[\s\S]*?mainWindow\.hide\(\)/);
});

test("all desktop activation paths restore the same main window", () => {
  assert.match(main, /requestSingleInstanceLock\(\)/);
  assert.match(main, /app\.on\("second-instance", \(\) => \{[\s\S]*?showMainWindow\(\)/);
  assert.match(applicationBootstrapBlock(), /onActivate: \(\) => showMainWindow\(\)/);
  assert.match(bootstrapCoordinator, /this\.#app\.on\("activate", this\.#activateHandler\)/);
  assert.match(main, /new DesktopTrayController\(\{[\s\S]*?onClick:\s*\(\) => showMainWindow\(\)/);
  assert.match(main, /function showMainWindow\(\) \{\s*return desktopSurfaceCoordinator\.showMainWindow\(\)/s);
  assert.match(surfaceCoordinator, /showMainWindow\(\)[\s\S]*?this\.createMainWindow\(\)[\s\S]*?petWindow\.hide\(\)/);
  assert.match(surfaceCoordinator, /presentMainWindow\([\s\S]*?ensureMainWindowBounds[\s\S]*?window\.show\(\)[\s\S]*?window\.focus\(\)/);
});

test("single-instance rejection exits before sidecar or windows start", () => {
  const lockIndex = main.indexOf("requestSingleInstanceLock()");
  const installIndex = main.indexOf("applicationBootstrapCoordinator.install()");
  assert.ok(lockIndex >= 0 && lockIndex < installIndex);
  assert.match(main.slice(lockIndex, installIndex), /if \(!hasSingleInstanceLock\) app\.quit\(\)/);
  assert.match(applicationBootstrapBlock(), /enabledProvider: \(\) => hasSingleInstanceLock/);
  assert.match(bootstrapCoordinator, /if \(!this\.#enabledProvider\(\)\) return Object\.freeze\(\{ status: "ignored", reason: "single_instance_lock_unavailable" \}\)/);
});

test("candidate-self launcher receipt is confined to the dual-gated E2E hook", () => {
  assert.match(main, /resolveCompanionLauncherE2E\(\{[\s\S]*?executablePath:\s*process\.execPath[\s\S]*?userDataRoot:\s*app\.getPath\("userData"\)/);
  const secondInstance = main.slice(main.indexOf('app.on("second-instance"'), main.indexOf("function installSidecarRequestAuthentication"));
  assert.match(secondInstance, /const launcher = deferredRuntimeRegistry\.get\("launcher"\)/);
  assert.match(secondInstance, /if \(launcher\?\.e2eTarget\) recordCompanionLauncherE2EReceipt\(launcher\.e2eTarget\)/);
  assert.match(main, /launchArgs:\s*\(entry\) => launcherE2ELaunchArgs\(entry, e2eTarget\)/);
  assert.match(main, /if \(e2eTarget\) seedCompanionLauncherE2ETarget\(controller, e2eTarget\)/);
});

test("the main-process routine lane uses the exact dual-gated companion clock", () => {
  const routine = main.slice(main.indexOf("const companionRoutineController"), main.indexOf("const companionStateController"));
  assert.match(routine, /now:\s*resolveCompanionE2EClockNow\(\) \|\| \(\(\) => new Date\(\)\)/);
});

test("application shutdown has one lifecycle owner and covers every installed IPC controller", () => {
  assert.match(main, /new ApplicationLifecycleCoordinator\(\{[\s\S]*?applicationLifecycleCoordinator\.install\(\)/);
  assert.doesNotMatch(main, /app\.on\("before-quit"/);
  assert.doesNotMatch(main, /app\.on\("will-quit"/);
  assert.match(main, /function quitApplication\(\) \{\s*return applicationLifecycleCoordinator\.quit\(\)/s);

  const installed = [...main.matchAll(/^([A-Za-z0-9_]+)\.install\(\);$/gm)]
    .map((match) => match[1])
    .filter((name) => name !== "applicationLifecycleCoordinator");
  const lifecycle = main.slice(main.indexOf("const applicationLifecycleCoordinator"), main.indexOf("function scheduleVaultRecoveryRelaunch"));
  for (const controller of installed) {
    assert.match(lifecycle, new RegExp(`${controller}\\.dispose\\(\\)`), `${controller} must have one shutdown disposal`);
  }
  assert.match(lifecycle, /companionMultiCharacterRuntimeController\.stop\(\)/);
  assert.match(lifecycle, /desktopRuntimeBootstrapCoordinator\.stop\(\)/);
});
