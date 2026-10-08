class DesktopSurfaceCoordinator {
  constructor({
    mainWindowProvider,
    petWindowProvider,
    overlayWindowProvider,
    createMainWindow,
    createPetWindow,
    createOverlayWindow,
    desktopReadyProvider,
    screen,
    ensureMainWindowBounds,
    resolveOverlayBounds,
    savePetWindowPosition,
    cancelActiveGesture,
    pollCompanionState,
  }) {
    this.mainWindowProvider = mainWindowProvider;
    this.petWindowProvider = petWindowProvider;
    this.overlayWindowProvider = overlayWindowProvider;
    this.createMainWindow = createMainWindow;
    this.createPetWindow = createPetWindow;
    this.createOverlayWindow = createOverlayWindow;
    this.desktopReadyProvider = desktopReadyProvider;
    this.screen = screen;
    this.ensureMainWindowBounds = ensureMainWindowBounds;
    this.resolveOverlayBounds = resolveOverlayBounds;
    this.savePetWindowPosition = savePetWindowPosition;
    this.cancelActiveGesture = cancelActiveGesture;
    this.pollCompanionState = pollCompanionState;
    this.desiredSurface = "main";
    this.overlayRequested = false;
    this.overlayFocus = true;
  }

  wants(surface) {
    return this.desiredSurface === surface;
  }

  showMainWindow() {
    this.desiredSurface = "main";
    if (!this.desktopReadyProvider()) return;
    let window = this.mainWindowProvider();
    if (!this.isUsable(window)) window = this.createMainWindow();
    const petWindow = this.petWindowProvider();
    if (this.isUsable(petWindow) && petWindow.isVisible()) petWindow.hide();
    this.hideOverlay();
    if (window.webContents.isLoadingMainFrame()) return;
    this.presentMainWindow(window);
  }

  onMainReady(window) {
    if (!this.wants("main") || window !== this.mainWindowProvider() || !this.isUsable(window)) return false;
    this.presentMainWindow(window);
    return true;
  }

  presentMainWindow(window = this.mainWindowProvider()) {
    if (!this.isUsable(window)) return false;
    const bounds = window.getBounds();
    this.ensureMainWindowBounds(window, this.screen.getDisplayMatching(bounds).workArea);
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
    return true;
  }

  showPetWindow() {
    this.desiredSurface = "pet";
    if (!this.desktopReadyProvider()) return;
    const mainWindow = this.mainWindowProvider();
    if (this.isUsable(mainWindow) && mainWindow.isVisible()) mainWindow.hide();
    let petWindow = this.petWindowProvider();
    if (!this.isUsable(petWindow)) petWindow = this.createPetWindow();
    if (petWindow.webContents.isLoadingMainFrame()) return;
    this.presentPetWindow(petWindow, { focus: true, poll: true });
  }

  onPetReady(window) {
    if (!this.wants("pet") || window !== this.petWindowProvider() || !this.isUsable(window)) return false;
    window.show();
    return true;
  }

  presentPetWindow(window, { focus, poll }) {
    window.setIgnoreMouseEvents(true, { forward: true });
    window.show();
    if (focus) window.focus();
    if (poll) void this.pollCompanionState();
  }

  hidePetSurfaces() {
    this.desiredSurface = "hidden";
    this.savePetWindowPosition();
    const petWindow = this.petWindowProvider();
    if (this.isUsable(petWindow)) petWindow.hide();
    this.cancelActiveGesture();
    this.hideOverlay();
    return { status: "hidden" };
  }

  showOverlay(options = {}) {
    this.overlayRequested = true;
    this.overlayFocus = options.focus !== false;
    const petWindow = this.petWindowProvider();
    if (!this.desktopReadyProvider() || !this.isUsable(petWindow) || !petWindow.isVisible()) return;
    let overlayWindow = this.overlayWindowProvider();
    if (!this.isUsable(overlayWindow)) overlayWindow = this.createOverlayWindow();
    this.positionOverlay();
    if (overlayWindow.webContents.isLoadingMainFrame()) return;
    this.presentOverlay(overlayWindow);
  }

  onOverlayReady(window) {
    if (!this.overlayRequested || window !== this.overlayWindowProvider() || !this.isUsable(window)) return false;
    const petWindow = this.petWindowProvider();
    if (!this.isUsable(petWindow) || !petWindow.isVisible()) return false;
    this.positionOverlay();
    this.presentOverlay(window);
    return true;
  }

  presentOverlay(window) {
    if (!this.overlayFocus) {
      window.showInactive();
      return;
    }
    window.show();
    window.focus();
  }

  hideOverlay() {
    this.overlayRequested = false;
    this.overlayFocus = true;
    const overlayWindow = this.overlayWindowProvider();
    if (this.isUsable(overlayWindow)) overlayWindow.hide();
  }

  onOverlayClosed() {
    this.overlayRequested = false;
    this.overlayFocus = true;
  }

  positionOverlay() {
    const petWindow = this.petWindowProvider();
    const overlayWindow = this.overlayWindowProvider();
    if (!this.isUsable(petWindow) || !this.isUsable(overlayWindow)) return false;
    const petBounds = petWindow.getBounds();
    const workArea = this.screen.getDisplayMatching(petBounds).workArea;
    const bounds = this.resolveOverlayBounds({ petBounds, workArea, size: overlayWindow.getBounds() });
    overlayWindow.setBounds({ x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height }, false);
    return true;
  }

  isUsable(window) {
    return window !== null && window !== undefined && !window.isDestroyed();
  }
}

module.exports = { DesktopSurfaceCoordinator };
