const CHANNELS = Object.freeze([
  "chriptmas:enter-companion-mode",
  "chriptmas:open-main-window",
  "chriptmas:pet-hide",
  "chriptmas:pet-context-menu",
  "chriptmas:pet-mouse-passthrough",
  "chriptmas:pet-window-move",
  "chriptmas:pet-window-move-end",
]);

class CompanionPetWindowIpcController {
  constructor({
    ipcMain,
    requireMainRenderer,
    requireKnownRenderer,
    petWindowProvider,
    overlayWindowProvider,
    showPetWindow,
    showMainWindow,
    hidePetSurfaces,
    showPetContextMenu,
    positionCompanionOverlay,
    clampPetWindowPosition,
    motionController,
  }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("companion_pet_window_ipc_invalid");
    }
    const functions = [
      requireMainRenderer, requireKnownRenderer, petWindowProvider, overlayWindowProvider,
      showPetWindow, showMainWindow, hidePetSurfaces, showPetContextMenu,
      positionCompanionOverlay, clampPetWindowPosition,
    ];
    if (functions.some((value) => typeof value !== "function")
      || !motionController || typeof motionController.beginDrag !== "function"
      || typeof motionController.settle !== "function") {
      throw new TypeError("companion_pet_window_boundary_invalid");
    }
    Object.assign(this, {
      ipcMain,
      requireMainRenderer,
      requireKnownRenderer,
      petWindowProvider,
      overlayWindowProvider,
      showPetWindow,
      showMainWindow,
      hidePetSurfaces,
      showPetContextMenu,
      positionCompanionOverlay,
      clampPetWindowPosition,
      motionController,
    });
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = [
      (event) => this.enterCompanionMode(event),
      (event) => this.openMainWindow(event),
      (event) => this.hidePet(event),
      (event) => this.openContextMenu(event),
      (event, enabled) => this.setMousePassthrough(event, enabled),
      (event, delta) => this.move(event, delta),
      (event) => this.finishMove(event),
    ];
    const registered = [];
    try {
      CHANNELS.forEach((channel, index) => {
        this.ipcMain.handle(channel, handlers[index]);
        registered.push(channel);
      });
    } catch (error) {
      for (const channel of registered) this.ipcMain.removeHandler(channel);
      throw error;
    }
    this.installed = true;
    return true;
  }

  enterCompanionMode(event) {
    this.requireMainRenderer(event);
    this.showPetWindow();
    return { status: "shown" };
  }

  openMainWindow(event) {
    this.requireKnownRenderer(event);
    this.showMainWindow();
    return { status: "shown" };
  }

  hidePet(event) {
    this.requireKnownRenderer(event);
    const petWindow = this.petWindowProvider();
    if (!petWindow || petWindow.isDestroyed() || !petWindow.isVisible()) {
      return { status: "already-hidden" };
    }
    return this.hidePetSurfaces();
  }

  openContextMenu(event) {
    this.requirePetRenderer(event, "Pet context menu requires the pet renderer");
    return this.showPetContextMenu();
  }

  setMousePassthrough(event, enabled) {
    const petWindow = this.requirePetRenderer(event, "Pet mouse passthrough requires the pet renderer");
    petWindow.setIgnoreMouseEvents(enabled === true, { forward: true });
    return { status: enabled === true ? "passthrough" : "interactive" };
  }

  move(event, delta) {
    const petWindow = this.requirePetRenderer(event, "Pet window movement requires the pet renderer");
    const dx = Number(delta?.dx);
    const dy = Number(delta?.dy);
    if (!Number.isFinite(dx) || !Number.isFinite(dy) || Math.abs(dx) > 80 || Math.abs(dy) > 80) {
      throw new Error("Pet window movement requires bounded deltas");
    }
    const [x, y] = petWindow.getPosition();
    this.motionController.beginDrag();
    const bounds = petWindow.getBounds();
    const next = this.clampPetWindowPosition(x + dx, y + dy, bounds.width, bounds.height);
    petWindow.setPosition(next.x, next.y, false);
    if (this.overlayWindowProvider()?.isVisible()) this.positionCompanionOverlay();
    return { status: "moved" };
  }

  finishMove(event) {
    const petWindow = this.requirePetRenderer(event, "Pet window movement requires the pet renderer");
    const target = this.motionController.settle(petWindow);
    return { status: target.mode === "unavailable" ? "unavailable" : "settling", mode: target.mode };
  }

  requirePetRenderer(event, message) {
    const petWindow = this.petWindowProvider();
    if (!petWindow || petWindow.isDestroyed() || event?.sender?.id !== petWindow.webContents.id) {
      throw new Error(message);
    }
    return petWindow;
  }

  dispose() {
    if (!this.installed) return false;
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, CompanionPetWindowIpcController };
