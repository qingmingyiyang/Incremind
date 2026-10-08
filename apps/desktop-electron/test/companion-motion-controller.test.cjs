const assert = require("node:assert/strict");
const test = require("node:test");

const { CompanionMotionController, resolvePetRestingTarget } = require("../src/companion/motion-controller.cjs");

test("keeps arbitrary resting positions and clamps only when outside a logical work area", () => {
  const workArea = { x: -1920, y: 40, width: 1920, height: 1000 };
  assert.deepEqual(resolvePetRestingTarget({ bounds: { x: -1000, y: 100, width: 180, height: 220 }, workArea }), { mode: "free", x: -1000, y: 100 });
  assert.deepEqual(resolvePetRestingTarget({ bounds: { x: -1940, y: 10, width: 180, height: 220 }, workArea }), { mode: "free", x: -1920, y: 40 });
  assert.deepEqual(resolvePetRestingTarget({ bounds: { x: -90, y: 900, width: 180, height: 220 }, workArea }), { mode: "free", x: -180, y: 820 });
  assert.deepEqual(resolvePetRestingTarget({ bounds: { x: 50, y: 90, width: 180, height: 220 }, workArea: { x: 0, y: 0, width: 100, height: 100 } }), { mode: "free", x: 0, y: 0 });
});

test("settles immediately where the user released the pet and persists that position", () => {
  const positions = [];
  const projections = [];
  const callbacks = [];
  const window = {
    isDestroyed: () => false,
    getBounds: () => ({ x: 400, y: 100, width: 180, height: 220 }),
    setPosition: (x, y) => positions.push([x, y]),
  };
  const controller = new CompanionMotionController({
    screen: { getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 1200, height: 900 } }) },
    publish: (value) => projections.push(value),
    clear: () => projections.push("clear"),
    onPosition: () => callbacks.push("position"),
    onSettled: (target) => callbacks.push(["settled", target]),
  });
  assert.equal(controller.beginDrag(), true);
  assert.equal(controller.beginDrag(), false);
  const target = controller.settle(window);
  assert.deepEqual(target, { mode: "free", x: 400, y: 100 });
  assert.deepEqual(positions, [[400, 100]]);
  assert.equal(projections.at(-1), "clear");
  assert.deepEqual(callbacks, ["position", ["settled", target]]);
});

test("settle is unavailable for a destroyed window", () => {
  const projections = [];
  const controller = new CompanionMotionController({
    screen: { getDisplayMatching: () => { throw new Error("must not query display"); } },
    publish: (value) => projections.push(value),
    clear: () => projections.push("clear"),
  });
  assert.deepEqual(controller.settle({ isDestroyed: () => true }), { mode: "unavailable" });
  assert.deepEqual(projections, []);
});
