const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const sourceRoot = path.join(__dirname, "..", "src");

test("original asset opening keeps Vault paths out of the renderer bridge", () => {
  const main = fs.readFileSync(path.join(sourceRoot, "main.cjs"), "utf8");
  const controller = fs.readFileSync(path.join(sourceRoot, "original-asset-ipc-controller.cjs"), "utf8");
  const preload = fs.readFileSync(path.join(sourceRoot, "preload.cjs"), "utf8");
  assert.match(preload, /openOriginalAsset: \(assetId\)/);
  assert.match(preload, /chriptmas:open-original-asset/);
  assert.doesNotMatch(preload, /resolved_path/);
  assert.match(main, /new OriginalAssetIpcController\(\{/);
  assert.doesNotMatch(main, /ipcMain\.handle\("chriptmas:open-original-asset"/);
  assert.match(controller, /this\.requireMainRenderer\(event\)/);
  assert.match(controller, /open-original-asset:\$\{assetId\}/);
  assert.match(controller, /X-Chriptmas-Main-Signature/);
  assert.match(controller, /this\.shell\.openPath\(payload\.resolved_path\)/);
});
