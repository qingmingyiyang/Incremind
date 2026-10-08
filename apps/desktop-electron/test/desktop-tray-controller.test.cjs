const assert = require("node:assert/strict");
const test = require("node:test");

const { DesktopTrayController } = require("../src/desktop-tray-controller.cjs");

function fixture({ empty = false, imageError = false, setupError = false } = {}) {
  const events = [];
  class Tray {
    constructor(icon) { this.icon = icon; events.push(["construct", icon]); }
    setToolTip(value) { events.push(["tooltip", value]); }
    setContextMenu(value) { if (setupError) throw new Error("setup_failed"); events.push(["menu", value]); }
    on(name, listener) { this.listener = listener; events.push(["on", name]); }
    removeListener(name, listener) { events.push(["remove", name, listener === this.listener]); }
    destroy() { events.push("destroy"); }
  }
  const resized = { isEmpty: () => empty };
  const fallback = { fallback: true };
  const nativeImage = {
    createFromPath: (value) => {
      events.push(["path", value]);
      if (imageError) throw new Error("image_failed");
      return { resize: (options) => { events.push(["resize", options]); return resized; } };
    },
    createEmpty: () => { events.push("empty"); return fallback; },
  };
  const actionRegistry = {
    menuTemplate: (request) => { events.push(["template", request]); return [{ id: "open" }]; },
  };
  const Menu = {
    buildFromTemplate: (template) => { events.push(["build", template]); return { built: true }; },
  };
  const controller = new DesktopTrayController({
    Tray,
    Menu,
    nativeImage,
    iconPath: "C:\\owned\\tray-icon.png",
    actionRegistry,
    onClick: () => events.push("click"),
  });
  return { controller, events, fallback, resized };
}

test("creates one tray from the fixed resized icon and tray action source", () => {
  const value = fixture();
  const tray = value.controller.create();
  assert.equal(value.controller.create(), tray);
  assert.equal(tray.icon, value.resized);
  assert.deepEqual(value.events, [
    ["path", "C:\\owned\\tray-icon.png"],
    ["resize", { width: 32, height: 32, quality: "best" }],
    ["construct", value.resized],
    ["tooltip", "Chriptmas OS"],
    ["template", { source: "tray" }],
    ["build", [{ id: "open" }]],
    ["menu", { built: true }],
    ["on", "click"],
  ]);
});

test("click uses the supplied main-window restoration entry", () => {
  const value = fixture();
  const tray = value.controller.create();
  tray.listener();
  assert.equal(value.events.at(-1), "click");
});

test("empty and failed local images use the bounded empty fallback", () => {
  for (const options of [{ empty: true }, { imageError: true }]) {
    const value = fixture(options);
    assert.equal(value.controller.create().icon, value.fallback);
    assert.equal(value.events.includes("empty"), true);
  }
});

test("dispose removes the owned listener and destroys exactly once", () => {
  const value = fixture();
  value.controller.create();
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.deepEqual(value.events.slice(-2), [["remove", "click", true], "destroy"]);
});

test("failed tray setup destroys the partial native owner", () => {
  const value = fixture({ setupError: true });
  assert.throws(() => value.controller.create(), /setup_failed/);
  assert.equal(value.events.at(-1), "destroy");
  assert.equal(value.controller.tray, null);
});

test("constructor rejects incomplete native or authority dependencies", () => {
  assert.throws(() => new DesktopTrayController({}), /desktop_tray_options_invalid/);
});
