const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { LONG_MEDIA_SECONDS, packagedLayout } = require("../scripts/profile-long-media.cjs");

test("long media profile is fixed at two hours and requires the packaged capability set", (t) => {
  assert.equal(LONG_MEDIA_SECONDS, 7200);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-long-profile-layout-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const sidecar = path.join(root, "sidecar");
  const app = path.join(root, "app");
  const bin = path.join(sidecar, "runtime", "Library", "bin");
  fs.mkdirSync(app, { recursive: true });
  fs.mkdirSync(bin, { recursive: true });
  fs.writeFileSync(path.join(sidecar, "runtime", process.platform === "win32" ? "python.exe" : "python"), "");
  fs.writeFileSync(path.join(bin, process.platform === "win32" ? "ffmpeg.exe" : "ffmpeg"), "");
  fs.writeFileSync(path.join(bin, process.platform === "win32" ? "ffprobe.exe" : "ffprobe"), "");

  const layout = packagedLayout(root);

  assert.equal(layout.appRoot, app);
  assert.equal(layout.sidecarRoot, sidecar);
});

test("long media harness keeps generated fixtures temporary and exercises required recovery boundaries", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "scripts", "profile-long-media.cjs"), "utf8");
  assert.match(source, /generated_non_private_fixture: true/);
  assert.match(source, /createSlowReadStream/);
  assert.match(source, /controller\.abort\(\)/);
  assert.match(source, /forceKill\(supervisor\.child\)/);
  assert.match(source, /startup partial recovery/);
  assert.match(source, /long_media_restart_created_duplicate_asset/);
  assert.match(source, /long_media_asr_boundary_not_honest/);
  assert.doesNotMatch(source, /Chriptmas_Replay\\library|Users\\Chrip/);
});
