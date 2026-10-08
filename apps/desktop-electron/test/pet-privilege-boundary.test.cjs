const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const sourceRoot = path.join(__dirname, "..", "src");

test("pet preload exposes bounded window operations and one-way voice bytes", () => {
  const petPreloadPath = path.join(sourceRoot, "pet-preload.cjs");
  assert.equal(fs.existsSync(petPreloadPath), true);
  const preload = fs.readFileSync(petPreloadPath, "utf8");
  assert.match(preload, /openMainWindow/);
  assert.match(preload, /openPetContextMenu/);
  assert.doesNotMatch(preload, /hidePet/);
  assert.match(preload, /setPetMousePassthrough/);
  assert.match(preload, /movePetWindow/);
  assert.match(preload, /finishPetWindowMove/);
  for (const operation of ["beginPetGesture", "updatePetGesture", "endPetGesture", "commitPetClick"]) {
    assert.match(preload, new RegExp(operation));
  }
  assert.match(preload, /subscribeCompanionState/);
  assert.match(preload, /subscribeCompanionAppearance/);
  assert.match(preload, /\["default", "red-scarf", "gold-star"\]/);
  assert.doesNotMatch(preload, /appearance.*(?:path|overlay|theme)/i);
  assert.match(preload, /subscribeCompanionWeather/);
  assert.match(preload, /\["clear", "cloudy", "rain", "snow", "extreme", "unknown"\]/);
  assert.doesNotMatch(preload, /location_name|latitude|longitude|temperature_c|terms_url/);
  assert.match(preload, /subscribeCompanionVoice/);
  assert.match(preload, /audio\.byteLength > 8 \* 1024 \* 1024/);
  assert.doesNotMatch(preload, /ref_audio_path|referencePath|voice-reference-select|voice-configure/);
  assert.match(preload, /removeListener\("chriptmas:companion-state", handler\)/);
  for (const forbidden of [
    "backendBaseUrl",
    "getPlatformInfo",
    "openPath",
    "showNotification",
    "registerShortcut",
    "selectLocalFile",
    "uploadLocalFile",
    "getAutoUpdateStatus",
    "getPetMood",
    "enterCompanionMode",
    "webUtils",
  ]) {
    assert.doesNotMatch(preload, new RegExp(forbidden));
  }
});

test("pet gesture facts are sender-bound and exclude privileged state", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const controller = fs.readFileSync(path.join(sourceRoot, "companion", "gesture-ipc-controller.cjs"), "utf8");
  assert.match(main, /new CompanionGestureIpcController\(\{[\s\S]*?petWindowProvider:[\s\S]*?gestureController:/);
  assert.doesNotMatch(main, /ipcMain\.on\("chriptmas:pet-gesture-/);
  assert.match(controller, /event\?\.sender\?\.id !== petWindow\.webContents\?\.id/);
  assert.match(controller, /this\.gestureController\[method\]\(payload\)/);
  for (const forbidden of ["affinity", "wallet", "prompt", "screenX", "screenY"]) {
    assert.doesNotMatch(controller, new RegExp(forbidden, "i"));
  }
});

test("application navigation owns the final main-process event boundary", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const controller = fs.readFileSync(path.join(sourceRoot, "application-navigation-controller.cjs"), "utf8");
  assert.match(main, /new ApplicationNavigationController\(\{[\s\S]*?mainWindowProvider:[\s\S]*?showMainWindow,[\s\S]*?validatePanelPayload,/);
  assert.doesNotMatch(main, /ipcMain\.on\s*\(/);
  assert.match(controller, /event\?\.sender !== webContents/);
  assert.match(controller, /event\?\.senderFrame !== webContents\.mainFrame/);
  assert.doesNotMatch(controller, /shell|dialog|fs\.|backend|vault/i);
});

test("pet context menu is native, sender-bound, and separates hide from app quit", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const surfaceCoordinator = fs.readFileSync(path.join(sourceRoot, "desktop-surface-coordinator.cjs"), "utf8");
  const petWindowIpc = fs.readFileSync(path.join(sourceRoot, "companion", "pet-window-ipc-controller.cjs"), "utf8");
  const marker = "  openContextMenu(event)";
  const start = petWindowIpc.indexOf(marker);
  assert.notEqual(start, -1);
  const block = petWindowIpc.slice(start, petWindowIpc.indexOf("\n  }", start));
  assert.match(block, /this\.requirePetRenderer\(event/);
  assert.match(block, /this\.showPetContextMenu\(\)/);
  const menu = main.slice(main.indexOf("function showPetContextMenu"), main.indexOf("function quitApplication"));
  assert.match(menu, /Menu\.buildFromTemplate/);
  assert.match(menu, /screen\.getCursorScreenPoint\(\)/);
  assert.match(menu, /callback:\s*\(\) => \{ petContextMenuOpen = false; \}/);
  assert.match(menu, /resolveNativeMenuE2EAction\(\)/);
  assert.match(menu, /item\?\.id === requestedAction/);
  assert.match(menu, /companionActionRegistry\.execute\(testAction, \{ source: "pet" \}\)/);
  const hide = main.slice(main.indexOf("function hidePetSurfaces"), main.indexOf("function showCompanionOverlay"));
  assert.match(hide, /desktopSurfaceCoordinator\.hidePetSurfaces\(\)/);
  assert.match(surfaceCoordinator, /hidePetSurfaces\(\)[\s\S]*?this\.savePetWindowPosition\(\)/);
  assert.match(surfaceCoordinator, /hidePetSurfaces\(\)[\s\S]*?petWindow\.hide\(\)/);
  assert.match(surfaceCoordinator, /hidePetSurfaces\(\)[\s\S]*?this\.hideOverlay\(\)/);
  assert.doesNotMatch(hide, /app\.quit/);
  assert.match(main, /function quitApplication\(\)[\s\S]*?app\.quit\(\)/);
  const preload = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  assert.doesNotMatch(preload, /native-menu-e2e|test_action|E2E_NATIVE_MENU/i);
  const mainPreload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  assert.match(mainPreload, /payload\?\.intent === "weekly_memory_review"/);
  assert.match(mainPreload, /payload\?\.view === "rebuild-library-overview" && payload\?\.filter === "pending_memory"/);
  assert.doesNotMatch(mainPreload, /memory_topic_discussion/);
  assert.match(main, /action\("companion\.center\.help", "帮助与数据"/);
  assert.match(main, /action\("companion\.center\.memory-review", "和小熊回顾记忆"/);
  assert.match(main, /applicationNavigationController\.openCompanion\("chat", "weekly_memory_review"\)/);
  assert.match(main, /id: "companion\.memory\.pending-review"/);
  assert.match(main, /enabled: \(\) => companionMemoryAttentionController\.hasPending\(\)/);
  assert.match(main, /actionId === "memory\.review"/);
  assert.match(main, /function showPetWindow\(\) \{\s*return desktopSurfaceCoordinator\.showPetWindow\(\)/s);
  assert.match(surfaceCoordinator, /presentPetWindow\([\s\S]*?window\.show\(\)[\s\S]*?this\.pollCompanionState\(\)/);
  assert.doesNotMatch(main, /action\("companion\.center\.history", "历史记录"/);
  assert.match(main, /action\("companion\.center\.note", "记一笔"/);
  assert.match(main, /action\("companion\.center\.games", "猜拳与掷骰"/);
  assert.match(main, /action\("companion\.center\.launchers", "管家服务与传送门"/);
  assert.match(main, /action\("companion\.center\.voice", "语音与视觉"/);
});

test("pet BrowserWindow uses an isolated in-memory session and dedicated preload", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const windowFactory = fs.readFileSync(path.join(sourceRoot, "desktop-window-factory.cjs"), "utf8");
  const petBlock = main.slice(main.indexOf("function createPetWindow"), main.indexOf("function showMainWindow"));
  assert.match(petBlock, /desktopWindowFactory\.preparePet\(\{ entry, savedPosition \}\)/);
  assert.match(windowFactory, /preload:\s*path\.join\(this\.#baseDir, "pet-preload\.cjs"\)/);
  assert.match(windowFactory, /partition:\s*PET_SESSION_PARTITION/);
  assert.match(windowFactory, /PET_SESSION_PARTITION\s*=\s*"chriptmas-pet"/);
  assert.doesNotMatch(windowFactory, /PET_SESSION_PARTITION\s*=\s*"persist:/);
  assert.match(windowFactory, /this\.#installPetSessionPolicy\(window\.webContents\.session/);
  assert.match(windowFactory, /this\.#installNavigationPolicy\(window\.webContents, \{ rendererOrigin \}\)/);
  assert.match(windowFactory, /setIgnoreMouseEvents\(true, \{ forward: true \}\)/);
  const security = fs.readFileSync(path.join(sourceRoot, "renderer-security.cjs"), "utf8");
  assert.match(security, /pathname\.startsWith\("\/api\/"\)/);
  const packageJson = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
  assert.equal(packageJson.build.files.includes("src/**/*"), true);
});

test("privileged desktop IPC handlers bind to the main renderer", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const desktopSystemIpc = fs.readFileSync(path.join(sourceRoot, "desktop-system-ipc.cjs"), "utf8");
  const fileGrantIpc = fs.readFileSync(path.join(sourceRoot, "file-grant-ipc-controller.cjs"), "utf8");
  const memoryTransferIpc = fs.readFileSync(path.join(sourceRoot, "memory-transfer-ipc-controller.cjs"), "utf8");
  const vaultRestoreIpc = fs.readFileSync(path.join(sourceRoot, "vault-restore-ipc-controller.cjs"), "utf8");
  const originalAssetIpc = fs.readFileSync(path.join(sourceRoot, "original-asset-ipc-controller.cjs"), "utf8");
  const launcherIpc = fs.readFileSync(path.join(sourceRoot, "companion", "launcher-ipc-controller.cjs"), "utf8");
  const petWindowIpc = fs.readFileSync(path.join(sourceRoot, "companion", "pet-window-ipc-controller.cjs"), "utf8");
  const companionDataIpc = fs.readFileSync(path.join(sourceRoot, "companion", "data-ipc-controller.cjs"), "utf8");
  const companionVoiceIpc = fs.readFileSync(path.join(sourceRoot, "companion", "voice-ipc-controller.cjs"), "utf8");
  const companionOverlayIpc = fs.readFileSync(path.join(sourceRoot, "companion", "overlay-ipc-controller.cjs"), "utf8");
  const companionVisionIpc = fs.readFileSync(path.join(sourceRoot, "companion", "screen-vision-ipc-controller.cjs"), "utf8");
  const companionVoiceCallIpc = fs.readFileSync(path.join(sourceRoot, "companion", "voice-call-ipc-controller.cjs"), "utf8");
  const companionRoutineIpc = fs.readFileSync(path.join(sourceRoot, "companion", "routine-ipc-controller.cjs"), "utf8");
  const companionWeatherIpc = fs.readFileSync(path.join(sourceRoot, "companion", "weather-ipc-controller.cjs"), "utf8");
  const companionMediaIpc = fs.readFileSync(path.join(sourceRoot, "companion", "media-session-ipc-controller.cjs"), "utf8");
  const easterEggIpc = fs.readFileSync(path.join(sourceRoot, "companion", "easter-egg-ipc-controller.cjs"), "utf8");
  const replyIpc = fs.readFileSync(path.join(sourceRoot, "companion", "reply-ipc-controller.cjs"), "utf8");
  const sensorIpc = fs.readFileSync(path.join(sourceRoot, "companion", "system-sensor-ipc-controller.cjs"), "utf8");
  assert.match(main, /function requireMainRenderer\(event\)/);
  assert.match(main, /new DesktopSystemIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?\}\)/);
  for (const channel of ["platform-info", "open-path", "show-notification", "register-shortcut", "auto-update-status"]) {
    assert.match(desktopSystemIpc, new RegExp(`"chriptmas:${channel}"`), `missing ${channel}`);
  }
  for (const method of ["platformInfo", "openPath", "showNotification", "registerShortcut", "autoUpdateStatus"]) {
    const declaration = new RegExp(`\\n  (?:async )?${method}\\(event`).exec(desktopSystemIpc);
    const start = declaration?.index ?? -1;
    const next = desktopSystemIpc.indexOf("\n  }", start);
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(desktopSystemIpc.slice(start, next), /this\.requireMainRenderer\(event\)/, `${method} is not main-sender-bound`);
  }
  assert.match(main, /new FileGrantIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?\}\)/);
  for (const channel of ["select-local-file", "upload-local-file", "cancel-file-upload"]) {
    assert.match(fileGrantIpc, new RegExp(`"chriptmas:${channel}"`), `missing ${channel}`);
  }
  for (const method of ["selectLocalFile", "uploadLocalFile", "cancelFileUpload"]) {
    const declaration = new RegExp(`\\n  (?:async )?${method}\\(event`).exec(fileGrantIpc);
    const start = declaration?.index ?? -1;
    const next = fileGrantIpc.indexOf("\n  }", start);
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(fileGrantIpc.slice(start, next), /this\.requireMainRenderer\(event\)/, `${method} is not main-sender-bound`);
  }
  assert.match(main, /new MemoryTransferIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?mainWindowProvider:[\s\S]*?sessionProvider:/);
  for (const channel of ["memory-assets-export", "memory-assets-import", "memory-export-save"]) {
    assert.match(memoryTransferIpc, new RegExp(`"chriptmas:${channel}"`), `missing ${channel}`);
  }
  for (const method of ["exportMemoryAssets", "importMemoryAssets", "saveMemoryExport"]) {
    const declaration = new RegExp(`\\n  async ${method}\\(event`).exec(memoryTransferIpc);
    const start = declaration?.index ?? -1;
    const next = memoryTransferIpc.indexOf("\n  }", start);
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(memoryTransferIpc.slice(start, next), /this\.requireMainRenderer\(event\)/, `${method} is not main-sender-bound`);
  }
  assert.match(main, /new VaultRestoreIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?mainWindowProvider:[\s\S]*?sessionProvider:[\s\S]*?recoveryProvider:/);
  assert.match(vaultRestoreIpc, /"chriptmas:vault-restore"/);
  assert.match(vaultRestoreIpc.slice(vaultRestoreIpc.indexOf("  async restore(event")), /this\.requireMainRenderer\(event\)/);
  assert.doesNotMatch(main, /ipcMain\.handle\("chriptmas:vault-restore"/);
  assert.match(main, /new OriginalAssetIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?sessionProvider:/);
  assert.match(originalAssetIpc, /"chriptmas:open-original-asset"/);
  assert.match(originalAssetIpc.slice(originalAssetIpc.indexOf("  async open(event")), /this\.requireMainRenderer\(event\)/);
  assert.doesNotMatch(main, /ipcMain\.handle\("chriptmas:open-original-asset"/);
  assert.match(main, /new CompanionLauncherIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?launcherProvider:/);
  for (const channel of ["companion-launchers-list", "companion-launcher-add-program", "companion-launcher-add-bookmark", "companion-launcher-rename", "companion-launcher-delete", "companion-launcher-open"]) {
    assert.match(launcherIpc, new RegExp(`"chriptmas:${channel}"`), `missing ${channel}`);
  }
  for (const method of ["list", "addProgram", "addBookmark", "rename", "remove", "launch"]) {
    const declaration = new RegExp(`\\n  (?:async )?${method}\\(event`).exec(launcherIpc);
    const start = declaration?.index ?? -1;
    const next = launcherIpc.indexOf("\n  }", start);
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(launcherIpc.slice(start, next), /this\.requireMainRenderer\(event\)/, `${method} is not main-sender-bound`);
  }
  assert.match(main, /new CompanionPetWindowIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?requireKnownRenderer,[\s\S]*?petWindowProvider:/);
  assert.match(petWindowIpc, /enterCompanionMode\(event\)[\s\S]*?this\.requireMainRenderer\(event\)/);
  assert.match(main, /new CompanionDataIpcController\(\{[\s\S]*?sessionProvider:[\s\S]*?mainWindowProvider:[\s\S]*?manualPathProvider:[\s\S]*?requireMainRenderer,/);
  for (const method of ["openManual", "backup", "restorePreflight", "restore"]) {
    const declaration = new RegExp(`\\n  async ${method}\\(event`).exec(companionDataIpc);
    const start = declaration?.index ?? -1;
    const next = companionDataIpc.indexOf("\n  }", start);
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(companionDataIpc.slice(start, next), /this\.requireMainRenderer\(event\)/, `${method} is not main-sender-bound`);
  }
  assert.match(main, /new CompanionVoiceIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?voiceProvider:[\s\S]*?isQuiet:[\s\S]*?dispatchAudio:/);
  for (const method of ["status", "configure", "selectReference", "testPlayback"]) {
    const declaration = new RegExp(`\\n  (?:async )?${method}\\(event`).exec(companionVoiceIpc);
    const start = declaration?.index ?? -1;
    const next = companionVoiceIpc.indexOf("\n  }", start);
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(companionVoiceIpc.slice(start, next), /this\.requireMainRenderer\(event\)/, `${method} is not main-sender-bound`);
  }
  assert.match(main, /new CompanionOverlayIpcController\(\{[\s\S]*?overlayWindowProvider:[\s\S]*?overlayController:[\s\S]*?validatePanelPayload,[\s\S]*?openCenter:/);
  for (const method of ["submit", "perform", "acknowledge", "close", "openCompanionCenter"]) {
    const declaration = new RegExp(`\\n  ${method}\\(event`).exec(companionOverlayIpc);
    const start = declaration?.index ?? -1;
    const next = companionOverlayIpc.indexOf("\n  }", start);
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(companionOverlayIpc.slice(start, next), /this\.requireOverlayRenderer\(event\)/, `${method} is not overlay-sender-bound`);
  }
  assert.doesNotMatch(main, /ipcMain\.(?:handle|on)\("chriptmas:companion-overlay-(?:submit|action|ack|close|open-center|ready)"/);
  assert.match(main, /new CompanionScreenVisionIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?visionProvider:/);
  for (const method of ["listSources", "capture", "confirm", "cancel"]) {
    const declaration = new RegExp(`\\n  ${method}\\(event`).exec(companionVisionIpc);
    const start = declaration?.index ?? -1;
    const next = companionVisionIpc.indexOf("\n  }", start);
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(companionVisionIpc.slice(start, next), /this\.requireMainRenderer\(event\)/, `${method} is not main-sender-bound`);
  }
  assert.match(main, /new CompanionVoiceCallIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?mainWindowProvider:[\s\S]*?petWindowProvider:[\s\S]*?microphonePermission:[\s\S]*?voiceCallProvider:[\s\S]*?presentationProvider:/);
  for (const method of ["arm", "transcribe", "present", "cancel"]) {
    const declaration = new RegExp(`\\n  ${method}\\(event`).exec(companionVoiceCallIpc);
    const start = declaration?.index ?? -1;
    const next = companionVoiceCallIpc.indexOf("\n  }", start);
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(companionVoiceCallIpc.slice(start, next), /this\.requireMainRenderer\(event\)/, `${method} is not main-sender-bound`);
  }
  assert.doesNotMatch(main, /ipcMain\.(?:handle|on)\("chriptmas:companion-(?:microphone-arm|voice-call-(?:transcribe|present|cancel)|voice-playback-status)"/);
  assert.match(main, /new CompanionRoutineIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?routineController:[\s\S]*?refreshRoutine:[\s\S]*?presentOverlay:/);
  for (const method of ["status", "refresh", "wake"]) {
    const declaration = new RegExp(`\\n  ${method}\\(event`).exec(companionRoutineIpc);
    const start = declaration?.index ?? -1;
    const next = companionRoutineIpc.indexOf("\n  }", start);
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(companionRoutineIpc.slice(start, next), /this\.requireMainRenderer\(event\)/, `${method} is not main-sender-bound`);
  }
  assert.doesNotMatch(main, /ipcMain\.handle\("chriptmas:companion-routine-(?:status|refresh|wake)"/);
  assert.match(main, /new CompanionWeatherIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?weatherController:[\s\S]*?petWindowProvider:[\s\S]*?openExternal:/);
  for (const method of ["refresh", "openTerms"]) {
    const declaration = new RegExp(`\\n  (?:async )?${method}\\(event`).exec(companionWeatherIpc);
    const start = declaration?.index ?? -1;
    const next = companionWeatherIpc.indexOf("\n  }", start);
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(companionWeatherIpc.slice(start, next), /this\.requireMainRenderer\(event\)/, `${method} is not main-sender-bound`);
  }
  assert.match(companionWeatherIpc, /const TERMS_URL = "https:\/\/open-meteo\.com\/en\/terms"/);
  assert.doesNotMatch(main, /ipcMain\.(?:handle|on)\("chriptmas:companion-weather-(?:refresh|terms-open|ready)"/);
  assert.match(main, /new CompanionMediaSessionIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?mediaController:[\s\S]*?mainWindowProvider:[\s\S]*?petWindowProvider:/);
  for (const method of ["refresh", "status"]) {
    const declaration = new RegExp(`\\n  ${method}\\(event`).exec(companionMediaIpc);
    const start = declaration?.index ?? -1;
    const next = companionMediaIpc.indexOf("\n  }", start);
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(companionMediaIpc.slice(start, next), /this\.requireMainRenderer\(event\)/, `${method} is not main-sender-bound`);
  }
  assert.doesNotMatch(main, /ipcMain\.(?:handle|on)\("chriptmas:companion-media-(?:refresh|status|ready)"/);
  for (const [controller, channels] of [
    [easterEggIpc, ["companion-easter-egg-status", "companion-easter-egg-enabled", "companion-local-action"]],
    [replyIpc, ["companion-reply-present"]],
    [sensorIpc, ["companion-sensors-refresh"]],
  ]) {
    assert.match(controller, /this\.requireMainRenderer\(event\)/);
    for (const channel of channels) assert.match(controller, new RegExp(`"chriptmas:${channel}"`));
  }
  assert.match(main, /new CompanionEasterEggIpcController\(\{/);
  assert.match(main, /new CompanionReplyIpcController\(\{/);
  assert.match(main, /new CompanionSystemSensorIpcController\(\{/);
  assert.doesNotMatch(main, /ipcMain\.(?:handle|on)\s*\(/);
});

test("companion backup and restore paths stay in main with fingerprint-bound confirmation", () => {
  const controller = fs.readFileSync(path.join(sourceRoot, "companion", "data-ipc-controller.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  const pet = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  const overlay = fs.readFileSync(path.join(sourceRoot, "companion-overlay-preload.cjs"), "utf8");
  assert.match(controller, /this\.dialog\.showSaveDialog\(mainWindow/);
  assert.match(controller, /this\.dialog\.showOpenDialog\(mainWindow/);
  assert.match(controller, /this\.restoreGrants\.set/);
  assert.match(controller, /senderId: event\.sender\.id/);
  assert.match(controller, /expiresAt: this\.now\(\) \+ RESTORE_GRANT_TTL_MS/);
  const start = controller.indexOf("  async restore(event, payload)");
  const end = controller.indexOf("\n  requireWindow()", start);
  const block = controller.slice(start, end);
  assert.match(block, /grant\.fingerprint !== payload\.expected_fingerprint/);
  assert.match(block, /this\.dialog\.showMessageBox\(mainWindow/);
  assert.match(block, /buttons: \["取消", "确认恢复"\]/);
  assert.match(block, /defaultId: 0/);
  assert.match(block, /cancelId: 0/);
  assert.ok(block.indexOf("showMessageBox") < block.lastIndexOf('requestData("restore"'));
  assert.match(controller, /this\.crypto\.createHmac\("sha256", active\.secret\)/);
  for (const method of ["exportCompanionBackup", "preflightCompanionRestore", "restoreCompanionBackup"]) {
    assert.match(preload, new RegExp(method));
    assert.doesNotMatch(pet, new RegExp(method));
    assert.doesNotMatch(overlay, new RegExp(method));
  }
  assert.doesNotMatch(preload, /companion.*(?:backup|restore).*(?:path|filePath)/i);
});

test("Vault restore requires native confirmation and adopts only after sidecar shutdown", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const controller = fs.readFileSync(path.join(sourceRoot, "vault-restore-ipc-controller.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  const pet = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  const overlay = fs.readFileSync(path.join(sourceRoot, "companion-overlay-preload.cjs"), "utf8");
  const start = controller.indexOf("  async restore(event, payload)");
  const end = controller.indexOf("\n  dispose()", start);
  const block = controller.slice(start, end);
  assert.match(block, /requireMainRenderer\(event\)/);
  assert.match(block, /mainWindow\?\.isVisible\(\).*mainWindow\?\.isFocused\(\)/s);
  assert.match(block, /buttons: \["取消", "确认恢复并重启"\]/);
  assert.match(block, /defaultId: 0/);
  assert.match(block, /cancelId: 0/);
  assert.ok(block.indexOf("showMessageBox") < block.indexOf("memory-snapshots"));
  assert.ok(block.indexOf("sidecarStop") < block.indexOf("recovery.adopt"));
  assert.match(block, /scheduleRelaunch/);
  assert.doesNotMatch(main, /ipcMain\.handle\("chriptmas:vault-restore"/);
  assert.match(main, /new VaultRestoreIpcController\(\{/);
  assert.match(main, /app\.relaunch\(\)/);
  assert.match(preload, /restoreMemorySnapshot/);
  assert.doesNotMatch(pet, /restoreMemorySnapshot|vault-restore/);
  assert.doesNotMatch(overlay, /restoreMemorySnapshot|vault-restore/);
});

test("memory asset export stays main-owned and writes only after native selection", () => {
  const transfer = fs.readFileSync(path.join(sourceRoot, "memory-transfer-ipc-controller.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  const pet = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  const overlay = fs.readFileSync(path.join(sourceRoot, "companion-overlay-preload.cjs"), "utf8");
  const block = transfer.slice(transfer.indexOf("async exportMemoryAssets"), transfer.indexOf("async importMemoryAssets"));
  assert.match(block, /requireMainRenderer\(event\)/);
  assert.match(block, /requireWindow\("memory_asset_export_window_required"\)/);
  assert.match(transfer, /mainWindow\?\.isVisible\(\).*mainWindow\?\.isFocused\(\)/);
  assert.match(block, /dialog\.showSaveDialog\(mainWindow/);
  assert.match(block, /selected\.canceled.*status: "cancelled"/);
  assert.ok(block.indexOf("showSaveDialog") < block.indexOf("/api/rebuild/memory-assets/export"));
  assert.match(block, /writeMemoryAssetPackage/);
  assert.match(preload, /exportMemoryAssetPackage/);
  assert.doesNotMatch(pet, /exportMemoryAssetPackage|memory-assets-export/);
  assert.doesNotMatch(overlay, /exportMemoryAssetPackage|memory-assets-export/);
});

test("memory asset import stays main-owned and never exposes the selected path", () => {
  const transfer = fs.readFileSync(path.join(sourceRoot, "memory-transfer-ipc-controller.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  const pet = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  const overlay = fs.readFileSync(path.join(sourceRoot, "companion-overlay-preload.cjs"), "utf8");
  const block = transfer.slice(transfer.indexOf("async importMemoryAssets"), transfer.indexOf("async saveMemoryExport"));
  assert.match(block, /requireMainRenderer\(event\)/);
  assert.match(block, /requireWindow\("memory_asset_import_window_required"\)/);
  assert.match(block, /dialog\.showOpenDialog\(mainWindow/);
  assert.match(block, /properties: \["openFile", "dontAddToRecent"\]/);
  assert.match(block, /path\.extname\(sourcePath\).*"\.zip"/);
  assert.match(block, /sourceStat\.size > this\.maxPackageBytes/);
  assert.match(block, /zipBytes\.byteLength > this\.maxPackageBytes/);
  assert.match(block, /\/api\/rebuild\/memory\/export\/round-trip/);
  assert.ok(block.indexOf("showOpenDialog") < block.indexOf("fsPromises.readFile"));
  assert.doesNotMatch(block, /file_path:\s*sourcePath|path:\s*sourcePath/);
  assert.match(preload, /importMemoryAssetPackage/);
  assert.doesNotMatch(pet, /importMemoryAssetPackage|memory-assets-import/);
  assert.doesNotMatch(overlay, /importMemoryAssetPackage|memory-assets-import/);
});

test("memory preset export stays main-owned and fetches bytes only after native selection", () => {
  const transfer = fs.readFileSync(path.join(sourceRoot, "memory-transfer-ipc-controller.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  const pet = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  const overlay = fs.readFileSync(path.join(sourceRoot, "companion-overlay-preload.cjs"), "utf8");
  const block = transfer.slice(transfer.indexOf("async saveMemoryExport"), transfer.indexOf("dispose()"));
  assert.match(block, /requireMainRenderer\(event\)/);
  assert.match(block, /requireWindow\("memory_export_window_required"\)/);
  assert.match(block, /validateMemoryExportRequest\(payload\)/);
  assert.match(block, /dialog\.showSaveDialog\(mainWindow/);
  assert.match(block, /selected\.canceled.*status: "cancelled"/);
  assert.ok(block.indexOf("showSaveDialog") < block.indexOf("/api/rebuild/memory/export/file"));
  assert.match(block, /writeMemoryPresetExport/);
  assert.match(preload, /saveMemoryExport/);
  assert.doesNotMatch(pet, /saveMemoryExport|memory-export-save/);
  assert.doesNotMatch(overlay, /saveMemoryExport|memory-export-save/);
});

test("screen capture is main-renderer-only and absent from pet and overlay bridges", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  const pet = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  const overlay = fs.readFileSync(path.join(sourceRoot, "companion-overlay-preload.cjs"), "utf8");
  assert.match(main, /desktopCapturer/);
  assert.match(preload, /listCompanionVisionSources/);
  assert.match(preload, /bytes\.byteLength <= 2 \* 1024 \* 1024/);
  for (const forbidden of ["desktopCapturer", "CompanionVision", "companion-vision", "screen capture"]) {
    assert.doesNotMatch(pet, new RegExp(forbidden, "i"));
    assert.doesNotMatch(overlay, new RegExp(forbidden, "i"));
  }
});

test("microphone arm and voice transcription stay absent from pet and overlay bridges", () => {
  const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  const pet = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  const overlay = fs.readFileSync(path.join(sourceRoot, "companion-overlay-preload.cjs"), "utf8");
  assert.match(preload, /armCompanionMicrophone/);
  assert.match(preload, /navigator\.userActivation\?\.isActive !== true/);
  assert.match(preload, /transcribeCompanionVoice/);
  assert.match(preload, /presentCompanionVoiceCallReply/);
  assert.match(preload, /bytes\.byteLength <= 12 \* 1024 \* 1024/);
  for (const forbidden of ["armCompanionMicrophone", "transcribeCompanionVoice", "presentCompanionVoiceCallReply", "voice-call-transcribe", "microphone-arm"]) {
    assert.doesNotMatch(pet, new RegExp(forbidden, "i"));
    assert.doesNotMatch(overlay, new RegExp(forbidden, "i"));
  }
});

test("routine controls stay main-renderer-only and pet clicks cannot choose wake duration", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const controller = fs.readFileSync(path.join(sourceRoot, "companion", "routine-ipc-controller.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  const pet = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  const overlay = fs.readFileSync(path.join(sourceRoot, "companion-overlay-preload.cjs"), "utf8");
  assert.match(preload, /wakeCompanionTemporarily:\s*\(\) => ipcRenderer\.invoke\("chriptmas:companion-routine-wake"\)/);
  assert.match(controller, /wakeForThirtyMinutes\(\)/);
  assert.match(controller, /text: "我先醒来陪你半小时。"/);
  assert.match(main, /text: "Zzz\.\.\."/);
  assert.match(main, /if \(companionRoutineController\.status\(\)\.sleeping\)/);
  for (const forbidden of ["Routine", "routine-status", "routine-refresh", "routine-wake", "wakeCompanion"]) {
    assert.doesNotMatch(pet, new RegExp(forbidden, "i"));
    assert.doesNotMatch(overlay, new RegExp(forbidden, "i"));
  }
});

test("launcher IPC keeps path selection in main and is absent from pet and overlay bridges", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const controller = fs.readFileSync(path.join(sourceRoot, "companion", "launcher-ipc-controller.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  const petPreload = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  const overlayPreload = fs.readFileSync(path.join(sourceRoot, "companion-overlay-preload.cjs"), "utf8");
  assert.match(main, /new CompanionLauncherIpcController\(/);
  assert.doesNotMatch(main, /ipcMain\.handle\("chriptmas:companion-launcher/);
  const start = controller.indexOf("async addProgram(event, payload)");
  const block = controller.slice(start, controller.indexOf("\n  addBookmark", start));
  assert.match(block, /dialog\.showOpenDialog\(this\.mainWindowProvider\(\)/);
  assert.match(block, /properties: \["openFile"\]/);
  assert.doesNotMatch(block, /payload\.path|selectedPath:\s*payload/);
  assert.match(preload, /addCompanionProgram/);
  for (const forbidden of ["addCompanionProgram", "addCompanionBookmark", "openCompanionLauncher", "companion-launcher-open", "companion-launcher-add-program"]) {
    assert.doesNotMatch(petPreload, new RegExp(forbidden));
    assert.doesNotMatch(overlayPreload, new RegExp(forbidden));
  }
});

test("file organizer paths and mutations stay main-renderer-only", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const controller = fs.readFileSync(path.join(sourceRoot, "companion", "file-organizer-ipc-controller.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  const pet = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  const overlay = fs.readFileSync(path.join(sourceRoot, "companion-overlay-preload.cjs"), "utf8");
  assert.match(main, /new CompanionFileOrganizerIpcController\(/);
  assert.doesNotMatch(main, /ipcMain\.handle\("chriptmas:companion-file-organizer-/);
  for (const channel of ["companion-file-organizer-preview", "companion-file-organizer-execute", "companion-file-organizer-history", "companion-file-organizer-undo"]) {
    const start = controller.indexOf(`"chriptmas:${channel}"`);
    assert.notEqual(start, -1);
    assert.match(preload, new RegExp(channel));
    assert.doesNotMatch(pet, new RegExp(channel));
    assert.doesNotMatch(overlay, new RegExp(channel));
  }
  assert.match(controller, /properties: \["openDirectory", "dontAddToRecent"\]/);
  const executeStart = controller.indexOf("async execute(event, payload)");
  const executeBlock = controller.slice(executeStart, controller.indexOf("history(event)", executeStart));
  assert.match(executeBlock, /this\.requireMainRenderer\(event\)/);
  assert.match(executeBlock, /this\.dialog\.showMessageBox\(mainWindow/);
  assert.match(executeBlock, /buttons: \["取消", "确认移动"\]/);
  assert.match(executeBlock, /defaultId: 0/);
  assert.match(executeBlock, /cancelId: 0/);
  assert.match(executeBlock, /confirmation\.response !== 1/);
  assert.ok(executeBlock.indexOf("showMessageBox") < executeBlock.indexOf("organizer.execute"));
});

test("multi-character settings and sends stay main-only without token or port projection", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8"); const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  const ipcController = fs.readFileSync(path.join(sourceRoot, "companion", "multicharacter-ipc-controller.cjs"), "utf8");
  const runtimeController = fs.readFileSync(path.join(sourceRoot, "companion", "multicharacter-runtime-controller.cjs"), "utf8");
  const pet = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8"); const overlay = fs.readFileSync(path.join(sourceRoot, "companion-overlay-preload.cjs"), "utf8");
  for (const channel of ["companion-multicharacter-status", "companion-multicharacter-configure", "companion-multicharacter-send"]) {
    assert.doesNotMatch(main, new RegExp(`ipcMain\\.handle\\("chriptmas:${channel}"`));
    assert.match(ipcController, new RegExp(`"chriptmas:${channel}"`));
    assert.match(preload, new RegExp(channel)); assert.doesNotMatch(pet, new RegExp(channel)); assert.doesNotMatch(overlay, new RegExp(channel));
  }
  assert.match(main, /new CompanionMultiCharacterRuntimeController\(\{/);
  assert.match(ipcController, /this\.requireMainRenderer\(event\)/);
  assert.doesNotMatch(runtimeController, /peer\.token|peer\.port|discovery|root/);
  assert.match(ipcController, /\["wave", "greeting", "cheer"\]/);
});

test("easter egg IPC is main-only and maps a fixed local action server-side", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const ipcController = fs.readFileSync(path.join(sourceRoot, "companion", "easter-egg-ipc-controller.cjs"), "utf8");
  const runtimeController = fs.readFileSync(path.join(sourceRoot, "companion", "easter-egg-runtime-controller.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  const petPreload = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  assert.match(ipcController, /payload\.action !== "minigame_play"/);
  assert.match(ipcController, /this\.runtime\.record\("minigame\.play"\)/);
  assert.match(main, /companionEasterEggRuntimeController\.record\("gesture\.pet"\)/);
  assert.match(runtimeController, /kind: "easter_egg"/);
  assert.match(preload, /recordCompanionLocalAction/);
  assert.match(preload, /action === "minigame_play"/);
  assert.doesNotMatch(petPreload, /EasterEgg|LocalAction|easter-egg|local-action/);
  const packageJson = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
  assert.equal(packageJson.build.extraResources.some((resource) => resource.to === "companion-config" && resource.filter.includes("behaviors.json")), true);
  assert.equal(packageJson.build.extraResources.some((resource) => resource.to === "companion-config" && resource.filter.includes("items.json") && resource.filter.includes("shop.json")), true);
  assert.equal(packageJson.build.extraResources.some((resource) => resource.to === "companion-assets/items" && resource.filter.includes("*.svg")), true);
});

test("manual editor uses a fixed main-owned authority and is absent from pet preload", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const controller = fs.readFileSync(path.join(sourceRoot, "companion", "data-ipc-controller.cjs"), "utf8");
  const start = controller.indexOf("  async openManual(event)");
  assert.notEqual(start, -1);
  const block = controller.slice(start, controller.indexOf("\n  }", start));
  assert.match(block, /this\.requireMainRenderer\(event\)/);
  assert.match(block, /this\.manualPathProvider\(\)/);
  assert.match(block, /this\.shell\.openPath/);
  assert.doesNotMatch(block, /payload|targetPath/);
  assert.match(main, /manualPathProvider: \(\) => resolveCompanionManualPath\(\{/);
  const petPreload = fs.readFileSync(path.join(sourceRoot, "pet-preload.cjs"), "utf8");
  assert.doesNotMatch(petPreload, /openCompanionManual|manual-open/);
});

test("pet window operations accept only a known main or pet renderer", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const petWindowIpc = fs.readFileSync(path.join(sourceRoot, "companion", "pet-window-ipc-controller.cjs"), "utf8");
  assert.match(main, /function requireKnownRenderer\(event\)/);
  for (const method of ["openMainWindow", "hidePet"]) {
    const marker = `  ${method}(event)`;
    const start = petWindowIpc.indexOf(marker);
    const block = petWindowIpc.slice(start, petWindowIpc.indexOf("\n  }", start));
    assert.notEqual(start, -1, `missing ${method}`);
    assert.match(block, /this\.requireKnownRenderer\(event\)/, `${method} accepts an unknown sender`);
  }
});

test("companion snapshot readiness accepts only the current pet renderer", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const controller = fs.readFileSync(path.join(sourceRoot, "companion", "projection-ready-ipc-controller.cjs"), "utf8");
  assert.match(main, /new CompanionProjectionReadyIpcController\(\{[\s\S]*?petWindowProvider:[\s\S]*?stateController:[\s\S]*?appearanceArbiter:[\s\S]*?appearanceRuntimeController:/);
  assert.doesNotMatch(main, /ipcMain\.on\("chriptmas:companion-(?:state|appearance)-ready"/);
  assert.match(controller, /event\?\.sender\?\.id !== petWindow\.webContents\?\.id/);
  assert.match(controller, /this\.stateController\.deliverCurrent\(\)/);
  assert.match(controller, /this\.appearanceArbiter\.deliverCurrent\(\)/);
  assert.match(controller, /event\?\.sender !== petWindow\.webContents/);
  assert.match(controller, /event\?\.senderFrame !== petWindow\.webContents\?\.mainFrame/);
  assert.match(controller, /this\.appearanceRuntimeController\.deliverCurrent\(\)/);
});

test("transparent pet hit testing is pet-sender-bound and forwards mouse movement", () => {
  const controller = fs.readFileSync(path.join(sourceRoot, "companion", "pet-window-ipc-controller.cjs"), "utf8");
  const marker = "  setMousePassthrough(event, enabled)";
  const start = controller.indexOf(marker);
  assert.notEqual(start, -1);
  const block = controller.slice(start, controller.indexOf("\n  }", start));
  assert.match(block, /this\.requirePetRenderer\(event/);
  assert.match(block, /setIgnoreMouseEvents\(enabled === true, \{ forward: true \}\)/);
});

test("pet movement is sender-bound, bounded to a display and persisted only at drag end", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const controller = fs.readFileSync(path.join(sourceRoot, "companion", "pet-window-ipc-controller.cjs"), "utf8");
  const marker = "  move(event, delta)";
  const start = controller.indexOf(marker);
  assert.notEqual(start, -1);
  const next = controller.indexOf("\n  }", start);
  const block = controller.slice(start, next);
  assert.match(block, /this\.requirePetRenderer\(event/);
  assert.match(block, /Number\.isFinite\(dx\)/);
  assert.match(block, /Math\.abs\(dx\) > 80/);
  assert.match(block, /this\.clampPetWindowPosition\(x \+ dx, y \+ dy, bounds\.width, bounds\.height\)/);
  assert.match(block, /setPosition\(next\.x, next\.y, false\)/);
  const endMarker = "  finishMove(event)";
  const endStart = controller.indexOf(endMarker);
  assert.notEqual(endStart, -1);
  const endBlock = controller.slice(endStart, controller.indexOf("\n  }", endStart));
  assert.match(endBlock, /this\.requirePetRenderer\(event/);
  assert.match(endBlock, /this\.motionController\.settle\(petWindow\)/);
  assert.match(main, /onSettled:\s*\(\) => savePetWindowPosition\(\)/);
  assert.match(main, /PET_POSITION_FILE = "pet-window-position\.json"/);
  assert.match(main, /screen\.getDisplayMatching/);
  assert.match(main, /fs\.renameSync\(temporary, target\)/);
});
