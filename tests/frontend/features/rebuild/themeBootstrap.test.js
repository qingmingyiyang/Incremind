import { describe, expect, it, vi } from "vitest";

import {
  DEFAULT_PERFORMANCE,
  DEFAULT_THEME,
  applyPerformance,
  applyTheme,
  bootstrapThemeAndPerformance,
  readStoredPerformance,
  readStoredTheme,
} from "@src/features/rebuild/themeBootstrap";

function makeStorage(initial = {}) {
  const store = { ...initial };
  return {
    getItem: vi.fn((key) => (key in store ? store[key] : null)),
    setItem: vi.fn((key, value) => {
      store[key] = String(value);
    }),
    removeItem: vi.fn((key) => {
      delete store[key];
    }),
    clear: vi.fn(() => {
      for (const key of Object.keys(store)) delete store[key];
    }),
  };
}

function makeRoot(initial = {}) {
  return {
    dataset: { ...initial },
  };
}

describe("themeBootstrap.readStoredTheme", () => {
  it("returns the stored value when valid", () => {
    const storage = makeStorage({ "chriptmas-os-theme": "dark" });
    expect(readStoredTheme(storage)).toBe("dark");
  });

  it("returns default for invalid values", () => {
    const storage = makeStorage({ "chriptmas-os-theme": "neon" });
    expect(readStoredTheme(storage)).toBe(DEFAULT_THEME);
  });

  it("returns default when missing", () => {
    const storage = makeStorage();
    expect(readStoredTheme(storage)).toBe(DEFAULT_THEME);
  });
});

describe("themeBootstrap.readStoredPerformance", () => {
  it("returns the stored value when valid", () => {
    const storage = makeStorage({ "chriptmas-os-performance": "low" });
    expect(readStoredPerformance(storage)).toBe("low");
  });

  it("returns default for invalid values", () => {
    const storage = makeStorage({ "chriptmas-os-performance": "ultra" });
    expect(readStoredPerformance(storage)).toBe(DEFAULT_PERFORMANCE);
  });

  it("returns default when missing", () => {
    const storage = makeStorage();
    expect(readStoredPerformance(storage)).toBe(DEFAULT_PERFORMANCE);
  });
});

describe("themeBootstrap.applyTheme", () => {
  it("writes data-theme and persists to localStorage", () => {
    const storage = makeStorage();
    const root = makeRoot();
    const bridge = { setWindowAppearance: vi.fn() };
    const scheduled = [];

    const result = applyTheme("dark", {
      storage,
      root,
      bridge,
      scheduleAppearance: (callback) => scheduled.push(callback),
    });

    expect(result).toBe("dark");
    expect(root.dataset.theme).toBe("dark");
    expect(storage.setItem).toHaveBeenCalledWith("chriptmas-os-theme", "dark");
    expect(bridge.setWindowAppearance).not.toHaveBeenCalled();
    scheduled[0]();
    expect(bridge.setWindowAppearance).toHaveBeenCalledWith("dark");
  });

  it("falls back to default for invalid input", () => {
    const storage = makeStorage();
    const root = makeRoot();

    const result = applyTheme("neon", { storage, root });

    expect(result).toBe(DEFAULT_THEME);
    expect(root.dataset.theme).toBe("light");
  });

  it("resolves the stored system preference to the dark renderer attribute", () => {
    const storage = makeStorage();
    const root = makeRoot();
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = vi.fn(() => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));

    expect(applyTheme("system", { storage, root })).toBe("system");
    expect(root.dataset.theme).toBe("dark");

    window.matchMedia = originalMatchMedia;
  });

  it("lets the renderer paint before changing native caption symbols", () => {
    const storage = makeStorage();
    const root = makeRoot();
    const bridge = { setWindowAppearance: vi.fn() };
    const frames = [];
    const originalRequestAnimationFrame = globalThis.requestAnimationFrame;
    globalThis.requestAnimationFrame = vi.fn((callback) => frames.push(callback));

    applyTheme("dark", { storage, root, bridge });
    expect(root.dataset.theme).toBe("dark");
    expect(frames).toHaveLength(1);
    frames.shift()();
    expect(bridge.setWindowAppearance).not.toHaveBeenCalled();
    expect(frames).toHaveLength(1);
    frames.shift()();
    expect(bridge.setWindowAppearance).toHaveBeenCalledWith("dark");

    globalThis.requestAnimationFrame = originalRequestAnimationFrame;
  });
});

describe("themeBootstrap.applyPerformance", () => {
  it("writes data-performance and persists to localStorage", () => {
    const storage = makeStorage();
    const root = makeRoot();

    const result = applyPerformance("low", { storage, root });

    expect(result).toBe("low");
    expect(root.dataset.performance).toBe("low");
    expect(storage.setItem).toHaveBeenCalledWith("chriptmas-os-performance", "low");
  });

  it("falls back to default for invalid input", () => {
    const storage = makeStorage();
    const root = makeRoot();

    const result = applyPerformance("ultra", { storage, root });

    expect(result).toBe(DEFAULT_PERFORMANCE);
    expect(root.dataset.performance).toBe(DEFAULT_PERFORMANCE);
  });
});

describe("themeBootstrap.bootstrapThemeAndPerformance", () => {
  it.each([true, false])("follows the system by default when dark preference is %s", (dark) => {
    const storage = makeStorage();
    const root = makeRoot();
    const originalMatchMedia = window.matchMedia;
    let onChange;
    const media = {
      matches: dark,
      addEventListener: vi.fn((_event, handler) => { onChange = handler; }),
      removeEventListener: vi.fn(),
    };
    window.matchMedia = vi.fn(() => media);
    try {
      expect(bootstrapThemeAndPerformance({ storage, root }).theme).toBe("system");
      expect(root.dataset.theme).toBe(dark ? "dark" : "light");
      expect(storage.setItem).not.toHaveBeenCalled();
      media.matches = !dark;
      onChange();
      expect(root.dataset.theme).toBe(dark ? "light" : "dark");
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });

  it("retains explicit light preference on a dark system", () => {
    const storage = makeStorage({ "chriptmas-os-theme": "light" });
    const root = makeRoot();
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = vi.fn(() => ({ matches: true,
      addEventListener: vi.fn(), removeEventListener: vi.fn() }));
    try {
      expect(bootstrapThemeAndPerformance({ storage, root }).theme).toBe("light");
      expect(root.dataset.theme).toBe("light");
      expect(storage.setItem).not.toHaveBeenCalled();
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });

  it("applies stored theme and performance to root without writing localStorage", () => {
    const storage = makeStorage({
      "chriptmas-os-theme": "dark",
      "chriptmas-os-performance": "low",
    });
    const root = makeRoot();
    const bridge = { setWindowAppearance: vi.fn() };

    const result = bootstrapThemeAndPerformance({ storage, root, bridge });

    expect(result).toEqual({ theme: "dark", performance: "low" });
    expect(root.dataset.theme).toBe("dark");
    expect(root.dataset.performance).toBe("low");
    // bootstrap 只读 localStorage，不写
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(bridge.setWindowAppearance).toHaveBeenCalledWith("dark");
  });

  it("applies defaults when storage is empty", () => {
    const storage = makeStorage();
    const root = makeRoot();

    const result = bootstrapThemeAndPerformance({ storage, root });

    expect(result).toEqual({ theme: DEFAULT_THEME, performance: DEFAULT_PERFORMANCE });
    expect(root.dataset.theme).toBe("light");
    expect(root.dataset.performance).toBe(DEFAULT_PERFORMANCE);
  });

  it("applies defaults for invalid stored values", () => {
    const storage = makeStorage({
      "chriptmas-os-theme": "neon",
      "chriptmas-os-performance": "ultra",
    });
    const root = makeRoot();

    const result = bootstrapThemeAndPerformance({ storage, root });

    expect(result).toEqual({ theme: DEFAULT_THEME, performance: DEFAULT_PERFORMANCE });
    expect(root.dataset.theme).toBe("light");
    expect(root.dataset.performance).toBe(DEFAULT_PERFORMANCE);
  });
});
