const assert = require("node:assert/strict");
const test = require("node:test");
const { resolveOverlayBounds } = require("../src/companion/overlay-position.cjs");

test("overlay prefers the right, flips left, and clamps to logical work areas", () => {
  assert.deepEqual(resolveOverlayBounds({ petBounds: { x: 100, y: 500, width: 180, height: 220 }, workArea: { x: 0, y: 0, width: 1920, height: 1040 } }), { x: 292, y: 520, width: 360, height: 180, side: "right" });
  assert.equal(resolveOverlayBounds({ petBounds: { x: 1700, y: 500, width: 180, height: 220 }, workArea: { x: 0, y: 0, width: 1920, height: 1040 } }).side, "left");
  const secondary = resolveOverlayBounds({ petBounds: { x: -1200, y: 930, width: 180, height: 220 }, workArea: { x: -1920, y: 0, width: 1920, height: 1080 }, size: { width: 440, height: 320 } });
  assert.equal(secondary.x >= -1920, true);
  assert.equal(secondary.y + secondary.height <= 1080, true);
  const tiny = resolveOverlayBounds({ petBounds: { x: 0, y: 0, width: 100, height: 100 }, workArea: { x: 0, y: 0, width: 200, height: 100 } });
  assert.deepEqual({ width: tiny.width, height: tiny.height, x: tiny.x, y: tiny.y }, { width: 200, height: 100, x: 0, y: 0 });
});
