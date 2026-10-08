class DesktopTrayController {
  constructor({ Tray, Menu, nativeImage, iconPath, actionRegistry, onClick, tooltip = "Chriptmas OS" }) {
    if (typeof Tray !== "function" || !Menu || typeof Menu.buildFromTemplate !== "function"
      || !nativeImage || typeof nativeImage.createFromPath !== "function" || typeof nativeImage.createEmpty !== "function"
      || typeof iconPath !== "string" || !iconPath || !actionRegistry || typeof actionRegistry.menuTemplate !== "function"
      || typeof onClick !== "function" || typeof tooltip !== "string" || !tooltip) {
      throw new TypeError("desktop_tray_options_invalid");
    }
    this.Tray = Tray;
    this.Menu = Menu;
    this.nativeImage = nativeImage;
    this.iconPath = iconPath;
    this.actionRegistry = actionRegistry;
    this.onClick = onClick;
    this.tooltip = tooltip;
    this.tray = null;
    this.clickListener = () => this.onClick();
  }

  create() {
    if (this.tray !== null) return this.tray;
    const tray = new this.Tray(this.createIcon());
    try {
      tray.setToolTip(this.tooltip);
      const contextMenu = this.Menu.buildFromTemplate(this.actionRegistry.menuTemplate({ source: "tray" }));
      tray.setContextMenu(contextMenu);
      tray.on("click", this.clickListener);
    } catch (error) {
      tray.destroy();
      throw error;
    }
    this.tray = tray;
    return tray;
  }

  createIcon() {
    try {
      const icon = this.nativeImage.createFromPath(this.iconPath).resize({ width: 32, height: 32, quality: "best" });
      return icon.isEmpty() ? this.nativeImage.createEmpty() : icon;
    } catch {
      return this.nativeImage.createEmpty();
    }
  }

  dispose() {
    if (this.tray === null) return false;
    const tray = this.tray;
    this.tray = null;
    tray.removeListener("click", this.clickListener);
    tray.destroy();
    return true;
  }
}

module.exports = { DesktopTrayController };
