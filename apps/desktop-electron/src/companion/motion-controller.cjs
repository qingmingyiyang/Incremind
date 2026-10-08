class CompanionMotionController {
  constructor({ screen, publish, clear, onPosition = () => {}, onSettled = () => {} }) {
    if (!screen || typeof publish !== "function" || typeof clear !== "function") throw new TypeError("motion controller dependencies are invalid");
    this.screen = screen;
    this.publish = publish;
    this.clear = clear;
    this.onPosition = onPosition;
    this.onSettled = onSettled;
    this.dragging = false;
  }

  beginDrag() {
    if (this.dragging) return false;
    this.cancel();
    this.dragging = true;
    this.publish({ state: "attention", mood: "curious", animation_key: "drag" });
    return true;
  }

  settle(window) {
    this.dragging = false;
    this.cancel({ clearProjection: false });
    if (!usableWindow(window)) return Object.freeze({ mode: "unavailable" });
    const bounds = window.getBounds();
    const display = this.screen.getDisplayMatching(bounds);
    const target = resolvePetRestingTarget({ bounds, workArea: display.workArea });
    window.setPosition(target.x, target.y, false);
    this.onPosition();
    this.clear();
    this.onSettled(target);
    return target;
  }

  cancel({ clearProjection = true } = {}) {
    this.dragging = false;
    if (clearProjection) this.clear();
  }

  dispose() { this.cancel(); }
}

function resolvePetRestingTarget({ bounds, workArea }) {
  for (const value of [bounds, workArea]) {
    if (!value || ![value.x, value.y, value.width, value.height].every(Number.isFinite) || value.width < 1 || value.height < 1) throw new TypeError("pet motion bounds are invalid");
  }
  const maxX = workArea.x + Math.max(0, workArea.width - bounds.width);
  const maxY = workArea.y + Math.max(0, workArea.height - bounds.height);
  const x = clamp(Math.round(bounds.x), workArea.x, maxX);
  const y = clamp(Math.round(bounds.y), workArea.y, maxY);
  return Object.freeze({ mode: "free", x, y });
}

function usableWindow(window) { return Boolean(window && typeof window.isDestroyed === "function" && !window.isDestroyed() && typeof window.getBounds === "function" && typeof window.setPosition === "function"); }
function clamp(value, minimum, maximum) { return Math.min(Math.max(value, minimum), maximum); }

module.exports = { CompanionMotionController, resolvePetRestingTarget };
