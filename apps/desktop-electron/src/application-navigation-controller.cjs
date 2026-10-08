const MAIN_NAVIGATION_READY_EVENT = "chriptmas:main-navigation-ready";
const MAIN_NAVIGATION_EVENT = "chriptmas:main-navigation";
const WEEKLY_MEMORY_REVIEW_INTENT = "weekly_memory_review";

class ApplicationNavigationController {
  constructor({ ipcMain, mainWindowProvider, showMainWindow, validatePanelPayload }) {
    if (!ipcMain || typeof ipcMain.on !== "function" || typeof ipcMain.removeListener !== "function") {
      throw new TypeError("application_navigation_ipc_invalid");
    }
    if (typeof mainWindowProvider !== "function" || typeof showMainWindow !== "function"
      || typeof validatePanelPayload !== "function") {
      throw new TypeError("application_navigation_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.mainWindowProvider = mainWindowProvider;
    this.showMainWindow = showMainWindow;
    this.validatePanelPayload = validatePanelPayload;
    this.pending = null;
    this.installed = false;
    this.readyListener = (event) => this.rendererReady(event);
  }

  install() {
    if (this.installed) return false;
    this.ipcMain.on(MAIN_NAVIGATION_READY_EVENT, this.readyListener);
    this.installed = true;
    return true;
  }

  openCompanion(panel, intent = "") {
    const safe = this.validatePanelPayload({ panel });
    const safeIntent = intent === WEEKLY_MEMORY_REVIEW_INTENT ? intent : "";
    this.pending = Object.freeze({
      view: "rebuild-companion",
      panel: safe.panel,
      ...(safeIntent ? { intent: safeIntent } : {}),
    });
    this.showMainWindow();
    this.deliver();
    return Object.freeze({ status: "shown", panel: safe.panel, intent: safeIntent });
  }

  openPendingMemoryReview() {
    this.pending = Object.freeze({
      view: "rebuild-library-overview",
      filter: "pending_memory",
    });
    this.showMainWindow();
    this.deliver();
    return Object.freeze({ status: "shown", view: "rebuild-library-overview", filter: "pending_memory" });
  }

  rendererReady(event) {
    const mainWindow = this.mainWindowProvider();
    const webContents = mainWindow?.webContents;
    if (!mainWindow || mainWindow.isDestroyed() || !webContents
      || event?.sender !== webContents || event?.senderFrame !== webContents.mainFrame) return false;
    this.deliver();
    return true;
  }

  deliver() {
    const mainWindow = this.mainWindowProvider();
    const webContents = mainWindow?.webContents;
    if (!this.pending || !mainWindow || mainWindow.isDestroyed() || !webContents
      || webContents.isDestroyed?.() || webContents.isLoadingMainFrame()) return false;
    const navigation = this.pending;
    webContents.send(MAIN_NAVIGATION_EVENT, navigation);
    this.pending = null;
    return true;
  }

  dispose() {
    if (!this.installed) return false;
    this.ipcMain.removeListener(MAIN_NAVIGATION_READY_EVENT, this.readyListener);
    this.installed = false;
    return true;
  }
}

module.exports = {
  ApplicationNavigationController,
  MAIN_NAVIGATION_EVENT,
  MAIN_NAVIGATION_READY_EVENT,
};
