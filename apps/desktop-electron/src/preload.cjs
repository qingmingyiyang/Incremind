const { contextBridge, ipcRenderer, webUtils } = require("electron");

const backendBaseUrl = (
  process.env.CHRIPTMAS_DESKTOP_BACKEND_ORIGIN || ""
).replace(/\/$/, "");

contextBridge.exposeInMainWorld(
  "electronAPI",
  Object.freeze({
    backendBaseUrl,
    shell: "electron",
    entry: "workspace",
    getPlatformInfo: () => ipcRenderer.invoke("chriptmas:platform-info"),
    setWindowAppearance: (theme) => ipcRenderer.invoke("chriptmas:window-appearance", {
      theme: ["light", "dark", "system"].includes(theme) ? theme : "light",
    }),
    exportMemoryAssetPackage: () => ipcRenderer.invoke("chriptmas:memory-assets-export"),
    importMemoryAssetPackage: () => ipcRenderer.invoke("chriptmas:memory-assets-import"),
    saveMemoryExport: (payload) => ipcRenderer.invoke("chriptmas:memory-export-save", payload),
    saveSessionResumeBundle: (request) => ipcRenderer.invoke("chriptmas:session-placement-save", request),
    importSessionResumeBundle: () => ipcRenderer.invoke("chriptmas:session-placement-import"),
    reconcileSessionResumeBundle: (bundleId, projectId) => ipcRenderer.invoke("chriptmas:session-placement-reconcile", {
      bundle_id: typeof bundleId === "string" ? bundleId : "",
      project_id: typeof projectId === "string" ? projectId : "",
    }),
    restoreMemorySnapshot: (snapshotId, rollbackId) => ipcRenderer.invoke("chriptmas:vault-restore", {
      snapshot_id: typeof snapshotId === "string" ? snapshotId.slice(0, 128) : "",
      rollback_id: typeof rollbackId === "string" ? rollbackId.slice(0, 32) : "",
    }),
    inspectRootMigration: () => ipcRenderer.invoke("chriptmas:root-migration-inspect"),
    selectRootMigrationFolder: (role) => ipcRenderer.invoke("chriptmas:root-migration-select", {
      role: ["vault", "model", "media"].includes(role) ? role : "",
    }),
    preflightRootMigration: (target) => ipcRenderer.invoke("chriptmas:root-migration-preflight", sanitizeRootMigrationTarget(target)),
    executeRootMigration: (operationId, target) => ipcRenderer.invoke("chriptmas:root-migration-execute", {
      operationId: typeof operationId === "string" ? operationId.slice(0, 96) : "",
      target: sanitizeRootMigrationTarget(target),
    }),
    recoverRootMigration: () => ipcRenderer.invoke("chriptmas:root-migration-recover"),
    openPath: (targetPath) => ipcRenderer.invoke("chriptmas:open-path", {
      path: typeof targetPath === "string" ? targetPath : "",
    }),
    openOriginalAsset: (assetId) => ipcRenderer.invoke("chriptmas:open-original-asset", {
      assetId: typeof assetId === "string" ? assetId : "",
    }),
    stageContextGraphImport: (projectId, sourceType, commandId) => ipcRenderer.invoke("chriptmas:stage-context-graph-import", {
      projectId: typeof projectId === "string" ? projectId : "",
      sourceType: typeof sourceType === "string" ? sourceType : "",
      commandId: typeof commandId === "string" ? commandId : "",
    }),
    generateDocumentPdf: (htmlDeliveryId, projectId) => ipcRenderer.invoke("chriptmas:document-pdf-generate", {
      htmlDeliveryId: typeof htmlDeliveryId === "string" ? htmlDeliveryId : "",
      projectId: typeof projectId === "string" ? projectId : "",
    }),
    showNotification: (options) => ipcRenderer.invoke("chriptmas:show-notification", {
      title: typeof options?.title === "string" ? options.title : "",
      body: typeof options?.body === "string" ? options.body : "",
    }),
    registerShortcut: (accelerator) => ipcRenderer.invoke("chriptmas:register-shortcut", {
      accelerator: typeof accelerator === "string" ? accelerator : "",
    }),
    selectLocalFile: (options) => ipcRenderer.invoke("chriptmas:select-local-file", {
      mediaKind: typeof options?.mediaKind === "string" ? options.mediaKind : "file",
    }),
    uploadLocalFile: (file, options = {}) => ipcRenderer.invoke("chriptmas:upload-local-file", {
      filePath: webUtils.getPathForFile(file),
      requestId: typeof options?.requestId === "string" ? options.requestId : "",
      mediaType: typeof options?.mediaType === "string" ? options.mediaType : "application/octet-stream",
      sourceKind: typeof options?.sourceKind === "string" ? options.sourceKind : "file",
    }),
    cancelLocalFileUpload: (requestId) => ipcRenderer.invoke("chriptmas:cancel-file-upload", {
      requestId: typeof requestId === "string" ? requestId : "",
    }),
    captureCredential: (credentialKind, credentialSubject, value, commandId) => ipcRenderer.invoke("chriptmas:credential-capture", {
      credential_kind: typeof credentialKind === "string" ? credentialKind : "",
      credential_subject: typeof credentialSubject === "string" ? credentialSubject : "",
      value: typeof value === "string" ? value : "",
      command_id: typeof commandId === "string" ? commandId : "",
    }),
    // 自动更新状态占位（spec 3.15：placeholder，不默认启用真实 auto-update）
    getAutoUpdateStatus: () => ipcRenderer.invoke("chriptmas:auto-update-status"),
    // 主窗口恢复入口；pet renderer使用独立的pet-preload.cjs。
    openMainWindow: () => ipcRenderer.invoke("chriptmas:open-main-window"),
    // 主窗口进入桌面陪伴模式；该能力不暴露给 pet renderer。
    enterCompanionMode: () => ipcRenderer.invoke("chriptmas:enter-companion-mode"),
    openCompanionManual: () => ipcRenderer.invoke("chriptmas:companion-manual-open"),
    exportCompanionBackup: () => ipcRenderer.invoke("chriptmas:companion-data-backup"),
    preflightCompanionRestore: () => ipcRenderer.invoke("chriptmas:companion-data-restore-preflight"),
    restoreCompanionBackup: (grantId, expectedFingerprint) => ipcRenderer.invoke("chriptmas:companion-data-restore", {
      grant_id: typeof grantId === "string" ? grantId : "",
      expected_fingerprint: typeof expectedFingerprint === "string" ? expectedFingerprint : "",
    }),
    subscribeMainNavigation: (listener) => {
      if (typeof listener !== "function") return () => {};
      const handler = (_event, payload) => {
        if (payload?.view === "rebuild-library-overview" && payload?.filter === "pending_memory") {
          listener(Object.freeze({ view: "rebuild-library-overview", filter: "pending_memory" }));
          return;
        }
        const panel = typeof payload?.panel === "string" ? payload.panel : "";
        if (!COMPANION_PANEL_IDS.has(panel) || payload?.view !== "rebuild-companion") return;
        const intent = payload?.intent === "weekly_memory_review" ? payload.intent : "";
        listener(Object.freeze({ view: "rebuild-companion", panel, ...(intent ? { intent } : {}) }));
      };
      ipcRenderer.on("chriptmas:main-navigation", handler);
      ipcRenderer.send("chriptmas:main-navigation-ready");
      return () => ipcRenderer.removeListener("chriptmas:main-navigation", handler);
    },
    getCompanionClipboardStatus: () => ipcRenderer.invoke("chriptmas:companion-clipboard-status"),
    setCompanionClipboardEnabled: (enabled) => ipcRenderer.invoke("chriptmas:companion-clipboard-enabled", { enabled: enabled === true }),
    getCompanionMultiCharacterStatus: () => ipcRenderer.invoke("chriptmas:companion-multicharacter-status"),
    configureCompanionMultiCharacter: (payload) => ipcRenderer.invoke("chriptmas:companion-multicharacter-configure", {
      enabled: payload?.enabled === true,
      consented: payload?.consented === true,
      character_id: typeof payload?.character_id === "string" ? payload.character_id : "",
      allowed_character_ids: Array.isArray(payload?.allowed_character_ids) ? payload.allowed_character_ids.filter((item) => typeof item === "string").slice(0, 12) : [],
      expected_revision: Number.isSafeInteger(payload?.expected_revision) ? payload.expected_revision : -1,
    }),
    sendCompanionMultiCharacterAction: (instanceId, action) => ipcRenderer.invoke("chriptmas:companion-multicharacter-send", { instance_id: typeof instanceId === "string" ? instanceId : "", action: ["wave", "greeting", "cheer"].includes(action) ? action : "" }),
    getCompanionEasterEggStatus: () => ipcRenderer.invoke("chriptmas:companion-easter-egg-status"),
    setCompanionEasterEggEnabled: (enabled) => ipcRenderer.invoke("chriptmas:companion-easter-egg-enabled", { enabled: enabled === true }),
    recordCompanionLocalAction: (action) => ipcRenderer.invoke("chriptmas:companion-local-action", {
      action: action === "minigame_play" ? action : "",
    }),
    getCompanionLaunchers: () => ipcRenderer.invoke("chriptmas:companion-launchers-list"),
    addCompanionProgram: (name) => ipcRenderer.invoke("chriptmas:companion-launcher-add-program", { name: typeof name === "string" ? name : "" }),
    addCompanionBookmark: (name, url) => ipcRenderer.invoke("chriptmas:companion-launcher-add-bookmark", {
      name: typeof name === "string" ? name : "",
      url: typeof url === "string" ? url : "",
    }),
    renameCompanionLauncher: (id, name) => ipcRenderer.invoke("chriptmas:companion-launcher-rename", {
      id: typeof id === "string" ? id : "",
      name: typeof name === "string" ? name : "",
    }),
    deleteCompanionLauncher: (id) => ipcRenderer.invoke("chriptmas:companion-launcher-delete", { id: typeof id === "string" ? id : "" }),
    openCompanionLauncher: (id) => ipcRenderer.invoke("chriptmas:companion-launcher-open", { id: typeof id === "string" ? id : "" }),
    previewCompanionFileOrganizer: () => ipcRenderer.invoke("chriptmas:companion-file-organizer-preview"),
    executeCompanionFileOrganizer: (planId) => ipcRenderer.invoke("chriptmas:companion-file-organizer-execute", { plan_id: typeof planId === "string" ? planId : "" }),
    getCompanionFileOrganizerHistory: () => ipcRenderer.invoke("chriptmas:companion-file-organizer-history"),
    undoCompanionFileOrganizer: (operationId) => ipcRenderer.invoke("chriptmas:companion-file-organizer-undo", { operation_id: typeof operationId === "string" ? operationId : "" }),
    presentCompanionReply: (text) => ipcRenderer.invoke("chriptmas:companion-reply-present", {
      text: typeof text === "string" ? text : "",
    }),
    getCompanionRoutineStatus: () => ipcRenderer.invoke("chriptmas:companion-routine-status"),
    refreshCompanionRoutine: () => ipcRenderer.invoke("chriptmas:companion-routine-refresh"),
    wakeCompanionTemporarily: () => ipcRenderer.invoke("chriptmas:companion-routine-wake"),
    refreshCompanionSensors: () => ipcRenderer.invoke("chriptmas:companion-sensors-refresh"),
    refreshCompanionWeather: () => ipcRenderer.invoke("chriptmas:companion-weather-refresh"),
    openCompanionWeatherTerms: () => ipcRenderer.invoke("chriptmas:companion-weather-terms-open"),
    refreshCompanionMediaSession: () => ipcRenderer.invoke("chriptmas:companion-media-refresh"),
    getCompanionMediaProjection: () => ipcRenderer.invoke("chriptmas:companion-media-status"),
    subscribeCompanionMediaSession: (listener) => {
      if (typeof listener !== "function") return () => {};
      const handler = (_event, value) => listener(value);
      ipcRenderer.on("chriptmas:companion-media-session", handler);
      ipcRenderer.send("chriptmas:companion-media-ready");
      return () => ipcRenderer.removeListener("chriptmas:companion-media-session", handler);
    },
    getCompanionVoiceStatus: () => ipcRenderer.invoke("chriptmas:companion-voice-status"),
    configureCompanionVoice: (payload) => ipcRenderer.invoke("chriptmas:companion-voice-configure", {
      enabled: payload?.enabled === true,
      origin: typeof payload?.origin === "string" ? payload.origin : "",
      text_lang: typeof payload?.text_lang === "string" ? payload.text_lang : "",
      prompt_lang: typeof payload?.prompt_lang === "string" ? payload.prompt_lang : "",
      prompt_text: typeof payload?.prompt_text === "string" ? payload.prompt_text : "",
    }),
    selectCompanionVoiceReference: () => ipcRenderer.invoke("chriptmas:companion-voice-reference-select"),
    testCompanionVoice: () => ipcRenderer.invoke("chriptmas:companion-voice-test"),
    listCompanionVisionSources: () => ipcRenderer.invoke("chriptmas:companion-vision-sources"),
    captureCompanionVisionSource: (sessionId, itemId) => ipcRenderer.invoke("chriptmas:companion-vision-capture", {
      session_id: typeof sessionId === "string" ? sessionId : "",
      item_id: typeof itemId === "string" ? itemId : "",
    }),
    confirmCompanionVision: (captureId, bytes, question, projectId) => ipcRenderer.invoke("chriptmas:companion-vision-confirm", {
      capture_id: typeof captureId === "string" ? captureId : "",
      bytes: bytes instanceof Uint8Array && bytes.byteLength <= 2 * 1024 * 1024 ? bytes : new Uint8Array(),
      question: typeof question === "string" ? question : "",
      project_id: typeof projectId === "string" ? projectId : "default",
    }),
    cancelCompanionVision: () => ipcRenderer.invoke("chriptmas:companion-vision-cancel"),
    armCompanionMicrophone: () => {
      if (navigator.userActivation?.isActive !== true) return Promise.reject(new Error("companion_microphone_activation_required"));
      return ipcRenderer.invoke("chriptmas:companion-microphone-arm", { transient_activation: true });
    },
    armWorkbenchMicrophone: () => {
      if (navigator.userActivation?.isActive !== true) return Promise.reject(new Error("workbench_microphone_activation_required"));
      return ipcRenderer.invoke("chriptmas:workbench-microphone-arm", { transient_activation: true });
    },
    requestWorkbenchRealtimeAsrTicket: () => ipcRenderer.invoke("chriptmas:workbench-realtime-asr-ticket"),
    transcribeCompanionVoice: (requestId, mediaType, bytes) => ipcRenderer.invoke("chriptmas:companion-voice-call-transcribe", {
      request_id: typeof requestId === "string" ? requestId : "",
      media_type: typeof mediaType === "string" ? mediaType : "",
      bytes: bytes instanceof Uint8Array && bytes.byteLength <= 12 * 1024 * 1024 ? bytes : new Uint8Array(),
    }),
    presentCompanionVoiceCallReply: (requestId, text) => ipcRenderer.invoke("chriptmas:companion-voice-call-present", {
      request_id: typeof requestId === "string" ? requestId : "",
      text: typeof text === "string" ? text : "",
    }),
    cancelCompanionVoiceCall: () => ipcRenderer.invoke("chriptmas:companion-voice-call-cancel"),
  }),
);

function sanitizeRootMigrationTarget(target) {
  return Object.freeze({
    vaultRoot: typeof target?.vaultRoot === "string" ? target.vaultRoot.slice(0, 1024) : "",
    modelRoot: typeof target?.modelRoot === "string" ? target.modelRoot.slice(0, 1024) : "",
    mediaRoot: typeof target?.mediaRoot === "string" ? target.mediaRoot.slice(0, 1024) : "",
  });
}

const COMPANION_PANEL_IDS = new Set([
  "chat", "character", "master_profile", "schedule", "focus", "inventory",
  "status", "launchers", "privacy", "voice_vision", "help_data",
]);
