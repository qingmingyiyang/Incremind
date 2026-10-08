const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld(
  "electronAPI",
  Object.freeze({
    openMainWindow: () => ipcRenderer.invoke("chriptmas:open-main-window"),
    openPetContextMenu: () => ipcRenderer.invoke("chriptmas:pet-context-menu"),
    setPetMousePassthrough: (enabled) => ipcRenderer.invoke("chriptmas:pet-mouse-passthrough", enabled === true),
    movePetWindow: (delta) => ipcRenderer.invoke("chriptmas:pet-window-move", delta),
    finishPetWindowMove: () => ipcRenderer.invoke("chriptmas:pet-window-move-end"),
    beginPetGesture: (payload) => ipcRenderer.send("chriptmas:pet-gesture-begin", payload),
    updatePetGesture: (payload) => ipcRenderer.send("chriptmas:pet-gesture-move", payload),
    endPetGesture: (payload) => ipcRenderer.send("chriptmas:pet-gesture-end", payload),
    commitPetClick: (payload) => ipcRenderer.send("chriptmas:pet-gesture-click", payload),
    subscribeCompanionState: (listener) => {
      if (typeof listener !== "function") return () => {};
      const handler = (_event, projection) => listener(projection);
      ipcRenderer.on("chriptmas:companion-state", handler);
      ipcRenderer.send("chriptmas:companion-state-ready");
      return () => ipcRenderer.removeListener("chriptmas:companion-state", handler);
    },
    subscribeCompanionAppearance: (listener) => {
      if (typeof listener !== "function") return () => {};
      const handler = (_event, value) => {
        if (!value || typeof value !== "object" || Array.isArray(value)) return;
        if (!["default", "red-scarf", "gold-star"].includes(value.outfit_id) || !["default", "night"].includes(value.background_id)) return;
        if (!["new", "friend", "partner", "confidant", "bonded"].includes(value.growth_stage) || !["default", "warm", "smile", "bright", "radiant"].includes(value.idle_variant)) return;
        if (!Number.isSafeInteger(value.revision) || value.revision < 1) return;
        listener(Object.freeze({ outfit_id: value.outfit_id, background_id: value.background_id, growth_stage: value.growth_stage, idle_variant: value.idle_variant, revision: value.revision }));
      };
      ipcRenderer.on("chriptmas:companion-appearance", handler);
      ipcRenderer.send("chriptmas:companion-appearance-ready");
      return () => ipcRenderer.removeListener("chriptmas:companion-appearance", handler);
    },
    subscribeCompanionWeather: (listener) => {
      if (typeof listener !== "function") return () => {};
      const handler = (_event, value) => {
        if (!value || typeof value !== "object" || Array.isArray(value)) return;
        if (!["clear", "cloudy", "rain", "snow", "extreme", "unknown"].includes(value.condition)) return;
        if (value.is_day !== null && typeof value.is_day !== "boolean") return;
        if (typeof value.stale !== "boolean" || !Number.isSafeInteger(value.revision) || value.revision < 0) return;
        listener(Object.freeze({ condition: value.condition, is_day: value.is_day, stale: value.stale, revision: value.revision }));
      };
      ipcRenderer.on("chriptmas:companion-weather", handler);
      ipcRenderer.send("chriptmas:companion-weather-ready");
      return () => ipcRenderer.removeListener("chriptmas:companion-weather", handler);
    },
    subscribeCompanionMediaSession: (listener) => {
      if (typeof listener !== "function") return () => {};
      const handler = (_event, value) => {
        if (!value || typeof value !== "object" || Array.isArray(value)) return;
        if (!["disabled", "unavailable", "error", "empty", "playing", "paused"].includes(value.status)) return;
        const title = typeof value.title === "string" && value.title.length <= 160 ? value.title : "";
        const artist = typeof value.artist === "string" && value.artist.length <= 160 ? value.artist : "";
        listener(Object.freeze({ status: value.status, title, artist }));
      };
      ipcRenderer.on("chriptmas:companion-media-session", handler);
      ipcRenderer.send("chriptmas:companion-media-ready");
      return () => ipcRenderer.removeListener("chriptmas:companion-media-session", handler);
    },
    subscribeCompanionVoice: (listener) => {
      if (typeof listener !== "function") return () => {};
      const audioHandler = (_event, payload) => {
        const audio = payload?.audio;
        if (typeof payload?.request_id !== "string" || payload.request_id.length > 80 || !(audio instanceof Uint8Array) || audio.byteLength < 12 || audio.byteLength > 8 * 1024 * 1024) return;
        listener(Object.freeze({ request_id: payload.request_id, audio: new Uint8Array(audio) }));
      };
      const cancelHandler = () => listener(Object.freeze({ cancelled: true }));
      ipcRenderer.on("chriptmas:companion-voice-audio", audioHandler);
      ipcRenderer.on("chriptmas:companion-voice-cancel", cancelHandler);
      return () => {
        ipcRenderer.removeListener("chriptmas:companion-voice-audio", audioHandler);
        ipcRenderer.removeListener("chriptmas:companion-voice-cancel", cancelHandler);
      };
    },
    reportCompanionVoicePlayback: (requestId, status) => {
      if (typeof requestId !== "string" || requestId.length > 80 || !["playing", "ended", "failed", "cancelled"].includes(status)) return;
      ipcRenderer.send("chriptmas:companion-voice-playback-status", { request_id: requestId, status });
    },
  }),
);
