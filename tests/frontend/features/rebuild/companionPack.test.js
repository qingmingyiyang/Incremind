import { describe, expect, it, vi } from "vitest";

import {
  FALLBACK_COMPANION_PACK,
  PACK_URL,
  loadCompanionPack,
  resolveCompanionState,
  validateCompanionPack,
} from "@src/features/rebuild/companionPack";

function validPack() {
  return {
    version: 2,
    pack_id: "test-pack",
    sprite: { src: "./mascots/test.png", width: 8, height: 8, frame_width: 4, frame_height: 4, columns: 2, rows: 2 },
    hit_region: { kind: "alpha", threshold: 18 },
    states: {
      ready: { row: 0, frames: [0, 1], fps: 2, loop: true, fallback: null, authentic: true },
      speaking: { fallback: "ready" },
    },
  };
}

function validV3Pack() {
  return {
    ...validPack(),
    version: 3,
    overlays: {
      rain: { src: "./mascots/rain-overlay.png", natural_width: 16, natural_height: 16, x: 0, y: 0, width: 4, height: 4 },
    },
  };
}

describe("companionPack", () => {
  it("loads only the fixed same-origin manifest and freezes a valid pack", async () => {
    const request = vi.fn().mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue(validPack()) });
    const result = await loadCompanionPack({ request });
    expect(request).toHaveBeenCalledWith(PACK_URL, { cache: "no-store", credentials: "same-origin" });
    expect(result.fallbackUsed).toBe(false);
    expect(Object.isFrozen(result.pack.states.ready.frames)).toBe(true);
  });

  it.each([
    ["version", (pack) => { pack.version = 4; }],
    ["traversal", (pack) => { pack.sprite.src = "../secret.png"; }],
    ["dimensions", (pack) => { pack.sprite.width = 7; }],
    ["row", (pack) => { pack.states.ready.row = 3; }],
    ["fps", (pack) => { pack.states.ready.fps = 120; }],
    ["unknown fallback", (pack) => { pack.states.speaking.fallback = "missing"; }],
  ])("rejects invalid %s", (_label, mutate) => {
    const pack = validPack();
    mutate(pack);
    expect(() => validateCompanionPack(pack)).toThrow();
  });

  it("accepts bounded v3 overlays and normalizes legacy v2 packs without one", () => {
    expect(validateCompanionPack(validV3Pack()).overlays.rain).toMatchObject({ x: 0, y: 0, width: 4, height: 4 });
    expect(validateCompanionPack(validPack()).overlays).toEqual({});
  });

  it.each([
    ["traversal", (pack) => { pack.overlays.rain.src = "../rain.png"; }],
    ["frame overflow", (pack) => { pack.overlays.rain.width = 5; }],
    ["negative position", (pack) => { pack.overlays.rain.x = -1; }],
    ["unknown field", (pack) => { pack.overlays.rain.secret = "no"; }],
  ])("rejects invalid v3 overlay %s", (_label, mutate) => {
    const pack = validV3Pack();
    mutate(pack);
    expect(() => validateCompanionPack(pack)).toThrow(/overlay/);
  });

  it("rejects fallback cycles and reports honest fallback use", () => {
    const pack = validPack();
    pack.states.ready = { fallback: "speaking" };
    expect(() => validateCompanionPack(pack)).toThrow(/cycle/);
    const safe = validateCompanionPack(validPack());
    expect(resolveCompanionState(safe, "speaking")).toMatchObject({ stateId: "ready", fallbackUsed: true });
    expect(resolveCompanionState(safe, "ready")).toMatchObject({ stateId: "ready", fallbackUsed: false });
  });

  it("falls back without throwing for missing, malformed or unavailable manifests", async () => {
    await expect(loadCompanionPack({ request: vi.fn().mockRejectedValue(new Error("offline")) }))
      .resolves.toEqual({ pack: FALLBACK_COMPANION_PACK, fallbackUsed: true });
    await expect(loadCompanionPack({ request: vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }) }))
      .resolves.toEqual({ pack: FALLBACK_COMPANION_PACK, fallbackUsed: true });
  });
});
