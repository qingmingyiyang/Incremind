"use strict";

const path = require("node:path");
const { installPetRendererSessionPolicy, installRendererNavigationPolicy } = require("./renderer-security.cjs");
const { DEFAULT_OVERLAY_SIZE } = require("./companion/overlay-position.cjs");
const {
  MAIN_WINDOW_DEFAULT,
  MAIN_WINDOW_MINIMUM,
  MAIN_WINDOW_TITLE_BAR,
  PRODUCT_NAME,
  configureMainWindowPresentation,
} = require("./desktop-window-presentation.cjs");

const PET_SESSION_PARTITION = "chriptmas-pet";
const OVERLAY_SESSION_PARTITION = "chriptmas-companion-overlay";

function validateEntry(entry) {
  if (!entry || !["url", "file"].includes(entry.type) || typeof entry.value !== "string" || !entry.value) throw new Error("desktop_window_entry_invalid");
  if (entry.hash !== undefined && (typeof entry.hash !== "string" || entry.hash.length > 512)) throw new Error("desktop_window_entry_invalid");
  return entry;
}

function loadEntry(window, entry) {
  if (entry.type === "url") window.loadURL(entry.value);
  else window.loadFile(entry.value, entry.hash ? { hash: entry.hash } : undefined);
}

function preparation(window, load) {
  let loaded = false;
  return Object.freeze({
    window,
    load() {
      if (loaded) throw new Error("desktop_window_load_already_started");
      loaded = true;
      load();
    },
  });
}

class DesktopWindowFactory {
  #BrowserWindow;
  #baseDir;
  #developmentOriginProvider;
  #installPetSessionPolicy;
  #installNavigationPolicy;
  #configureMainWindow;

  constructor({
    BrowserWindow,
    baseDir,
    developmentOriginProvider = () => process.env.CHRIPTMAS_REPLAY_FRONTEND_URL || "",
    installPetSessionPolicy = installPetRendererSessionPolicy,
    installNavigationPolicy = installRendererNavigationPolicy,
    configureMainWindow = configureMainWindowPresentation,
  } = {}) {
    if (typeof BrowserWindow !== "function" || typeof baseDir !== "string" || !path.isAbsolute(baseDir) || typeof developmentOriginProvider !== "function" || typeof installPetSessionPolicy !== "function" || typeof installNavigationPolicy !== "function" || typeof configureMainWindow !== "function") {
      throw new Error("desktop_window_factory_options_invalid");
    }
    this.#BrowserWindow = BrowserWindow;
    this.#baseDir = baseDir;
    this.#developmentOriginProvider = developmentOriginProvider;
    this.#installPetSessionPolicy = installPetSessionPolicy;
    this.#installNavigationPolicy = installNavigationPolicy;
    this.#configureMainWindow = configureMainWindow;
  }

  prepareMain({ entry, backendOrigin } = {}) {
    validateEntry(entry);
    if (typeof backendOrigin !== "string") throw new Error("desktop_window_backend_origin_invalid");
    const window = new this.#BrowserWindow({
      width: MAIN_WINDOW_DEFAULT.width,
      height: MAIN_WINDOW_DEFAULT.height,
      minWidth: MAIN_WINDOW_MINIMUM.width,
      minHeight: MAIN_WINDOW_MINIMUM.height,
      title: PRODUCT_NAME,
      autoHideMenuBar: true,
      titleBarStyle: "hidden",
      titleBarOverlay: MAIN_WINDOW_TITLE_BAR.light,
      backgroundColor: "#F3F5F9",
      icon: path.join(this.#baseDir, "tray-icon.png"),
      show: false,
      webPreferences: {
        preload: path.join(this.#baseDir, "preload.cjs"),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
      },
    });
    this.#configureMainWindow(window);
    const rendererOrigin = entry.type === "url" ? new URL(entry.value).origin : "file://";
    this.#installNavigationPolicy(window.webContents, { rendererOrigin, backendOrigin });
    return preparation(window, () => loadEntry(window, entry));
  }

  preparePet({ entry, savedPosition = null } = {}) {
    validateEntry(entry);
    const window = new this.#BrowserWindow({
      width: 180,
      height: 220,
      frame: false,
      transparent: true,
      resizable: false,
      maximizable: false,
      minimizable: false,
      skipTaskbar: true,
      alwaysOnTop: true,
      hasShadow: false,
      focusable: true,
      show: false,
      ...(savedPosition || {}),
      webPreferences: {
        preload: path.join(this.#baseDir, "pet-preload.cjs"),
        partition: PET_SESSION_PARTITION,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
      },
    });
    const rendererOrigin = entry.type === "url" ? new URL(entry.value).origin : "file://";
    this.#installPetSessionPolicy(window.webContents.session, { developmentOrigin: this.#developmentOriginProvider() });
    this.#installNavigationPolicy(window.webContents, { rendererOrigin });
    window.setBackgroundColor("#00000000");
    window.setIgnoreMouseEvents(true, { forward: true });
    return preparation(window, () => {
      loadEntry(window, entry);
      window.setAlwaysOnTop(true, "floating", 1);
    });
  }

  prepareOverlay() {
    const window = new this.#BrowserWindow({
      ...DEFAULT_OVERLAY_SIZE,
      maxWidth: 440,
      maxHeight: 320,
      minWidth: 240,
      minHeight: 120,
      frame: false,
      transparent: true,
      resizable: true,
      maximizable: false,
      minimizable: false,
      skipTaskbar: true,
      alwaysOnTop: true,
      hasShadow: false,
      focusable: true,
      show: false,
      webPreferences: {
        preload: path.join(this.#baseDir, "companion-overlay-preload.cjs"),
        partition: OVERLAY_SESSION_PARTITION,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
        devTools: false,
      },
    });
    this.#installPetSessionPolicy(window.webContents.session, { developmentOrigin: "" });
    this.#installNavigationPolicy(window.webContents, { rendererOrigin: "file://" });
    window.setBackgroundColor("#00000000");
    return preparation(window, () => {
      window.setAlwaysOnTop(true, "floating", 1);
      window.loadFile(path.join(this.#baseDir, "companion", "overlay.html"));
    });
  }
}

module.exports = { DesktopWindowFactory, OVERLAY_SESSION_PARTITION, PET_SESSION_PARTITION };
