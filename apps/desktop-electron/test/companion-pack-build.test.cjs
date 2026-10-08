const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { defaultManifest, verifyCompanionPack } = require("../scripts/verify-companion-pack.cjs");

test("verified companion pack matches the real PNG dimensions and declares honest fallbacks", () => {
  const result = verifyCompanionPack();
  assert.deepEqual(result, {
    pack_id: "bear-companion-v3",
    sprite: "bear_companion_sprite-v1.png",
    width: 1536,
    height: 1872,
    states: 17,
    overlays: [
      { id: "rain", file: "companion-weather-umbrella.png", width: 1254, height: 1254 },
      { id: "outfit_red_scarf", file: "red-scarf.svg", width: 96, height: 96 },
      { id: "outfit_gold_star", file: "gold-star.svg", width: 96, height: 96 },
    ],
  });
  const pack = JSON.parse(fs.readFileSync(defaultManifest, "utf8"));
  for (const unavailable of ["sleeping", "drag", "falling", "hang_left", "hang_right"]) {
    assert.deepEqual(Object.keys(pack.states[unavailable]), ["fallback"]);
  }
});

test("build verification rejects traversal, dimension drift, missing sprites and fallback cycles", () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-pack-"));
  const sourceRoot = path.dirname(defaultManifest);
  const spriteName = "bear_companion_sprite-v1.png";
  fs.copyFileSync(path.join(sourceRoot, spriteName), path.join(temporary, spriteName));
  fs.copyFileSync(path.join(sourceRoot, "companion-weather-umbrella.png"), path.join(temporary, "companion-weather-umbrella.png"));
  fs.mkdirSync(path.join(temporary, "items"));
  fs.copyFileSync(path.join(sourceRoot, "items", "red-scarf.svg"), path.join(temporary, "items", "red-scarf.svg"));
  fs.copyFileSync(path.join(sourceRoot, "items", "gold-star.svg"), path.join(temporary, "items", "gold-star.svg"));
  const original = JSON.parse(fs.readFileSync(defaultManifest, "utf8"));
  const manifest = path.join(temporary, "companion-pack.json");
  const write = (value) => fs.writeFileSync(manifest, JSON.stringify(value));
  try {
    write({ ...original, sprite: { ...original.sprite, src: "../secret.png" } });
    assert.throws(() => verifyCompanionPack(manifest), /path/);
    write({ ...original, sprite: { ...original.sprite, width: 1 } });
    assert.throws(() => verifyCompanionPack(manifest), /dimensions/);
    fs.renameSync(path.join(temporary, spriteName), path.join(temporary, "missing.png"));
    write(original);
    assert.throws(() => verifyCompanionPack(manifest), /ENOENT|missing/);
    fs.renameSync(path.join(temporary, "missing.png"), path.join(temporary, spriteName));
    write({ ...original, states: { ...original.states, ready: { fallback: "booting" }, booting: { fallback: "ready" } } });
    assert.throws(() => verifyCompanionPack(manifest), /cycle/);
    write({ ...original, overlays: { rain: { ...original.overlays.rain, src: "../secret.png" } } });
    assert.throws(() => verifyCompanionPack(manifest), /overlay path/);
    write({ ...original, overlays: { rain: { ...original.overlays.rain, width: original.sprite.frame_width } } });
    assert.throws(() => verifyCompanionPack(manifest), /overlay bounds/);
    fs.renameSync(path.join(temporary, "companion-weather-umbrella.png"), path.join(temporary, "missing-overlay.png"));
    write(original);
    assert.throws(() => verifyCompanionPack(manifest), /ENOENT|overlay is missing/);
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});
