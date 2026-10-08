const { app, BrowserWindow, Notification, clipboard, desktopCapturer, dialog, globalShortcut, ipcMain, shell, session, screen, powerMonitor, Tray, Menu, nativeImage, nativeTheme, net } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const crypto = require("node:crypto");
const { PACKAGED_STARTUP_TIMEOUT_MS, SidecarSupervisor, SESSION_HEADER } = require("./sidecar-supervisor.cjs");
const { resolveMixedMediaE2E, SWITCH_NAME: MIXED_MEDIA_E2E_SWITCH } = require("./mixed-media-e2e-hook.cjs");
const { resolvePluginHookFaultE2E, SWITCH_NAME: PLUGIN_HOOK_FAULT_E2E_SWITCH } = require("./plugin-hook-fault-e2e-hook.cjs");
const { resolveXhsControlledCredentialE2E, resolveXhsControlledCredentialRealOcrE2E, SWITCH_NAME: XHS_CONTROLLED_CREDENTIAL_E2E_SWITCH } = require("./xhs-controlled-credential-e2e-hook.cjs");
const {
  installRendererSessionPolicy,
} = require("./renderer-security.cjs");
const { resolvePackagedVaultRoot, resolveRootConfig, RootConfigMigrationController, verifyCopiedTree } = require("./vault-root.cjs");
const { RootMigrationIpcController } = require("./root-migration-ipc-controller.cjs");
const { VaultRecoveryController } = require("./vault-recovery-controller.cjs");
const { VaultRestoreIpcController } = require("./vault-restore-ipc-controller.cjs");
const { FileGrantIpcController } = require("./file-grant-ipc-controller.cjs");
const { MemoryTransferIpcController } = require("./memory-transfer-ipc-controller.cjs");
const { SessionPlacementTransferIpcController } = require("./session-placement-transfer-ipc-controller.cjs");
const { OriginalAssetIpcController } = require("./original-asset-ipc-controller.cjs");
const { ContextGraphImportIpcController } = require("./context-graph-import-ipc-controller.cjs");
const { DocumentPdfIpcController } = require("./document-pdf-ipc-controller.cjs");
const { createStartupWindow } = require("./startup-window.cjs");
const { CompanionStateController, projectionFromMood } = require("./companion-state.cjs");
const { resolveFrontendEntry: resolveEntry } = require("./frontend-entry.cjs");
const { CompanionActionRegistry, validatePanelPayload } = require("./companion/action-registry.cjs");
const { resolveCompanionE2EClockEnv, resolveCompanionE2EClockNow } = require("./companion/clock-e2e-env.cjs");
const { resolveNativeMenuE2EAction } = require("./companion/native-menu-e2e-hook.cjs");
const { CompanionOverlayController } = require("./companion/overlay-controller.cjs");
const { CompanionOverlayIpcController } = require("./companion/overlay-ipc-controller.cjs");
const { resolveOverlayBounds } = require("./companion/overlay-position.cjs");
const { CompanionClipboardWatcher } = require("./companion/clipboard-watcher.cjs");
const { CompanionClipboardIpcController } = require("./companion/clipboard-ipc-controller.cjs");
const { CompanionGestureController } = require("./companion/gesture-controller.cjs");
const { CompanionGestureIpcController } = require("./companion/gesture-ipc-controller.cjs");
const { recordCompanionInteraction } = require("./companion/interaction-recorder.cjs");
const { resolveCompanionManualPath } = require("./companion/manual-path.cjs");
const { CompanionEasterEggController } = require("./companion/easter-egg-controller.cjs");
const { CompanionEasterEggRuntimeController } = require("./companion/easter-egg-runtime-controller.cjs");
const { CompanionEasterEggIpcController } = require("./companion/easter-egg-ipc-controller.cjs");
const { CompanionLauncherController } = require("./companion/launcher-controller.cjs");
const { CompanionLauncherIpcController } = require("./companion/launcher-ipc-controller.cjs");
const { CompanionPetWindowIpcController } = require("./companion/pet-window-ipc-controller.cjs");
const { CompanionDataIpcController } = require("./companion/data-ipc-controller.cjs");
const { launcherE2ELaunchArgs, recordCompanionLauncherE2EReceipt, resolveCompanionLauncherE2E, seedCompanionLauncherE2ETarget } = require("./companion/launcher-e2e-hook.cjs");
const { CompanionFileOrganizer } = require("./companion/file-organizer.cjs");
const { CompanionFileOrganizerIpcController } = require("./companion/file-organizer-ipc-controller.cjs");
const { CompanionMultiCharacterLink, CompanionMultiCharacterSettings } = require("./companion/multicharacter-link.cjs");
const { resolveMultiCharacterE2ERoot } = require("./companion/multicharacter-e2e-root.cjs");
const { CompanionMultiCharacterRuntimeController } = require("./companion/multicharacter-runtime-controller.cjs");
const { CompanionMultiCharacterIpcController } = require("./companion/multicharacter-ipc-controller.cjs");
const { CompanionAppearanceArbiter } = require("./companion/appearance-arbiter.cjs");
const { CompanionAppearanceRuntimeController } = require("./companion/appearance-runtime-controller.cjs");
const { CompanionFocusRuntimeController } = require("./companion/focus-runtime-controller.cjs");
const { CompanionAmbientRuntimeController } = require("./companion/ambient-runtime-controller.cjs");
const { CompanionNetworkHealthAdapter } = require("./companion/network-health-adapter.cjs");
const { CompanionSystemSensorRuntimeController } = require("./companion/system-sensor-runtime-controller.cjs");
const { CompanionSystemSensorIpcController } = require("./companion/system-sensor-ipc-controller.cjs");
const { CompanionWeatherRuntimeController } = require("./companion/weather-runtime-controller.cjs");
const { CompanionWeatherIpcController } = require("./companion/weather-ipc-controller.cjs");
const { WindowsMediaSessionAdapter } = require("./companion/media-session-adapter.cjs");
const { CompanionMediaSessionRuntimeController } = require("./companion/media-session-runtime-controller.cjs");
const { CompanionMediaSessionIpcController } = require("./companion/media-session-ipc-controller.cjs");
const { CompanionVoiceController } = require("./companion/voice-controller.cjs");
const { CompanionVoiceIpcController } = require("./companion/voice-ipc-controller.cjs");
const { CompanionScreenVisionController } = require("./companion/screen-vision-controller.cjs");
const { CompanionScreenVisionIpcController } = require("./companion/screen-vision-ipc-controller.cjs");
const { CompanionProjectionReadyIpcController } = require("./companion/projection-ready-ipc-controller.cjs");
const { CompanionMicrophonePermission } = require("./companion/microphone-permission.cjs");
const { CompanionVoiceCallController, CompanionVoiceCallPresentationController } = require("./companion/voice-call-controller.cjs");
const { CompanionVoiceCallIpcController } = require("./companion/voice-call-ipc-controller.cjs");
const { CompanionReplyPresentationController } = require("./companion/reply-presentation-controller.cjs");
const { CompanionReplyIpcController } = require("./companion/reply-ipc-controller.cjs");
const { CompanionMotionController } = require("./companion/motion-controller.cjs");
const { CompanionRoutineController } = require("./companion/routine-controller.cjs");
const { CompanionRoutineIpcController } = require("./companion/routine-ipc-controller.cjs");
const { CompanionEventConsumer, CompanionReminderPresenter } = require("./companion/reminder-runtime-controller.cjs");
const { CompanionMemoryAttentionController } = require("./companion/memory-attention-controller.cjs");
const {
  configureMainWindowPresentation,
  ensureMainWindowBounds,
  resolveMainWindowTitleBar,
} = require("./desktop-window-presentation.cjs");
const { DesktopSystemIpcController } = require("./desktop-system-ipc.cjs");
const { readRuntimeCandidateIdentity } = require("./candidate-identity.cjs");
const { ApplicationNavigationController } = require("./application-navigation-controller.cjs");
const { DesktopSurfaceCoordinator } = require("./desktop-surface-coordinator.cjs");
const { ApplicationLifecycleCoordinator } = require("./application-lifecycle-coordinator.cjs");
const { RendererCrashRecoveryController } = require("./renderer-crash-recovery-controller.cjs");
const { DesktopTrayController } = require("./desktop-tray-controller.cjs");
const { DesktopRuntimeBootstrapCoordinator } = require("./desktop-runtime-bootstrap-coordinator.cjs");
const { AuthenticatedLocalGateway } = require("./authenticated-local-gateway.cjs");
const { WorkbenchRealtimeAsrIpcController } = require("./workbench-realtime-asr-ipc-controller.cjs");
const { CredentialCaptureIpcController } = require("./credential-capture-ipc-controller.cjs");
const { SidecarFailurePresenter } = require("./sidecar-failure-presenter.cjs");
const { DesktopWindowRegistry } = require("./desktop-window-registry.cjs");
const { DesktopWindowFactory } = require("./desktop-window-factory.cjs");
const { ApplicationBootstrapCoordinator } = require("./application-bootstrap-coordinator.cjs");
const { DesktopPowerMonitorController } = require("./desktop-power-monitor-controller.cjs");
const { DeferredRuntimeRegistry } = require("./deferred-runtime-registry.cjs");

// ── 前端入口解析 ──
// 开发模式：设置 CHRIPTMAS_REPLAY_FRONTEND_URL 环境变量，用 loadURL 加载 dev server。
// 打包模式：不设置该变量，用 loadFile 加载本地 build 产物（frontend-dist/index.html）。
const FRONTEND_DIST_DIR = path.join(__dirname, "..", "frontend-dist");
const FRONTEND_DIST_INDEX = path.join(FRONTEND_DIST_DIR, "index.html");
const PET_POSITION_FILE = "pet-window-position.json";

function clampPetWindowPosition(x, y, width = 180, height = 220) {
  const display = screen.getDisplayMatching({ x: Math.round(x), y: Math.round(y), width, height });
  const area = display.workArea;
  return {
    x: Math.min(Math.max(Math.round(x), area.x), area.x + Math.max(0, area.width - width)),
    y: Math.min(Math.max(Math.round(y), area.y), area.y + Math.max(0, area.height - height)),
  };
}

function readPetWindowPosition() {
  try {
    const value = JSON.parse(fs.readFileSync(path.join(app.getPath("userData"), PET_POSITION_FILE), "utf8"));
    if (!Number.isFinite(value?.x) || !Number.isFinite(value?.y)) return null;
    return clampPetWindowPosition(value.x, value.y);
  } catch {
    return null;
  }
}

function savePetWindowPosition() {
  if (!desktopWindowRegistry.pet || desktopWindowRegistry.pet.isDestroyed()) return;
  try {
    const [x, y] = desktopWindowRegistry.pet.getPosition();
    const target = path.join(app.getPath("userData"), PET_POSITION_FILE);
    const temporary = `${target}.tmp`;
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(temporary, `${JSON.stringify({ x, y })}\n`, { encoding: "utf8", mode: 0o600 });
    fs.renameSync(temporary, target);
  } catch (error) {
    console.warn(`[pet] failed to persist window position: ${error.message}`);
  }
}
function resolveFrontendEntry(petView = false) {
  return resolveEntry({
    devUrl: process.env.CHRIPTMAS_REPLAY_FRONTEND_URL,
    petView,
    frontendIndex: FRONTEND_DIST_INDEX,
  });
}

// 后端地址（供 will-navigate 白名单使用；API base URL 由 preload.cjs 通过 electronAPI 暴露）
function backendOrigin() {
  return desktopRuntimeBootstrapCoordinator.session?.origin || "";
}

let overlayChatSessionId = null;
let companionGameModeRestore = null;
let petContextMenuOpen = false;
const desktopWindowRegistry = new DesktopWindowRegistry();
const rendererCrashRecoveryController = new RendererCrashRecoveryController({
  isApplicationQuitting: () => app.isQuitting === true,
  log: (message) => console.warn(message),
});
const desktopWindowFactory = new DesktopWindowFactory({
  BrowserWindow,
  baseDir: __dirname,
  configureMainWindow: (window) => configureMainWindowPresentation(window, { nativeTheme }),
});
const companionMicrophonePermission = new CompanionMicrophonePermission({ mainWindowProvider: () => desktopWindowRegistry.main });
const companionOverlayController = new CompanionOverlayController({
  emitEvent: (event) => {
    if (!desktopWindowRegistry.overlay || desktopWindowRegistry.overlay.isDestroyed() || desktopWindowRegistry.overlay.webContents.isLoadingMainFrame()) return;
    desktopWindowRegistry.overlay.webContents.send("chriptmas:companion-overlay-event", event);
  },
  show: (options) => showCompanionOverlay(options),
  hide: () => hideCompanionOverlay(),
  onSubmit: async (request) => submitCompanionOverlayChat(request),
  onAction: async (action) => handleCompanionOverlayAction(action),
  onAcknowledge: async () => Object.freeze({ status: "acknowledged" }),
});
const companionEasterEggRuntimeController = new CompanionEasterEggRuntimeController({
  controllerProvider: () => deferredRuntimeRegistry.get("easter-eggs"),
  overlayController: companionOverlayController,
});
const companionReminderPresenter = new CompanionReminderPresenter({
  windowProvider: () => desktopWindowRegistry.main,
  overlayController: companionOverlayController,
  beep: () => shell.beep(),
});
const companionEventConsumer = new CompanionEventConsumer({
  readEvent: async () => readNextCompanionEvent(),
  onEvent: async (event) => companionReminderPresenter.present(event),
  onError: (error) => console.warn(`[companion] event transport unavailable: ${error.message}`),
  canRead: () => !companionReminderPresenter.isBlocking(),
});
const companionClipboardWatcher = new CompanionClipboardWatcher({
  clipboard,
  onEvent: (event) => companionOverlayController.present(event, { focus: false }),
  isQuiet: () => companionFocusRuntimeController.isActive()
    || companionSystemSensorRuntimeController.isGameQuiet()
    || companionRoutineController.status().sleeping
    || companionReminderPresenter.isBlocking(),
});
const companionMemoryAttentionController = new CompanionMemoryAttentionController({
  present: (event) => companionOverlayController.present(event, { focus: false }),
  isQuiet: () => companionFocusRuntimeController.isActive()
    || companionSystemSensorRuntimeController.isGameQuiet()
    || companionRoutineController.status().sleeping
    || companionReminderPresenter.isBlocking()
    || !desktopWindowRegistry.pet?.isVisible(),
});
const companionGestureController = new CompanionGestureController({
  onIntent: (intent) => handleCompanionGestureIntent(intent),
});
const companionActionRegistry = new CompanionActionRegistry([
  action("companion.main.open", "打开主界面", ["pet", "overlay", "tray"], () => showMainWindow(), 0, 10),
  action("companion.center.chat", "对话", ["pet"], () => applicationNavigationController.openCompanion("chat"), 1, 20),
  action("companion.center.memory-review", "和小熊回顾记忆", ["pet"], () => applicationNavigationController.openCompanion("chat", "weekly_memory_review"), 1, 30),
  {
    id: "companion.memory.pending-review",
    label: "审阅待确认记忆",
    sources: ["pet"],
    handler: async () => applicationNavigationController.openPendingMemoryReview(),
    enabled: () => companionMemoryAttentionController.hasPending(),
    menu: { group: 1, order: 35 },
  },
  action("companion.center.master-profile", "御主档案", ["pet"], () => applicationNavigationController.openCompanion("master_profile"), 1, 40),
  action("companion.center.focus", "专注时钟", ["pet"], () => applicationNavigationController.openCompanion("focus"), 1, 50),
  action("companion.center.ambient", "随机小剧场", ["pet"], () => applicationNavigationController.openCompanion("ambient"), 1, 55),
  action("companion.center.inventory", "背包与商城", ["pet"], () => applicationNavigationController.openCompanion("inventory"), 1, 60),
  action("companion.center.launchers", "管家服务与传送门", ["pet"], () => applicationNavigationController.openCompanion("launchers"), 1, 62),
  action("companion.center.games", "猜拳与掷骰", ["pet"], () => applicationNavigationController.openCompanion("launchers"), 1, 65),
  action("companion.center.voice", "语音与视觉", ["pet"], () => applicationNavigationController.openCompanion("voice_vision"), 1, 68),
  action("companion.center.help", "帮助与数据", ["pet"], () => applicationNavigationController.openCompanion("help_data"), 1, 70),
  action("companion.center.note", "记一笔", ["pet"], () => applicationNavigationController.openCompanion("help_data"), 1, 75),
  action("companion.pet.hide", "关闭宠物（隐藏显示）", ["pet", "main"], () => hidePetSurfaces(), 2, 80),
  action("companion.pet.show", "显示宠物", ["tray", "main"], () => showPetWindow(), 0, 20),
  action("app.quit", "退出 Chriptmas OS", ["pet", "tray"], () => quitApplication(), 3, 100),
]);
const desktopTrayController = new DesktopTrayController({
  Tray,
  Menu,
  nativeImage,
  iconPath: path.join(__dirname, "tray-icon.png"),
  actionRegistry: companionActionRegistry,
  onClick: () => showMainWindow(),
});
const companionAppearanceArbiter = new CompanionAppearanceArbiter({
  emit: (projection) => {
    if (!desktopWindowRegistry.pet || desktopWindowRegistry.pet.isDestroyed() || desktopWindowRegistry.pet.webContents.isLoadingMainFrame()) return;
    desktopWindowRegistry.pet.webContents.send("chriptmas:companion-state", projection);
  },
});
const companionMotionController = new CompanionMotionController({
  screen,
  publish: (projection) => companionAppearanceArbiter.set("motion", projection),
  clear: () => companionAppearanceArbiter.clear("motion"),
  onPosition: () => { if (desktopWindowRegistry.overlay?.isVisible()) positionCompanionOverlay(); },
  onSettled: () => savePetWindowPosition(),
});
const companionRoutineController = new CompanionRoutineController({
  publish: (projection) => companionAppearanceArbiter.set("schedule", projection),
  clear: () => companionAppearanceArbiter.clear("schedule"),
  onMorning: (localDay) => presentMorningGreeting(localDay),
  // The dual-gated packaged test clock drives both the sidecar and the main
  // process schedule lane. Production remains on the real local clock.
  now: resolveCompanionE2EClockNow() || (() => new Date()),
});
const companionStateController = new CompanionStateController({
  readProjection: async () => {
    if (!authenticatedLocalGateway.isAvailable()) return { state: "offline", mood: "idle" };
    await refreshCompanionRoutine().catch(() => companionRoutineController.evaluate());
    const result = await authenticatedLocalGateway.requestJson({
      pathname: "/api/rebuild/pet/mood",
      unavailableError: "companion_state_sidecar_unavailable",
    });
    if (!result.ok) throw new Error(`companion_state_http_${result.status}`);
    const payload = result.payload;
    companionMemoryAttentionController.update(payload);
    return projectionFromMood(payload);
  },
  emitProjection: (projection) => {
    if (projection.state === "offline") companionAppearanceArbiter.set("critical", projection);
    else {
      companionAppearanceArbiter.clear("critical");
      companionAppearanceArbiter.set("idle", projection);
    }
  },
});
const companionFocusRuntimeController = new CompanionFocusRuntimeController({
  readSession: async () => (await requestCompanionFocus("GET", "/api/rebuild/companion/focus")).session,
  observe: async (flags) => (await requestCompanionFocus("POST", "/api/rebuild/companion/focus/observe", flags)).session,
  onWarning: (session) => companionOverlayController.present({ event_id: `focus_warning_${session.revision}`, kind: "focus_warning", visual_state: "warning", text: "专注时间里先把分心应用放一放吧。", actions: [], requires_ack: false }, { focus: false }),
  onComplete: () => companionOverlayController.present({ event_id: `focus_complete_${Date.now()}`, kind: "focus_complete", visual_state: "happy", text: "专注完成，辛苦啦！奖励已经记到账本。", actions: [], requires_ack: false }, { focus: false }),
  onError: (error) => console.warn(`[companion] focus runtime unavailable: ${error.message}`),
  isGameQuiet: () => companionSystemSensorRuntimeController.isGameQuiet(),
});
const desktopPowerMonitorController = new DesktopPowerMonitorController({
  powerMonitor,
  onLock: () => companionFocusRuntimeController.setLocked(true),
  onUnlock: () => companionFocusRuntimeController.setLocked(false),
  onSuspend: () => companionFocusRuntimeController.setSuspended(true),
  onResume: () => companionFocusRuntimeController.setSuspended(false),
});
const companionAmbientRuntimeController = new CompanionAmbientRuntimeController({
  offer: async (body) => {
    const focus = await requestCompanionFocus("GET", "/api/rebuild/companion/focus").catch(() => ({ session: null }));
    return requestCompanionAmbient("POST", "/api/rebuild/companion/ambient/offer", { ...body, quiet: body.quiet || ["running", "paused"].includes(focus.session?.status) });
  },
  choose: async (eventId, optionId, revision) => requestCompanionAmbient("POST", `/api/rebuild/companion/ambient/events/${encodeURIComponent(eventId)}/choose`, { option_id: optionId, expected_revision: revision }),
  idle: async (body) => requestCompanionAmbient("POST", "/api/rebuild/companion/ambient/idle", body),
  present: (event) => companionOverlayController.present(event, { focus: false }),
  flags: () => ({ quiet: companionReminderPresenter.isBlocking(), game: companionSystemSensorRuntimeController.isGameQuiet(), sleeping: companionRoutineController.status().sleeping, idleSeconds: desktopPowerMonitorController.getSystemIdleTime() }),
  onError: (error) => console.warn(`[companion] ambient runtime unavailable: ${error.message}`),
});
const companionNetworkHealthAdapter = new CompanionNetworkHealthAdapter({ net, isOnline: () => net.isOnline() });
const companionSystemSensorRuntimeController = new CompanionSystemSensorRuntimeController({
  readStatus: () => requestCompanionSensors("GET", "/api/rebuild/companion/sensors"),
  sample: (body) => requestCompanionSensors("POST", "/api/rebuild/companion/sensors/sample", body),
  probeNetwork: (origin) => companionNetworkHealthAdapter.probe(origin),
  onSnapshot: (snapshot) => {
    const sample = snapshot?.sample;
    if (["hot", "memory_low"].includes(sample?.resource)) companionAppearanceArbiter.set("system", { state: "warning", mood: "idle", animation_key: "warn" });
    else if (sample?.network === "offline") companionAppearanceArbiter.set("system", { state: "offline", mood: "idle", animation_key: "idle" });
    else if (sample?.network === "slow") companionAppearanceArbiter.set("system", { state: "attention", mood: "idle", animation_key: "warn" });
    else companionAppearanceArbiter.clear("system");
  },
  onGameModeChanged: (current) => applyCompanionGameMode(current),
  onAlert: (resource) => companionOverlayController.present({
    event_id: `system_resource_${resource.state}_${Date.now()}`,
    kind: "system_resource",
    visual_state: "warning",
    text: resource.resource === "memory_low" ? "脑容量不够了，先关掉一些不用的程序吧。" : "电脑好烫，我也快晕啦。",
    actions: [],
    requires_ack: false,
  }, { focus: false }),
  onError: (error) => console.warn(`[companion] system sensors unavailable: ${error.message}`),
});
const desktopSurfaceCoordinator = new DesktopSurfaceCoordinator({
  mainWindowProvider: () => desktopWindowRegistry.main,
  petWindowProvider: () => desktopWindowRegistry.pet,
  overlayWindowProvider: () => desktopWindowRegistry.overlay,
  createMainWindow,
  createPetWindow,
  createOverlayWindow: createCompanionOverlayWindow,
  desktopReadyProvider: () => applicationBootstrapCoordinator.ready,
  screen,
  ensureMainWindowBounds,
  resolveOverlayBounds,
  savePetWindowPosition,
  cancelActiveGesture: () => companionGestureController.cancelActive(),
  pollCompanionState: () => companionStateController.poll(),
});
const companionReplyPresentationController = new CompanionReplyPresentationController({
  overlayController: companionOverlayController,
  appearanceArbiter: companionAppearanceArbiter,
  isQuiet: () => companionSystemSensorRuntimeController.isGameQuiet(),
  isSleeping: () => companionRoutineController.status().sleeping,
  speak: (text) => speakCompanionReply(text),
});
const companionAppearanceRuntimeController = new CompanionAppearanceRuntimeController({
  readStatus: async () => {
    const value = await requestCompanionJson("GET", "/api/rebuild/companion/appearance", undefined, "companion_appearance_sidecar_unavailable", "companion_appearance_http");
    return { outfit_id: value?.outfit_id, background_id: value?.background_id, growth_stage: value?.growth_stage, idle_variant: value?.idle_variant, state_revision: value?.state_revision };
  },
  onProjection: (projection) => {
    if (!desktopWindowRegistry.pet || desktopWindowRegistry.pet.isDestroyed() || desktopWindowRegistry.pet.webContents.isLoadingMainFrame()) return;
    desktopWindowRegistry.pet.webContents.send("chriptmas:companion-appearance", projection);
  },
  onError: () => {},
});
const companionWeatherRuntimeController = new CompanionWeatherRuntimeController({
  readStatus: () => requestCompanionWeather("GET", "/api/rebuild/companion/weather"),
  onProjection: (projection) => {
    if (!desktopWindowRegistry.pet || desktopWindowRegistry.pet.isDestroyed() || desktopWindowRegistry.pet.webContents.isLoadingMainFrame()) return;
    desktopWindowRegistry.pet.webContents.send("chriptmas:companion-weather", projection);
  },
  onExtreme: () => companionOverlayController.present({
    event_id: `weather_extreme_${Date.now()}`,
    kind: "weather_extreme",
    visual_state: "warning",
    text: "天气可能比较剧烈，请以当地官方预警为准。",
    actions: [],
    requires_ack: false,
  }, { focus: false }),
  quiet: () => companionFocusRuntimeController.isActive() || companionSystemSensorRuntimeController.isGameQuiet() || companionRoutineController.status().sleeping || companionReminderPresenter.isBlocking(),
  onError: () => {},
});
const companionMediaSessionAdapter = new WindowsMediaSessionAdapter();
const companionMediaSessionRuntimeController = new CompanionMediaSessionRuntimeController({
  readStatus: () => requestCompanionMedia("GET", "/api/rebuild/companion/media-session"),
  observe: (body) => requestCompanionMedia("POST", "/api/rebuild/companion/media-session/observe", body),
  adapter: companionMediaSessionAdapter,
  onProjection: (projection) => {
    for (const target of [desktopWindowRegistry.main, desktopWindowRegistry.pet]) {
      if (!target || target.isDestroyed() || target.webContents.isLoadingMainFrame()) continue;
      target.webContents.send("chriptmas:companion-media-session", projection);
    }
  },
  onCommentary: (projection) => companionOverlayController.present({
    event_id: `media_session_${Date.now()}`,
    kind: "media_session",
    visual_state: "attention",
    text: projection.commentary,
    actions: [],
    requires_ack: false,
  }, { focus: false }),
  quiet: () => companionFocusRuntimeController.isActive() || companionSystemSensorRuntimeController.isGameQuiet() || companionRoutineController.status().sleeping || companionReminderPresenter.isBlocking(),
  onError: () => {},
});
const sidecarFailurePresenter = new SidecarFailurePresenter({
  app,
  dialog,
  markBackendOffline: () => companionStateController.markOffline(),
});
const mixedMediaE2EEnabled = resolveMixedMediaE2E({
  commandLineToken: app.commandLine.getSwitchValue(MIXED_MEDIA_E2E_SWITCH),
});
const pluginHookFaultE2EEnabled = resolvePluginHookFaultE2E({
  commandLineToken: app.commandLine.getSwitchValue(PLUGIN_HOOK_FAULT_E2E_SWITCH),
});
const xhsControlledCredentialE2EEnabled = resolveXhsControlledCredentialE2E({
  commandLineToken: app.commandLine.getSwitchValue(XHS_CONTROLLED_CREDENTIAL_E2E_SWITCH),
});
const xhsControlledCredentialRealOcrE2EEnabled = resolveXhsControlledCredentialRealOcrE2E({
  commandLineToken: app.commandLine.getSwitchValue(XHS_CONTROLLED_CREDENTIAL_E2E_SWITCH),
});
if (process.env.CHRIPTMAS_E2E_MIXED_MEDIA_RUN_TOKEN) {
  console.warn(`[mixed-media-e2e] electron-gate=${mixedMediaE2EEnabled ? "enabled" : "disabled"}`);
}
const desktopRuntimeBootstrapCoordinator = new DesktopRuntimeBootstrapCoordinator({
  app,
  repositoryRoot: path.resolve(__dirname, "..", "..", ".."),
  resourcesRootProvider: () => process.resourcesPath,
  platform: process.platform,
  processEnv: process.env,
  SidecarSupervisor,
  VaultRecoveryController,
  resolvePackagedVaultRoot,
  createRootMigrationController: (assertSourceQuiescent) => createDesktopRootMigrationController(assertSourceQuiescent),
  resolveRuntimeRootConfig: () => {
    const resolved = resolveRootConfig({ userDataDir: app.getPath("userData") });
    const pointerStats = resolved.pointerPath ? fs.statSync(resolved.pointerPath) : null;
    const revision = pointerStats ? `pointer:${pointerStats.mtimeMs}:${pointerStats.size}` : `implicit:${resolved.mode}`;
    return Object.freeze({
      version: 1,
      revision,
      roots: Object.freeze({ vault: resolved.config.vaultRoot, model: resolved.config.modelRoot, media: resolved.config.mediaRoot }),
    });
  },
  packagedStartupTimeoutMs: PACKAGED_STARTUP_TIMEOUT_MS,
  resolveCompanionEnv: resolveCompanionE2EClockEnv,
  e2eMediaFixture: mixedMediaE2EEnabled,
  e2ePluginHookFault: pluginHookFaultE2EEnabled,
  e2eXhsControlledCredential: xhsControlledCredentialE2EEnabled,
  e2eXhsControlledRealOcr: xhsControlledCredentialRealOcrE2EEnabled,
  onVaultConflict: () => sidecarFailurePresenter.presentVaultConflict(),
  onRootMigrationRecoveryRequired: () => dialog.showMessageBox({
    type: "error",
    title: "数据根迁移等待恢复",
    message: "上次数据根迁移尚未完成，应用保持离线以保护原始资料。",
    detail: "请重新打开应用继续恢复核对。旧数据和迁移记录都会保留，应用不会自动回到旧根目录写入。",
    buttons: ["退出"],
    defaultId: 0,
    noLink: true,
  }),
  onUnexpectedExit: () => sidecarFailurePresenter.presentUnexpectedExit(),
  onRenewalFailure: (event) => sidecarFailurePresenter.presentSessionRenewalFailure(event),
  installRequestAuthentication: installSidecarRequestAuthentication,
  installRendererSecurityPolicies,
  startRuntimeControllers: () => {
    companionStateController.start();
    companionAppearanceRuntimeController.start();
  },
  log: (message) => console.warn(`[sidecar] ${message}`),
});
const authenticatedLocalGateway = new AuthenticatedLocalGateway({
  sessionProvider: () => desktopRuntimeBootstrapCoordinator.session,
  sessionHeader: SESSION_HEADER,
});
const deferredRuntimeRegistry = new DeferredRuntimeRegistry({
  "easter-eggs": {
    create: () => {
      const repositoryRoot = path.resolve(__dirname, "..", "..", "..");
      const catalogPath = app.isPackaged
        ? path.join(process.resourcesPath, "companion-config", "behaviors.json")
        : path.join(repositoryRoot, "config", "companion", "behaviors.json");
      return new CompanionEasterEggController({
        catalogPath,
        statePath: path.join(app.getPath("userData"), "companion-easter-eggs.json"),
      });
    },
  },
  launcher: {
    create: () => {
      const e2eTarget = resolveCompanionLauncherE2E({ executablePath: process.execPath, userDataRoot: app.getPath("userData") });
      const controller = new CompanionLauncherController({
        statePath: path.join(app.getPath("userData"), "companion", "launchers.json"),
        openExternal: (url) => shell.openExternal(url),
        launchArgs: (entry) => launcherE2ELaunchArgs(entry, e2eTarget),
      });
      if (e2eTarget) seedCompanionLauncherE2ETarget(controller, e2eTarget);
      return Object.freeze({ controller, e2eTarget });
    },
  },
  "file-organizer": {
    create: () => new CompanionFileOrganizer({ journalPath: path.join(app.getPath("userData"), "companion", "file-organizer-journal.json") }),
  },
  voice: {
    create: () => new CompanionVoiceController({ statePath: path.join(app.getPath("userData"), "companion", "voice.json") }),
    dispose: (controller) => controller.cancel(),
  },
  "screen-vision": {
    create: () => new CompanionScreenVisionController({
      desktopCapturer,
      nativeImage,
      tempRoot: path.join(app.getPath("temp"), "chriptmas-screen-vision"),
      sessionProvider: () => desktopRuntimeBootstrapCoordinator.session,
    }),
    dispose: (controller) => controller.cancel(),
  },
  "voice-call": {
    create: () => new CompanionVoiceCallController({
      tempRoot: path.join(app.getPath("temp"), "chriptmas-companion-voice-call-main"),
      sessionProvider: () => desktopRuntimeBootstrapCoordinator.session,
    }),
    dispose: (controller) => controller.cancel(),
  },
  "voice-call-presentation": {
    create: () => new CompanionVoiceCallPresentationController({
      voiceProvider: () => deferredRuntimeRegistry.get("voice"),
      isQuiet: () => companionSystemSensorRuntimeController.isGameQuiet() || companionRoutineController.status().sleeping,
      dispatchAudio: (audio, requestId) => sendCompanionVoiceAudio(audio, requestId, { voiceCall: true }),
      cancelAudio: () => sendCompanionVoiceCancel(),
    }),
    dispose: (controller) => controller.cancel(),
  },
});
const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) app.quit();

app.on("second-instance", () => {
  const launcher = deferredRuntimeRegistry.get("launcher");
  if (launcher?.e2eTarget) recordCompanionLauncherE2EReceipt(launcher.e2eTarget);
  showMainWindow();
});

function installSidecarRequestAuthentication() {
  session.defaultSession.webRequest.onBeforeSendHeaders((details, callback) => {
    const active = desktopRuntimeBootstrapCoordinator.session;
    const backendOrigin = desktopRuntimeBootstrapCoordinator.supervisor?.session?.origin;
    if (!active && backendOrigin && new URL(details.url).origin === backendOrigin) return callback({ cancel: true });
    if (!active || new URL(details.url).origin !== active.origin) return callback({ requestHeaders: details.requestHeaders });
    callback({ requestHeaders: { ...details.requestHeaders, [SESSION_HEADER]: active.secret } });
  });
}

async function requestCompanionJson(method, pathname, body, unavailableError, httpErrorPrefix, { timeoutMs = 5000, nullableJson = false } = {}) {
  const result = await authenticatedLocalGateway.requestJson({ method, pathname, body, timeoutMs, unavailableError, nullableJson });
  if (!result.ok) throw new Error(`${httpErrorPrefix}_${result.status}`);
  return result.payload;
}

async function requestCompanionFocus(method, pathname, body) {
  return requestCompanionJson(method, pathname, body, "companion_focus_sidecar_unavailable", "companion_focus_http");
}

async function requestCompanionAmbient(method, pathname, body) {
  return requestCompanionJson(method, pathname, body, "companion_ambient_sidecar_unavailable", "companion_ambient_http");
}

async function requestCompanionSensors(method, pathname, body) {
  return requestCompanionJson(method, pathname, body, "companion_sensors_sidecar_unavailable", "companion_sensors_http");
}

function applyCompanionGameMode(current) {
  companionMotionController.cancel();
  if (!desktopWindowRegistry.pet || desktopWindowRegistry.pet.isDestroyed()) return;
  if (current.active) {
    if (companionGameModeRestore === null) companionGameModeRestore = { visible: desktopWindowRegistry.pet.isVisible(), bounds: desktopWindowRegistry.pet.getBounds() };
    hideCompanionOverlay();
    deferredRuntimeRegistry.get("voice")?.cancel();
    sendCompanionVoiceCancel();
    companionReplyPresentationController.cancel();
    if (current.behavior === "hide") desktopWindowRegistry.pet.hide();
    else if (current.behavior === "corner") {
      const bounds = desktopWindowRegistry.pet.getBounds();
      const area = screen.getDisplayMatching(bounds).workArea;
      desktopWindowRegistry.pet.setPosition(area.x + Math.max(0, area.width - bounds.width), area.y + Math.max(0, area.height - bounds.height), false);
      if (companionGameModeRestore.visible && desktopSurfaceCoordinator.wants("pet")) desktopWindowRegistry.pet.showInactive();
    } else {
      const restoredPosition = clampPetWindowPosition(companionGameModeRestore.bounds.x, companionGameModeRestore.bounds.y, companionGameModeRestore.bounds.width, companionGameModeRestore.bounds.height);
      desktopWindowRegistry.pet.setBounds({ ...companionGameModeRestore.bounds, ...restoredPosition }, false);
      if (companionGameModeRestore.visible && desktopSurfaceCoordinator.wants("pet")) desktopWindowRegistry.pet.showInactive();
    }
    return;
  }
  const restore = companionGameModeRestore;
  companionGameModeRestore = null;
  if (!restore) return;
  const restoredPosition = clampPetWindowPosition(restore.bounds.x, restore.bounds.y, restore.bounds.width, restore.bounds.height);
  desktopWindowRegistry.pet.setBounds({ ...restore.bounds, ...restoredPosition }, false);
  if (restore.visible && desktopSurfaceCoordinator.wants("pet")) desktopWindowRegistry.pet.showInactive();
}

function installRendererSecurityPolicies() {
  const developmentOrigin = process.env.CHRIPTMAS_REPLAY_FRONTEND_URL || "";
  installRendererSessionPolicy(session.defaultSession, {
    backendOrigin: backendOrigin(),
    developmentOrigin,
    permissionCheck: (sender, permission, details) => companionMicrophonePermission.decideCheck(sender, permission, details),
    permissionRequest: (sender, permission, details) => companionMicrophonePermission.decideRequest(sender, permission, details),
  });
}

function createMainWindow() {
  const entry = resolveFrontendEntry(false);
  const preparedMainWindow = desktopWindowFactory.prepareMain({ entry, backendOrigin: backendOrigin() });
  const createdMainWindow = preparedMainWindow.window;
  desktopWindowRegistry.register("main", createdMainWindow);
  rendererCrashRecoveryController.watch(createdMainWindow.webContents);

  createdMainWindow.once("ready-to-show", () => {
    if (!desktopSurfaceCoordinator.onMainReady(createdMainWindow)) return;
    desktopWindowRegistry.destroy("startup");
  });
  preparedMainWindow.load();

  // 关闭主窗口后进入轻量陪伴层；应用仍可从pet或tray恢复。
  createdMainWindow.on("close", (event) => {
    companionMicrophonePermission.clear();
    deferredRuntimeRegistry.get("voice-call")?.cancel();
    if (!app.isQuitting) {
      event.preventDefault();
      showPetWindow();
    }
  });

  return createdMainWindow;
}

function createPetWindow() {
  const entry = resolveFrontendEntry(true);
  const savedPosition = readPetWindowPosition();
  const preparedPetWindow = desktopWindowFactory.preparePet({ entry, savedPosition });
  const createdPetWindow = preparedPetWindow.window;
  desktopWindowRegistry.register("pet", createdPetWindow, {
    onClosed: () => {
      companionGestureController.dispose();
      companionMotionController.cancel();
    },
  });

  createdPetWindow.once("ready-to-show", () => {
    desktopSurfaceCoordinator.onPetReady(createdPetWindow);
  });
  preparedPetWindow.load();
  createdPetWindow.webContents.on("did-finish-load", () => {
    companionStateController.deliverCurrent();
    companionAppearanceArbiter.deliverCurrent();
  });

  // 宠物关闭只释放陪伴surface，主应用与tray继续拥有生命周期。
  createdPetWindow.on("blur", () => companionGestureController.cancelActive());

  return createdPetWindow;
}

function createCompanionOverlayWindow() {
  const preparedOverlayWindow = desktopWindowFactory.prepareOverlay();
  const createdOverlayWindow = preparedOverlayWindow.window;
  desktopWindowRegistry.register("overlay", createdOverlayWindow, {
    onClosed: () => desktopSurfaceCoordinator.onOverlayClosed(),
  });
  createdOverlayWindow.once("ready-to-show", () => {
    desktopSurfaceCoordinator.onOverlayReady(createdOverlayWindow);
  });
  preparedOverlayWindow.load();
  return createdOverlayWindow;
}

function showMainWindow() {
  return desktopSurfaceCoordinator.showMainWindow();
}

function showPetWindow() {
  return desktopSurfaceCoordinator.showPetWindow();
}

function hidePetSurfaces() {
  return desktopSurfaceCoordinator.hidePetSurfaces();
}

function showCompanionOverlay(options = {}) {
  return desktopSurfaceCoordinator.showOverlay(options);
}

function hideCompanionOverlay() {
  return desktopSurfaceCoordinator.hideOverlay();
}

function positionCompanionOverlay() {
  return desktopSurfaceCoordinator.positionOverlay();
}

function showPetContextMenu() {
  if (!desktopWindowRegistry.pet || desktopWindowRegistry.pet.isDestroyed() || petContextMenuOpen) return { status: petContextMenuOpen ? "already-open" : "unavailable" };
  let menu;
  let testAction = null;
  try {
    const template = companionActionRegistry.menuTemplate({ source: "pet" });
    const requestedAction = resolveNativeMenuE2EAction();
    if (requestedAction) {
      const entry = template.find((item) => item?.id === requestedAction && item.enabled !== false);
      if (entry) testAction = requestedAction;
    }
    menu = Menu.buildFromTemplate(template);
  } catch {
    menu = Menu.buildFromTemplate([
      { label: "打开主界面", click: () => showMainWindow() },
      { label: "关闭宠物（隐藏显示）", click: () => hidePetSurfaces() },
      { type: "separator" },
      { label: "退出 Chriptmas OS", click: () => quitApplication() },
    ]);
  }
  petContextMenuOpen = true;
  const point = screen.getCursorScreenPoint();
  menu.popup({ window: desktopWindowRegistry.pet, x: Math.round(point.x), y: Math.round(point.y), callback: () => { petContextMenuOpen = false; } });
  if (testAction) {
    setTimeout(() => {
      void companionActionRegistry.execute(testAction, { source: "pet" }).catch(() => {});
    }, 50);
  }
  return { status: "opened", test_action: testAction };
}

function quitApplication() {
  return applicationLifecycleCoordinator.quit();
}

function action(id, label, sources, handler, group, order) {
  return { id, label, sources, handler: async () => handler(), menu: { group, order } };
}

async function handleCompanionOverlayAction(actionRequest) {
  const actionId = actionRequest?.action_id;
  const eventId = actionRequest?.event_id;
  let projection;
  if (actionId === "clipboard.inspect") projection = companionClipboardWatcher.inspect(eventId);
  else if (actionId === "clipboard.eat") projection = companionClipboardWatcher.eat(eventId);
  else if (actionId === "clipboard.undo") projection = companionClipboardWatcher.undo(eventId);
  else if (actionId === "memory.review" && companionMemoryAttentionController.hasPending()) {
    const result = applicationNavigationController.openPendingMemoryReview();
    companionOverlayController.close({ force: true });
    return result;
  } else if (["acknowledge", "snooze_5m", "complete"].includes(actionId) && companionReminderPresenter.activeEvent?.event_id === eventId) {
    const result = await actOnCompanionReminder(eventId, actionId);
    companionReminderPresenter.settle(eventId);
    return result;
  } else if (companionAmbientRuntimeController.owns(eventId, actionId)) return await companionAmbientRuntimeController.act(eventId, actionId);
  else return Object.freeze({ status: "unavailable", reason: "companion_action_not_connected" });
  companionOverlayController.present(projection, { focus: false });
  return Object.freeze({ status: "completed", action_id: actionId });
}

async function readNextCompanionEvent() {
  if (!authenticatedLocalGateway.isAvailable()) return null;
  const result = await authenticatedLocalGateway.requestJson({
    pathname: "/api/rebuild/companion/events/next",
    timeoutMs: 3000,
    unavailableError: "companion_event_backend_unavailable",
  });
  if (!result.ok) throw new Error(`companion_event_http_${result.status}`);
  return result.payload?.event || null;
}

async function actOnCompanionReminder(eventId, actionId) {
  return requestCompanionJson(
    "POST",
    `/api/rebuild/companion/events/${encodeURIComponent(eventId)}/action`,
    { action: actionId },
    "companion_event_backend_unavailable",
    "companion_event_action_http",
  );
}

async function handleCompanionGestureIntent(intent) {
  if (intent.intent === "open_main") return showMainWindow();
  const isPetting = intent.intent === "petting";
  if (!isPetting && intent.intent !== "petting_cooldown") return;
  if (companionRoutineController.status().sleeping) {
    companionOverlayController.present({
      event_id: `routine:sleep:${Date.now().toString(36)}`,
      kind: "routine_sleep",
      visual_state: "sleeping",
      text: "Zzz...",
      actions: [],
      requires_ack: false,
    }, { focus: false });
    return;
  }
  const eventId = `gesture:${isPetting ? "petting" : "cooldown"}:${Date.now().toString(36)}`;
  companionOverlayController.present({
    event_id: eventId,
    kind: isPetting ? "petting" : "petting_cooldown",
    visual_state: isPetting ? "happy" : "idle",
    text: isPetting ? "嗯……这里摸起来很舒服。" : "刚刚已经摸过啦，等一会儿再来。",
    actions: [],
    requires_ack: false,
  }, { focus: false });
  if (isPetting) {
    companionEasterEggRuntimeController.record("gesture.pet");
    await recordPettingInteraction(eventId);
  }
}

async function refreshCompanionRoutine() {
  const result = await authenticatedLocalGateway.requestJson({
    pathname: "/api/rebuild/companion/settings",
    unavailableError: "companion_routine_unavailable",
    nullableJson: true,
    parseErrorJson: true,
  });
  if (!result.ok || !companionRoutineController.applySnapshot(result.payload)) throw new Error("companion_routine_invalid");
  return companionRoutineController.status();
}

async function presentMorningGreeting(localDay) {
  if (!authenticatedLocalGateway.isAvailable()) return;
  try {
    const result = await authenticatedLocalGateway.requestJson({
      method: "POST",
      pathname: "/api/rebuild/companion/routine/morning-claim",
      unavailableError: "companion_routine_unavailable",
      nullableJson: true,
      parseErrorJson: true,
    });
    if (!result.ok || result.payload?.claimed !== true || result.payload?.local_day !== localDay) return;
    companionOverlayController.present({
      event_id: `routine:morning:${localDay}`,
      kind: "routine_morning",
      visual_state: "happy",
      text: "早安。今天也一起慢慢把事情做好吧。",
      actions: [],
      requires_ack: false,
    }, { focus: false });
  } catch {}
}

function multiCharacterDiscoveryRoot() {
  return resolveMultiCharacterE2ERoot({ userDataRoot: app.getPath("userData") })
    || path.join(app.getPath("appData"), "Chriptmas", "companion-network");
}

function presentMultiCharacterEvent(event) {
  if (event.type === "emote") companionAppearanceArbiter.set("interactive", { state: "attention", mood: "curious", animation_key: event.emote === "sleepy" ? "sleep" : "idle" }, { ttlMs: 3000 });
  if (event.type === "short_line") companionOverlayController.present({ event_id: `peer:${crypto.randomUUID()}`, kind: "peer_short_line", visual_state: "attention", text: `${event.character_id}：${event.text}`, actions: [], requires_ack: false }, { focus: false });
}

async function requestCompanionWeather(method, pathname, body) {
  return requestCompanionJson(method, pathname, body, "companion_weather_sidecar_unavailable", "companion_weather_http");
}

async function requestCompanionMedia(method, pathname, body) {
  return requestCompanionJson(method, pathname, body, "companion_media_sidecar_unavailable", "companion_media_http");
}

async function submitCompanionOverlayChat(request) {
  const result = await requestCompanionChat({
    requestId: request.request_id,
    text: request.text,
    sessionId: overlayChatSessionId,
  });
  overlayChatSessionId = result.session.session_id;
  companionReplyPresentationController.present(result.assistant_message.content);
  return Object.freeze({
    status: "completed",
    session_id: overlayChatSessionId,
    message_id: result.assistant_message.message_id,
    source: result.source,
  });
}

async function requestCompanionChat({ requestId, text, sessionId = null }) {
  const body = sessionId
    ? { request_id: requestId, session_id: sessionId, text }
    : { request_id: requestId, text };
  const result = await authenticatedLocalGateway.requestJson({
    method: "POST",
    pathname: "/api/rebuild/companion/chat",
    body,
    timeoutMs: 65_000,
    unavailableError: "companion_chat_unavailable",
    nullableJson: true,
    parseErrorJson: true,
  });
  const payload = result.payload;
  if (!result.ok || !payload?.session?.session_id || typeof payload?.assistant_message?.content !== "string") {
    throw new Error("companion_chat_unavailable");
  }
  return payload;
}

async function speakCompanionReply(text) {
  const voiceController = deferredRuntimeRegistry.get("voice");
  if (!voiceController?.status().enabled) return;
  try {
    const result = await voiceController.speak(text);
    if (companionSystemSensorRuntimeController.isGameQuiet() || companionRoutineController.status().sleeping) return;
    sendCompanionVoiceAudio(result.audio);
  } catch (error) {
    if (!["companion_voice_superseded", "companion_voice_disabled"].some((code) => String(error?.message || "").includes(code))) console.warn(`[companion] local voice unavailable: ${error.message}`);
  }
}

function sendCompanionVoiceAudio(audio, requestId = crypto.randomUUID(), { voiceCall = false } = {}) {
  if (!desktopWindowRegistry.pet || desktopWindowRegistry.pet.isDestroyed() || desktopWindowRegistry.pet.webContents.isLoadingMainFrame()) return false;
  const bytes = Buffer.from(audio);
  if (bytes.length < 12 || bytes.length > 8 * 1024 * 1024) return false;
  if (!voiceCall) deferredRuntimeRegistry.get("voice-call-presentation")?.supersedePlayback();
  desktopWindowRegistry.pet.webContents.send("chriptmas:companion-voice-audio", { request_id: requestId, audio: bytes });
  return true;
}

function sendCompanionVoiceCancel() {
  if (!desktopWindowRegistry.pet || desktopWindowRegistry.pet.isDestroyed() || desktopWindowRegistry.pet.webContents.isLoadingMainFrame()) return false;
  desktopWindowRegistry.pet.webContents.send("chriptmas:companion-voice-cancel");
  return true;
}

async function recordPettingInteraction(eventId) {
  const active = desktopRuntimeBootstrapCoordinator.session;
  if (!active) return { status: "unavailable" };
  try {
    return await recordCompanionInteraction({
      origin: active.origin,
      secret: active.secret,
      sessionHeader: SESSION_HEADER,
      eventId,
    });
  } catch {
    return { status: "unavailable" };
  }
}

function togglePetWindow() {
  if (desktopWindowRegistry.pet !== null && !desktopWindowRegistry.pet.isDestroyed() && desktopWindowRegistry.pet.isVisible()) showMainWindow();
  else showPetWindow();
}

function requireMainRenderer(event) {
  if (!desktopWindowRegistry.main || desktopWindowRegistry.main.isDestroyed() || event.sender.id !== desktopWindowRegistry.main.webContents.id) {
    throw new Error("ipc_main_sender_rejected");
  }
}

function requireKnownRenderer(event) {
  const senderId = event.sender.id;
  const isMain = desktopWindowRegistry.main && !desktopWindowRegistry.main.isDestroyed() && senderId === desktopWindowRegistry.main.webContents.id;
  const isPet = desktopWindowRegistry.pet && !desktopWindowRegistry.pet.isDestroyed() && senderId === desktopWindowRegistry.pet.webContents.id;
  if (!isMain && !isPet) throw new Error("ipc_window_sender_rejected");
}

ipcMain.handle("chriptmas:workbench-microphone-arm", (event, payload) => {
  requireMainRenderer(event);
  if (!payload || typeof payload !== "object" || Array.isArray(payload)
    || Object.keys(payload).join() !== "transient_activation" || payload.transient_activation !== true) {
    throw new Error("workbench_microphone_activation_required");
  }
  const mainWindow = desktopWindowRegistry.main;
  return companionMicrophonePermission.arm(event.sender, {
    purpose: "workbench-transcription",
    transientActivation: true,
    topFrame: event.senderFrame === mainWindow?.webContents?.mainFrame,
  });
});

const workbenchRealtimeAsrIpcController = new WorkbenchRealtimeAsrIpcController({
  ipcMain,
  requireMainRenderer,
  gateway: authenticatedLocalGateway,
});
workbenchRealtimeAsrIpcController.install();

const desktopSystemIpcController = new DesktopSystemIpcController({
  ipcMain,
  app,
  Notification,
  globalShortcut,
  shell,
  requireMainRenderer,
  showMainWindow,
  setWindowAppearance: (theme) => {
    nativeTheme.themeSource = theme;
    desktopWindowRegistry.main?.setTitleBarOverlay?.(
      resolveMainWindowTitleBar(nativeTheme.shouldUseDarkColors === true),
    );
  },
  platform: process.platform,
  pathSeparator: path.sep,
  candidateIdentityProvider: () => readRuntimeCandidateIdentity({
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
  }),
});
desktopSystemIpcController.install();

const originalAssetIpcController = new OriginalAssetIpcController({
  ipcMain,
  shell,
  requireMainRenderer,
  sessionProvider: () => desktopRuntimeBootstrapCoordinator.session,
});
originalAssetIpcController.install();

const contextGraphImportIpcController = new ContextGraphImportIpcController({
  ipcMain,
  dialog,
  requireMainRenderer,
  mainWindowProvider: () => desktopWindowRegistry.main,
  sessionProvider: () => desktopRuntimeBootstrapCoordinator.session,
});
contextGraphImportIpcController.install();

const documentPdfIpcController = new DocumentPdfIpcController({
  ipcMain,
  BrowserWindow,
  requireMainRenderer,
  sessionProvider: () => desktopRuntimeBootstrapCoordinator.session,
});
documentPdfIpcController.install();

const fileGrantIpcController = new FileGrantIpcController({
  ipcMain,
  dialog,
  requireMainRenderer,
  sessionProvider: () => desktopRuntimeBootstrapCoordinator.session,
});
fileGrantIpcController.install();

const memoryTransferIpcController = new MemoryTransferIpcController({
  ipcMain,
  dialog,
  requireMainRenderer,
  mainWindowProvider: () => desktopWindowRegistry.main,
  sessionProvider: () => desktopRuntimeBootstrapCoordinator.session,
});
memoryTransferIpcController.install();
const sessionPlacementTransferIpcController = new SessionPlacementTransferIpcController({
  ipcMain,
  dialog,
  requireMainRenderer,
  mainWindowProvider: () => desktopWindowRegistry.main,
  sessionProvider: () => desktopRuntimeBootstrapCoordinator.session,
});
sessionPlacementTransferIpcController.install();

const vaultRestoreIpcController = new VaultRestoreIpcController({
  ipcMain,
  dialog,
  requireMainRenderer,
  mainWindowProvider: () => desktopWindowRegistry.main,
  sessionProvider: () => desktopRuntimeBootstrapCoordinator.session,
  recoveryProvider: () => desktopRuntimeBootstrapCoordinator.recovery,
  sidecarStop: () => desktopRuntimeBootstrapCoordinator.stop(),
  scheduleRelaunch: () => scheduleVaultRecoveryRelaunch(),
  sessionHeader: SESSION_HEADER,
});
vaultRestoreIpcController.install();

const rootMigrationIpcController = new RootMigrationIpcController({
  ipcMain,
  dialog,
  requireMainRenderer,
  mainWindowProvider: () => desktopWindowRegistry.main,
  controllerFactory: (assertSourceQuiescent) => createDesktopRootMigrationController(assertSourceQuiescent),
  runExclusive: (work, options) => desktopRuntimeBootstrapCoordinator.runRootMigrationExclusive(work, options),
  runtimeAvailable: () => app.isPackaged === true,
});
rootMigrationIpcController.install();

function createDesktopRootMigrationController(assertSourceQuiescent) {
  return new RootConfigMigrationController({
    userDataDir: app.getPath("userData"),
    assertSourceQuiescent,
    verifyCopiedTree: (source, target, manifest, roles) => desktopRuntimeBootstrapCoordinator.verifyRootMigrationCopy(
      source,
      target,
      manifest,
      roles,
      verifyCopiedTree,
    ),
  });
}

const companionFileOrganizerIpcController = new CompanionFileOrganizerIpcController({
  ipcMain,
  dialog,
  requireMainRenderer,
  mainWindowProvider: () => desktopWindowRegistry.main,
  organizerProvider: () => deferredRuntimeRegistry.get("file-organizer"),
  onExecuted: (result) => companionOverlayController.present({
    event_id: `organizer:${result.operation_id}`,
    kind: "organizer_feedback",
    visual_state: result.status === "completed" ? "happy" : "warning",
    text: result.status === "completed"
      ? `桌面整理完毕，共整理 ${result.moved} 个文件。`
      : `已整理 ${result.moved} 个文件，剩余项目因冲突或变更停止。`,
    actions: [],
    requires_ack: false,
  }, { focus: false }),
});
companionFileOrganizerIpcController.install();

const companionLauncherIpcController = new CompanionLauncherIpcController({
  ipcMain,
  dialog,
  requireMainRenderer,
  mainWindowProvider: () => desktopWindowRegistry.main,
  launcherProvider: () => deferredRuntimeRegistry.get("launcher")?.controller || null,
  onLaunched: (result) => companionOverlayController.present({
    event_id: `launcher:${Date.now().toString(36)}`,
    kind: "launcher_feedback",
    visual_state: "happy",
    text: `正在为您启动${result.name}……`,
    actions: [],
    requires_ack: false,
  }, { focus: false }),
});
companionLauncherIpcController.install();

const companionPetWindowIpcController = new CompanionPetWindowIpcController({
  ipcMain,
  requireMainRenderer,
  requireKnownRenderer,
  petWindowProvider: () => desktopWindowRegistry.pet,
  overlayWindowProvider: () => desktopWindowRegistry.overlay,
  showPetWindow,
  showMainWindow,
  hidePetSurfaces,
  showPetContextMenu,
  positionCompanionOverlay,
  clampPetWindowPosition,
  motionController: companionMotionController,
});
companionPetWindowIpcController.install();

const companionDataIpcController = new CompanionDataIpcController({
  ipcMain,
  dialog,
  shell,
  crypto,
  fetchImpl: fetch,
  sessionHeader: SESSION_HEADER,
  sessionProvider: () => desktopRuntimeBootstrapCoordinator.session,
  mainWindowProvider: () => desktopWindowRegistry.main,
  manualPathProvider: () => resolveCompanionManualPath({
    packaged: app.isPackaged,
    repositoryRoot: path.resolve(__dirname, "..", "..", ".."),
    resourcesRoot: app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "..", "..", ".."),
    userDataRoot: app.getPath("userData"),
  }),
  requireMainRenderer,
  createTimeoutSignal: (milliseconds) => AbortSignal.timeout(milliseconds),
});
companionDataIpcController.install();

const companionVoiceIpcController = new CompanionVoiceIpcController({
  ipcMain,
  dialog,
  requireMainRenderer,
  mainWindowProvider: () => desktopWindowRegistry.main,
  voiceProvider: () => deferredRuntimeRegistry.get("voice"),
  isQuiet: () => companionSystemSensorRuntimeController.isGameQuiet() || companionRoutineController.status().sleeping,
  dispatchAudio: (audio) => sendCompanionVoiceAudio(audio),
});
companionVoiceIpcController.install();

const applicationNavigationController = new ApplicationNavigationController({
  ipcMain,
  mainWindowProvider: () => desktopWindowRegistry.main,
  showMainWindow,
  validatePanelPayload,
});
applicationNavigationController.install();

const companionClipboardIpcController = new CompanionClipboardIpcController({
  ipcMain,
  requireMainRenderer,
  watcher: companionClipboardWatcher,
  overlayController: companionOverlayController,
  userDataPathProvider: () => app.getPath("userData"),
});
companionClipboardIpcController.install();

const companionMultiCharacterRuntimeController = new CompanionMultiCharacterRuntimeController({
  createSettings: () => new CompanionMultiCharacterSettings({
    statePath: path.join(app.getPath("userData"), "companion", "multicharacter.json"),
  }),
  createLink: (settings) => new CompanionMultiCharacterLink({
    root: multiCharacterDiscoveryRoot(),
    characterId: settings.character_id,
    allowedCharacterIds: settings.allowed_character_ids,
    quiet: () => companionFocusRuntimeController.isActive()
      || companionSystemSensorRuntimeController.isGameQuiet()
      || companionRoutineController.status().sleeping
      || companionReminderPresenter.isBlocking(),
    stateProvider: () => companionRoutineController.status().sleeping
      ? "sleeping"
      : companionFocusRuntimeController.isActive()
        ? "busy"
        : companionSystemSensorRuntimeController.isGameQuiet() ? "playing" : "idle",
    onEvent: (event) => presentMultiCharacterEvent(event),
  }),
});
const companionMultiCharacterIpcController = new CompanionMultiCharacterIpcController({
  ipcMain,
  requireMainRenderer,
  runtimeController: companionMultiCharacterRuntimeController,
});
companionMultiCharacterIpcController.install();

const companionOverlayIpcController = new CompanionOverlayIpcController({
  ipcMain,
  overlayWindowProvider: () => desktopWindowRegistry.overlay,
  overlayController: companionOverlayController,
  validatePanelPayload,
  openCenter: (panel) => applicationNavigationController.openCompanion(panel),
});
companionOverlayIpcController.install();

const companionScreenVisionIpcController = new CompanionScreenVisionIpcController({
  ipcMain,
  requireMainRenderer,
  visionProvider: () => deferredRuntimeRegistry.get("screen-vision"),
});
companionScreenVisionIpcController.install();

const companionVoiceCallIpcController = new CompanionVoiceCallIpcController({
  ipcMain,
  requireMainRenderer,
  mainWindowProvider: () => desktopWindowRegistry.main,
  petWindowProvider: () => desktopWindowRegistry.pet,
  microphonePermission: companionMicrophonePermission,
  voiceProvider: () => deferredRuntimeRegistry.get("voice"),
  voiceCallProvider: () => deferredRuntimeRegistry.get("voice-call"),
  presentationProvider: () => deferredRuntimeRegistry.get("voice-call-presentation"),
  cancelAudio: () => sendCompanionVoiceCancel(),
  presentReply: (text, options) => companionReplyPresentationController.present(text, options),
});
companionVoiceCallIpcController.install();

const companionRoutineIpcController = new CompanionRoutineIpcController({
  ipcMain,
  requireMainRenderer,
  routineController: companionRoutineController,
  refreshRoutine: () => refreshCompanionRoutine(),
  presentOverlay: (event, options) => companionOverlayController.present(event, options),
});
companionRoutineIpcController.install();

const companionWeatherIpcController = new CompanionWeatherIpcController({
  ipcMain,
  requireMainRenderer,
  weatherController: companionWeatherRuntimeController,
  petWindowProvider: () => desktopWindowRegistry.pet,
  openExternal: (url) => shell.openExternal(url),
});
companionWeatherIpcController.install();

const companionMediaSessionIpcController = new CompanionMediaSessionIpcController({
  ipcMain,
  requireMainRenderer,
  mediaController: companionMediaSessionRuntimeController,
  mainWindowProvider: () => desktopWindowRegistry.main,
  petWindowProvider: () => desktopWindowRegistry.pet,
});
companionMediaSessionIpcController.install();

const companionProjectionReadyIpcController = new CompanionProjectionReadyIpcController({
  ipcMain,
  petWindowProvider: () => desktopWindowRegistry.pet,
  stateController: companionStateController,
  appearanceArbiter: companionAppearanceArbiter,
  appearanceRuntimeController: companionAppearanceRuntimeController,
});
companionProjectionReadyIpcController.install();

const companionGestureIpcController = new CompanionGestureIpcController({
  ipcMain,
  petWindowProvider: () => desktopWindowRegistry.pet,
  gestureController: companionGestureController,
});
companionGestureIpcController.install();

const companionEasterEggIpcController = new CompanionEasterEggIpcController({
  ipcMain,
  requireMainRenderer,
  runtime: companionEasterEggRuntimeController,
});
companionEasterEggIpcController.install();

const companionReplyIpcController = new CompanionReplyIpcController({
  ipcMain,
  requireMainRenderer,
  presentation: companionReplyPresentationController,
});
companionReplyIpcController.install();

const credentialCaptureIpcController = new CredentialCaptureIpcController({
  ipcMain,
  requireMainRenderer,
  captureCredential: async (payload) => {
    const result = await authenticatedLocalGateway.requestJson({
      method: "POST",
      pathname: "/api/rebuild/security/credentials/capture",
      body: payload,
      timeoutMs: 15_000,
      unavailableError: "credential_capture_unavailable",
      parseErrorJson: true,
    });
    if (!result.ok) throw new Error("credential_capture_failed");
    return result.payload;
  },
});
credentialCaptureIpcController.install();

const companionSystemSensorIpcController = new CompanionSystemSensorIpcController({
  ipcMain,
  requireMainRenderer,
  sensorRuntime: companionSystemSensorRuntimeController,
});
companionSystemSensorIpcController.install();

const applicationLifecycleCoordinator = new ApplicationLifecycleCoordinator({
  app,
  log: (message) => console.warn(message),
  shutdownSteps: [
    { name: "companion-state", run: () => companionStateController.stop() },
    { name: "companion-appearance-runtime", run: () => companionAppearanceRuntimeController.stop() },
    { name: "companion-events", run: () => companionEventConsumer.stop() },
    { name: "companion-focus", run: () => companionFocusRuntimeController.stop() },
    { name: "companion-ambient", run: () => companionAmbientRuntimeController.stop() },
    { name: "companion-system-sensors", run: () => companionSystemSensorRuntimeController.dispose() },
    { name: "companion-weather", run: () => companionWeatherRuntimeController.dispose() },
    { name: "companion-media-session", run: () => companionMediaSessionRuntimeController.dispose() },
    { name: "companion-voice", run: () => deferredRuntimeRegistry.dispose("voice") },
    { name: "companion-screen-vision", run: () => deferredRuntimeRegistry.dispose("screen-vision") },
    { name: "companion-microphone", run: () => companionMicrophonePermission.clear() },
    { name: "companion-voice-call", run: () => deferredRuntimeRegistry.dispose("voice-call") },
    { name: "companion-voice-presentation", run: () => deferredRuntimeRegistry.dispose("voice-call-presentation") },
    { name: "companion-reply-presentation", run: () => companionReplyPresentationController.dispose() },
    { name: "companion-reminders", run: () => companionReminderPresenter.shutdown() },
    { name: "companion-overlay", run: () => companionOverlayController.close({ force: true }) },
    { name: "companion-gestures", run: () => companionGestureController.dispose() },
    { name: "companion-motion", run: () => companionMotionController.dispose() },
    { name: "companion-appearance-arbiter", run: () => companionAppearanceArbiter.dispose() },
    { name: "companion-multicharacter", run: () => companionMultiCharacterRuntimeController.stop() },
    { name: "file-grants-ipc", run: () => fileGrantIpcController.dispose() },
    { name: "memory-transfer-ipc", run: () => memoryTransferIpcController.dispose() },
    { name: "session-placement-transfer-ipc", run: () => sessionPlacementTransferIpcController.dispose() },
    { name: "original-asset-ipc", run: () => originalAssetIpcController.dispose() },
    { name: "workbench-realtime-asr-ipc", run: () => workbenchRealtimeAsrIpcController.dispose() },
    { name: "context-graph-import-ipc", run: () => contextGraphImportIpcController.dispose() },
    { name: "document-pdf-ipc", run: () => documentPdfIpcController.dispose() },
    { name: "vault-restore-ipc", run: () => vaultRestoreIpcController.dispose() },
    { name: "root-migration-ipc", run: () => rootMigrationIpcController.dispose() },
    { name: "companion-file-organizer-ipc", run: () => companionFileOrganizerIpcController.dispose() },
    { name: "companion-launcher-ipc", run: () => companionLauncherIpcController.dispose() },
    { name: "companion-pet-window-ipc", run: () => companionPetWindowIpcController.dispose() },
    { name: "companion-data-ipc", run: () => companionDataIpcController.dispose() },
    { name: "companion-voice-ipc", run: () => companionVoiceIpcController.dispose() },
    { name: "application-navigation", run: () => applicationNavigationController.dispose() },
    { name: "companion-clipboard-ipc", run: () => companionClipboardIpcController.dispose() },
    { name: "companion-multicharacter-ipc", run: () => companionMultiCharacterIpcController.dispose() },
    { name: "companion-easter-egg-ipc", run: () => companionEasterEggIpcController.dispose() },
    { name: "companion-reply-ipc", run: () => companionReplyIpcController.dispose() },
    { name: "credential-capture-ipc", run: () => credentialCaptureIpcController.dispose() },
    { name: "companion-system-sensor-ipc", run: () => companionSystemSensorIpcController.dispose() },
    { name: "companion-overlay-ipc", run: () => companionOverlayIpcController.dispose() },
    { name: "companion-screen-vision-ipc", run: () => companionScreenVisionIpcController.dispose() },
    { name: "companion-voice-call-ipc", run: () => companionVoiceCallIpcController.dispose() },
    { name: "companion-routine-ipc", run: () => companionRoutineIpcController.dispose() },
    { name: "companion-weather-ipc", run: () => companionWeatherIpcController.dispose() },
    { name: "companion-media-session-ipc", run: () => companionMediaSessionIpcController.dispose() },
    { name: "companion-projection-ready-ipc", run: () => companionProjectionReadyIpcController.dispose() },
    { name: "companion-gesture-ipc", run: () => companionGestureIpcController.dispose() },
    { name: "desktop-tray", run: () => desktopTrayController.dispose() },
    { name: "desktop-power-monitor", run: () => desktopPowerMonitorController.dispose() },
    { name: "application-bootstrap", run: () => applicationBootstrapCoordinator.dispose() },
    { name: "desktop-system-ipc", run: () => desktopSystemIpcController.dispose() },
    { name: "sidecar", run: () => desktopRuntimeBootstrapCoordinator.stop() },
  ],
});
applicationLifecycleCoordinator.install();

function scheduleVaultRecoveryRelaunch() {
  app.relaunch();
  setTimeout(() => app.exit(0), 200);
}

function presentSidecarStartupFailure(error) {
  const startupMessage = error?.code === "SIDECAR_STARTUP_TIMEOUT"
    ? "本地后端启动超过 45 秒，应用已停止等待。Windows 安全扫描或磁盘繁忙可能拖慢运行时加载，请稍后重试。"
    : "本地后端未能启动。请重新启动应用；若问题持续，请检查打包运行时是否完整。";
  console.error("[sidecar] startup failed", error?.diagnostic || error);
  dialog.showErrorBox("Chriptmas OS 启动失败", startupMessage);
  desktopWindowRegistry.destroy("startup");
  app.quit();
}

const applicationBootstrapCoordinator = new ApplicationBootstrapCoordinator({
  app,
  enabledProvider: () => hasSingleInstanceLock,
  beginStartupFeedback: () => desktopWindowRegistry.register("startup", createStartupWindow({ BrowserWindow, version: app.getVersion() })),
  startRequiredRuntime: () => desktopRuntimeBootstrapCoordinator.start(),
  onRequiredRuntimeFailure: presentSidecarStartupFailure,
  steps: [
    { name: "easter-eggs", optional: true, run: () => deferredRuntimeRegistry.initialize("easter-eggs"), onError: (error) => console.warn(`[companion] easter eggs unavailable: ${error.message}`) },
    { name: "launchers", optional: true, run: () => deferredRuntimeRegistry.initialize("launcher"), onError: (error) => console.warn(`[companion] launchers unavailable: ${error.message}`) },
    { name: "file-organizer", optional: true, run: () => deferredRuntimeRegistry.initialize("file-organizer"), onError: (error) => console.warn(`[companion] file organizer unavailable: ${error.message}`) },
    { name: "local-voice", optional: true, run: () => deferredRuntimeRegistry.initialize("voice"), onError: (error) => console.warn(`[companion] local voice unavailable: ${error.message}`) },
    { name: "screen-vision", optional: true, run: () => deferredRuntimeRegistry.initialize("screen-vision"), onError: (error) => console.warn(`[companion] screen vision unavailable: ${error.message}`) },
    { name: "voice-call", optional: true, run: () => {
      deferredRuntimeRegistry.initialize("voice-call");
      deferredRuntimeRegistry.initialize("voice-call-presentation");
    }, onError: (error) => console.warn(`[companion] voice call unavailable: ${error.message}`) },
    { name: "main-window", run: createMainWindow },
    { name: "pet-window", run: createPetWindow },
    { name: "overlay-window", run: createCompanionOverlayWindow },
    { name: "desktop-tray", run: () => desktopTrayController.create() },
    { name: "multi-character", optional: true, run: () => companionMultiCharacterRuntimeController.initialize(), onError: (error) => console.warn(`[companion] multi-character link unavailable: ${error.message}`) },
    { name: "clipboard", run: () => companionClipboardIpcController.initialize() },
    { name: "companion-events", run: () => companionEventConsumer.start() },
    { name: "focus-runtime", run: () => companionFocusRuntimeController.start() },
    { name: "ambient-runtime", run: () => companionAmbientRuntimeController.start() },
    { name: "system-sensors", run: () => companionSystemSensorRuntimeController.refresh() },
    { name: "weather", run: () => companionWeatherRuntimeController.refresh() },
    { name: "media-session", run: () => companionMediaSessionRuntimeController.refreshConfig() },
    { name: "power-monitor", run: () => desktopPowerMonitorController.install() },
  ],
  onReady: () => {
    showMainWindow();
    void documentPdfIpcController.recoverWaiting().catch((error) => console.warn(`[document-pdf] ${error?.message || error}`));
  },
  onActivate: () => showMainWindow(),
});
applicationBootstrapCoordinator.install();

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
