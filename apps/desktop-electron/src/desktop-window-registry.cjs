"use strict";

const WINDOW_KINDS = Object.freeze(["main", "pet", "overlay", "startup"]);
const WINDOW_KIND_SET = new Set(WINDOW_KINDS);

function assertKind(kind) {
  if (!WINDOW_KIND_SET.has(kind)) throw new Error("desktop_window_kind_invalid");
}

class DesktopWindowRegistry {
  #windows = new Map(WINDOW_KINDS.map((kind) => [kind, null]));

  get main() { return this.#windows.get("main"); }
  get pet() { return this.#windows.get("pet"); }
  get overlay() { return this.#windows.get("overlay"); }
  get startup() { return this.#windows.get("startup"); }

  register(kind, window, { onClosed } = {}) {
    assertKind(kind);
    if (!window || typeof window.once !== "function" || typeof window.isDestroyed !== "function") throw new Error("desktop_window_invalid");
    if (onClosed !== undefined && typeof onClosed !== "function") throw new Error("desktop_window_closed_callback_invalid");
    const current = this.#windows.get(kind);
    if (current === window) return window;
    if (current && !current.isDestroyed()) throw new Error(`desktop_window_already_registered:${kind}`);
    this.#windows.set(kind, window);
    window.once("closed", () => {
      if (this.clear(kind, window)) onClosed?.();
    });
    return window;
  }

  isCurrent(kind, window) {
    assertKind(kind);
    return this.#windows.get(kind) === window;
  }

  clear(kind, expectedWindow) {
    assertKind(kind);
    const current = this.#windows.get(kind);
    if (expectedWindow !== undefined && current !== expectedWindow) return false;
    this.#windows.set(kind, null);
    return current !== null;
  }

  destroy(kind) {
    assertKind(kind);
    const current = this.#windows.get(kind);
    if (!current) return false;
    if (!current.isDestroyed()) current.destroy();
    this.clear(kind, current);
    return true;
  }
}

module.exports = { DesktopWindowRegistry };
